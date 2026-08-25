import pandas as pd
import numpy as np
from itertools import combinations
from delturnr_otp_candidates import add_tu_delturnr_to_otp_candidates
from best_otp_candidate import find_best_match_by_rmse
from otp_client import load_all_candidates, request_direct_leg
from otp_utils import (
	has_invalid_route_name,
	resolve_route_short_names,
	get_via_stops,
	filter_candidates_by_requirements
)
from constant import (
	DIRECT_ACCESS_MODE_MAP,
	REASON_NO_VALID_MODES,
	REASON_INVALID_ROUTE_NAME,
	REASON_NO_OTP_CANDIDATES,
	REASON_NO_DIRECT_ACCESS_ROUTE,
	REASON_NO_DIRECT_EGRESS_ROUTE,
	REASON_NO_BEST_MATCH,
	REASON_CAR_LEG_NOT_SATISFIED,
)
from station_anchor_fallback import find_known_anchor_stations, stitch_candidates




def match_tu_trip_to_otp(
	tu_tur_row,
	tu_deltur,
	otp_mode_routes_cache,
	otp_route_name_index,
	otp_url,
	search_window,
	max_itinerary_candidates,
	tu_gtfs_station_df,
	walk_reluctance=2,
	car_reluctance=2,
	transit_retry_enabled=True,
	transit_reluctance_sequence=(0.5, 0.25, 0.1),
	walk_retry_enabled=True,
	walk_reluctance_sequence=(3, 4, 6),
	print_deviation_details= True,
	return_trip_summary= False,
	request_timeout=60,
	print_query=False,
	station_anchor_wait_min=0
):
	i_TurId = tu_tur_row["TurId"]
	tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
	used_anchor_fallback = False

	def _empty_trip_summary(last_print_if_not_found, failure_reason):
		return {
			"TurId": i_TurId,
			"SessionId": tu_tur_row.get("SessionId"),
			"trip_found": 0,
			"trip_not_found": 1,
			"last_print_if_not_found": last_print_if_not_found,
			"failure_reason": failure_reason,
			"used_anchor_fallback": used_anchor_fallback,
			"rmse": pd.NA,
			"depart_deviation_min": pd.NA,
			"arrival_deviation_min": pd.NA,
			"weighted_diff_duration": pd.NA,
			"weighted_diff_distance": pd.NA,
			"iteration_id": pd.NA
		}

	def _return_not_found(last_print_if_not_found, failure_reason=None):
		print(last_print_if_not_found)
		if return_trip_summary:
			return None, _empty_trip_summary(last_print_if_not_found, failure_reason or last_print_if_not_found)
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
		otp_mode_routes_cache,
		otp_route_name_index
	)
	print(f"route_short_name: {', '.join(route_names)}")
	print(f"route_names_ext: {', '.join(route_names_ext)}")

	if not modes_json:
		return _return_not_found(REASON_NO_VALID_MODES)
	print(f"modes_json: {', '.join(m['mode'] for m in modes_json)}")
	if any(mode in ["BUS", "S_TRAIN"] for mode in modes_list) and has_invalid_route_name(route_names):
		return _return_not_found(f"{REASON_INVALID_ROUTE_NAME} (routes={route_names})", REASON_INVALID_ROUTE_NAME)

	# Get the gtfs stop_ids for stations respondent travel through
	if (tu_deltur_sub["StageMode"].isin([32, 33, 34])).any():
		via_stopids = get_via_stops(tu_deltur_sub=tu_deltur_sub, tu_gtfs_station_df=tu_gtfs_station_df)
		print(f"via_stopids: {', '.join(via_stopids)}")
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

	tu_deltur_sorted = tu_deltur_sub.sort_values("Delturnr")
	is_car_access = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[0]), "WALK") == "CAR"
	is_car_egress = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[-1]), "WALK") == "CAR"

	_candidate_kwargs = dict(
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
		transit_retry_enabled=transit_retry_enabled,
		transit_reluctance_sequence=transit_reluctance_sequence,
		walk_retry_enabled=walk_retry_enabled,
		walk_reluctance_sequence=walk_reluctance_sequence,
		search_window=search_window,
		max_itinerary_candidates=max_itinerary_candidates,
		otp_url=otp_url,
		request_timeout=request_timeout,
		print_query=print_query
	)

	otp_candidates_df = pd.DataFrame()
	msg_filter = ""

	if is_car_access or is_car_egress:
		# CAR access/egress has no reliable direct request to OTP (see otp_client._get_access_egress):
		# access bundles ["WALK", "CAR_DROP_OFF"] and OTP is free to silently return WALK instead. So
		# CAR trips are anchored at a known rail/S-train/subway station and stitched with an
		# unambiguous single-mode CAR direct leg (station_anchor_fallback.py) instead of relying on
		# that bundled request.
		first_stop_id, last_stop_id = find_known_anchor_stations(tu_deltur_sub, tu_gtfs_station_df)
		anchor_usable = (
			(first_stop_id or not is_car_access)
			and (last_stop_id or not is_car_egress)
			and (first_stop_id or last_stop_id)
		)
		if anchor_usable:
			otp_candidates_df, msg_filter = _try_station_anchored_fallback(
				**_candidate_kwargs,
				first_stop_id=first_stop_id,
				last_stop_id=last_stop_id,
				wait_min=station_anchor_wait_min
			)
			if not otp_candidates_df.empty:
				used_anchor_fallback = True
				print(f"ANCHOR_FALLBACK_USED (car access/egress): TurId={i_TurId}")

		if otp_candidates_df.empty:
			# No usable anchor station for the required CAR side (e.g. CAR combined with a
			# bus-only trip, which has no named stations in TU) — fall back to the normal
			# full-route query, then verify below that the CAR side actually came back as CAR,
			# since OTP may have silently substituted WALK.
			otp_candidates_df, msg_filter = _load_candidates_with_reluctance_retries(**_candidate_kwargs)
			if otp_candidates_df.empty:
				return _return_not_found(msg_filter)

			valid_iterations = otp_candidates_df.groupby("iteration_id").apply(
				lambda g: (
					(not is_car_access or g.loc[g["leg_id"].idxmin(), "mode"] == "CAR")
					and (not is_car_egress or g.loc[g["leg_id"].idxmax(), "mode"] == "CAR")
				)
			)
			keep_ids = valid_iterations[valid_iterations].index
			otp_candidates_df = otp_candidates_df[otp_candidates_df["iteration_id"].isin(keep_ids)]
			if otp_candidates_df.empty:
				fail_msg = (
					f"{REASON_CAR_LEG_NOT_SATISFIED}: no candidate itinerary had CAR on the "
					f"required access/egress leg (OTP returned WALK instead). TurId={i_TurId}"
				)
				return _return_not_found(fail_msg, REASON_CAR_LEG_NOT_SATISFIED)
	else:
		otp_candidates_df, msg_filter = _load_candidates_with_reluctance_retries(**_candidate_kwargs)

	if otp_candidates_df.empty:
		first_stop_id, last_stop_id = find_known_anchor_stations(tu_deltur_sub, tu_gtfs_station_df)
		if not first_stop_id and not last_stop_id:
			return _return_not_found(msg_filter)

		otp_candidates_df, fallback_msg = _try_station_anchored_fallback(
			**_candidate_kwargs,
			first_stop_id=first_stop_id,
			last_stop_id=last_stop_id,
			wait_min=station_anchor_wait_min
		)
		if otp_candidates_df.empty:
			return _return_not_found(f"{msg_filter}; fallback={fallback_msg}", fallback_msg)

		used_anchor_fallback = True
		print(f"ANCHOR_FALLBACK_USED: TurId={i_TurId}")

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
		return _return_not_found(REASON_NO_BEST_MATCH)
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
			"used_anchor_fallback": used_anchor_fallback,
			"rmse": round(best_trip_summary["rmse"],3),
			"depart_deviation_min": round(best_trip_summary["depart_deviation_min"]),
			"arrival_deviation_min": round(best_trip_summary["arrival_deviation_min"]),
			"weighted_diff_duration": round(np.sqrt(best_trip_summary["weighted_sq_diff_duration"]),1),
			"weighted_diff_distance": round(np.sqrt(best_trip_summary["weighted_sq_diff_distance"]),3),
			"iteration_id": best_iteration
		}
		return best_trip_candidate, trip_summary

	return best_trip_candidate

def _build_reluctance_attempts(
		modes_list,
		transit_retry_enabled=True,
		transit_reluctance_sequence=(0.8, 0.75, 0.5),
		walk_retry_enabled=True,
		walk_reluctance_sequence=(2.5, 3, 4)
):
	"""
	Builds the ordered sequence of (transit_reluctances, walk_reluctance_override) attempts
	tried after the baseline query finds no candidate. transit_reluctances lowers the cost of
	specific transit modes, surfacing routes OTP's search ranked as too expensive to return.
	walk_reluctance_override raises the cost of walking, countering OTP preferring a longer
	walk to a "better" stop/route over the shorter walk to the one the respondent actually
	used (a behavior TU respondents often don't follow). Each element of the returned list is
	(transit_reluctances: dict | None, walk_reluctance: float | None); None means "use the
	caller's baseline value for this attempt".
	"""
	attempts = [(None, None)]

	if walk_retry_enabled:
		for walk_reluctance in walk_reluctance_sequence:
			attempts.append((None, walk_reluctance))

	if transit_retry_enabled:
		transit_modes = ["SUBWAY", "BUS", "RAIL", "S_TRAIN", "TRAM"]
		tu_transit_modes = [
			mode
			for mode in dict.fromkeys(modes_list)
			if mode in transit_modes
		]

		for reluctance in transit_reluctance_sequence:
			for mode in tu_transit_modes:
				attempts.append(({mode: reluctance}, None))

		for reluctance in transit_reluctance_sequence:
			for n_modes in range(2, len(tu_transit_modes) + 1):
				for modes in combinations(tu_transit_modes, n_modes):
					attempts.append(({mode: reluctance for mode in modes}, None))

	return attempts


def _align_and_filter_candidates(
		otp_candidates_df,
		tu_deltur_sub,
		tu_gtfs_station_df,
		route_names,
		modes_list,
		tur_id
):
	otp_candidates_df = add_tu_delturnr_to_otp_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		bike_stage_modes=(2, 8)
	)

	return filter_candidates_by_requirements(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		route_names=route_names,
		modes_list=modes_list,
		tur_id=tur_id
	)


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
		transit_reluctances=None,
		skip_alignment=False,
		origin_location_override=None,
		destination_location_override=None,
		depart_dt_str_override=None,
		depart_dt_override=None,
		access_mode_override=None,
		egress_mode_override=None
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
		request_timeout=request_timeout,
		origin_location_override=origin_location_override,
		destination_location_override=destination_location_override,
		depart_dt_str_override=depart_dt_str_override,
		depart_dt_override=depart_dt_override,
		access_mode_override=access_mode_override,
		egress_mode_override=egress_mode_override
	)

	if otp_candidates_df.empty:
		return otp_candidates_df, REASON_NO_OTP_CANDIDATES

	if skip_alignment:
		return otp_candidates_df, ""

	return _align_and_filter_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_names=route_names,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"]
	)


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
		print_query,
		transit_retry_enabled=True,
		transit_reluctance_sequence=(0.8, 0.75, 0.5),
		walk_retry_enabled=True,
		walk_reluctance_sequence=(2.5, 3, 4),
		skip_alignment=False,
		origin_location_override=None,
		destination_location_override=None,
		depart_dt_str_override=None,
		depart_dt_override=None,
		access_mode_override=None,
		egress_mode_override=None
):
	last_msg = ""

	reluctance_attempts = _build_reluctance_attempts(
		modes_list,
		transit_retry_enabled=transit_retry_enabled,
		transit_reluctance_sequence=transit_reluctance_sequence,
		walk_retry_enabled=walk_retry_enabled,
		walk_reluctance_sequence=walk_reluctance_sequence
	)

	for transit_reluctances, walk_reluctance_override in reluctance_attempts:
		if transit_reluctances:
			print(f"Retrying with transit reluctances: {transit_reluctances}")
		if walk_reluctance_override is not None:
			print(f"Retrying with walk_reluctance: {walk_reluctance_override}")

		otp_candidates_df, msg_filter = _load_add_and_filter_candidates(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			tu_gtfs_station_df=tu_gtfs_station_df,
			modes_json=modes_json,
			route_names=route_names,
			route_short_name_for_loading=route_short_name_for_loading,
			modes_list=modes_list,
			via_stopids=via_stopids,
			walk_reluctance=walk_reluctance if walk_reluctance_override is None else walk_reluctance_override,
			car_reluctance=car_reluctance,
			search_window=search_window,
			max_itinerary_candidates=max_itinerary_candidates,
			otp_url=otp_url,
			request_timeout=request_timeout,
			print_query=print_query,
			transit_reluctances=transit_reluctances,
			skip_alignment=skip_alignment,
			origin_location_override=origin_location_override,
			destination_location_override=destination_location_override,
			depart_dt_str_override=depart_dt_str_override,
			depart_dt_override=depart_dt_override,
			access_mode_override=access_mode_override,
			egress_mode_override=egress_mode_override
		)

		if not otp_candidates_df.empty:
			return otp_candidates_df, ""

		last_msg = msg_filter

	return pd.DataFrame(), last_msg


def _try_station_anchored_fallback(
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
		first_stop_id,
		last_stop_id,
		transit_retry_enabled=True,
		transit_reluctance_sequence=(0.5, 0.25, 0.1),
		walk_retry_enabled=True,
		walk_reluctance_sequence=(3, 4, 6),
		wait_min=0
):
	"""
	Fallback for TU trips where the normal full-route OTP search finds nothing, but the
	first and/or last S_TRAIN/RAIL/SUBWAY leg's station is known from tu_gtfs_station_df. OTP's
	own street-access routing sometimes prefers a different, nearby station instead of
	the one the respondent actually used; anchoring the query at the known station
	avoids that. The known-station leg is queried once as a street-only (walk/car) trip
	and stitched onto every transit candidate found from/to that station.

	Callers are expected to check whether first_stop_id/last_stop_id resolved (via
	find_known_anchor_stations) before calling this, so that trips with no anchor at all
	don't produce a fallback-attempt message.
	"""

	tu_deltur_sorted = tu_deltur_sub.sort_values("Delturnr")

	access_leg_df = None
	if first_stop_id:
		access_mode = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[0]), "WALK")
		access_leg_df = request_direct_leg(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			stop_id=first_stop_id,
			direct_mode=access_mode,
			side="access",
			depart_dt_str=tu_tur_row["depart_dt_str"],
			otp_url=otp_url,
			request_timeout=request_timeout,
			print_query=print_query
		)
		if access_leg_df.empty:
			return pd.DataFrame(), REASON_NO_DIRECT_ACCESS_ROUTE

	egress_leg_df = None
	if last_stop_id:
		egress_mode = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[-1]), "WALK")
		egress_leg_df = request_direct_leg(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			stop_id=last_stop_id,
			direct_mode=egress_mode,
			side="egress",
			depart_dt_str=tu_tur_row["depart_dt_str"],
			otp_url=otp_url,
			request_timeout=request_timeout,
			print_query=print_query
		)
		if egress_leg_df.empty:
			return pd.DataFrame(), REASON_NO_DIRECT_EGRESS_ROUTE

	if first_stop_id:
		access_duration_min = int(access_leg_df["duration_min"].sum())
		depart_dt_transit = tu_tur_row["depart_dt"] + pd.Timedelta(minutes=access_duration_min + wait_min)
	else:
		depart_dt_transit = tu_tur_row["depart_dt"]
	depart_dt_str_transit = depart_dt_transit.strftime("%Y-%m-%dT%H:%M:%S%z")

	via_stopids_filtered = via_stopids
	if via_stopids:
		exclude = {stop_id for stop_id in (first_stop_id, last_stop_id) if stop_id}
		via_stopids_filtered = [stop_id for stop_id in via_stopids if stop_id not in exclude] or None

	raw_transit_df, _ = _load_candidates_with_reluctance_retries(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		modes_json=modes_json,
		route_names=route_names,
		route_short_name_for_loading=route_short_name_for_loading,
		modes_list=modes_list,
		via_stopids=via_stopids_filtered,
		walk_reluctance=walk_reluctance,
		car_reluctance=car_reluctance,
		transit_retry_enabled=transit_retry_enabled,
		transit_reluctance_sequence=transit_reluctance_sequence,
		walk_retry_enabled=walk_retry_enabled,
		walk_reluctance_sequence=walk_reluctance_sequence,
		search_window=search_window,
		max_itinerary_candidates=max_itinerary_candidates,
		otp_url=otp_url,
		request_timeout=request_timeout,
		print_query=print_query,
		skip_alignment=True,
		origin_location_override={"stopLocation": {"stopLocationId": first_stop_id}} if first_stop_id else None,
		destination_location_override={"stopLocation": {"stopLocationId": last_stop_id}} if last_stop_id else None,
		depart_dt_str_override=depart_dt_str_transit,
		depart_dt_override=depart_dt_transit,
		access_mode_override="WALK" if first_stop_id else None,
		egress_mode_override="WALK" if last_stop_id else None
	)

	if raw_transit_df.empty:
		return pd.DataFrame(), f"anchored_{REASON_NO_OTP_CANDIDATES}"

	stitched_df = stitch_candidates(raw_transit_df, access_leg_df, egress_leg_df, wait_min)

	filtered_df, reason = _align_and_filter_candidates(
		otp_candidates_df=stitched_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_names=route_names,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"]
	)
	if filtered_df.empty:
		return filtered_df, f"anchored_{reason}"
	return filtered_df, reason
