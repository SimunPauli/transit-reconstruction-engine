import sys
from contextlib import redirect_stdout
import pandas as pd
from src import load_TU_data
from src.tu_otp_matching import match_tu_trip_to_otp
from src.otp_client import get_all_routes_for_mode
from src.otp_utils import build_route_name_index
from src.tu_gtfs_stations_match import match_tu_gtfs_stations
from src.config_loader import get_config
from src.constant import MODE_MAP
from src.export_files import _write_failures_file, _print_and_export_summary_stats, _reorder_rmse_columns


class _Tee:
	"""Writes to multiple files at once, so print() can go to both the console and log_file."""
	def __init__(self, *files): self.files = files
	def write(self, data): [file.write(data) for file in self.files]
	def flush(self): [file.flush() for file in self.files]


def main():
	config = get_config()

	# Opened here (not just under `if __name__ == "__main__"`) so the log file is written
	# regardless of how main() is invoked - e.g. PyCharm's "Run 'main'" gutter action imports
	# this module and calls main() directly, without ever executing the __main__ guard below.
	with open(config["paths"]["log_file"], "w", encoding="utf-8") as log_file:
		with redirect_stdout(_Tee(sys.stdout, log_file)):
			_run(config)


def _run(config):
	return_trip_summary = config.get("matching", {}).get("return_trip_summary", True)
	station_anchor_wait_min = config.get("matching", {}).get("station_anchor_wait_min", 0)
	transit_retry_cfg = config.get("reluctance_retries", {}).get("transit", {})
	walk_retry_cfg = config.get("reluctance_retries", {}).get("walk", {})
	transit_retry_enabled = transit_retry_cfg.get("enabled", True)
	transit_reluctance_sequence = tuple(transit_retry_cfg.get("sequence", [0.5, 0.25, 0.1]))
	walk_retry_enabled = walk_retry_cfg.get("enabled", True)
	walk_reluctance_sequence = tuple(walk_retry_cfg.get("sequence", [3, 4, 6]))
	config_request = config.get("request")
	otp_url = config_request["otp_url"]
	search_window = config_request["search_window"]
	max_itinerary_candidates = config_request["max_itinerary_candidates"]
	request_timeout = config_request["request_timeout"]
	data_dir = config["paths"]["data_dir"]
	YEAR = config["tu_subset"]["year"]

	print(f"Search window: {search_window}")


	print("Loading TU data...")
	tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu(
		data_dir=str(data_dir),
		YEAR = YEAR,
		session_file=config["tu_files"]["session_file"],
		tur_file=config["tu_files"]["tur_file"],
		deltur_file=config["tu_files"]["deltur_file"],
		stations_file=config["tu_files"]["stations_file"],
		transit_code_tu = [31, 32, 33, 34, 37],
	)

	tu_deltur["otp_mode"] = tu_deltur["StageMode"].map(MODE_MAP)
	print("TU data loaded")



	# RAIL, TRAM and SUBWAY are missing route name in TU.
	# So taking all routes for these modes. Which will be used when modes
	# that do include route name in TU only can access those routes, but for
	# those that do not, all routes will be used.
	otp_mode_routes_cache = {mode: get_all_routes_for_mode(mode) for mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"]}

	# BUS and S_TRAIN do carry a route name in TU, but respondents free-text the bus
	# ones and spell them inconsistently ('102 A' for '102A') or drop the trailing
	# letter ('150' for '150S'). Index the real GTFS names so TU's spelling can be
	# translated into one OTP will actually match.
	otp_route_name_index = {
		mode: build_route_name_index(get_all_routes_for_mode(mode))
		for mode in ["BUS", "S_TRAIN"]
	}

	#Map TU and GTFS stations
	tu_gtfs_station_df = match_tu_gtfs_stations(
		tu_stations,
		period=(tu_tur["DiaryDate"].min(), tu_tur["DiaryDate"].max()),
		bbox_buffer_m=1000,
		name_match_threshold=0.7
	)
	print("Mapping of TU and GTFS station has been exported to", config["paths"]["tu_gtfs_station_file"])
	tu_gtfs_station_df.to_csv(config["paths"]["tu_gtfs_station_file"], index=False, sep=",", decimal=".")

	rmse_based_matches = []
	trip_matching_summaries = []

	for i, tu_tur_row in tu_tur.iterrows():
		try:
			match_result = match_tu_trip_to_otp(
				tu_tur_row=tu_tur_row,
				tu_deltur=tu_deltur,
				otp_mode_routes_cache=otp_mode_routes_cache,
				otp_route_name_index=otp_route_name_index,
				otp_url=otp_url,
				search_window=search_window,
				max_itinerary_candidates=max_itinerary_candidates,
				tu_gtfs_station_df=tu_gtfs_station_df,
				return_trip_summary=return_trip_summary,
				request_timeout=request_timeout,
				station_anchor_wait_min=station_anchor_wait_min,
				transit_retry_enabled=transit_retry_enabled,
				transit_reluctance_sequence=transit_reluctance_sequence,
				walk_retry_enabled=walk_retry_enabled,
				walk_reluctance_sequence=walk_reluctance_sequence
			)
		except Exception as exc:
			print(f"Skipping TurId {tu_tur_row['TurId']} due to unexpected error: {exc}")
			if return_trip_summary:
				trip_matching_summaries.append({
					"TurId": tu_tur_row['TurId'],
					"SessionId": tu_tur_row.get("SessionId"),
					"trip_found": 0,
					"trip_not_found": 1,
					"last_print_if_not_found": str(exc),
					"failure_reason": type(exc).__name__,
					"used_anchor_fallback": False,
					"route_name_ignored": False,
					"rmse": pd.NA, "depart_deviation_min": pd.NA,
					"arrival_deviation_min": pd.NA, "weighted_diff_duration": pd.NA,
					"weighted_diff_distance": pd.NA, "iteration_id": pd.NA
				})
			continue

		if return_trip_summary:
			rmse_based_match, trip_matching_summary = match_result
		else:
			rmse_based_match = match_result
			trip_matching_summary = None
		if rmse_based_match is None:
			if return_trip_summary:
				trip_matching_summaries.append(trip_matching_summary)
			print(f"No rmse-based match found for TurId: {tu_tur_row['TurId']}")
			continue

		rmse_based_match_print_col = ["mode", "distance_km", "waitingtime", "duration_min", "route_short_name", "from", "to", "tu_deltur_depart_time", "otp_leg_depart_time"]
		print("rmse_based_match:")
		print(rmse_based_match[rmse_based_match_print_col].to_string(index=False, max_colwidth=None))

		rmse_based_matches.append(rmse_based_match)
		if return_trip_summary:
			trip_matching_summaries.append(trip_matching_summary)

	if not rmse_based_matches:
		print("No rmse-based matches found.")
		if trip_matching_summaries:
			trip_matching_summaries = pd.DataFrame(trip_matching_summaries)
			trip_matching_summaries.to_excel(config["paths"]["trip_matching_summaries_file"], index=False)
			_write_failures_file(trip_matching_summaries, config["paths"]["failures_file"])
			_print_and_export_summary_stats(trip_matching_summaries, config["paths"]["summary_stats_file"])
		return

	all_rmse_based_matches = pd.concat(rmse_based_matches, ignore_index=True)
	all_rmse_based_matches = _reorder_rmse_columns(all_rmse_based_matches)
	all_rmse_based_matches.to_csv(config["paths"]["rmse_based_matches_file"], sep = ",", decimal = ".", index=False)
	if return_trip_summary:
		trip_matching_summaries = pd.DataFrame(trip_matching_summaries)
		trip_matching_summaries.to_excel(config["paths"]["trip_matching_summaries_file"], index=False)
		_write_failures_file(trip_matching_summaries, config["paths"]["failures_file"])
		_print_and_export_summary_stats(trip_matching_summaries, config["paths"]["summary_stats_file"])
	print(f"\n\n\n____________________________________________________________________________________________")
	print(f"\n\n\nall_rmse_based_matches has been exported to {config['paths']['rmse_based_matches_file']}")
	print(f"Saved {len(all_rmse_based_matches)} rmse-based matches to {config['paths']['rmse_based_matches_file'].name}")

if __name__ == "__main__":
	main()