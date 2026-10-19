import io
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import pandas as pd
from src.tu import load_TU_data
from src.config_loader import get_config, start_run
from src.run_utils import Tee, wait_for_output_dir
from src.tu.read_chosen_trips import find_chosen_trips_file, read_chosen_trips
from src.tu.tu_utils import tu_trip_query, tu_trip_legs
from src.choice_set.generate_alternatives import ALTERNATIVE_KEYS, build_choice_set, print_choice_set, walk_to_bicycle
from src.constant import WALK_BIKE_TIME_RATIO
from src import otp

def main():
	config = get_config()
	chosen_trips_path = find_chosen_trips_file(config)
	config = start_run(config, stage="choice_set")

	with open(config["choice_set"]["paths"]["log_file"], "w", encoding="utf-8") as log_file:
		tee = Tee(sys.stdout, log_file)
		with redirect_stdout(tee):
			_run(config, tee, chosen_trips_path)

def otp_settings(config):
	"""otp.client.load_all_candidates settings, the same for every choice-set trip."""
	request = config["request"]
	return dict(
		search_window=config["choice_set"]["search_window"],
		max_itinerary_candidates=request["max_itinerary_candidates"],
		otp_url=request["otp_url"],
		request_timeout=request["request_timeout"],
		#TODO: decide on debug_profile. Currently default ("LIST_ALL")
	)

def trip_candidates(tu_tur_row, tu_deltur, settings, access_egress_mode=None):
	"""
	The TU trip's OTP itineraries, bike access/egress relabelled BICYCLE.
	access_egress_mode, if given, replaces the access/egress mode derived from the TU legs.
	"""
	tu_legs = tu_trip_legs(tu_tur_row, tu_deltur)
	query = tu_trip_query(tu_tur_row, tu_deltur)
	if access_egress_mode:
		query.update(access_mode=access_egress_mode, egress_mode=access_egress_mode)
	candidates = otp.client.load_all_candidates(**query, **settings)
	return walk_to_bicycle(candidates, tu_legs["otp_mode"].iloc[0] == "BIKE",
		tu_legs["otp_mode"].iloc[-1] == "BIKE", WALK_BIKE_TIME_RATIO)

def generate_choice_sets(config, tee, tu_tur, tu_deltur, chosen, chosen_public_transport, choice_set_file, access_egress_mode=None):
	"""
	Choice sets for every trip in tu_tur, written to choice_set_file. chosen(tu_tur_row) returns the
	trip's (chosen_start, chosen_legs); chosen_public_transport is passed on to build_choice_set,
	access_egress_mode to trip_candidates.
	"""
	key_columns = ALTERNATIVE_KEYS[config["choice_set"]["alternative_key"]]
	settings = otp_settings(config)
	max_workers = config["run"]["max_workers"]

	def choice_set_one(tu_tur_row):
		"""One trip's choice set, buffering its prints. Returns (output, choice set or None)."""
		tee.local.buffer = io.StringIO()
		otp.client.trace.id = f"TurId={tu_tur_row['TurId']}"
		try:
			candidates = trip_candidates(tu_tur_row, tu_deltur, settings, access_egress_mode)
			chosen_start, chosen_legs = chosen(tu_tur_row)
			trip_choice_set = build_choice_set(tu_tur_row["TurId"], candidates, key_columns, chosen_start, chosen_legs, chosen_public_transport)
			print_choice_set(trip_choice_set)
			print(f"TurId {tu_tur_row['TurId']}: {trip_choice_set['alt_id'].max()} alternative(s) besides the chosen")
		except Exception as exc:
			print(f"Skipping TurId {tu_tur_row['TurId']} due to unexpected error: {exc}")
			trip_choice_set = None
		finally:
			output = tee.local.buffer.getvalue()
			tee.local.buffer = None
			otp.client.trace.id = None
		return output, trip_choice_set

	# Trips are independent and I/O-bound on OTP, hence threads. map() yields in TU order.
	print(f"Generating choice sets for {len(tu_tur)} trips with {max_workers} worker(s)...")
	choice_sets = []
	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		for output, trip_choice_set in executor.map(choice_set_one, (row for _, row in tu_tur.iterrows())):
			print(output, end="")
			if trip_choice_set is not None:
				choice_sets.append(trip_choice_set)

	# Before writing: the output dir is on a network share
	wait_for_output_dir(config["paths"]["output_dir"])
	if not choice_sets:
		print("No choice sets generated.")
		return
	pd.concat(choice_sets, ignore_index=True).rename(columns={"trip_id": "TurId"}).to_csv(choice_set_file, sep=",", decimal=".", index=False)
	print(f"Saved {len(choice_sets)} choice sets to {choice_set_file}")


def public_trans_choice_set(config, tee, tu_session, tu_tur, tu_deltur, chosen_trips_path):
	"""
	---- Public transport choice-set for public transport trips ----

	Reconstructed trips are the "chosen trips". Alternatives are OTP itineraries for the same
	origin, destination, departure (within window) and access/egress mode on any route, collapsed
	per	choice_set.alternative_key (what makes two itineraries the same alternative).
	"""
	chosen_trips = read_chosen_trips(chosen_trips_path, config["reconstruct"]["paths"]["trip_matching_summaries_file"])
	tu_session, tu_tur, tu_deltur = load_TU_data.select_trips(tu_session, tu_tur, tu_deltur, chosen_trips["TurId"])

	def chosen(tu_tur_row):
		chosen_legs = chosen_trips[chosen_trips["TurId"] == tu_tur_row["TurId"]]
		# TODO: gap from the reconstructed start or the TU respondent's answered depart (tu_tur_row["depart_dt"], as for car)?
		return chosen_legs["start_trip"].iloc[0], chosen_legs

	generate_choice_sets(config, tee, tu_tur, tu_deltur, chosen, chosen_public_transport=True,
		choice_set_file=config["choice_set"]["paths"]["public_choice_set_file"])

def car_choice_set(config, tee, tu_session, tu_tur, tu_deltur):
	"""
	---- Public transport choice-set for car trip ----

	Chosen trips are with car. Alternatives are OTP itineraries for the same
	origin, destination and departure with WALK access/egress on any route, collapsed per
	choice_set.alternative_key (what makes two itineraries the same alternative).
	"""
	turid_car = tu_tur.loc[tu_tur["PrimMode"] == 11,"TurId"] #primary mode is car
	legs = tu_deltur[tu_deltur["TurId"].isin(turid_car)]
	only_car_walk = legs["StageMode"].isin([1,11]).groupby(legs["TurId"]).all()
	turid_car = only_car_walk[only_car_walk].index
	tu_session, tu_tur, tu_deltur = load_TU_data.select_trips(tu_session, tu_tur, tu_deltur, turid_car)

	def chosen(tu_tur_row):
		# The TU car legs as the chosen, in the choice-set columns TU has
		tu_legs = tu_deltur[tu_deltur["TurId"] == tu_tur_row["TurId"]].sort_values("Delturnr")
		chosen_legs = pd.DataFrame({
			"leg_id": tu_legs["Delturnr"] - 1,
			"start_trip": tu_tur_row["depart_dt"],
			"end_trip": tu_tur_row["arrival_dt"],
			"mode": tu_legs["otp_mode"],
			"distance_km": tu_legs["StageLength"],
			"duration_min": tu_legs["StageDurationMin"],
		})
		return tu_tur_row["depart_dt"], chosen_legs

	generate_choice_sets(config, tee, tu_tur, tu_deltur, chosen, chosen_public_transport=False,
		choice_set_file=config["choice_set"]["paths"]["car_choice_set_file"], access_egress_mode="WALK")

def _run(config, tee, chosen_trips_path):
	print("Loading TU data...")
	tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu_from_config(config)
	print("TU data loaded")

	public_trans_choice_set(config, tee, tu_session, tu_tur, tu_deltur, chosen_trips_path)
	car_choice_set(config, tee, tu_session, tu_tur, tu_deltur)


if __name__ == "__main__":
	main()
