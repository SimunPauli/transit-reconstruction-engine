import io
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import pandas as pd
from src.tu import load_TU_data
from src.config_loader import get_config, start_run
from src.run_utils import Tee, elapsed, wait_for_output_dir
from src.tu.read_chosen_trips import find_chosen_trips_file, read_chosen_trips
from src.tu.tu_utils import tu_trip_query
from src.choice_set.generate_alternatives import trip_choice_set, choice_set_summary, print_choice_set
from src.constant import TRANSIT_STAGE_MODES
from src import otp

def main():
	config = get_config()
	chosen_trips_path = find_chosen_trips_file(config)
	config = start_run(config, stage="choice_set")

	with open(config["choice_set"]["paths"]["log_file"], "w", encoding="utf-8") as log_file:
		tee = Tee(sys.stdout, log_file)
		with redirect_stdout(tee):
			_run(config, tee, chosen_trips_path)

def generate_choice_sets(config, tee, tu_tur, tu_deltur, chosen, choice_set_file, access_egress_mode=None,
                         groups=None, trip_columns=None):
	"""
	Choice sets for every trip in tu_tur, written to choice_set_file; prints and returns their
	choice_set_summary per groups (None if there are none). chosen(tu_tur_row) returns the trip's
	chosen_legs. access_egress_mode, if given, replaces the access/egress mode derived from the TU
	legs. trip_columns (DataFrame indexed by TurId), if given, is written after TurId.
	"""
	start = time.perf_counter()
	max_workers = config["run"]["max_workers"]

	def choice_set_one(tu_tur_row):
		"""One trip's choice set, buffering its prints. Returns (output, choice set or None)."""
		tee.local.buffer = io.StringIO()
		otp.client.trace.id = f"TurId={tu_tur_row['TurId']}"
		try:
			trip_query = tu_trip_query(tu_tur_row, tu_deltur)
			if access_egress_mode:
				trip_query.update(access_mode=access_egress_mode, egress_mode=access_egress_mode)
			choice_set = trip_choice_set(tu_tur_row["TurId"], **trip_query, config=config,
			                             chosen_legs=chosen(tu_tur_row))
			print_choice_set(choice_set)
			print(f"TurId {tu_tur_row['TurId']}: {choice_set['alt_id'].max()} alternative(s) besides the chosen")
		except Exception as exc:
			print(f"Skipping TurId {tu_tur_row['TurId']} due to unexpected error: {exc}")
			choice_set = None
		finally:
			output = tee.local.buffer.getvalue()
			tee.local.buffer = None
			otp.client.trace.id = None
		return output, choice_set

	# Trips are independent and I/O-bound on OTP, hence threads. map() yields in TU order.
	print(f"Generating choice sets for {len(tu_tur)} trips with {max_workers} worker(s)...")
	choice_sets = []
	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		for output, choice_set in executor.map(choice_set_one, (row for _, row in tu_tur.iterrows())):
			print(output, end="")
			if choice_set is not None:
				choice_sets.append(choice_set)
	# Before waiting on the share, so an outage isn't counted
	run_time = elapsed(start)

	# Before writing: the output dir is on a network share
	wait_for_output_dir(config["paths"]["output_dir"])
	if not choice_sets:
		print("No choice sets generated.")
		return None
	choice_sets = pd.concat(choice_sets, ignore_index=True)
	if trip_columns is not None:
		for position, name in enumerate(trip_columns, start=1):
			choice_sets.insert(position, name, choice_sets["trip_id"].map(trip_columns[name]))
	choice_sets.rename(columns={"trip_id": "TurId"}).to_csv(choice_set_file, sep=",", decimal=".", index=False)
	print(f"Saved {choice_sets['trip_id'].nunique()} choice sets to {choice_set_file}")
	summary = choice_set_summary(choice_sets, groups)
	summary["run_time"] = run_time
	print(f"Choice-set summary (alternative_key={config['choice_set']['alternative_key']}):")
	print(summary.to_string(index=False))
	return summary


def public_trans_choice_set(config, tee, tu_session, tu_tur, tu_deltur, chosen_trips_path):
	"""
	---- Public transport choice-set for public transport trips ----

	Reconstructed trips are the "chosen trips". Alternatives are OTP itineraries for the same
	origin, destination, departure (within window) and access/egress mode on any route, collapsed
	per	choice_set.alternative_key (what makes two itineraries the same alternative).
	"""
	chosen_trips = read_chosen_trips(chosen_trips_path, config["reconstruct"]["paths"]["trip_matching_summaries_file"])
	legs = tu_deltur[tu_deltur["TurId"].isin(chosen_trips["TurId"])]
	only_transit_walk = legs["StageMode"].isin([1, *TRANSIT_STAGE_MODES]).groupby(legs["TurId"]).all()
	turid_walk_transit = only_transit_walk[only_transit_walk].index #TODO: include trips with other street modes then WALK??
	tu_session, tu_tur, tu_deltur = load_TU_data.select_trips(tu_session, tu_tur, tu_deltur, turid_walk_transit)

	def chosen(tu_tur_row):
		# TODO: gap from the reconstructed start (trip_choice_set's chosen start) or the TU respondent's answered depart (tu_tur_row["depart_dt"], as for car)?
		return chosen_trips[chosen_trips["TurId"] == tu_tur_row["TurId"]]

	outcome = chosen_trips.groupby("TurId")["outcome"].first()
	return generate_choice_sets(config, tee, tu_tur, tu_deltur, chosen,
		choice_set_file=config["choice_set"]["paths"]["public_choice_set_file"], groups=outcome,
		trip_columns=pd.DataFrame({name: outcome.eq(name).astype(int) for name in ["trip_found", "trip_near_route"]}))

def tu_chosen(tu_tur_row, tu_deltur):
	"""chosen_legs of a street-mode trip: its TU legs, in the choice-set columns TU has."""
	tu_legs = tu_deltur[tu_deltur["TurId"] == tu_tur_row["TurId"]].sort_values("Delturnr")
	chosen_legs = pd.DataFrame({
		"leg_id": tu_legs["Delturnr"] - 1,
		"start_trip": tu_tur_row["depart_dt"],
		"end_trip": tu_tur_row["arrival_dt"],
		"mode": tu_legs["otp_mode"],
		"distance_km": tu_legs["StageLength"],
		"duration_min": tu_legs["StageDurationMin"],
	})
	#TODO: Should chosen be OTP shorest path with car instead of TU given answer?
	return chosen_legs

def street_choice_set(config, tee, tu_session, tu_tur, tu_deltur, stage_modes, choice_set_file):
	"""
	---- Public transport choice-set for street-mode (car, bike) trips ----

	Chosen trips have primary mode in stage_modes and only those and walk legs. Alternatives are
	OTP itineraries for the same origin, destination and departure with WALK access/egress on any
	route, collapsed per choice_set.alternative_key (what makes two itineraries the same alternative).
	"""
	turid = tu_tur.loc[tu_tur["PrimMode"].isin(stage_modes), "TurId"]
	legs = tu_deltur[tu_deltur["TurId"].isin(turid)]
	only_mode_walk = legs["StageMode"].isin([1, *stage_modes]).groupby(legs["TurId"]).all()
	turid = only_mode_walk[only_mode_walk].index
	tu_session, tu_tur, tu_deltur = load_TU_data.select_trips(tu_session, tu_tur, tu_deltur, turid)

	return generate_choice_sets(config, tee, tu_tur, tu_deltur, lambda tu_tur_row: tu_chosen(tu_tur_row, tu_deltur),
		choice_set_file=choice_set_file, access_egress_mode="WALK")

def _run(config, tee, chosen_trips_path):
	print("Loading TU data...")
	tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu_from_config(config)
	print("TU data loaded")

	summaries = {
		"public": public_trans_choice_set(config, tee, tu_session, tu_tur, tu_deltur, chosen_trips_path),
		"car": street_choice_set(config, tee, tu_session, tu_tur, tu_deltur, {11},
			config["choice_set"]["paths"]["car_choice_set_file"]),
		"bike": street_choice_set(config, tee, tu_session, tu_tur, tu_deltur, {2},
			config["choice_set"]["paths"]["bike_choice_set_file"]),
	}
	if any(summary is not None for summary in summaries.values()):
		summary_file = config["choice_set"]["paths"]["summary_file"]
		pd.concat(summaries, names=["choice_set"]).reset_index(level="choice_set").convert_dtypes().to_csv(
			summary_file, sep=",", decimal=".", index=False)
		print(f"Saved choice-set summary to {summary_file}")


if __name__ == "__main__":
	main()
