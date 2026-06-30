import pandas as pd
import numpy as np
from itertools import combinations
from delturnr_otp_candidates import add_tu_delturnr_to_otp_candidates
from best_otp_candidate import find_best_match_by_rmse
from otp_client import load_all_candidates
from otp_utils import (
	has_invalid_route_name,
	resolve_route_short_names,
	get_via_stops,
	filter_candidates_by_requirements
)




def match_tu_trip_to_otp(
	tu_tur_row,
	tu_deltur,
	otp_mode_routes_cache,
	otp_url,
	walk_reluctance,
	car_reluctance,
	search_window,
	max_itinerary_candidates,
	tu_gtfs_station_df,
	print_deviation_details= True,
	return_trip_summary= False,
	request_timeout=60,
	print_query=False
):
	i_TurId = tu_tur_row["TurId"]
	tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]

	def _empty_trip_summary(last_print_if_not_found):
		return {
			"TurId": i_TurId,
			"SessionId": tu_tur_row.get("SessionId"),
			"trip_found": 0,
			"trip_not_found": 1,
			"last_print_if_not_found": last_print_if_not_found,
			"rmse": pd.NA,
			"depart_deviation_min": pd.NA,
			"arrival_deviation_min": pd.NA,
			"weighted_diff_duration": pd.NA,
			"weighted_diff_distance": pd.NA,
			"iteration_id": pd.NA
		}

	def _return_not_found(last_print_if_not_found):
		print(last_print_if_not_found)
		if return_trip_summary:
			return None, _empty_trip_summary(last_print_if_not_found)
		return None

	print("\n\n____________________________________________________________________________________________")
	print(f"TurId: {i_TurId}. With SessionId: {tu_tur_row['SessionId']}.")
	print(f"Tur coordinates origin (lat lon) :     {tu_tur_row['orig_lat']} {tu_tur_row['orig_lon']}")
	print(f"Tur coordinates destination (lat lon): {tu_tur_row['tiladrlat']} {tu_tur_row['tiladrlon']}")
	print(f"Depart: {tu_tur_row['depart_dt_str']}. Arrival: {tu_tur_row['arrival_dt_str']}.")
	# Print for debugging
	tu_deltur_sub_print_col = ["StageMode", "StageLength", "StageWaitMin", "StageDurationMin", "Route", "FromStation",
							   "ToStation"]
	print("tu_deltur_sub:")
	print(tu_deltur_sub[tu_deltur_sub_print_col].to_string(index=False, max_colwidth=None))

	route_names, route_names_ext, modes_json, modes_list = resolve_route_short_names(
		tu_deltur_sub,
		otp_mode_routes_cache
	)
	print(f"route_short_name: {route_names}")
	print(f"route_names_ext: {route_names_ext}")

	if not modes_json:
		return _return_not_found(f"No valid public transport modes found for TurId: {i_TurId}")
	print(f"modes_json: {modes_json}")
	if any(mode in ["BUS", "S_TRAIN"] for mode in modes_list) and has_invalid_route_name(route_names):
		return _return_not_found(f"Invalid route name: {route_names}")

	# Get the gtfs stop_ids for stations respondent travel through
	if (tu_deltur_sub["StageMode"].isin([32, 33, 34])).any():
		via_stopids = get_via_stops(tu_deltur_sub=tu_deltur_sub, tu_gtfs_station_df=tu_gtfs_station_df)
		print(f"via_stopids: {via_stopids}")
	else:
		via_stopids = None

	# 2. Fetch all candidates (handles pagination & concat internally)
	is_bus_s_train = any(mode in ["BUS", "S_TRAIN"] for mode in modes_list)
	is_rail_tram_subway_ferry = any(mode in ["RAIL", "SUBWAY", "TRAM", "FERRY"] for mode in modes_list)
	if not is_bus_s_train and not is_rail_tram_subway_ferry:
		raise ValueError("No valid transit modes found. TurId: ", i_TurId, ".")
	if is_bus_s_train and is_rail_tram_subway_ferry:
		route_short_name_for_loading = route_names_ext
	elif is_bus_s_train:
		route_short_name_for_loading = route_names
	elif is_rail_tram_subway_ferry:
		route_short_name_for_loading = None
	else:
		return _return_not_found(f"No valid transit modes found. TurId: {i_TurId}. Something went wrong.")

	otp_candidates_df, msg_filter = _load_candidates_with_reluctance_retries(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		modes_json=modes_json,
		route_names=route_names,
		route_short_name_for_loading=route_short_name_for_loading,
		modes_list=modes_list,
		via_stopids=via_stopids,
		walk_reluctance=walk_reluctance,
		car_reluctance=car_reluctance,
		search_window=search_window,
		max_itinerary_candidates=max_itinerary_candidates,
		otp_url=otp_url,
		request_timeout=request_timeout,
		print_query=print_query
	)

	if otp_candidates_df.empty:
		return _return_not_found(msg_filter)

	# calculate waiting time
	otp_candidates_df = otp_candidates_df.sort_values(
		["iteration_id", "start_leg"]
	).reset_index(drop=True)

	otp_candidates_df["waitingtime"] = (
			(otp_candidates_df["start_leg"] - otp_candidates_df.groupby("iteration_id")["end_leg"].shift()) / 60 / 1000)
	otp_candidates_df["waitingtime"] = otp_candidates_df["waitingtime"].fillna(0)

	#find root sum squared of weighted differences
	trips = find_best_match_by_rmse(
		tu_tur_row,
		tu_deltur_sub,
		otp_candidates_df
	)
	if trips is None:
		return _return_not_found(f"No best trip found for TurId: {i_TurId}")
	# Find the best matching trip (minimum RMSE)
	best_trip_summary = trips.loc[trips["rmse"].idxmin()].copy()
	best_iteration = trips.loc[trips["rmse"].idxmin(), "iteration_id"]

	if print_deviation_details:
		print(f"Best matching trip: iteration_id = {best_iteration}")
		print(f"RMSE details (top 10):")
		trips_display = trips.copy()
		trips_display["weighted_diff_duration"] = np.sqrt(trips_display["weighted_sq_diff_duration"])
		trips_display["weighted_diff_distance"] = np.sqrt(trips_display["weighted_sq_diff_distance"])
		detail_cols = ["iteration_id", "depart_deviation_min", "arrival_deviation_min",
		               "weighted_diff_duration", "weighted_diff_distance", "rmse"]
		print(trips_display[detail_cols].sort_values("rmse").head(10).to_string(index=False))
	# Filter otp_candidates_df to get only the best trip
	best_trip_candidate = otp_candidates_df[otp_candidates_df["iteration_id"] == best_iteration].copy()
	best_trip_candidate["TurId"] = i_TurId

	if return_trip_summary:
		trip_summary = {
			"TurId": i_TurId,
			"SessionId": tu_tur_row.get("SessionId"),
			"trip_found": 1,
			"trip_not_found": 0,
			"last_print_if_not_found": "",
			"rmse": round(best_trip_summary["rmse"],3),
			"depart_deviation_min": round(best_trip_summary["depart_deviation_min"]),
			"arrival_deviation_min": round(best_trip_summary["arrival_deviation_min"]),
			"weighted_diff_duration": round(np.sqrt(best_trip_summary["weighted_sq_diff_duration"]),1),
			"weighted_diff_distance": round(np.sqrt(best_trip_summary["weighted_sq_diff_distance"]),3),
			"iteration_id": best_iteration
		}
		return best_trip_candidate, trip_summary

	return best_trip_candidate

def _build_transit_reluctance_attempts(modes_list, reluctance_sequence=(0.9, 0.7, 0.5)):
	transit_modes = ["SUBWAY", "BUS", "RAIL", "S_TRAIN", "TRAM"]
	tu_transit_modes = [
		mode
		for mode in dict.fromkeys(modes_list)
		if mode in transit_modes
	]

	attempts = [None]

	for reluctance in reluctance_sequence:
		for mode in tu_transit_modes:
			attempts.append({mode: reluctance})

	for reluctance in reluctance_sequence:
		for n_modes in range(2, len(tu_transit_modes) + 1):
			for modes in combinations(tu_transit_modes, n_modes):
				attempts.append({mode: reluctance for mode in modes})

	return attempts


def _load_add_and_filter_candidates(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		modes_json,
		route_names,
		route_short_name_for_loading,
		modes_list,
		via_stopids,
		walk_reluctance,
		car_reluctance,
		search_window,
		max_itinerary_candidates,
		otp_url,
		request_timeout,
		print_query,
		transit_reluctances=None
):
	otp_candidates_df = load_all_candidates(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=tu_deltur_sub,
		modes_json=modes_json,
		route_short_name=route_short_name_for_loading,
		via_stopids=via_stopids,
		walk_reluctance=walk_reluctance,
		car_reluctance=car_reluctance,
		transit_reluctances=transit_reluctances,
		search_window=search_window,
		max_itinerary_candidates=max_itinerary_candidates,
		otp_url=otp_url,
		print_query=print_query,
		request_timeout=request_timeout
	)

	if otp_candidates_df.empty:
		return otp_candidates_df, "No OTP trips found"

	otp_candidates_df = add_tu_delturnr_to_otp_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		bike_stage_modes=(2, 8)
	)

	otp_candidates_df, msg_filter =  filter_candidates_by_requirements(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		route_names=route_names,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"]
	)
	return otp_candidates_df, msg_filter


def _load_candidates_with_reluctance_retries(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		modes_json,
		route_names,
		route_short_name_for_loading,
		modes_list,
		via_stopids,
		walk_reluctance,
		car_reluctance,
		search_window,
		max_itinerary_candidates,
		otp_url,
		request_timeout,
		print_query
):
	last_msg = ""

	for transit_reluctances in _build_transit_reluctance_attempts(modes_list):
		if transit_reluctances:
			print(f"Retrying with transit reluctances: {transit_reluctances}")

		otp_candidates_df, msg_filter = _load_add_and_filter_candidates(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			tu_gtfs_station_df=tu_gtfs_station_df,
			modes_json=modes_json,
			route_names=route_names,
			route_short_name_for_loading=route_short_name_for_loading,
			modes_list=modes_list,
			via_stopids=via_stopids,
			walk_reluctance=walk_reluctance,
			car_reluctance=car_reluctance,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url,
			request_timeout=request_timeout,
			print_query=print_query,
			transit_reluctances=transit_reluctances
		)

		if not otp_candidates_df.empty:
			return otp_candidates_df, ""

		last_msg = msg_filter

	return pd.DataFrame(), last_msg