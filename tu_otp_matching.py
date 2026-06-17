import pandas as pd
import numpy as np
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
	max_itinerary_candidates,
	tu_gtfs_station_df,
	print_deviation_details= True
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
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url)
	elif is_bus_s_train:
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			modes_json=modes_json,
			route_short_name=route_names,
			via_stopids=via_stopids,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url)
	elif is_rail_tram_subway_ferry:
		otp_candidates_df = load_all_candidates(
			tu_tur_row=tu_tur_row,
			modes_json=modes_json,
			route_short_name=None,
			via_stopids=via_stopids,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url)
	else:
		print("No valid transit modes found. TurId: ", i_TurId, ". Something went wrong.")
		return None

	if otp_candidates_df.empty:
		print(f"No OTP trips found for TurId: {i_TurId}")
		return None

	# Match otp leg with TU delturnr (leg number). This will add column to otp_candidates_df
	# with delturnr to each leg. Missing legs from TU will get pd.NA
	otp_candidates_df = add_tu_delturnr_to_otp_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df
	)

	# 	Filter OTP candidates to ensure all required routes and modes are present and TU transit deltur
	# 	is matched.
	otp_candidates_df = filter_candidates_by_requirements(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		route_names=route_names,
		modes_list=modes_list,
		tur_id=i_TurId
	)

	if otp_candidates_df is None or otp_candidates_df.empty:
		print(f"No OTP trips left after filtering for TurId: {i_TurId}")
		return None

	# calculate waiting time
	otp_candidates_df = otp_candidates_df.sort_values(
		["iteration_id", "start_leg"]
	).reset_index(drop=True)

	otp_candidates_df["waitingtime"] = (
			(otp_candidates_df["start_leg"] - otp_candidates_df.groupby("iteration_id")["end_leg"].shift()) / 60 / 1000)
	otp_candidates_df["waitingtime"] = otp_candidates_df["waitingtime"].fillna(0)

	trips = find_best_match_by_rmse(
		tu_tur_row,
		tu_deltur_sub,
		otp_candidates_df,
		w_departure_min=1.0,
		w_arrival_min=1.0,
		w_walk_min=1.0,
		w_transit_min=1.0,
		w_walk_km=1.0,
		w_transit_km=1.0
	)
	if trips is None:
		print(f"No best trip found for TurId: {i_TurId}")
		return None

	# Find the best matching trip (minimum RMSE)
	best_iteration = trips.loc[trips["rmse"].idxmin(), "iteration_id"]

	if print_deviation_details:
		print(f"Best matching trip: iteration_id = {best_iteration}")
		print(f"RMSE details (top 10):")
		detail_cols = ["iteration_id", "depart_deviation_min", "arrival_deviation_min",
		               "weighted_sq_diff_duration", "weighted_sq_diff_distance", "rmse"]
		print(trips[detail_cols].sort_values("rmse").head(10).to_string(index=False))

	# Filter otp_candidates_df to get only the best trip
	best_trip_candidate = otp_candidates_df[otp_candidates_df["iteration_id"] == best_iteration].copy()

	return best_trip_candidate

def find_best_match_by_rmse(
		tu_tur_row,
		tu_deltur_sub,
		otp_candidates_df,
		w_departure_min=1.0,
		w_arrival_min=1.0,
		w_walk_min=1.0,
		w_transit_min=1.0,
		w_walk_km=1.0,
		w_transit_km=1.0,
		print_deviation_details=False):
	"""
	Find the best matching OTP trip using weighted RMSE.

	Calculates squared differences for:
	- Departure time (minutes)
	- Arrival time (minutes)
	- Duration per leg (minutes) - split by WALK vs transit
	- Distance per leg (km) - split by WALK vs transit

	Parameters
	----------
	tu_tur_row : pd.Series
		Row from tu_tur with trip-level info (depart_dt, arrival_dt, etc.)
	tu_deltur_sub : pd.DataFrame
		Subset of tu_deltur for this TurId, with Delturnr, StageMode, StageLength, StageDurationMin
	otp_candidates_df : pd.DataFrame
		OTP candidates with tu_Delturnr column already added
	w_departure_min : float
		Weight for departure time difference
	w_arrival_min : float
		Weight for arrival time difference
	w_walk_min : float
		Weight for WALK leg duration difference
	w_transit_min : float
		Weight for transit leg duration difference
	w_walk_km : float
		Weight for WALK leg distance difference
	w_transit_km : float
		Weight for transit leg distance difference
	print_deviation_details : bool
		Whether to print top 10 matches

	Returns
	-------
	pd.DataFrame
		Best matching trip (all legs from single iteration_id)
	"""

	# Expected values from TU
	expected_depart = tu_tur_row["depart_dt"]
	expected_arrival = tu_tur_row["arrival_dt"]

	# Convert times to datetime
	otp_candidates_df["start_trip"] = pd.to_datetime(otp_candidates_df["start_trip"], utc=True).dt.tz_convert("Europe/Copenhagen")
	otp_candidates_df["end_trip"] = pd.to_datetime(otp_candidates_df["end_trip"], utc=True).dt.tz_convert("Europe/Copenhagen")

	# Map TU leg attributes by Delturnr
	tu_leg_duration = (
		tu_deltur_sub
		.set_index("Delturnr")["StageDurationMin"]
		.astype(float)
	)
	tu_leg_dist = (
		tu_deltur_sub
		.set_index("Delturnr")["StageLength"]
		.astype(float)
	)

	otp_candidates_df["tu_duration_min"] = otp_candidates_df["tu_Delturnr"].map(tu_leg_duration)
	otp_candidates_df["tu_distance_km"] = otp_candidates_df["tu_Delturnr"].map(tu_leg_dist)

	# Calculate per-leg squared differences
	# For legs with no TU match (tu_Delturnr is NA), the difference is 0 (don't penalize extra OTP legs)
	otp_candidates_df["sq_diff_duration_min"] = 0.0
	otp_candidates_df["sq_diff_distance_km"] = 0.0

	# Only calculate differences for matched legs
	matched_mask = otp_candidates_df["tu_Delturnr"].notna()

	otp_candidates_df.loc[matched_mask, "sq_diff_duration_min"] = (
			(otp_candidates_df.loc[matched_mask, "duration_min"] - otp_candidates_df.loc[matched_mask, "tu_duration_min"]) ** 2
	)
	otp_candidates_df.loc[matched_mask, "sq_diff_distance_km"] = (
			(otp_candidates_df.loc[matched_mask, "distance_km"] - otp_candidates_df.loc[matched_mask, "tu_distance_km"]) ** 2
	)

	# Apply weights based on mode (WALK vs transit)
	otp_candidates_df["weighted_sq_diff_duration"] = 0.0
	otp_candidates_df["weighted_sq_diff_distance"] = 0.0

	walk_mask = (otp_candidates_df["mode"] == "WALK") & matched_mask
	transit_mask = (otp_candidates_df["mode"] != "WALK") & matched_mask

	otp_candidates_df.loc[walk_mask, "weighted_sq_diff_duration"] = (
			w_walk_min * otp_candidates_df.loc[walk_mask, "sq_diff_duration_min"]
	)
	otp_candidates_df.loc[walk_mask, "weighted_sq_diff_distance"] = (
			w_walk_km * otp_candidates_df.loc[walk_mask, "sq_diff_distance_km"]
	)

	otp_candidates_df.loc[transit_mask, "weighted_sq_diff_duration"] = (
			w_transit_min * otp_candidates_df.loc[transit_mask, "sq_diff_duration_min"]
	)
	otp_candidates_df.loc[transit_mask, "weighted_sq_diff_distance"] = (
			w_transit_km * otp_candidates_df.loc[transit_mask, "sq_diff_distance_km"]
	)

	# Aggregate per iteration
	trips = otp_candidates_df.groupby("iteration_id").agg({
		"start_trip": "first",
		"end_trip": "first",
		"weighted_sq_diff_duration": "sum",
		"weighted_sq_diff_distance": "sum",
		"system_notice_tag": "first"
	}).reset_index()

	# Calculate trip-level time deviations (minutes)
	trips["depart_deviation_min"] = (trips["start_trip"] - expected_depart).dt.total_seconds() / 60
	trips["arrival_deviation_min"] = (trips["end_trip"] - expected_arrival).dt.total_seconds() / 60

	trips["sq_diff_depart"] = w_departure_min * (trips["depart_deviation_min"] ** 2)
	trips["sq_diff_arrival"] = w_arrival_min * (trips["arrival_deviation_min"] ** 2)

	# Total sum of weighted squared differences
	trips["sum_weighted_sq_diff"] = (
			trips["sq_diff_depart"] +
			trips["sq_diff_arrival"] +
			trips["weighted_sq_diff_duration"] +
			trips["weighted_sq_diff_distance"]
	)

	# Calculate number of terms for RMSE (denominator)
	# Always have departure + arrival = 2 terms
	# Plus number of matched legs × 2 (duration + distance per leg)
	n_matched_legs_per_iteration = (
		otp_candidates_df[otp_candidates_df["tu_Delturnr"].notna()]
		.groupby("iteration_id")
		.size()
	)
	trips["n_terms"] = 2 + (n_matched_legs_per_iteration * 2)
	trips["n_terms"] = trips["n_terms"].fillna(2).astype(int)  # If no matched legs, just departure + arrival

	# Calculate RMSE
	trips["rmse"] = np.sqrt(trips["sum_weighted_sq_diff"] / trips["n_terms"])

	if trips.empty or trips["rmse"].isna().all():
		print("No trips found with valid RMSE.")
		return None

	return trips


def add_tu_delturnr_to_otp_candidates(otp_candidates_df, tu_deltur_sub, tu_gtfs_station_df):
	"""
	Add a tu_Delturnr column to OTP legs by aligning each OTP itinerary with the TU leg sequence.

	OTP may contain legs missing from TU, especially transfer WALK legs between transit legs.
	Those unmatched OTP legs get pd.NA.

	Matching rules:
	- Legs are matched in chronological/order sequence within each iteration_id.
	- WALK matches TU StageMode == 1.
	- Transit matches TU otp_mode, e.g. BUS, SUBWAY, S_TRAIN, RAIL, TRAM, FERRY.
	- For BUS/S_TRAIN, route_short_name is also checked when TU Route is available.
	- For non-bus transit, FromStation/ToStation are matched against GTFS station IDs via tu_gtfs_station_df.
	"""
	tu_legs = (
		tu_deltur_sub
		.sort_values("Delturnr")
		.reset_index(drop=True)
		.copy()
	)

	# Build a lookup: (otp_mode, tu_station_name) → set of gtfs_station_ids
	station_lookup = {}
	if tu_gtfs_station_df is not None and not tu_gtfs_station_df.empty:
		for _, row in tu_gtfs_station_df.iterrows():
			key = (row["otp_mode"], row["tu_station_name"])
			if key not in station_lookup:
				station_lookup[key] = set()
			station_lookup[key].add(row["gtfs_station_id"])

	def _clean(value):
		if pd.isna(value):
			return None
		value = str(value).strip()
		if not value or value.lower() in {"nan", "none"}:
			return None
		return value

	def _station_matches(tu_station_name, otp_stop_name, mode):
		"""Check if TU station matches OTP stop using GTFS mapping."""
		tu_station_name = _clean(tu_station_name)
		otp_stop_name = _clean(otp_stop_name)

		if tu_station_name is None:
			return True
		if otp_stop_name is None:
			return False

		# Try exact GTFS ID match first if available
		lookup_key = (mode, tu_station_name)
		if lookup_key in station_lookup:
			# Check if otp_stop_name contains any of the mapped GTFS IDs
			# or if it matches the station name pattern
			gtfs_ids = station_lookup[lookup_key]
			for gtfs_id in gtfs_ids:
				if gtfs_id in otp_stop_name:
					return True

		# Fallback to fuzzy name matching
		return tu_station_name.casefold() in otp_stop_name.casefold()

	def _route_matches(tu_route, otp_route):
		tu_route = _clean(tu_route)
		otp_route = _clean(otp_route)

		if tu_route is None:
			return True
		if otp_route is None:
			return False

		return tu_route == otp_route

	def _leg_matches(otp_leg, tu_leg):
		tu_mode = tu_leg.get("otp_mode")

		if pd.isna(tu_mode):
			tu_mode = "WALK" if tu_leg.get("StageMode") == 1 else None

		if otp_leg["mode"] != tu_mode:
			return False

		# For BUS/S_TRAIN, check route name
		if otp_leg["mode"] in {"BUS", "S_TRAIN"}:
			if not _route_matches(tu_leg.get("Route"), otp_leg.get("route_short_name")):
				return False

		# For transit with stations, check station match using GTFS mapping
		if otp_leg["mode"] in {"SUBWAY", "RAIL", "TRAM", "FERRY", "S_TRAIN"}:
			if not _station_matches(
				tu_leg.get("FromStation"),
				otp_leg.get("from"),
				otp_leg["mode"]
			):
				return False
			if not _station_matches(
				tu_leg.get("ToStation"),
				otp_leg.get("to"),
				otp_leg["mode"]
			):
				return False

		return True

	def _align_iteration(iteration_df):
		iteration_id = iteration_df.name
		iteration_df.insert(0, "iteration_id", iteration_id)
		iteration_df = iteration_df.sort_values("leg_id").copy()

		delturnrs = []
		tu_pos = 0

		for _, otp_leg in iteration_df.iterrows():
			matched_delturnr = pd.NA

			while tu_pos < len(tu_legs):
				tu_leg = tu_legs.iloc[tu_pos]

				if _leg_matches(otp_leg, tu_leg):
					matched_delturnr = tu_leg["Delturnr"]
					tu_pos += 1
					break

				# If the current TU leg does not match this OTP leg, do not consume the TU leg.
				# The OTP leg is treated as an extra OTP leg, e.g. a transfer walk missing in TU.
				break

			delturnrs.append(matched_delturnr)

		iteration_df["tu_Delturnr"] = delturnrs
		return iteration_df

	return (
		otp_candidates_df
		.groupby("iteration_id", group_keys=False)
		.apply(_align_iteration)
		.reset_index(drop=True)
	)