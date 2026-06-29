import pandas as pd
import numpy as np

from constant import WALK_BIKE_TIME_RATIO
from otp_client import load_all_candidates
from otp_utils import (
	has_invalid_route_name,
	resolve_route_short_names,
	get_via_stops,
	filter_candidates_by_requirements
)
from config import get_config




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
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			modes_json=modes_json,
			route_short_name=route_names_ext,
			via_stopids=via_stopids,
			walk_reluctance=walk_reluctance,
			car_reluctance=car_reluctance,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url,
			print_query=print_query,
			request_timeout=request_timeout
		)
	elif is_bus_s_train:
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			modes_json=modes_json,
			route_short_name=route_names,
			via_stopids=via_stopids,
			walk_reluctance=walk_reluctance,
			car_reluctance=car_reluctance,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url,
			print_query=print_query,
			request_timeout=request_timeout
		)
	elif is_rail_tram_subway_ferry:
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			modes_json=modes_json,
			route_short_name=None,
			via_stopids=via_stopids,
			walk_reluctance=walk_reluctance,
			car_reluctance=car_reluctance,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url,
			print_query=print_query,
			request_timeout=request_timeout
		)
	else:
		return _return_not_found(f"No valid transit modes found. TurId: {i_TurId}. Something went wrong.")
	if otp_candidates_df.empty:
		return _return_not_found(f"No OTP trips found for TurId: {i_TurId}")

	# Match otp leg with TU delturnr (leg number). This will add column to otp_candidates_df
	# with delturnr to each leg. Missing legs from TU will get pd.NA
	otp_candidates_df = add_tu_delturnr_to_otp_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		bike_stage_modes=(2,8)
	)

	# 	Filter OTP candidates to ensure all required routes and modes are present and TU transit deltur
	# 	is matched.
	otp_candidates_df, msg_filter = filter_candidates_by_requirements(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		route_names=route_names,
		modes_list=modes_list,
		tur_id=i_TurId
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

def find_best_match_by_rmse(
		tu_tur_row,
		tu_deltur_sub,
		otp_candidates_df):
	"""
	Find the best matching OTP trip using weighted sum of squares (we call it rmse).

	Calculates weighted squared differences for:
	- Departure time (minutes)
	- Arrival time (minutes)
	- Duration per leg (minutes) - split by street_mode vs transit
	- Distance per leg (km) - split by street_mode vs transit

	Parameters "w_" are weights for each of the metrics.
	"""
	config = get_config()
	config_weights = config["squared_error_weights"]
	w_departure_min   = config_weights["w_departure_min"]
	w_arrival_min     = config_weights["w_arrival_min"]
	w_street_mode_min = config_weights["w_street_mode_min"]
	w_transit_min     = config_weights["w_transit_min"]
	w_street_mode_km  = config_weights["w_street_mode_km"]
	w_transit_km      = config_weights["w_transit_km"]

	expected_depart  = tu_tur_row["depart_dt"]
	expected_arrival = tu_tur_row["arrival_dt"]

	legs = otp_candidates_df.copy()

	# Convert trip-level times to local datetime
	legs["start_trip"] = pd.to_datetime(legs["start_trip"], utc=True).dt.tz_convert("Europe/Copenhagen")
	legs["end_trip"]   = pd.to_datetime(legs["end_trip"],   utc=True).dt.tz_convert("Europe/Copenhagen")

	# Map TU leg attributes by Delturnr
	tu_leg_duration = tu_deltur_sub.set_index("Delturnr")["StageDurationMin"].astype(float)
	tu_leg_dist     = tu_deltur_sub.set_index("Delturnr")["StageLength"].astype(float)

	legs["tu_duration_min"] = legs["tu_Delturnr"].map(tu_leg_duration)
	legs["tu_distance_km"]  = legs["tu_Delturnr"].map(tu_leg_dist)

	street_modes = {
		"WALK", "BIKE", "BIKE_RENTAL", "BIKE_TO_PARK", "CAR", "CARPOOL",
		"CAR_HAILING", "CAR_RENTAL", "CAR_TO_PARK", "FLEXIBLE", "SCOOTER_RENTAL"
	}

	matched_mask     = legs["tu_Delturnr"].notna()
	street_mode_mask = legs["mode"].isin(street_modes) & matched_mask
	transit_mask     = ~legs["mode"].isin(street_modes) & matched_mask

	# Weighted squared differences — 0 for unmatched legs (no penalty for extra OTP legs)
	legs["weighted_sq_diff_duration"] = 0.0
	legs["weighted_sq_diff_distance"] = 0.0

	for mask, w_min, w_km in [
		(street_mode_mask, w_street_mode_min, w_street_mode_km),
		(transit_mask,     w_transit_min,     w_transit_km),
	]:
		legs.loc[mask, "weighted_sq_diff_duration"] = (
			w_min * ((legs.loc[mask, "duration_min"] - legs.loc[mask, "tu_duration_min"]) ** 2)
		)
		legs.loc[mask, "weighted_sq_diff_distance"] = (
			w_km * ((legs.loc[mask, "distance_km"] - legs.loc[mask, "tu_distance_km"]) ** 2)
		)

	# Aggregate per iteration
	trips = legs.groupby("iteration_id").agg(
		start_trip = ("start_trip", "first"),
		end_trip = ("end_trip", "first"),
		weighted_sq_diff_duration = ("weighted_sq_diff_duration", "sum"),
		weighted_sq_diff_distance = ("weighted_sq_diff_distance", "sum"),
		system_notice_tag = ("system_notice_tag", "first"),
	).reset_index()

	# Trip-level time deviations
	trips["depart_deviation_min"]  = (trips["start_trip"] - expected_depart).dt.total_seconds() / 60
	trips["arrival_deviation_min"] = (trips["end_trip"]   - expected_arrival).dt.total_seconds() / 60

	trips["weighted_sq_diff_depart"]  = w_departure_min * (trips["depart_deviation_min"] ** 2)
	trips["weighted_sq_diff_arrival"] = w_arrival_min   * (trips["arrival_deviation_min"] ** 2)

	trips["sum_weighted_sq_diff"] = (
		trips["weighted_sq_diff_depart"]    +
		trips["weighted_sq_diff_arrival"]   +
		trips["weighted_sq_diff_duration"]  +
		trips["weighted_sq_diff_distance"]
	)

	# root for interpretability. Doesn't affect the ranking across trips.
	trips["rmse"] = np.sqrt(trips["sum_weighted_sq_diff"])

	if trips.empty or trips["rmse"].isna().all():
		print("No trips found with valid Weighted Sum of Squares (rmse) values.")
		return None

	return trips


def add_tu_delturnr_to_otp_candidates(
		otp_candidates_df,
		tu_deltur_sub,
		tu_gtfs_station_df,
		bike_stage_modes=(2,8)
):
	"""
	Add a tu_Delturnr column to OTP legs by aligning each OTP itinerary with the TU leg sequence.

	OTP may contain legs missing from TU, especially transfer WALK legs between transit legs.
	Those unmatched OTP legs get pd.NA.

	Bicycle TU legs can be matched to OTP WALK legs as placeholders. For those legs,
	duration_min is multiplied by walk_bike_ratio to approximate cycling time.
	"""
	from tu_gtfs_stations_match import _normalise_name

	tu_deltur_sub = (
		tu_deltur_sub
		.sort_values("Delturnr")
		.reset_index(drop=True)
		.copy()
	)

	bike_stage_modes = set(bike_stage_modes)

	# Build a lookup: (otp_mode, tu_station_name) → set of gtfs_station_ids
	station_lookup = {}
	if tu_gtfs_station_df is not None and not tu_gtfs_station_df.empty:
		for _, row in tu_gtfs_station_df.iterrows():
			key = (row["otp_mode"],_normalise_name(str(row["tu_station_name"])))
			if key not in station_lookup:
				station_lookup[key] = set()
			station_lookup[key].add(row["gtfs_station_id"])

	def _route_matches(tu_route, otp_route):
		if tu_route is None or (isinstance(tu_route, float) and pd.isna(tu_route)):
			return True
		if otp_route is None or (isinstance(otp_route, float) and pd.isna(otp_route)):
			return False
		tu_route = str(tu_route).strip()
		otp_route = str(otp_route).strip()
		if not tu_route:
			return True
		return tu_route == otp_route

	def _station_matches(tu_station_name, otp_gtfs_id, mode):
		"""Check if TU station matches OTP stop using GTFS mapping."""
		if tu_station_name is None or (isinstance(tu_station_name, float) and pd.isna(tu_station_name)):
			return True
		if otp_gtfs_id is None and pd.isna(otp_gtfs_id):
			return False

		lookup_key = (mode, _normalise_name(str(tu_station_name)))
		if lookup_key in station_lookup:
			return otp_gtfs_id in station_lookup[lookup_key]

		return False

	def _leg_matches(otp_leg, tu_deltur_sub_leg):
		tu_mode = tu_deltur_sub_leg.get("otp_mode")
		tu_stage_mode = int(tu_deltur_sub_leg.get("StageMode"))

		if tu_stage_mode in bike_stage_modes:
			tu_mode = "WALK"
		elif pd.isna(tu_mode):
			tu_mode = "WALK"  #TODO: All missing modes are set to WALK!

		if otp_leg["mode"] != tu_mode:
			return False

		# For BUS/S_TRAIN, check route name
		if otp_leg["mode"] in {"BUS", "S_TRAIN"}:
			if not _route_matches(tu_deltur_sub_leg.get("Route"), otp_leg.get("route_short_name")):
				return False
		# For transit with stations, check station match using GTFS mapping
		elif otp_leg["mode"] in {"SUBWAY", "RAIL", "S_TRAIN"}:
			if not _station_matches(
				tu_deltur_sub_leg.get("FromStation"),
				otp_leg.get("from_gtfs_id"),
				otp_leg["mode"]
			):
				return False
			if not _station_matches(
				tu_deltur_sub_leg.get("ToStation"),
				otp_leg.get("to_gtfs_id"),
				otp_leg["mode"]
			):
				return False

			#TRAM and FERRY only have stops and route_name when it has been added manually during data-processing
		return True

	def _align_iteration(iteration_df):
		iteration_id = iteration_df.name
		iteration_df = iteration_df.sort_values("leg_id").copy()

		delturnrs = []
		is_bike_placeholders = []
		tu_pos = 0

		for _, otp_leg in iteration_df.iterrows():
			matched_delturnr = pd.NA
			is_bike_placeholder = False

			while tu_pos < len(tu_deltur_sub):
				tu_deltur_sub_leg = tu_deltur_sub.iloc[tu_pos]

				if _leg_matches(otp_leg, tu_deltur_sub_leg):
					matched_delturnr = tu_deltur_sub_leg["Delturnr"]
					is_bike_placeholder = (
						otp_leg["mode"] == "WALK" and (tu_deltur_sub_leg["StageMode"] in bike_stage_modes)
					)
					tu_pos += 1
					break

				# If the current TU leg does not match this OTP leg, do not consume the TU leg.
				# The OTP leg is treated as an extra OTP leg, e.g. a transfer walk missing in TU.
				break

			delturnrs.append(matched_delturnr)
			is_bike_placeholders.append(is_bike_placeholder)


		iteration_df["tu_Delturnr"] = delturnrs
		iteration_df["is_bike_placeholder"] = is_bike_placeholders
		iteration_df["iteration_id"] = iteration_id

		return iteration_df

	otp_candidates_df = (
		otp_candidates_df
		.groupby("iteration_id", group_keys=False)
		.apply(_align_iteration)
		.reset_index(drop=True)
	)
	otp_candidates_df["duration_min_otp"] = otp_candidates_df["duration_min"]
	otp_candidates_df["duration_min_otp"] = otp_candidates_df["duration_min"].astype(float)
	otp_candidates_df.loc[otp_candidates_df["is_bike_placeholder"], "duration_min"] = (
		otp_candidates_df.loc[otp_candidates_df["is_bike_placeholder"], "duration_min"]
		* WALK_BIKE_TIME_RATIO
	).round().astype(int)
	otp_candidates_df.loc[otp_candidates_df["is_bike_placeholder"], "mode"] = "BICYCLE"
	return otp_candidates_df