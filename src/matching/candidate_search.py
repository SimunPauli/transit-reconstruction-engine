import pandas as pd
from dataclasses import dataclass
from itertools import combinations
from .delturnr_otp_candidates import add_tu_delturnr_to_otp_candidates, summarize_alignment_diagnostics
from src import otp
from .route_matching_utils import resolve_route_short_names
from src.stations.tu_gtfs_stations_match import get_via_stations, drop_via_stations
from src.constant import (
	ACCESS_EGRESS_MODE_MAP,
	STREET_MODES,
	REASON_NO_OTP_CANDIDATES,
	REASON_MISSING_ROUTE_SHORT_NAME_COLUMN,
	REASON_MISSING_MODE_COLUMN,
	REASON_NO_REQUIRED_ROUTES,
	REASON_NO_REQUIRED_MODES,
	REASON_NO_MATCHING_LEG_SEQUENCE,
	ROUTE_MATCH_EXACT,
	ROUTE_MATCH_IGNORED,
)


@dataclass(frozen=True)
class SearchSettings:
	"""Per-run OTP search settings (from config.json), shared by every query of every trip."""
	otp_url: str
	search_window: str
	max_itinerary_candidates: int
	request_timeout: int = 60
	print_query: bool = False
	walk_reluctance: float = 2
	car_reluctance: float = 2
	transit_retry_enabled: bool = True
	transit_reluctance_sequence: tuple = (0.5, 0.25, 0.1)
	walk_retry_enabled: bool = True
	walk_reluctance_sequence: tuple = (3, 4, 6)
	station_anchor_wait_min: float = 0


def tu_endpoints(tu_tur_row, origin_stop_id=None, destination_stop_id=None):
	"""OTP origin/destination locations: the given GTFS stop, else the TU trip's own coordinate."""
	origin = otp.client.stop_location(origin_stop_id) if origin_stop_id else otp.client.coordinate_location(tu_tur_row["orig_lat"], tu_tur_row["orig_lon"])
	destination = otp.client.stop_location(destination_stop_id) if destination_stop_id else otp.client.coordinate_location(tu_tur_row["tiladrlat"], tu_tur_row["tiladrlon"])
	return origin, destination


def _get_access_egress(tu_deltur_sub: pd.DataFrame):
	first_mode = int(tu_deltur_sub["StageMode"].iloc[0])
	last_mode = int(tu_deltur_sub["StageMode"].iloc[-1])

	def _normalise_access_egress_mode(stage_mode: int, side: str):
		if stage_mode < 27:  # Street modes are less than 27 in TU StageMode
			mode = ACCESS_EGRESS_MODE_MAP.get(stage_mode, "WALK")
		else:
			mode = "WALK"  # fallback

		if mode == "CAR_DROP_OFF":
			if side == "access":
				return ["WALK", "CAR_DROP_OFF"]
			if side == "egress":
				return ["WALK", "CAR_PICKUP"]

		return mode

	tu_access = _normalise_access_egress_mode(first_mode, "access")
	tu_egress = _normalise_access_egress_mode(last_mode, "egress")

	return tu_access, tu_egress


def resolve_segment_search_params(
		tu_deltur_sub,
		tu_gtfs_station_df,
		otp_mode_routes_cache,
		otp_route_name_index,
		exclude_via_stopids=None,
		route_match=ROUTE_MATCH_EXACT,
):
	"""
	Derives one OTP search's route/mode/via-stop parameters from a tu_deltur_sub - shared by
	_match_once (the whole trip) and _load_and_align_segment_candidates (one anchor-split
	segment), so this resolution logic lives in exactly one place.

	exclude_via_stopids drops the vias at the given stop IDs' stations - used by segment
	queries to exclude their own origin/destination boundary stops, which are already
	the query's origin/destination override rather than a via constraint.

	Returns (modes_json, modes_list, is_bus_s_train, route_names, route_names_ext,
	route_short_name_for_loading, route_name_groups_for_search, via_stopids).
	"""
	route_names, route_names_ext, modes_json, modes_list, route_name_groups = resolve_route_short_names(
		tu_deltur_sub, otp_mode_routes_cache, otp_route_name_index, route_match=route_match
	)

	is_bus_s_train = any(mode in ["BUS", "S_TRAIN"] for mode in modes_list) #only BUS and S_TRAIN have stated route names
	is_rail_tram_subway_ferry = any(mode in ["RAIL", "SUBWAY", "TRAM", "FERRY"] for mode in modes_list)

	if route_match == ROUTE_MATCH_IGNORED:
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
		via_stopids = get_via_stations(tu_deltur_sub=tu_deltur_sub, tu_gtfs_station_df=tu_gtfs_station_df)
	else:
		via_stopids = None
	if exclude_via_stopids:
		via_stopids = drop_via_stations(via_stopids, exclude_via_stopids)

	return (
		modes_json, modes_list, is_bus_s_train, route_names, route_names_ext,
		route_short_name_for_loading, route_name_groups_for_search, via_stopids,
	)


def _build_reluctance_attempts(modes_list, settings):
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

	if settings.walk_retry_enabled:
		for walk_reluctance in settings.walk_reluctance_sequence:
			attempts.append((None, walk_reluctance))

	if settings.transit_retry_enabled:
		transit_modes = ["SUBWAY", "BUS", "RAIL", "S_TRAIN", "TRAM"]
		tu_transit_modes = [
			mode
			for mode in dict.fromkeys(modes_list)
			if mode in transit_modes
		]

		for reluctance in settings.transit_reluctance_sequence:
			for mode in tu_transit_modes:
				attempts.append(({mode: reluctance}, None))

		for reluctance in settings.transit_reluctance_sequence:
			for n_modes in range(2, len(tu_transit_modes) + 1):
				for modes in combinations(tu_transit_modes, n_modes):
					attempts.append(({mode: reluctance for mode in modes}, None))

	return attempts


def filter_candidates_by_requirements(
		otp_candidates_df,
		tu_deltur_sub,
		route_name_groups: list = None,
		modes_list: list = None,
		tur_id: int = None
) -> tuple[pd.DataFrame, str]:
	"""
	Filter OTP candidates to ensure all required routes and modes are present and TU transit deltur
	is matched.

	Args:
		otp_candidates_df: DataFrame with OTP candidate trips
		tu_deltur_sub: DataFrame with TU deltur legs
		route_name_groups: List of frozensets, one per required TU route leg, each
			holding that leg's acceptable spelling(s) (from resolve_route_short_names).
			An itinerary must contain at least one name from every group — the
			members within a group are alternatives for the same leg (e.g.
			{'114', '114N'}), not routes that must all appear together.
		modes_list: List of required transit modes (optional)
		tur_id: Trip ID for logging purposes
	"""
	filtered_df = otp_candidates_df.copy()

	# Filter by required routes (for BUS/S_TRAIN)
	if route_name_groups:
		if "route_short_name" not in filtered_df.columns:
			return pd.DataFrame(), REASON_MISSING_ROUTE_SHORT_NAME_COLUMN

		# A collapsed interlined leg carries the boarding route in route_short_name and
		# the continuation's route alongside it, so TU recording either line still matches.
		def _has_every_required_route(legs):
			present = set(legs["route_short_name"].dropna().astype(str))
			if "interlined_route_short_names" in legs.columns:
				for names in legs["interlined_route_short_names"]:
					if isinstance(names, (list, tuple)):   # NaN when frames are concatenated
						present.update(str(name) for name in names)
			return all(present & group for group in route_name_groups)

		route_cols = ["route_short_name"]
		if "interlined_route_short_names" in filtered_df.columns:
			route_cols.append("interlined_route_short_names")

		iteration_ids_with_required_routes = (
			filtered_df.groupby("iteration_id")[route_cols]
			.apply(_has_every_required_route)
		)

		filtered_df = filtered_df[
			filtered_df["iteration_id"].isin(
				iteration_ids_with_required_routes[
					iteration_ids_with_required_routes
				].index
			)
		].reset_index(drop=True)

		if filtered_df.empty:
			return pd.DataFrame(), REASON_NO_REQUIRED_ROUTES

	# Filter by required transit modes
	if modes_list:
		if "mode" not in filtered_df.columns:
			return pd.DataFrame(), REASON_MISSING_MODE_COLUMN

		required_modes = set(modes_list)

		iteration_ids_with_required_modes = (
			filtered_df.groupby("iteration_id")["mode"]
			.apply(lambda modes: required_modes.issubset(set(modes)))
		)

		filtered_df = filtered_df[
			filtered_df["iteration_id"].isin(
				iteration_ids_with_required_modes[
					iteration_ids_with_required_modes
				].index
			)
		].reset_index(drop=True)

		if filtered_df.empty:
			return pd.DataFrame(), REASON_NO_REQUIRED_MODES

	# Filter by TU transit leg order, and reject itineraries with unmatched non-street legs
	if tu_deltur_sub is not None and "tu_Delturnr" in otp_candidates_df.columns:
		transit_modes = ["SUBWAY", "BUS", "RAIL", "S_TRAIN", "TRAM"]

		tu_transit_delturnrs = (
			tu_deltur_sub
			.loc[
				tu_deltur_sub["otp_mode"].notna()
				& tu_deltur_sub["otp_mode"].isin(transit_modes)
			].sort_values("Delturnr")["Delturnr"]
			.tolist()
		)

		valid_ids = (
			otp_candidates_df
			.groupby("iteration_id")
			.filter(lambda g: _tu_transit_legs_matched_in_order(g, tu_transit_delturnrs))
			["iteration_id"]
			.unique()
		)
		filtered_df = filtered_df[filtered_df["iteration_id"].isin(valid_ids)].reset_index(drop=True)

		if filtered_df.empty:
			return pd.DataFrame(), REASON_NO_MATCHING_LEG_SEQUENCE

	return filtered_df, ""

def _tu_transit_legs_matched_in_order(otp_candidates_sub, tu_transit_delturnrs):
    """
    Check that all TU transit Delturnrs appear in this OTP iteration's
    matched Delturnrs, in order (as a subsequence), and that every
    unmatched OTP leg is a street mode.
    """
    unmatched = otp_candidates_sub["tu_Delturnr"].isna()
    if (~otp_candidates_sub.loc[unmatched, "mode"].isin(STREET_MODES)).any():
        return False

    matched = (
        otp_candidates_sub.loc[otp_candidates_sub["tu_Delturnr"].notna(), "tu_Delturnr"]
        .tolist()
    )
    # Check subsequence
    it = iter(matched)
    return all(delturnr in it for delturnr in tu_transit_delturnrs)



def align_and_filter_candidates(
		otp_candidates_df,
		tu_deltur_sub,
		tu_gtfs_station_df,
		route_name_groups,
		modes_list,
		tur_id,
		route_match=ROUTE_MATCH_EXACT
):
	alignment_diagnostics = []
	otp_candidates_df = add_tu_delturnr_to_otp_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		bike_stage_modes=(2, 8),
		diagnostics=alignment_diagnostics,
		route_match=route_match
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
		settings,
		walk_reluctance,
		origin,
		destination,
		depart_dt,
		access_mode,
		egress_mode,
		transit_reluctances=None,
		skip_alignment=False,
		route_match=ROUTE_MATCH_EXACT,
		debug_profile="LIST_ALL",
):
	otp_candidates_df = otp.client.load_all_candidates(
		origin=origin,
		destination=destination,
		depart_dt=depart_dt,
		access_mode=access_mode,
		egress_mode=egress_mode,
		modes_json=modes_json,
		route_short_name=route_short_name_for_loading,
		via_stopids=via_stopids,
		walk_reluctance=walk_reluctance,
		car_reluctance=settings.car_reluctance,
		transit_reluctances=transit_reluctances,
		search_window=settings.search_window,
		max_itinerary_candidates=settings.max_itinerary_candidates,
		otp_url=settings.otp_url,
		print_query=settings.print_query,
		request_timeout=settings.request_timeout,
		debug_profile=debug_profile,
	)

	if otp_candidates_df.empty:
		return otp_candidates_df, REASON_NO_OTP_CANDIDATES

	if skip_alignment:
		return otp_candidates_df, ""

	return align_and_filter_candidates(
		otp_candidates_df=otp_candidates_df,
		tu_deltur_sub=tu_deltur_sub,
		tu_gtfs_station_df=tu_gtfs_station_df,
		route_name_groups=route_name_groups,
		modes_list=modes_list,
		tur_id=tu_tur_row["TurId"],
		route_match=route_match
	)


def load_candidates_with_reluctance_retries(
		tu_tur_row,
		tu_deltur_sub,
		tu_gtfs_station_df,
		modes_json,
		route_name_groups,
		route_short_name_for_loading,
		modes_list,
		via_stopids,
		settings,
		skip_alignment=False,
		origin_stop_id=None,
		destination_stop_id=None,
		depart_dt=None,
		route_match=ROUTE_MATCH_EXACT,
		debug_profile="LIST_ALL"
):
	"""
	origin_stop_id/destination_stop_id anchor that end of the query at a GTFS stop, reached on
	foot; None uses the TU trip's own coordinate and access/egress mode. depart_dt defaults to
	the TU trip's own departure.
	"""
	last_msg = ""

	origin, destination = tu_endpoints(tu_tur_row, origin_stop_id, destination_stop_id)
	tu_access, tu_egress = _get_access_egress(tu_deltur_sub)
	access_mode = "WALK" if origin_stop_id else tu_access
	egress_mode = "WALK" if destination_stop_id else tu_egress
	if depart_dt is None:
		depart_dt = tu_tur_row["depart_dt"]

	reluctance_attempts = _build_reluctance_attempts(modes_list, settings)

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
			settings=settings,
			walk_reluctance=settings.walk_reluctance if walk_reluctance_override is None else walk_reluctance_override,
			origin=origin,
			destination=destination,
			depart_dt=depart_dt,
			access_mode=access_mode,
			egress_mode=egress_mode,
			transit_reluctances=transit_reluctances,
			skip_alignment=skip_alignment,
			route_match=route_match,
			debug_profile=debug_profile,
		)

		if not otp_candidates_df.empty:
			return otp_candidates_df, ""

		last_msg = msg_filter

	return pd.DataFrame(), last_msg
