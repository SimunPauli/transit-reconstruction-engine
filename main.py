import pandas as pd
import load_TU_data
from tu_otp_matching import match_tu_trip_to_otp
from otp_client import get_all_routes_for_mode
from tu_gtfs_stations_match import match_tu_gtfs_stations
from config_loader import load_config

def main():
	config = load_config()

	#Configurartion
	mode_map = {
		#TU: OTP
		31: "BUS",
		32: "S_TRAIN",
		33: "RAIL",
		34: "SUBWAY",
		37: "TRAM",
		41: "FERRY",
		35: "BUS"
	}

	otp_url = config["otp_url"]
	search_window = config["search_window"]
	max_itinerary_candidates = config["max_itinerary_candidates"]
	data_dir = config["paths"]["data_dir"]

	print(f"Search window: {search_window}")


	print("Loading TU data...")
	tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu(
		data_dir=str(data_dir),
		session_file=config["tu_files"]["session_file"],
		tur_file=config["tu_files"]["tur_file"],
		deltur_file=config["tu_files"]["deltur_file"],
		stations_file=config["tu_files"]["stations_file"]
	)
	#Small processing of TU data
	tu_tur = tu_tur[tu_tur["PtPrimMode"].isin([31, 32, 33, 34, 37])] #Not ferry
	tu_tur = tu_tur[(tu_tur["DiaryYear"] == 2024) & (tu_tur["DiaryMonth"] == 6)]
	tu_deltur = tu_deltur[tu_deltur["TurId"].isin(tu_tur["TurId"])].copy()
	tu_deltur["otp_mode"] = tu_deltur["StageMode"].map(mode_map)
	print("TU data loaded")



	# RAIL, TRAM and SUBWAY are missing route name in TU.
	# So taking all routes for these modes. Which will be used when modes
	# that do include route name in TU only can access those routes, but for
	# those that do not, all routes will be used.
	otp_mode_routes_cache = {mode: get_all_routes_for_mode(mode) for mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"]}

	#Map TU and GTFS stations
	tu_gtfs_station_df = match_tu_gtfs_stations(
		tu_stations,
		period=(tu_tur["DiaryDate"].min(), tu_tur["DiaryDate"].max()),
		bbox_buffer_m=1000,
		name_match_threshold=0.6
	)
	print("Mapping of TU and GTFS station has been exported to", config["paths"]["tu_gtfs_station_file"])
	tu_gtfs_station_df.to_csv(config["paths"]["tu_gtfs_station_file"])

	time_based_matches = []
	for i, tu_tur_row in tu_tur.iterrows():
		time_based_match = match_tu_trip_to_otp(
			tu_tur_row =tu_tur_row,
			tu_deltur=tu_deltur,
			mode_map=mode_map,
			otp_mode_routes_cache=otp_mode_routes_cache,
			otp_url=otp_url,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			tu_gtfs_station_df=tu_gtfs_station_df,
		)
		if time_based_match is None:
			continue

		time_based_match_print_col = ["mode", "distance_km", "waitingtime", "duration_min", "route_short_name", "from", "to"]
		print("time_based_match:")
		print(time_based_match[time_based_match_print_col].to_string(index=False, max_colwidth=None))

		time_based_matches.append(time_based_match)

	if not time_based_matches:
		print("No time-based matches found. Nothing to save.")
		return

	all_time_based_matches = pd.concat(time_based_matches, ignore_index=True)
	all_time_based_matches.to_csv(config["paths"]["time_based_matches_file"], index=False)
	print(f"\n\n\n____________________________________________________________________________________________")
	print(f"\n\n\nall_time_based_matches has been exported to {config['paths']['time_based_matches_file']}")
	print(f"Saved {len(all_time_based_matches)} time-based matches to {config['paths']['time_based_matches_file'].name}")

if __name__ == "__main__":
	import sys
	from contextlib import redirect_stdout

	config = load_config()

	#All prints will be written in both console and log file
	class Tee:
		def __init__(self, *files): self.files = files
		def write(self, data): [file.write(data) for file in self.files]
		def flush(self): [file.flush() for file in self.files]

	with open(config["paths"]["log_file"], "w", encoding="utf-8") as log_file:
		with redirect_stdout(Tee(sys.stdout, log_file)):
			main()