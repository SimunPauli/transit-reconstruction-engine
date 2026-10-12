import io
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import pandas as pd
from src.tu import load_TU_data
from src.matching.tu_otp_matching import match_tu_trip_to_otp
from src.matching.candidate_search import SearchSettings
from src import otp
from src.matching.route_matching_utils import build_route_name_index
from src.stations.tu_gtfs_stations_match import match_tu_gtfs_stations
from src.matching.anchor_stitching import build_anchor_station_lookup
from src.config_loader import get_config, start_run
from src.matching.export_files import _write_failures_file, _print_and_export_summary_stats, _reorder_rmse_columns
from src.run_utils import Tee, wait_for_output_dir


def main():
	config = start_run(get_config())

	# Opened here (not just under `if __name__ == "__main__"`) so the log file is written
	# regardless of how main() is invoked - e.g. PyCharm's "Run 'main'" gutter action imports
	# this module and calls main() directly, without ever executing the __main__ guard below.
	with open(config["paths"]["log_file"], "w", encoding="utf-8") as log_file:
		tee = Tee(sys.stdout, log_file)
		with redirect_stdout(tee):
			_run(config, tee)


def _run(config, tee):
	return_trip_summary = config.get("matching", {}).get("return_trip_summary", True)
	transit_retry_cfg = config.get("reluctance_retries", {}).get("transit", {})
	walk_retry_cfg = config.get("reluctance_retries", {}).get("walk", {})
	config_request = config.get("request")
	settings = SearchSettings(
		otp_url=config_request["otp_url"],
		search_window=config_request["search_window"],
		max_itinerary_candidates=config_request["max_itinerary_candidates"],
		request_timeout=config_request["request_timeout"],
		transit_retry_enabled=transit_retry_cfg.get("enabled", True),
		transit_reluctance_sequence=tuple(transit_retry_cfg.get("sequence", [0.5, 0.25, 0.1])),
		walk_retry_enabled=walk_retry_cfg.get("enabled", True),
		walk_reluctance_sequence=tuple(walk_retry_cfg.get("sequence", [3, 4, 6])),
		station_anchor_wait_min=config.get("matching", {}).get("station_anchor_wait_min", 0),
	)
	# Concurrent OTP requests. The ceiling is OTP's own parallelism, not Python's.
	max_workers = config.get("run", {}).get("max_workers", 8)

	print(f"Search window: {settings.search_window}")


	print("Loading TU data...")
	tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu_from_config(config)
	print("TU data loaded")



	# RAIL, TRAM and SUBWAY are missing route name in TU.
	# So taking all routes for these modes. Which will be used when modes
	# that do include route name in TU only can access those routes, but for
	# those that do not, all routes will be used.
	otp_mode_routes_cache = {mode: otp.client.get_all_routes_for_mode(mode) for mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"]}

	# BUS and S_TRAIN do carry a route name in TU, but respondents free-text the bus
	# ones and spell them inconsistently ('102 A' for '102A') or drop the trailing
	# letter ('150' for '150S'). Index the real GTFS names so TU's spelling can be
	# translated into one OTP will actually match.
	otp_route_name_index = {
		mode: build_route_name_index(otp.client.get_all_routes_for_mode(mode))
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
	anchor_station_lookup = build_anchor_station_lookup(tu_gtfs_station_df)

	rmse_based_matches = []
	trip_matching_summaries = []

	def match_one(tu_tur_row):
		"""Match one TU trip, buffering its prints. Returns (output, match, summary)."""
		tee.local.buffer = io.StringIO()
		otp.client.trace.id = f"TurId={tu_tur_row['TurId']}"
		try:
			match_result = match_tu_trip_to_otp(
				tu_tur_row=tu_tur_row,
				tu_deltur=tu_deltur,
				otp_mode_routes_cache=otp_mode_routes_cache,
				otp_route_name_index=otp_route_name_index,
				tu_gtfs_station_df=tu_gtfs_station_df,
				anchor_station_lookup=anchor_station_lookup,
				settings=settings,
				return_trip_summary=return_trip_summary,
			)
			if return_trip_summary:
				rmse_based_match, trip_matching_summary = match_result
			else:
				rmse_based_match, trip_matching_summary = match_result, None
			if rmse_based_match is None:
				print(f"No rmse-based match found for TurId: {tu_tur_row['TurId']}")
			else:
				rmse_based_match_print_col = ["mode", "distance_km", "waitingtime", "duration_min", "route_short_name", "from", "to", "tu_deltur_depart_time", "otp_leg_depart_time"]
				print("rmse_based_match:")
				print(rmse_based_match[rmse_based_match_print_col].to_string(index=False, max_colwidth=None))
		except Exception as exc:
			print(f"Skipping TurId {tu_tur_row['TurId']} due to unexpected error: {exc}")
			rmse_based_match, trip_matching_summary = None, None
			if return_trip_summary:
				trip_matching_summary = {
					"TurId": tu_tur_row['TurId'],
					"SessionId": tu_tur_row.get("SessionId"),
					"trip_found": 0,
					"trip_near_route": 0,
					"trip_wrong_route": 0,
					"trip_not_found": 1,
					"route_match": pd.NA,
					"last_print_if_not_found": str(exc),
					"failure_reason": type(exc).__name__,
					"used_anchor_fallback": False,
					"rmse": pd.NA, "depart_deviation_min": pd.NA,
					"arrival_deviation_min": pd.NA, "deviation_duration_min": pd.NA,
					"deviation_distance_km": pd.NA, "iteration_id": pd.NA
				}
		finally:
			output = tee.local.buffer.getvalue()
			tee.local.buffer = None
			otp.client.trace.id = None
		return output, rmse_based_match, trip_matching_summary

	# Trips are independent and I/O-bound on OTP, hence threads. map() yields in TU order.
	print(f"Matching {len(tu_tur)} trips with {max_workers} worker(s)...")
	with ThreadPoolExecutor(max_workers=max_workers) as executor:
		for output, rmse_based_match, trip_matching_summary in executor.map(match_one, (row for _, row in tu_tur.iterrows())):
			print(output, end="")
			if rmse_based_match is not None:
				rmse_based_matches.append(rmse_based_match)
			if trip_matching_summary is not None:
				trip_matching_summaries.append(trip_matching_summary)

	wait_for_output_dir(config["paths"]["output_dir"])

	if not rmse_based_matches:
		print("No rmse-based matches found.")
		if trip_matching_summaries:
			trip_matching_summaries = pd.DataFrame(trip_matching_summaries)
			trip_matching_summaries.to_excel(config["paths"]["trip_matching_summaries_file"], index=False)
			_write_failures_file(trip_matching_summaries, config["paths"]["failures_file"])
			_print_and_export_summary_stats(
				trip_matching_summaries, config["paths"]["summary_stats_file"], tu_deltur, tu_gtfs_station_df
			)
		return

	all_rmse_based_matches = pd.concat(rmse_based_matches, ignore_index=True)
	all_rmse_based_matches = _reorder_rmse_columns(all_rmse_based_matches)
	all_rmse_based_matches.to_csv(config["paths"]["rmse_based_matches_file"], sep = ",", decimal = ".", index=False)
	if return_trip_summary:
		trip_matching_summaries = pd.DataFrame(trip_matching_summaries)
		trip_matching_summaries.to_excel(config["paths"]["trip_matching_summaries_file"], index=False)
		_write_failures_file(trip_matching_summaries, config["paths"]["failures_file"])
		_print_and_export_summary_stats(
			trip_matching_summaries, config["paths"]["summary_stats_file"], tu_deltur, tu_gtfs_station_df
		)
	print(f"\n\n\n____________________________________________________________________________________________")
	print(f"\n\n\nall_rmse_based_matches has been exported to {config['paths']['rmse_based_matches_file']}")
	print(f"Saved {len(all_rmse_based_matches)} rmse-based matches to {config['paths']['rmse_based_matches_file'].name}")

if __name__ == "__main__":
	main()