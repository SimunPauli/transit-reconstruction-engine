import io
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import pandas as pd
from src.tu import load_TU_data
from src.config_loader import get_config, start_run
from src.run_utils import Tee, wait_for_output_dir
from src.tu.read_chosen_trips import find_chosen_trips_file, read_chosen_trips
from src.tu.tu_utils import tu_trip_query, tu_trip_legs
from src.choice_set.generate_alternatives import (ALTERNATIVE_KEYS, build_choice_set, exclude_stop_rounds,
	print_choice_set, print_coverage, walk_to_bicycle)
from src.constant import WALK_BIKE_TIME_RATIO, TRANSIT_STAGE_MODES, STREET_MODES
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

def trip_candidates(tu_tur_row, tu_deltur, settings, chosen_legs, key_columns,
                    exclude_stops, access_egress_mode=None):
	"""
	The TU trip's OTP itineraries plus those of the exclude_stops rounds (exclude_stop_rounds, seeded
	per trip by exclude_stops["seed"] and TurId), bike access/egress relabelled BICYCLE.
	access_egress_mode, if given, replaces the access/egress mode derived from the TU legs.
	"""
	tu_legs = tu_trip_legs(tu_tur_row, tu_deltur)
	trip_query = tu_trip_query(tu_tur_row, tu_deltur)
	if access_egress_mode:
		trip_query.update(access_mode=access_egress_mode, egress_mode=access_egress_mode)

	direct = otp.client.request_direct_leg(trip_query["origin"], trip_query["destination"], "WALK",
	                                       trip_query["depart_dt"], otp_url=settings["otp_url"],
	                                       request_timeout=settings["request_timeout"])
	direct_km = None if direct.empty else direct.groupby("iteration_id")["distance_km"].sum().min()
	def query(exclude_stopids=None):
		candidates = otp.client.load_all_candidates(**trip_query, **settings, exclude_stopids=exclude_stopids)
		if direct_km is None or candidates.empty:
			return candidates
		walk_km = candidates["distance_km"].where(candidates["mode"] == "WALK", 0).groupby(candidates["iteration_id"]).transform("sum")
		keep = walk_km + 0.01 < direct_km
		n_dropped = candidates.loc[~keep, "iteration_id"].nunique()
		if n_dropped:
			print(f"Dropped {n_dropped} itierary(ies) walking at least as far as the direct walk ({direct_km:.3f} km)")
		return candidates[keep].reset_index(drop=True)

	# Seeded per trip, so draws don't depend on thread order
	rng = random.Random(f"{exclude_stops['seed']}:{tu_tur_row['TurId']}")
	candidates = exclude_stop_rounds(query, query(), chosen_legs, key_columns,
		exclude_stops["rounds"], exclude_stops["stops_per_round"], rng)
	return walk_to_bicycle(candidates, tu_legs["otp_mode"].iloc[0] == "BIKE",
		tu_legs["otp_mode"].iloc[-1] == "BIKE", WALK_BIKE_TIME_RATIO)

def generate_choice_sets(config, tee, tu_tur, tu_deltur, chosen,
                         chosen_public_transport, choice_set_file, access_egress_mode=None):
	"""
	Choice sets for every trip in tu_tur, written to choice_set_file and returned (None if there are
	none). chosen(tu_tur_row) returns the trip's (chosen_start, chosen_legs); chosen_public_transport
	is passed on to build_choice_set, access_egress_mode to trip_candidates.
	"""
	key_columns = ALTERNATIVE_KEYS[config["choice_set"]["alternative_key"]]
	exclude_stops = config["choice_set"]["exclude_stops"]
	settings = otp_settings(config)
	max_workers = config["run"]["max_workers"]

	def choice_set_one(tu_tur_row):
		"""One trip's choice set, buffering its prints. Returns (output, choice set or None)."""
		tee.local.buffer = io.StringIO()
		otp.client.trace.id = f"TurId={tu_tur_row['TurId']}"
		try:
			chosen_start, chosen_legs = chosen(tu_tur_row)
			#Generated alternatives (many will be duplicated)
			candidates = trip_candidates(tu_tur_row, tu_deltur, settings, chosen_legs, key_columns,
			                             exclude_stops, access_egress_mode)
			#Filter alternatives; drop duplicates only keeping one closest to departure
			trip_choice_set = build_choice_set(tu_tur_row["TurId"], candidates, key_columns,
			                                   chosen_start, chosen_legs, chosen_public_transport)
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
		return None
	choice_sets = pd.concat(choice_sets, ignore_index=True)
	choice_sets.rename(columns={"trip_id": "TurId"}).to_csv(choice_set_file, sep=",", decimal=".", index=False)
	print(f"Saved {choice_sets['trip_id'].nunique()} choice sets to {choice_set_file}")
	return choice_sets


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
		chosen_legs = chosen_trips[chosen_trips["TurId"] == tu_tur_row["TurId"]]
		# TODO: gap from the reconstructed start or the TU respondent's answered depart (tu_tur_row["depart_dt"], as for car)?
		return chosen_legs["start_trip"].iloc[0], chosen_legs

	choice_sets = generate_choice_sets(config, tee, tu_tur, tu_deltur, chosen, chosen_public_transport=True,
		choice_set_file=config["choice_set"]["paths"]["public_choice_set_file"])
	if choice_sets is not None:
		print_coverage(choice_sets, config["choice_set"]["alternative_key"],
			groups=chosen_trips.groupby("TurId")["outcome"].first())

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
