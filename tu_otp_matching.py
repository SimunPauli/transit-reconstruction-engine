import pandas as pd
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
	mode_map,
	otp_mode_routes_cache,
	otp_url,
	search_window,
	tu_gtfs_station_df,
):
	i_TurId = tu_tur_row["TurId"]
	tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]

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
		mode_map,
		otp_mode_routes_cache
	)
	print(f"route_short_name: {route_names}")
	print(f"route_names_ext: {route_names_ext}")

	if not modes_json:
		print(f"No valid public transport modes found for TurId: {i_TurId}")
		return None
	print(f"modes_json: {modes_json}")
	if any(mode in ["BUS", "S_TRAIN"] for mode in modes_list) and not route_names:
		print(f"No valid route found for TurId: {i_TurId}")
		return None
	if has_invalid_route_name(route_names) and route_names:
		print(f"Invalid route name: {route_names}")
		return None

	# Get the gtfs stop_ids for stations respondent travel through
	via_stopids = get_via_stops(tu_deltur_sub=tu_deltur_sub, tu_gtfs_station_df=tu_gtfs_station_df)

	# 2. Fetch all candidates (handles pagination & concat internally)
	is_bus_s_train = any(mode in ["BUS", "S_TRAIN"] for mode in modes_list)
	is_rail_tram_subway_ferry = any(mode in ["RAIL", "SUBWAY", "TRAM", "FERRY"] for mode in modes_list)
	if not is_bus_s_train and not is_rail_tram_subway_ferry:
		raise ValueError("No valid transit modes found. TurId: ", i_TurId, ".")
	if is_bus_s_train and is_rail_tram_subway_ferry:
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			modes_json=modes_json,
			route_short_name=route_names_ext,
			via_stopids=via_stopids,
			search_window=search_window,
			otp_url=otp_url)
	elif is_bus_s_train:
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			modes_json=modes_json,
			route_short_name=route_names,
			via_stopids=via_stopids,
			search_window=search_window,
			otp_url=otp_url)
	elif is_rail_tram_subway_ferry:
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			modes_json=modes_json,
			route_short_name=None,
			via_stopids=via_stopids,
			search_window=search_window,
			otp_url=otp_url)
	else:
		print("No valid transit modes found. TurId: ", i_TurId, ". Something went wrong.")

	if otp_candidates_df.empty:
		print(f"No OTP trips found for TurId: {i_TurId}")
		return None

	otp_candidates_df = filter_candidates_by_requirements(
		otp_candidates_df=otp_candidates_df,
		route_names=route_names,
		modes_list=modes_list,
		tur_id=i_TurId
	)
	# calculate waiting time
	otp_candidates_df = otp_candidates_df.sort_values(
		["iteration_id", "start_leg"]
	).reset_index(drop=True)

	otp_candidates_df["waitingtime"] = (
			(otp_candidates_df["start_leg"] - otp_candidates_df.groupby("iteration_id")["end_leg"].shift()) / 60 / 1000)
	otp_candidates_df["waitingtime"] = otp_candidates_df["waitingtime"].fillna(0)

	time_based_match = find_similar_trip(
		tu_tur_row,
		otp_candidates_df,
		arrival_dev_weight=1,
		print_devation_details=True
	)
	if time_based_match is None:
		print(f"No best trip found for TurId: {i_TurId}")
		return None
	time_based_match["TurId"] = i_TurId
	if otp_candidates_df.empty:
		print(f"No OTP trips found for TurId: {i_TurId}")
		return None

	time_based_match = find_similar_trip(
		tu_tur_row,
		otp_candidates_df,
		arrival_dev_weight=1,
		print_devation_details=True,
	)

	if time_based_match is None:
		print(f"No best trip found for TurId: {i_TurId}")
		return None

	time_based_match["TurId"] = i_TurId
	return time_based_match

def find_similar_trip(
		tu_tur_row,
		candidate_df,
		arrival_dev_weight=1,
		print_devation_details=False):
	candidate_df = candidate_df.copy()
	expected_depart = tu_tur_row["depart_dt"]
	expected_arrival = tu_tur_row["arrival_dt"]

	# Convert candidate_df times to datetime
	candidate_df["start_dt"] = pd.to_datetime(candidate_df["start_trip"], utc=True).dt.tz_convert("Europe/Copenhagen")
	candidate_df["end_dt"] = pd.to_datetime(candidate_df["end_trip"], utc=True).dt.tz_convert("Europe/Copenhagen")

	# Group by iteration_id and get start/end times for each trip
	trips = candidate_df.groupby("iteration_id").agg({
		"start_dt": "first",
		"end_dt": "first", #start/end are start/stop of the whole trip not that leg (deltur)
		"system_notice_tag": "first"
	}).reset_index()

	# Calculate total deviation (in minutes) for each trip
	trips["depart_deviation"] = (trips["start_dt"] - expected_depart).dt.total_seconds() / 60
	trips["arrival_deviation"] = (trips["end_dt"] - expected_arrival).dt.total_seconds() / 60

	trips["total_deviation"] = abs(trips["depart_deviation"]) + abs(trips["arrival_deviation"])*arrival_dev_weight

	if trips.empty or trips["total_deviation"].isna().all():
		print("No trips found with similar departure and arrival times.")
		return None

	# Find the best matching trip
	best_iteration = trips.loc[trips["total_deviation"].idxmin(), "iteration_id"]
	if print_devation_details:
		print(f"Best matching trip: iteration_id = {best_iteration}")
		print(f"Deviation details:")
		print(trips[["iteration_id", "depart_deviation", "arrival_deviation", "total_deviation"]].sort_values("total_deviation").head(10))

	# Filter candidate_df to get only the best trip
	best_trip_candidate_df = candidate_df[candidate_df["iteration_id"] == best_iteration].copy()

	return best_trip_candidate_df