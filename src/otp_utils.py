import pandas as pd
from .constant import (
	MODE_MAP,
	INVALID_ROUTE_CHARS,
	STREET_MODES,
	REASON_MISSING_ROUTE_SHORT_NAME_COLUMN,
	REASON_MISSING_MODE_COLUMN,
	REASON_NO_REQUIRED_ROUTES,
	REASON_NO_REQUIRED_MODES,
	REASON_NO_MATCHING_LEG_SEQUENCE,
)

def normalize_route_name(route_name):
	"""
	Normalize a route short name to a canonical DIGITS+REST form so equivalent
	TU and GTFS spellings (e.g. '102 A', '102a', '102A') compare equal.

	Mirrors the R helper:
		function(x) {
		  if (is.na(x)) return(NA)
		  x_no_space <- gsub(" ", "", x)
		  digits <- gsub("[^0-9]", "", x_no_space)
		  letters <- gsub("[0-9]", "", x_no_space) %>% str_to_upper()
		  paste0(digits, letters)
		}
	Space is stripped, digits are pulled to the front, and whatever's left
	(letters, but also any other stray characters) is uppercased and appended.
	"""
	if pd.isna(route_name):
		return None
	route_name_no_space = str(route_name).replace(" ", "")
	digits = "".join(char for char in route_name_no_space if char.isdigit())
	rest = "".join(char for char in route_name_no_space if not char.isdigit()).upper()
	return digits + rest

def build_route_name_index(route_short_names):
	"""
	Index the real GTFS route short names for a mode, so TU's spelling can be
	translated into the spelling OTP actually knows.

	Returns (exact, digits_only):
		exact       normalized name -> {real names}   '150S' -> {'150S'}
		digits_only digit part      -> {real names}   '150'  -> {'150S'}

	digits_only only holds routes that actually carry a non-digit suffix. It
	exists to recover the letter TU respondents leave off when free-texting a
	bus route ('150' for '150S', '4' for '4A'), so it must never be consulted
	for S_TRAIN, where the survey uses a dropdown and the value is trustworthy.
	"""
	exact = {}
	digits_only = {}
	for real_name in route_short_names:
		normalized = normalize_route_name(real_name)
		if not normalized:
			continue
		exact.setdefault(normalized, set()).add(real_name)
		digits = "".join(char for char in normalized if char.isdigit())
		if digits and digits != normalized:
			digits_only.setdefault(digits, set()).add(real_name)
	return exact, digits_only

def resolve_tu_route_name(tu_route, route_name_index, allow_missing_letter=False):
	"""
	Translate a TU route name into the real GTFS spelling(s), so the
	routeShortNames filter sent to OTP carries values that exist in the graph.

	Returns a set of real names, empty when the route is unknown to this feed.
	A purely numeric TU value takes both the route of that exact number and
	every letter-suffixed variant of it, since a respondent writing '5' may
	mean either '5' or '5C'. So '5' -> {'5', '5C'} and '4' -> {'4A', '4C'}.
	That is intended: the filter only narrows the search, and the leg
	comparison plus RMSE ranking pick the winner. A TU value that already
	carries a letter is taken at its word and resolves to the exact route only.
	"""
	if not route_name_index:
		return set()
	exact, digits_only = route_name_index
	normalized = normalize_route_name(tu_route)
	if not normalized:
		return set()
	resolved = set(exact.get(normalized, ()))
	if allow_missing_letter and normalized.isdigit():
		resolved |= digits_only.get(normalized, set())
	return resolved

def route_names_match(tu_route, otp_route, allow_missing_letter=False) -> bool:
	"""
	Compare a TU route name against a GTFS/OTP one on their normalized forms.

	With allow_missing_letter (BUS only), a purely numeric TU value also matches
	a route that adds a letter suffix to it — '150' matches '150S'. The suffix
	must be all letters, so '15' still does not match '150S' and '150' does not
	match '1500'.
	"""
	tu_normalized = normalize_route_name(tu_route)
	otp_normalized = normalize_route_name(otp_route)
	if not tu_normalized or not otp_normalized:
		return False
	if tu_normalized == otp_normalized:
		return True
	if allow_missing_letter and tu_normalized.isdigit() and otp_normalized.startswith(tu_normalized):
		return otp_normalized[len(tu_normalized):].isalpha()
	return False

def has_invalid_route_name(route_names) -> bool:
	if not route_names:
		return True

	for route_name in route_names:
		if pd.isna(route_name):
			return True
		route_name = str(route_name).strip()
		if not route_name or route_name.lower() in {"nan", "none"}:
			return True
		if any(char in route_name for char in INVALID_ROUTE_CHARS):
			return True

	return False

def resolve_route_short_names(tu_deltur_sub, otp_mode_routes_cache, otp_route_name_index=None):
	"""
	Extracts and maps transit modes and routes for a given TurId.
	Handles fallback routes for RAIL, TRAM, and SUBWAY using a cache.

	otp_route_name_index maps "BUS"/"S_TRAIN" to a build_route_name_index() pair,
	used to translate TU's spelling of a route into the real GTFS one. Passing
	None skips that translation and sends TU's names as written.

	Returns:
		tuple: (route_names, route_names_ext, modes_json, modes_list, route_name_groups)
			   where route_names is a flat, deduplicated list of strings suitable for
			   OTP's routeShortNames "include" filter (an OR across every acceptable
			   spelling of every required route),
			   route_name_groups is a list of frozensets, one per distinct TU
			   route leg, each holding that leg's acceptable spelling(s) — use this
			   (not route_names) to check an itinerary actually contains every
			   required route, since a single TU leg can expand to multiple
			   alternative spellings ('114' -> {'114', '114N'}) that are alternatives
			   for each other, not routes that must all appear together,
			   and modes_json is a list of dicts like [{"mode": "BUS"}].
	"""
	# 1. Identify all valid modes for this TurId
	valid_stage_modes = [31, 32, 33, 34, 37, 41]
	modes_list = (
		tu_deltur_sub.loc[tu_deltur_sub["StageMode"].isin(valid_stage_modes),
		"StageMode"]
		.drop_duplicates()
		.map(MODE_MAP)
		.tolist()
	)
	if not modes_list:
		return [], [], [], [], []

	modes_json = [{"mode": mode} for mode in modes_list]

	# 2. Extract explicit routes for modes that provide them (BUS=31, S_TRAIN=32)
	if not any(mode in ["BUS", "S_TRAIN"] for mode in modes_list):
		return [], [], modes_json, modes_list, []

	# Resolve TU's spelling into the real GTFS names, so the routeShortNames filter
	# carries values that exist in the graph. BUS is free-texted in TU and gets the
	# missing-letter fallback ('150' -> '150S'); S_TRAIN comes from a survey dropdown
	# and is taken at its word. A name that resolves to nothing is kept as written,
	# so has_invalid_route_name still sees it and the trip fails the same way as before.
	stage_mode_to_otp_mode = {31: "BUS", 32: "S_TRAIN"}
	route_names = []
	route_name_groups = []
	for stage_mode, otp_mode in stage_mode_to_otp_mode.items():
		tu_route_names = (
			tu_deltur_sub.loc[
				tu_deltur_sub["StageMode"] == stage_mode,
				"Route"
			]
			.map(lambda route: route.strip() if isinstance(route, str) else route)
			.drop_duplicates()
			.tolist()
		)
		for tu_route_name in tu_route_names:
			resolved = resolve_tu_route_name(
				tu_route_name,
				otp_route_name_index.get(otp_mode) if otp_route_name_index else None,
				allow_missing_letter=(otp_mode == "BUS"),
			)
			if resolved:
				route_names.extend(resolved)
				route_name_groups.append(frozenset(resolved))
			else:
				route_names.append(tu_route_name)
				route_name_groups.append(frozenset({tu_route_name}))

	if not has_invalid_route_name(route_names):
		route_names = [str(route_name).strip() for route_name in route_names]

	# 3. For RAIL, TRAM, SUBWAY, FERRY, append cached routes if the mode is used in this trip
	route_names_ext = list(route_names)
	if any(mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"] for mode in modes_list):
		for mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"]:
			if mode in modes_list:
				route_names_ext = route_names_ext + otp_mode_routes_cache.get(mode, [])

	# 4. Deduplicate and clean up
	route_names = list(set(route_names))
	route_names_ext = list(set(route_names_ext))

	return route_names, route_names_ext, modes_json, modes_list, route_name_groups

def get_via_stops(tu_deltur_sub, tu_gtfs_station_df):
	if not (tu_deltur_sub["StageMode"].isin([32, 33, 34])).any():
		return None
	stops_row = []
	for _, row in tu_deltur_sub.loc[tu_deltur_sub["StageMode"].isin([32, 33, 34])].iterrows(): #TRAM is only added in data-processing. And my stationlist does not include these TRAM stations.
		stops_row.append({"otp_mode": row["otp_mode"], "tu_station_name": row["FromStation"]})
		stops_row.append({"otp_mode": row["otp_mode"], "tu_station_name": row["ToStation"]})
	if not stops_row:
		return None
	via_stopid = (
		pd.DataFrame(stops_row)
		.dropna(subset=["tu_station_name"])
		.drop_duplicates(subset=["otp_mode", "tu_station_name"], keep="first")
		.reset_index(drop=True)
	)
	if via_stopid.empty:
		return None

	required_columns = ["otp_mode", "tu_station_name", "gtfs_station_id"]
	missing_columns = [column for column in required_columns if column not in tu_gtfs_station_df.columns]
	if missing_columns:
		raise KeyError(f"tu_gtfs_station_df is missing required columns: {missing_columns}")


	# Merge and maintain order
	merged = (
		via_stopid
		.merge(
			tu_gtfs_station_df[["otp_mode", "tu_station_name", "gtfs_station_id"]],
			on=["otp_mode", "tu_station_name"],
			how="left"  # preserve order of via_stopid
		)
	)
	# Keep only rows with non-null gtfs_station_id and remove duplicates while preserving order
	via_stopids = (
		merged[merged["gtfs_station_id"].notna()]
		.drop_duplicates(subset=["gtfs_station_id"], keep='first')
		["gtfs_station_id"]
		.tolist()
	)
	return via_stopids


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