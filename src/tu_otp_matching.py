import pandas as pd
import numpy as np
from itertools import combinations
from .delturnr_otp_candidates import add_tu_delturnr_to_otp_candidates, summarize_alignment_diagnostics
from .best_otp_candidate import find_best_match_by_rmse
from .otp_client import load_all_candidates, request_direct_leg
from .otp_utils import (
	has_invalid_route_name,
	resolve_route_short_names,
	get_via_stops,
	filter_candidates_by_requirements,
)
from .tu_utils import add_tu_deltur_depart_times, absorb_short_walk_into_car
from .constant import (
	DIRECT_ACCESS_MODE_MAP,
	LOCAL_TIMEZONE,
	REASON_NO_VALID_MODES,
	REASON_INVALID_ROUTE_NAME,
	REASON_NO_OTP_CANDIDATES,
	REASON_NO_DIRECT_ACCESS_ROUTE,
	REASON_NO_DIRECT_EGRESS_ROUTE,
	REASON_NO_DIRECT_INTERIOR_ROUTE,
	REASON_NO_CONNECTING_SEGMENT,
	REASON_NO_BEST_MATCH,
	REASON_CAR_LEG_NOT_SATISFIED,
	REASON_NO_MATCHING_LEG_SEQUENCE,
	ROUTE_RELATED_FAILURE_REASONS,
)
from .station_anchor_fallback import (
	find_known_anchor_stations,
	stitch_candidates,
	compute_anchor_split_segments,
	stitch_segment_chains,
)


def _resolve_segment_search_params(
		tu_deltur_sub,
		tu_gtfs_station_df,
		otp_mode_routes_cache,
		otp_route_name_index,
		exclude_via_stopids=None,
		ignore_route_name=False,
):
	"""
	Derives one OTP search's route/mode/via-stop parameters from a tu_deltur_sub - shared by
	_match_once (the whole trip) and _load_and_align_segment_candidates (one anchor-split
	segment), so this resolution logic lives in exactly one place.

	exclude_via_stopids drops the given stop IDs from the resolved via_stopids - used by
	segment queries to exclude their own origin/destination boundary stops, which are already
	the query's origin/destination override rather than a via constraint.

	Returns (modes_json, modes_list, is_bus_s_train, route_names, route_names_ext,
	route_short_name_for_loading, route_name_groups_for_search, via_stopids).
	"""
	route_names, route_names_ext, modes_json, modes_list, route_name_groups = resolve_route_short_names(
		tu_deltur_sub, otp_mode_routes_cache, otp_route_name_index
	)

	is_bus_s_train = any(mode in ["BUS", "S_TRAIN"] for mode in modes_list) #only BUS and S_TRAIN have stated route names
	is_rail_tram_subway_ferry = any(mode in ["RAIL", "SUBWAY", "TRAM", "FERRY"] for mode in modes_list)

	if ignore_route_name:
		# Drop OTP's own routeShortNames include-filter entirely
		route_short_name_for_loading = None
		route_name_groups_for_search = []
	elif is_bus_s_train and is_rail_tram_subway_ferry:
		route_short_name_for_loading = route_names_ext
		route_name_groups_for_search = route_name_groups
	elif is_bus_s_train:
		route_short_name_for_loading = route_names
		route_name_groups_for_search = route_name_groups
	else:
		route_short_name_for_loading = None
		route_name_groups_for_search = route_name_groups

	if (tu_deltur_sub["StageMode"].isin([32, 33, 34])).any(): #Station only stated for RAIL, S_TRAIN and SUBWAY
		via_stopids = get_via_stops(tu_deltur_sub=tu_deltur_sub, tu_gtfs_station_df=tu_gtfs_station_df)
	else:
		via_stopids = None
	if via_stopids and exclude_via_stopids:
		via_stopids = [stop_id for stop_id in via_stopids if stop_id not in exclude_via_stopids] or None

	return (
		modes_json, modes_list, is_bus_s_train, route_names, route_names_ext,
		route_short_name_for_loading, route_name_groups_for_search, via_stopids,
	)


def _match_once(
	tu_tur_row,
	tu_deltur,
	otp_mode_routes_cache,
	otp_route_name_index,
	otp_url,
	search_window,
	max_itinerary_candidates,
	tu_gtfs_station_df,
	anchor_station_lookup,
	walk_reluctance=2,
	car_reluctance=2,
	transit_retry_enabled=True,
	transit_reluctance_sequence=(0.5, 0.25, 0.1),
	walk_retry_enabled=True,
	walk_reluctance_sequence=(3, 4, 6),
	print_deviation_details= True,
	request_timeout=60,
	print_query=False,
	station_anchor_wait_min=0,
	ignore_route_name=False,
	print_trip_header=True,
	debug_profile="LIST_ALL"
):
	"""
	Runs the full trip-matching search once, either enforcing the TU-recorded BUS/S_TRAIN
	route name (ignore_route_name=False) or ignoring it entirely (ignore_route_name=True, used
	by match_tu_trip_to_otp's route-name-ignored retry). Always returns (result_df_or_None,
	trip_summary_dict) - callers that don't want the summary strip it themselves.
	"""
	i_TurId = tu_tur_row["TurId"]
	tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
	tu_deltur_sub = absorb_short_walk_into_car(tu_deltur_sub)
	tu_deltur_sub = add_tu_deltur_depart_times(tu_deltur_sub, tu_tur_row["depart_dt"])
	used_anchor_fallback = False

	def _empty_trip_summary(last_print_if_not_found, failure_reason):
		return {
			"TurId": i_TurId,
			"SessionId": tu_tur_row.get("SessionId"),
			"trip_found": 0,
			"trip_wrong_route": 0,
			"trip_not_found": 1,
			"last_print_if_not_found": last_print_if_not_found,
			"failure_reason": failure_reason,
			"used_anchor_fallback": used_anchor_fallback,
			"rmse": pd.NA,
			"depart_deviation_min": pd.NA,
			"arrival_deviation_min": pd.NA,
			"deviation_duration_min": pd.NA,
			"deviation_distance_km": pd.NA,
			"iteration_id": pd.NA
		}

	def _return_not_found(last_print_if_not_found, failure_reason=None):
		print(last_print_if_not_found)
		return None, _empty_trip_summary(last_print_if_not_found, failure_reason or last_print_if_not_found)

	if print_trip_header:
		print("\n\n____________________________________________________________________________________________")
		print(f"TurId: {i_TurId}. With SessionId: {tu_tur_row['SessionId']}.")
		print(f"Tur coordinates origin (lat lon) :     {round(tu_tur_row['orig_lat'],5)} {round(tu_tur_row['orig_lon'],5)}")
		print(f"Tur coordinates destination (lat lon): {round(tu_tur_row['tiladrlat'],5)} {round(tu_tur_row['tiladrlon'],5)}")
		print(f"Depart: {tu_tur_row['depart_dt_str']}. Arrival: {tu_tur_row['arrival_dt_str']}.")
		# Print for debugging
		tu_deltur_sub_print_col = ["Delturnr", "tu_deltur_depart_time", "StageMode", "StageLength", "StageWaitMin",
		                           "StageDurationMin", "Route", "FromStation", "ToStation"]
		print("tu_deltur_sub:")
		print(tu_deltur_sub[tu_deltur_sub_print_col].to_string(index=False, max_colwidth=None))

	modes_json, modes_list, is_bus_s_train, route_names, route_names_ext, route_short_name_for_loading, route_name_groups_for_search, via_stopids = _resolve_segment_search_params(
		tu_deltur_sub, tu_gtfs_station_df, otp_mode_routes_cache, otp_route_name_index,
		ignore_route_name=ignore_route_name,
	)
	if print_trip_header:
		print(f"route_short_name: {', '.join(str(r) for r in route_names)}")
		print(f"route_names_ext: {', '.join(str(r) for r in route_names_ext)}")

	if not modes_json:
		return _return_not_found(REASON_NO_VALID_MODES)
	if print_trip_header:
		print(f"modes_json: {', '.join(m['mode'] for m in modes_json)}")
	if (
		not ignore_route_name
		and is_bus_s_train
		and has_invalid_route_name(route_names)
	):
		return _return_not_found(f"{REASON_INVALID_ROUTE_NAME} (routes={route_names})", REASON_INVALID_ROUTE_NAME)
	if print_trip_header and via_stopids:
		print(f"via_stopids: {', '.join(via_stopids)}")

	# is_bus_s_train/is_rail_tram_subway_ferry are the two TRANSIT_MODES categories with
	# different route-name handling (see _resolve_segment_search_params); every TU trip must
	# contain at least one, so this is a defensive check against malformed input data.
	is_rail_tram_subway_ferry = any(mode in ["RAIL", "SUBWAY", "TRAM", "FERRY"] for mode in modes_list)
	if not is_bus_s_train and not is_rail_tram_subway_ferry:
		raise ValueError("No valid transit modes found. TurId: ", i_TurId, ".")

	tu_deltur_sorted = tu_deltur_sub.sort_values("Delturnr")
	is_car_access = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[0]), "WALK") == "CAR"
	is_car_egress = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[-1]), "WALK") == "CAR"

	_candidate_kwargs = dict(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		modes_json=modes_json,
		route_name_groups=route_name_groups_for_search,
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
		print_query=print_query,
		ignore_route_name=ignore_route_name,
		debug_profile=debug_profile,
	)

	otp_candidates_df = pd.DataFrame()
	msg_filter = ""

	def _log_stage(stage, status, detail=""):
		suffix = f" ({detail})" if detail else ""
		print(f"[TurId={i_TurId}] {stage}: {status}{suffix}")

	if is_car_access or is_car_egress:
		# CAR access/egress has no reliable direct request to OTP (see otp_client._get_access_egress):
		# access bundles ["WALK", "CAR_DROP_OFF"] and OTP is free to silently return WALK instead. So
		# CAR trips are anchored at a known rail/S-train/subway station and stitched with an
		# unambiguous single-mode CAR direct leg (station_anchor_fallback.py) instead of relying on
		# that bundled request.
		first_stop_id, last_stop_id = find_known_anchor_stations(tu_deltur_sub, anchor_station_lookup)
		anchor_usable = (
			(first_stop_id or not is_car_access)
			and (last_stop_id or not is_car_egress)
			and (first_stop_id or last_stop_id)
		)
		if anchor_usable:
			_log_stage(
				"STATION_ANCHOR_FALLBACK (car access/egress)", "START",
				f"first_stop_id={first_stop_id} last_stop_id={last_stop_id}"
			)
			otp_candidates_df, msg_filter = _try_station_anchored_fallback(
				**_candidate_kwargs,
				first_stop_id=first_stop_id,
				last_stop_id=last_stop_id,
				wait_min=station_anchor_wait_min
			)
			if not otp_candidates_df.empty:
				used_anchor_fallback = True
				_log_stage("STATION_ANCHOR_FALLBACK (car access/egress)", "SUCCEEDED")
			else:
				_log_stage("STATION_ANCHOR_FALLBACK (car access/egress)", "FAILED", msg_filter)
		else:
			_log_stage(
				"STATION_ANCHOR_FALLBACK (car access/egress)", "SKIPPED",
				"no usable anchor station for the required CAR side"
			)

		if otp_candidates_df.empty:
			# No usable anchor station for the required CAR side (e.g. CAR combined with a
			# bus-only trip, which has no named stations in TU) — fall back to the normal
			# full-route query, then verify below that the CAR side actually came back as CAR,
			# since OTP may have silently substituted WALK.
			_log_stage("FULL_ROUTE_QUERY (car access/egress fallback)", "START")
			otp_candidates_df, msg_filter = _load_candidates_with_reluctance_retries(**_candidate_kwargs)
			if otp_candidates_df.empty:
				_log_stage("FULL_ROUTE_QUERY (car access/egress fallback)", "FAILED", msg_filter)
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
				_log_stage(
					"FULL_ROUTE_QUERY (car access/egress fallback)", "FAILED",
					"OTP returned WALK instead of CAR on the required leg"
				)
				return _return_not_found(fail_msg, REASON_CAR_LEG_NOT_SATISFIED)
			_log_stage("FULL_ROUTE_QUERY (car access/egress fallback)", "SUCCEEDED")
	else:
		_log_stage("FULL_ROUTE_QUERY", "START")
		otp_candidates_df, msg_filter = _load_candidates_with_reluctance_retries(**_candidate_kwargs)
		_log_stage("FULL_ROUTE_QUERY", "SUCCEEDED" if not otp_candidates_df.empty else "FAILED", msg_filter)

	if otp_candidates_df.empty:
		anchor_segments = compute_anchor_split_segments(tu_deltur_sub, anchor_station_lookup)
		if not anchor_segments:
			_log_stage("STATION_ANCHOR_FALLBACK", "SKIPPED", "no usable anchor station")
			return _return_not_found(msg_filter)

		_log_stage(
			"STATION_ANCHOR_FALLBACK", "START",
			f"{len(anchor_segments)} segment(s): " + " | ".join(
				f"{seg['kind']}[{seg['origin_stop_id'] or 'origin'} -> {seg['destination_stop_id'] or 'dest'}]"
				for seg in anchor_segments
			)
		)
		otp_candidates_df, fallback_msg = _try_split_station_anchored_fallback(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			tu_gtfs_station_df=tu_gtfs_station_df,
			otp_mode_routes_cache=otp_mode_routes_cache,
			otp_route_name_index=otp_route_name_index,
			route_name_groups=route_name_groups_for_search,
			modes_list=modes_list,
			anchor_segments=anchor_segments,
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
			wait_min=station_anchor_wait_min,
			ignore_route_name=ignore_route_name,
		)
		if otp_candidates_df.empty:
			_log_stage("STATION_ANCHOR_FALLBACK", "FAILED", fallback_msg)
			return _return_not_found(f"{msg_filter}; fallback={fallback_msg}", fallback_msg)

		used_anchor_fallback = True
		_log_stage("STATION_ANCHOR_FALLBACK", "SUCCEEDED")

	# calculate waiting time
	otp_candidates_df = otp_candidates_df.sort_values(
		["iteration_id", "start_leg"]
	).reset_index(drop=True)

	otp_candidates_df["waitingtime"] = (
			(otp_candidates_df["start_leg"] - otp_candidates_df.groupby("iteration_id")["end_leg"].shift()) / 60 / 1000)
	otp_candidates_df["waitingtime"] = otp_candidates_df["waitingtime"].fillna(0)

	otp_candidates_df["otp_leg_depart_time"] = (
		pd.to_datetime(otp_candidates_df["start_leg"], unit="ms", utc=True)
		.dt.tz_convert("Europe/Copenhagen")
		.dt.strftime("%H:%M")
	)

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
		detail_cols = ["iteration_id", "depart_deviation_min", "arrival_deviation_min",
		               "deviation_duration_min", "deviation_distance_km", "rmse"]
		print(trips_display[detail_cols].sort_values("rmse").head(10).to_string(index=False))

	# Filter otp_candidates_df to get only the best trip
	best_trip_candidate = otp_candidates_df[otp_candidates_df["iteration_id"] == best_iteration].copy()
	best_trip_candidate["TurId"] = i_TurId
	best_trip_candidate["tu_deltur_depart_time"] = best_trip_candidate["tu_Delturnr"].map(
		tu_deltur_sub.set_index("Delturnr")["tu_deltur_depart_time"]
	)

	trip_summary = {
		"TurId": i_TurId,
		"SessionId": tu_tur_row.get("SessionId"),
		"trip_found": 0 if ignore_route_name else 1,
		"trip_wrong_route": 1 if ignore_route_name else 0,
		"trip_not_found": 0,
		"last_print_if_not_found": "",
		"used_anchor_fallback": used_anchor_fallback,
		"rmse": round(best_trip_summary["rmse"],3),
		"depart_deviation_min": round(best_trip_summary["depart_deviation_min"],0),
		"arrival_deviation_min": round(best_trip_summary["arrival_deviation_min"],0),
		"deviation_duration_min": round(best_trip_summary["deviation_duration_min"],0),
		"deviation_distance_km": round(best_trip_summary["deviation_distance_km"],2),
		"iteration_id": best_iteration
	}
	return best_trip_candidate, trip_summary


def match_tu_trip_to_otp(
	tu_tur_row,
	tu_deltur,
	otp_mode_routes_cache,
	otp_route_name_index,
	otp_url,
	search_window,
	max_itinerary_candidates,
	tu_gtfs_station_df,
	anchor_station_lookup,
	walk_reluctance=2,
	car_reluctance=2,
	transit_retry_enabled=True,
	transit_reluctance_sequence=(0.5, 0.25, 0.1),
	walk_retry_enabled=True,
	walk_reluctance_sequence=(3, 4, 6),
	print_deviation_details=True,
	return_trip_summary=False,
	request_timeout=60,
	print_query=False,
	station_anchor_wait_min=0
):
	"""
	Matches one TU trip to the best OTP itinerary. Runs _match_once enforcing the TU-recorded
	BUS/S_TRAIN route name; if that fails for a route-related reason (invalid route name, or
	the route restriction narrowing the search down to nothing - see
	ROUTE_RELATED_FAILURE_REASONS) and the trip actually has a BUS/S_TRAIN leg, retries once
	more with the route name ignored (matching by mode + leg order instead). A trip only found
	this way is reported as its own outcome - trip_summary["trip_wrong_route"] == 1 rather than
	trip_summary["trip_found"] == 1 - since its transit legs aren't guaranteed to run the routes
	TU recorded.
	"""
	i_TurId = tu_tur_row["TurId"]
	_match_kwargs = dict(
		tu_tur_row=tu_tur_row,
		tu_deltur=tu_deltur,
		otp_mode_routes_cache=otp_mode_routes_cache,
		otp_route_name_index=otp_route_name_index,
		otp_url=otp_url,
		search_window=search_window,
		max_itinerary_candidates=max_itinerary_candidates,
		tu_gtfs_station_df=tu_gtfs_station_df,
		anchor_station_lookup=anchor_station_lookup,
		walk_reluctance=walk_reluctance,
		car_reluctance=car_reluctance,
		transit_retry_enabled=transit_retry_enabled,
		transit_reluctance_sequence=transit_reluctance_sequence,
		walk_retry_enabled=walk_retry_enabled,
		walk_reluctance_sequence=walk_reluctance_sequence,
		print_deviation_details=print_deviation_details,
		request_timeout=request_timeout,
		print_query=print_query,
		station_anchor_wait_min=station_anchor_wait_min
	)

	result_df, trip_summary = _match_once(ignore_route_name=False, **_match_kwargs)

	if result_df is None:
		tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
		is_bus_s_train = tu_deltur_sub["StageMode"].isin([31, 32]).any()
		if is_bus_s_train and trip_summary["failure_reason"] in ROUTE_RELATED_FAILURE_REASONS:
			print(f"ROUTE_NAME_IGNORED_RETRY: TurId={i_TurId}")
			retry_df, retry_summary = _match_once(ignore_route_name=True, print_trip_header=False, **_match_kwargs)
			if retry_df is not None:
				print(f"ROUTE_NAME_IGNORED_MATCH_USED: TurId={i_TurId}")
				result_df, trip_summary = retry_df, retry_summary

	if return_trip_summary:
		return result_df, trip_summary
	return result_df


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
		route_name_groups,
		modes_list,
		tur_id,
		ignore_route_name=False
):
	alignment_diagnostics = []
	otp_candidates_df = add_tu_delturnr_to_otp_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		bike_stage_modes=(2, 8),
		diagnostics=alignment_diagnostics,
		ignore_route_name=ignore_route_name
	)

	filtered_df, reason = filter_candidates_by_requirements(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		route_name_groups=route_name_groups,
		modes_list=modes_list,
		tur_id=tur_id
	)

	# no_matching_leg_sequence means the route and mode filters both passed, i.e. OTP
	# returned itineraries with the modes and routes TU recorded and alignment still
	# rejected every one of them. On its own that reason cannot distinguish a bad
	# station mapping from a genuinely absent leg, so spell out where alignment stopped.
	if reason == REASON_NO_MATCHING_LEG_SEQUENCE:
		summary = summarize_alignment_diagnostics(alignment_diagnostics)
		if summary:
			print(summary)

	return filtered_df, reason


def _load_add_and_filter_candidates(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		modes_json,
		route_name_groups,
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
		egress_mode_override=None,
		ignore_route_name=False,
		debug_profile="LIST_ALL",
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
		egress_mode_override=egress_mode_override,
		debug_profile=debug_profile,
	)

	if otp_candidates_df.empty:
		return otp_candidates_df, REASON_NO_OTP_CANDIDATES

	if skip_alignment:
		return otp_candidates_df, ""

	return _align_and_filter_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"],
		ignore_route_name=ignore_route_name
	)


def _load_candidates_with_reluctance_retries(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		modes_json,
		route_name_groups,
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
		egress_mode_override=None,
		ignore_route_name=False,
		debug_profile="LIST_ALL"
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
			route_name_groups=route_name_groups,
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
			egress_mode_override=egress_mode_override,
			ignore_route_name=ignore_route_name,
			debug_profile=debug_profile,
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
		route_name_groups,
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
		wait_min=0,
		ignore_route_name=False,
		debug_profile="LIST_ALL"
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
			direct_mode=access_mode,
			depart_dt_str=tu_tur_row["depart_dt_str"],
			otp_url=otp_url,
			request_timeout=request_timeout,
			print_query=print_query,
			destination_stop_id=first_stop_id
		)
		if access_leg_df.empty:
			return pd.DataFrame(), REASON_NO_DIRECT_ACCESS_ROUTE

	egress_leg_df = None
	if last_stop_id:
		egress_mode = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[-1]), "WALK")
		egress_leg_df = request_direct_leg(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			direct_mode=egress_mode,
			depart_dt_str=tu_tur_row["depart_dt_str"],
			otp_url=otp_url,
			request_timeout=request_timeout,
			print_query=print_query,
			origin_stop_id=last_stop_id
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
		route_name_groups=route_name_groups,
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
		egress_mode_override="WALK" if last_stop_id else None,
		ignore_route_name=ignore_route_name,
		debug_profile=debug_profile
	)

	if raw_transit_df.empty:
		return pd.DataFrame(), f"anchored_{REASON_NO_OTP_CANDIDATES}"

	stitched_df = stitch_candidates(raw_transit_df, access_leg_df, egress_leg_df, wait_min)

	filtered_df, reason = _align_and_filter_candidates(
		otp_candidates_df=stitched_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"],
		ignore_route_name=ignore_route_name
	)
	if filtered_df.empty:
		return filtered_df, f"anchored_{reason}"
	return filtered_df, reason


def _query_direct_segment(tu_tur_row, segment, otp_url, request_timeout, print_query):
	"""
	Query a "direct" (pure WALK/CAR, no transit leg) anchor-split segment once, using the TU
	trip's own depart time as a placeholder - street-mode travel time in OTP doesn't depend
	on time of day, so only the duration is reused (see stitch_segment_chains, which computes
	the segment's actual placement in each stitched itinerary relative to its transit
	neighbor).
	"""
	legs = segment["legs"].sort_values("Delturnr")
	direct_mode = DIRECT_ACCESS_MODE_MAP.get(int(legs["StageMode"].iloc[0]), "WALK")
	return request_direct_leg(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=legs,
		direct_mode=direct_mode,
		depart_dt_str=tu_tur_row["depart_dt_str"],
		otp_url=otp_url,
		request_timeout=request_timeout,
		print_query=print_query,
		origin_stop_id=segment["origin_stop_id"],
		destination_stop_id=segment["destination_stop_id"],
	)


def _load_and_align_segment_candidates(
		tu_tur_row,
		segment_legs,
		tu_gtfs_station_df,
		otp_mode_routes_cache,
		otp_route_name_index,
		origin_stop_id,
		destination_stop_id,
		depart_dt,
		walk_reluctance,
		car_reluctance,
		transit_retry_enabled,
		transit_reluctance_sequence,
		walk_retry_enabled,
		walk_reluctance_sequence,
		search_window,
		max_itinerary_candidates,
		otp_url,
		request_timeout,
		print_query,
		ignore_route_name=False,
		debug_profile="LIST_ALL",
):
	"""
	Runs one anchor-split "transit" segment's own OTP search plus leg alignment, mirroring
	what _match_once does for the whole trip (route names/modes, via-stops, reluctance
	retries, alignment/filtering) but scoped to segment_legs only. Used by
	_try_split_station_anchored_fallback for every segment containing at least one transit
	leg - a segment that's pure WALK/CAR end-to-end uses _query_direct_segment instead.
	"""
	exclude_via_stopids = {stop_id for stop_id in (origin_stop_id, destination_stop_id) if stop_id}
	modes_json, modes_list, is_bus_s_train, route_names, route_names_ext, route_short_name_for_loading, route_name_groups_for_search, via_stopids = _resolve_segment_search_params(
		segment_legs, tu_gtfs_station_df, otp_mode_routes_cache, otp_route_name_index,
		exclude_via_stopids=exclude_via_stopids,
		ignore_route_name=ignore_route_name,
	)
	if not modes_json:
		return pd.DataFrame(), REASON_NO_VALID_MODES

	depart_dt_str = depart_dt.strftime("%Y-%m-%dT%H:%M:%S%z")

	raw_df, msg = _load_candidates_with_reluctance_retries(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=segment_legs,
		tu_gtfs_station_df=tu_gtfs_station_df,
		modes_json=modes_json,
		route_name_groups=route_name_groups_for_search,
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
		print_query=print_query,
		skip_alignment=True,
		origin_location_override={"stopLocation": {"stopLocationId": origin_stop_id}} if origin_stop_id else None,
		destination_location_override={"stopLocation": {"stopLocationId": destination_stop_id}} if destination_stop_id else None,
		depart_dt_str_override=depart_dt_str,
		depart_dt_override=depart_dt,
		access_mode_override="WALK" if origin_stop_id else None,
		egress_mode_override="WALK" if destination_stop_id else None,
		ignore_route_name=ignore_route_name,
		debug_profile=debug_profile,
	)
	if raw_df.empty:
		return pd.DataFrame(), msg or REASON_NO_OTP_CANDIDATES

	filtered_df, reason = _align_and_filter_candidates(
		otp_candidates_df=raw_df,
		tu_deltur_sub=segment_legs,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups_for_search,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"],
		ignore_route_name=ignore_route_name,
	)
	if filtered_df.empty:
		return filtered_df, reason

	#Return the surviving itineraries' RAW legs, not the aligned ones: alignment rewrites a
	#TU bike leg matched to an OTP WALK leg (mode -> "BICYCLE", duration_min scaled by
	#WALK_BIKE_TIME_RATIO - see add_tu_delturnr_to_otp_candidates), so aligning the stitched
	#trip a second time in _try_split_station_anchored_fallback would no longer recognise
	#those legs and the whole itinerary would fail the leg-sequence check. Segment alignment
	#is only used to decide which itineraries survive; the stitched trip is then aligned
	#exactly once, like the single-query path. filter_candidates_by_requirements only ever
	#drops whole iteration_ids, never individual legs, so this loses nothing.
	surviving_ids = filtered_df["iteration_id"].unique()
	return raw_df[raw_df["iteration_id"].isin(surviving_ids)].reset_index(drop=True), ""


def _try_split_station_anchored_fallback(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		otp_mode_routes_cache,
		otp_route_name_index,
		route_name_groups,
		modes_list,
		anchor_segments,
		walk_reluctance,
		car_reluctance,
		search_window,
		max_itinerary_candidates,
		otp_url,
		request_timeout,
		print_query,
		transit_retry_enabled=False,
		transit_reluctance_sequence=(0.5, 0.25, 0.1),
		walk_retry_enabled=False,
		walk_reluctance_sequence=(3, 4, 6),
		wait_min=0,
		ignore_route_name=False,
):
	"""
	Generalized station-anchored fallback: splits the TU trip at every resolvable
	S_TRAIN/RAIL/SUBWAY station (anchor_segments, from compute_anchor_split_segments) rather
	than only the trip's outer first/last leg, queries each segment independently, and
	stitches every surviving end-to-end chain back into one candidate itinerary.

	Segments are resolved strictly left to right. A "direct" segment is queried once (its
	travel time doesn't depend on time of day) and cached in direct_leg_cache. A "transit"
	segment is queried once per currently surviving chain, seeded at that chain's running
	depart_dt (the TU trip's own depart time, advanced past every earlier segment's duration/
	arrival); every surviving OTP itinerary for that segment spawns its own continuation
	chain - no collapsing to "best of segment N", since the true best whole-trip match isn't
	necessarily the one built from each segment's individually-best candidate.
	"""
	tur_id = tu_tur_row["TurId"]
	wait_delta = pd.Timedelta(minutes=wait_min)

	def _log(stage, status, detail=""):
		suffix = f" ({detail})" if detail else ""
		print(f"[TurId={tur_id}] SPLIT_ANCHOR_FALLBACK/{stage}: {status}{suffix}")

	direct_leg_cache = {}
	for idx, seg in enumerate(anchor_segments):
		if seg["kind"] != "direct":
			continue
		leg_df = _query_direct_segment(tu_tur_row, seg, otp_url, request_timeout, print_query)
		if leg_df.empty:
			if idx == 0:
				reason = REASON_NO_DIRECT_ACCESS_ROUTE
			elif idx == len(anchor_segments) - 1:
				reason = REASON_NO_DIRECT_EGRESS_ROUTE
			else:
				reason = REASON_NO_DIRECT_INTERIOR_ROUTE
			_log(f"segment {idx} (direct)", "FAILED", reason)
			return pd.DataFrame(), f"anchored_{reason}"
		direct_leg_cache[idx] = leg_df
		_log(f"segment {idx} (direct)", "SUCCEEDED", f"duration_min={int(leg_df['duration_min'].sum())}")

	chains = [{"depart_dt": tu_tur_row["depart_dt"], "parts": []}]
	last_reason = REASON_NO_OTP_CANDIDATES

	for idx, seg in enumerate(anchor_segments):
		if seg["kind"] == "direct":
			duration_min = int(direct_leg_cache[idx]["duration_min"].sum())
			for chain in chains:
				chain["depart_dt"] = chain["depart_dt"] + pd.Timedelta(minutes=duration_min) + wait_delta
				chain["parts"].append(("direct", idx, None))
			continue

		new_chains = []
		for chain in chains:
			#load_all_candidates paginates backward as well as forward, so it returns
			#itineraries departing up to search_window *before* the requested time. Before this
			#chain's first transit segment that's fine (a leading direct segment is re-timed
			#against it in stitch_segment_chains, exactly like the single-anchor fallback does),
			#but once a transit segment is fixed in time, a later segment departing before
			#chain["depart_dt"] would mean leaving the boundary station before the previous
			#segment arrived there.
			has_fixed_predecessor = any(kind == "transit" for kind, _, _ in chain["parts"])
			segment_df, reason = _load_and_align_segment_candidates(
				tu_tur_row=tu_tur_row,
				segment_legs=seg["legs"],
				tu_gtfs_station_df=tu_gtfs_station_df,
				otp_mode_routes_cache=otp_mode_routes_cache,
				otp_route_name_index=otp_route_name_index,
				origin_stop_id=seg["origin_stop_id"],
				destination_stop_id=seg["destination_stop_id"],
				depart_dt=chain["depart_dt"],
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
				ignore_route_name=ignore_route_name,
				debug_profile = "OFF"
			)
			if segment_df.empty:
				last_reason = reason
				continue
			for _, group in segment_df.groupby("iteration_id"):
				if has_fixed_predecessor:
					departure = pd.to_datetime(group["start_leg"].min(), unit="ms", utc=True)
					if departure < chain["depart_dt"]:
						last_reason = REASON_NO_CONNECTING_SEGMENT
						continue
				arrival = pd.to_datetime(group["end_leg"].max(), unit="ms", utc=True).tz_convert(LOCAL_TIMEZONE)
				new_chains.append({
					"depart_dt": arrival + wait_delta,
					"parts": chain["parts"] + [("transit", idx, group)],
				})

		_log(
			f"segment {idx} (transit)", "SUCCEEDED" if new_chains else "FAILED",
			f"{len(new_chains)} surviving chain(s) from {len(chains)} seed(s)" if new_chains else last_reason
		)
		chains = new_chains
		if not chains:
			return pd.DataFrame(), f"anchored_{last_reason}"

	stitched_df = stitch_segment_chains(chains, direct_leg_cache, wait_min)

	filtered_df, reason = _align_and_filter_candidates(
		otp_candidates_df=stitched_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups,
		modes_list=modes_list,
		tur_id=tur_id,
		ignore_route_name=ignore_route_name,
	)
	if filtered_df.empty:
		return filtered_df, f"anchored_{reason}"
	return filtered_df, ""
