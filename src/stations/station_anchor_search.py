import time
import pandas as pd
from src.matching.candidate_search import (
	resolve_segment_search_params,
	align_and_filter_candidates,
	load_candidates_with_reluctance_retries,
	tu_endpoints,
)
from src import otp
from src.matching.route_matching_utils import drop_via_stations
from src.constant import (
	DIRECT_ACCESS_MODE_MAP,
	LOCAL_TIMEZONE,
	REASON_NO_VALID_MODES,
	REASON_NO_OTP_CANDIDATES,
	REASON_NO_DIRECT_ACCESS_ROUTE,
	REASON_NO_DIRECT_EGRESS_ROUTE,
	REASON_NO_DIRECT_INTERIOR_ROUTE,
	REASON_NO_CONNECTING_SEGMENT,
	REASON_ANCHOR_TIMEOUT,
	ANCHOR_TIMEOUT_MIN,
	ROUTE_MATCH_EXACT,
)
from .station_anchor_fallback import stitch_candidates, stitch_segment_chains


def _fastest_itinerary(direct_df):
	"""OTP may return more than one direct itinerary (e.g. walking a bike); keep the fastest."""
	if direct_df.empty:
		return direct_df
	fastest_iteration = direct_df.groupby("iteration_id")["duration_min"].sum().idxmin()
	return direct_df[direct_df["iteration_id"] == fastest_iteration].reset_index(drop=True)


def try_station_anchored_fallback(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		modes_json,
		route_name_groups,
		route_short_name_for_loading,
		modes_list,
		via_stopids,
		settings,
		first_stop_id,
		last_stop_id,
		route_match=ROUTE_MATCH_EXACT,
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
		origin, destination = tu_endpoints(tu_tur_row, destination_stop_id=first_stop_id)
		access_leg_df = _fastest_itinerary(otp.client.request_direct_leg(
			origin=origin,
			destination=destination,
			direct_mode=access_mode,
			depart_dt=tu_tur_row["depart_dt"],
			otp_url=settings.otp_url,
			request_timeout=settings.request_timeout,
			print_query=settings.print_query,
		))
		if access_leg_df.empty:
			return pd.DataFrame(), REASON_NO_DIRECT_ACCESS_ROUTE

	egress_leg_df = None
	if last_stop_id:
		egress_mode = DIRECT_ACCESS_MODE_MAP.get(int(tu_deltur_sorted["StageMode"].iloc[-1]), "WALK")
		origin, destination = tu_endpoints(tu_tur_row, origin_stop_id=last_stop_id)
		egress_leg_df = _fastest_itinerary(otp.client.request_direct_leg(
			origin=origin,
			destination=destination,
			direct_mode=egress_mode,
			depart_dt=tu_tur_row["depart_dt"],
			otp_url=settings.otp_url,
			request_timeout=settings.request_timeout,
			print_query=settings.print_query,
		))
		if egress_leg_df.empty:
			return pd.DataFrame(), REASON_NO_DIRECT_EGRESS_ROUTE

	if first_stop_id:
		access_duration_min = int(access_leg_df["duration_min"].sum())
		depart_dt_transit = tu_tur_row["depart_dt"] + pd.Timedelta(minutes=access_duration_min + settings.station_anchor_wait_min)
	else:
		depart_dt_transit = tu_tur_row["depart_dt"]

	via_stopids_filtered = drop_via_stations(via_stopids, (first_stop_id, last_stop_id))

	raw_transit_df, _ = load_candidates_with_reluctance_retries(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		modes_json=modes_json,
		route_name_groups=route_name_groups,
		route_short_name_for_loading=route_short_name_for_loading,
		modes_list=modes_list,
		via_stopids=via_stopids_filtered,
		settings=settings,
		skip_alignment=True,
		origin_stop_id=first_stop_id,
		destination_stop_id=last_stop_id,
		depart_dt=depart_dt_transit,
		route_match=route_match,
		debug_profile=debug_profile
	)

	if raw_transit_df.empty:
		return pd.DataFrame(), f"anchored_{REASON_NO_OTP_CANDIDATES}"

	stitched_df = stitch_candidates(raw_transit_df, access_leg_df, egress_leg_df, settings.station_anchor_wait_min)

	filtered_df, reason = align_and_filter_candidates(
		otp_candidates_df=stitched_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"],
		route_match=route_match
	)
	if filtered_df.empty:
		return filtered_df, f"anchored_{reason}"
	return filtered_df, reason


def _query_direct_segment(tu_tur_row, segment, settings):
	"""
	Query a "direct" (pure WALK/CAR, no transit leg) anchor-split segment once, using the TU
	trip's own depart time as a placeholder - street-mode travel time in OTP doesn't depend
	on time of day, so only the duration is reused (see stitch_segment_chains, which computes
	the segment's actual placement in each stitched itinerary relative to its transit
	neighbor).
	"""
	legs = segment["legs"].sort_values("Delturnr")
	direct_mode = DIRECT_ACCESS_MODE_MAP.get(int(legs["StageMode"].iloc[0]), "WALK")
	origin, destination = tu_endpoints(tu_tur_row, segment["origin_stop_id"], segment["destination_stop_id"])
	return _fastest_itinerary(otp.client.request_direct_leg(
		origin=origin,
		destination=destination,
		direct_mode=direct_mode,
		depart_dt=tu_tur_row["depart_dt"],
		otp_url=settings.otp_url,
		request_timeout=settings.request_timeout,
		print_query=settings.print_query,
	))


def _load_and_align_segment_candidates(
		tu_tur_row,
		segment_legs,
		tu_gtfs_station_df,
		otp_mode_routes_cache,
		otp_route_name_index,
		origin_stop_id,
		destination_stop_id,
		depart_dt,
		settings,
		route_match=ROUTE_MATCH_EXACT,
		debug_profile="LIST_ALL",
):
	"""
	Runs one anchor-split "transit" segment's own OTP search plus leg alignment, mirroring
	what tu_otp_matching._match_once does for the whole trip (route names/modes, via-stops,
	reluctance retries, alignment/filtering) but scoped to segment_legs only. Used by
	try_split_station_anchored_fallback for every segment containing at least one transit
	leg - a segment that's pure WALK/CAR end-to-end uses _query_direct_segment instead.
	"""
	exclude_via_stopids = {stop_id for stop_id in (origin_stop_id, destination_stop_id) if stop_id}
	modes_json, modes_list, is_bus_s_train, route_names, route_names_ext, route_short_name_for_loading, route_name_groups_for_search, via_stopids = resolve_segment_search_params(
		segment_legs, tu_gtfs_station_df, otp_mode_routes_cache, otp_route_name_index,
		exclude_via_stopids=exclude_via_stopids,
		route_match=route_match,
	)
	if not modes_json:
		return pd.DataFrame(), REASON_NO_VALID_MODES

	raw_df, msg = load_candidates_with_reluctance_retries(
		tu_tur_row=tu_tur_row,
		tu_deltur_sub=segment_legs,
		tu_gtfs_station_df=tu_gtfs_station_df,
		modes_json=modes_json,
		route_name_groups=route_name_groups_for_search,
		route_short_name_for_loading=route_short_name_for_loading,
		modes_list=modes_list,
		via_stopids=via_stopids,
		settings=settings,
		skip_alignment=True,
		origin_stop_id=origin_stop_id,
		destination_stop_id=destination_stop_id,
		depart_dt=depart_dt,
		route_match=route_match,
		debug_profile=debug_profile,
	)
	if raw_df.empty:
		return pd.DataFrame(), msg or REASON_NO_OTP_CANDIDATES

	filtered_df, reason = align_and_filter_candidates(
		otp_candidates_df=raw_df,
		tu_deltur_sub=segment_legs,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups_for_search,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"],
		route_match=route_match,
	)
	if filtered_df.empty:
		return filtered_df, reason

	#Return the surviving itineraries' RAW legs, not the aligned ones: alignment rewrites a
	#TU bike leg matched to an OTP WALK leg (mode -> "BICYCLE", duration_min scaled by
	#WALK_BIKE_TIME_RATIO - see add_tu_delturnr_to_otp_candidates), so aligning the stitched
	#trip a second time in try_split_station_anchored_fallback would no longer recognise
	#those legs and the whole itinerary would fail the leg-sequence check. Segment alignment
	#is only used to decide which itineraries survive; the stitched trip is then aligned
	#exactly once, like the single-query path. filter_candidates_by_requirements only ever
	#drops whole iteration_ids, never individual legs, so this loses nothing.
	surviving_ids = filtered_df["iteration_id"].unique()
	return raw_df[raw_df["iteration_id"].isin(surviving_ids)].reset_index(drop=True), ""


def try_split_station_anchored_fallback(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		otp_mode_routes_cache,
		otp_route_name_index,
		route_name_groups,
		modes_list,
		anchor_segments,
		settings,
		route_match=ROUTE_MATCH_EXACT,
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
	wait_delta = pd.Timedelta(minutes=settings.station_anchor_wait_min)
	deadline = time.monotonic() + ANCHOR_TIMEOUT_MIN * 60 if ANCHOR_TIMEOUT_MIN else None

	def _log(stage, status, detail=""):
		suffix = f" ({detail})" if detail else ""
		print(f"[TurId={tur_id}] SPLIT_ANCHOR_FALLBACK/{stage}: {status}{suffix}")

	direct_leg_cache = {}
	for idx, seg in enumerate(anchor_segments):
		if seg["kind"] != "direct":
			continue
		leg_df = _query_direct_segment(tu_tur_row, seg, settings)
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
			#Per seed, not per segment - one segment can hold hundreds of seeds.
			if deadline and time.monotonic() > deadline:
				_log(f"segment {idx} (transit)", "ABORTED", REASON_ANCHOR_TIMEOUT)
				return pd.DataFrame(), f"anchored_{REASON_ANCHOR_TIMEOUT}"
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
				settings=settings,
				route_match=route_match,
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

	stitched_df = stitch_segment_chains(chains, direct_leg_cache, settings.station_anchor_wait_min)

	filtered_df, reason = align_and_filter_candidates(
		otp_candidates_df=stitched_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups,
		modes_list=modes_list,
		tur_id=tur_id,
		route_match=route_match,
	)
	if filtered_df.empty:
		return filtered_df, f"anchored_{reason}"
	return filtered_df, ""
