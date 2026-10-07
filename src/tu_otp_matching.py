import pandas as pd
from .best_otp_candidate import find_best_match_by_rmse
from .candidate_search import (
	resolve_segment_search_params,
	load_candidates_with_reluctance_retries,
)
from .otp_utils import (
	has_invalid_route_name,
	resolve_route_short_names,
)
from .tu_utils import add_tu_deltur_depart_times, absorb_short_walk_into_car
from .constant import (
	DIRECT_ACCESS_MODE_MAP,
	REASON_NO_VALID_MODES,
	REASON_INVALID_ROUTE_NAME,
	REASON_DURATION_OUTSIDE_SEARCH_WINDOW,
	REASON_NO_BEST_MATCH,
	REASON_CAR_LEG_NOT_SATISFIED,
	ROUTE_RELATED_FAILURE_REASONS,
	ROUTE_MATCH_EXACT,
	ROUTE_MATCH_IGNORED,
	ROUTE_MATCH_LEVELS,
)
from .station_anchor_fallback import find_known_anchor_stations, compute_anchor_split_segments
from .station_anchor_search import try_station_anchored_fallback, try_split_station_anchored_fallback


def _match_once(
	tu_tur_row,
	tu_deltur,
	otp_mode_routes_cache,
	otp_route_name_index,
	tu_gtfs_station_df,
	anchor_station_lookup,
	settings,
	print_deviation_details= True,
	route_match=ROUTE_MATCH_EXACT,
	print_trip_header=True,
	debug_profile="LIST_ALL"
):
	"""
	Runs the full trip-matching search once, enforcing the TU-recorded BUS/S_TRAIN route name
	at the given route_match level (a ROUTE_MATCH_* constant; match_tu_trip_to_otp steps
	through them). Always returns (result_df_or_None, trip_summary_dict) - callers that
	don't want the summary strip it themselves.
	"""
	i_TurId = tu_tur_row["TurId"]
	used_anchor_fallback = False

	def _empty_trip_summary(last_print_if_not_found, failure_reason):
		return {
			"TurId": i_TurId,
			"SessionId": tu_tur_row.get("SessionId"),
			"trip_found": 0,
			"trip_wrong_route": 0,
			"trip_not_found": 1,
			"route_match": pd.NA,
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

	# After the header, so WALK_CAR_ABSORBED prints inside its own trip's block.
	tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
	tu_deltur_sub = absorb_short_walk_into_car(tu_deltur_sub, verbose=print_trip_header)
	tu_deltur_sub = add_tu_deltur_depart_times(tu_deltur_sub, tu_tur_row["depart_dt"])

	if print_trip_header:
		# Print for debugging
		tu_deltur_sub_print_col = ["Delturnr", "tu_deltur_depart_time", "StageMode", "StageLength", "StageWaitMin",
		                           "StageDurationMin", "Route", "FromStation", "ToStation"]
		print("tu_deltur_sub:")
		print(tu_deltur_sub[tu_deltur_sub_print_col].to_string(index=False, max_colwidth=None))

	modes_json, modes_list, is_bus_s_train, route_names, route_names_ext, route_short_name_for_loading, route_name_groups_for_search, via_stopids = resolve_segment_search_params(
		tu_deltur_sub, tu_gtfs_station_df, otp_mode_routes_cache, otp_route_name_index,
		route_match=route_match,
	)
	if print_trip_header:
		print(f"route_short_name: {', '.join(str(r) for r in route_names)}")
		print(f"route_names_ext: {', '.join(str(r) for r in route_names_ext)}")

	if not modes_json:
		return _return_not_found(REASON_NO_VALID_MODES)
	if print_trip_header:
		print(f"modes_json: {', '.join(m['mode'] for m in modes_json)}")
	if (
		route_match != ROUTE_MATCH_IGNORED
		and is_bus_s_train
		and has_invalid_route_name(route_names)
	):
		return _return_not_found(f"{REASON_INVALID_ROUTE_NAME} (routes={route_names})", REASON_INVALID_ROUTE_NAME)
	if print_trip_header and via_stopids:
		print(f"via_stopids: {', '.join('/'.join(stop_ids) for stop_ids in via_stopids)}")

	# is_bus_s_train/is_rail_tram_subway_ferry are the two TRANSIT_MODES categories with
	# different route-name handling (see resolve_segment_search_params); every TU trip must
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
		settings=settings,
		route_match=route_match,
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
		# unambiguous single-mode CAR direct leg (station_anchor_search.py) instead of relying on
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
			otp_candidates_df, msg_filter = try_station_anchored_fallback(
				**_candidate_kwargs,
				first_stop_id=first_stop_id,
				last_stop_id=last_stop_id,
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
			otp_candidates_df, msg_filter = load_candidates_with_reluctance_retries(**_candidate_kwargs)
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
		otp_candidates_df, msg_filter = load_candidates_with_reluctance_retries(**_candidate_kwargs)
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
		otp_candidates_df, fallback_msg = try_split_station_anchored_fallback(
			tu_tur_row=tu_tur_row,
			tu_deltur_sub=tu_deltur_sub,
			tu_gtfs_station_df=tu_gtfs_station_df,
			otp_mode_routes_cache=otp_mode_routes_cache,
			otp_route_name_index=otp_route_name_index,
			route_name_groups=route_name_groups_for_search,
			modes_list=modes_list,
			anchor_segments=anchor_segments,
			settings=settings,
			route_match=route_match,
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

	# Arrival check as OTP departure + TU trip duration, so a departure shift isn't counted twice.
	# Catches itineraries departing on time but arriving hours late, e.g. a long wait forced by via stops.
	tu_trip_duration = tu_tur_row["arrival_dt"] - tu_tur_row["depart_dt"]
	if pd.notna(tu_trip_duration):
		otp_trip_duration = (
			pd.to_datetime(otp_candidates_df["end_trip"], utc=True)
			- pd.to_datetime(otp_candidates_df["start_trip"], utc=True)
		)
		within = (otp_trip_duration - tu_trip_duration).abs() <= pd.Timedelta(settings.search_window)
		if not within.all():
			n_dropped = otp_candidates_df.loc[~within, "iteration_id"].nunique()
			print(f"Dropped {n_dropped} itinerary(ies) whose duration differs from TU's by more than the search window")
			otp_candidates_df = otp_candidates_df[within]
		if otp_candidates_df.empty:
			return _return_not_found(REASON_DURATION_OUTSIDE_SEARCH_WINDOW)

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
		"trip_found": 1 if route_match == ROUTE_MATCH_EXACT else 0,
		"trip_wrong_route": 0 if route_match == ROUTE_MATCH_EXACT else 1,
		"trip_not_found": 0,
		"route_match": route_match if is_bus_s_train else pd.NA,
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
	tu_gtfs_station_df,
	anchor_station_lookup,
	settings,
	print_deviation_details=True,
	return_trip_summary=False,
):
	"""
	Matches one TU trip to the best OTP itinerary. Runs _match_once enforcing the TU-recorded
	BUS/S_TRAIN route name as written; if that fails for a route-related reason (invalid route
	name, or the route restriction narrowing the search down to nothing - see
	ROUTE_RELATED_FAILURE_REASONS) and the trip actually has a BUS/S_TRAIN leg, retries at each
	looser ROUTE_MATCH_LEVELS level in turn (S-train sibling line, missing letter, one digit
	moved by 1, route name ignored) until one matches. A level whose route set is no wider than the previous one's is
	skipped, since it would repeat the same query. trip_summary["route_match"] records the level
	used. A trip only found at a widened level is reported as its own outcome -
	trip_summary["trip_wrong_route"] == 1 rather than trip_summary["trip_found"] == 1 - since its
	transit legs don't run the routes exactly as TU recorded them.
	"""
	i_TurId = tu_tur_row["TurId"]
	_match_kwargs = dict(
		tu_tur_row=tu_tur_row,
		tu_deltur=tu_deltur,
		otp_mode_routes_cache=otp_mode_routes_cache,
		otp_route_name_index=otp_route_name_index,
		tu_gtfs_station_df=tu_gtfs_station_df,
		anchor_station_lookup=anchor_station_lookup,
		settings=settings,
		print_deviation_details=print_deviation_details,
	)

	result_df, trip_summary = _match_once(route_match=ROUTE_MATCH_EXACT, **_match_kwargs)

	if result_df is None:
		tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
		is_bus_s_train = tu_deltur_sub["StageMode"].isin([31, 32]).any()
		if is_bus_s_train and trip_summary["failure_reason"] in ROUTE_RELATED_FAILURE_REASONS:
			previous_groups = resolve_route_short_names(
				tu_deltur_sub, otp_mode_routes_cache, otp_route_name_index, route_match=ROUTE_MATCH_EXACT
			)[4]
			for route_match in ROUTE_MATCH_LEVELS[1:]:
				if route_match != ROUTE_MATCH_IGNORED:
					groups = resolve_route_short_names(
						tu_deltur_sub, otp_mode_routes_cache, otp_route_name_index, route_match=route_match
					)[4]
					if groups == previous_groups:
						print(f"ROUTE_MATCH_RETRY_SKIPPED ({route_match}, no new routes): TurId={i_TurId}")
						continue
					previous_groups = groups
				print(f"ROUTE_MATCH_RETRY ({route_match}): TurId={i_TurId}")
				retry_df, retry_summary = _match_once(route_match=route_match, print_trip_header=False, **_match_kwargs)
				if retry_df is not None:
					print(f"ROUTE_MATCH_USED ({route_match}): TurId={i_TurId}")
					result_df, trip_summary = retry_df, retry_summary
					break

	if return_trip_summary:
		return result_df, trip_summary
	return result_df
