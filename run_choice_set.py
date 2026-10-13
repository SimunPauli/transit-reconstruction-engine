import io
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import pandas as pd
from src.tu import load_TU_data
from src.config_loader import get_config, start_run
from src.run_utils import Tee, wait_for_output_dir
from src.choice_set.read_chosen_trips import find_chosen_trips_file, read_chosen_trips
from src.choice_set.generate_alternatives import ALTERNATIVE_KEYS, query_candidates, build_choice_set
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
	"""OTP query settings shared by every choice-set mode."""
	request = config["request"]
	return dict(
		search_window=config["choice_set"]["search_window"],
		max_itinerary_candidates=request["max_itinerary_candidates"],
		otp_url=request["otp_url"],
		request_timeout=request["request_timeout"],
	)


def public_trans_choice_set(config, tee, tu_session, tu_tur, tu_deltur, chosen_trips_path):
	"""
	---- Choice set for public transport ----

	Reconstructed trips are the "chosen trips". Alternatives are OTP itineraries for the same
	origin, destination, departure and access/egress on any route, collapsed per
	choice_set.alternative_key (what makes two itineraries the same alternative).
	"""
	config_choice_set = config["choice_set"]
	key_columns = ALTERNATIVE_KEYS[config_choice_set["alternative_key"]]
	settings = otp_settings(config)
	max_workers = config["run"]["max_workers"]

	chosen_trips = read_chosen_trips(chosen_trips_path, config["reconstruct"]["paths"]["trip_matching_summaries_file"])
	tu_session, tu_tur, tu_deltur = load_TU_data.select_trips(tu_session, tu_tur, tu_deltur, chosen_trips["TurId"])

	def choice_set_one(tu_tur_row):
		"""One trip's choice set, buffering its prints. Returns (output, choice set or None)."""
		tee.local.buffer = io.StringIO()
		otp.client.trace.id = f"TurId={tu_tur_row['TurId']}"
		try:
			candidates = query_candidates(tu_tur_row, tu_deltur, settings)
			chosen_legs = chosen_trips[chosen_trips["TurId"] == tu_tur_row["TurId"]]
			trip_choice_set = build_choice_set(chosen_legs, candidates, key_columns)
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
	pd.concat(choice_sets, ignore_index=True).to_csv(config_choice_set["paths"]["choice_set_file"], sep=",", decimal=".", index=False)
	print(f"Saved {len(choice_sets)} choice sets to {config_choice_set['paths']['choice_set_file']}")


def _run(config, tee, chosen_trips_path):
	print("Loading TU data...")
	tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu_from_config(config)
	print("TU data loaded")

	public_trans_choice_set(config, tee, tu_session, tu_tur, tu_deltur, chosen_trips_path)
	# car_choice_set(config, tee, tu_session, tu_tur, tu_deltur)


if __name__ == "__main__":
	main()
