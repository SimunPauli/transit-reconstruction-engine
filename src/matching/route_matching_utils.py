import pandas as pd
from src.constant import (
	MODE_MAP,
	INVALID_ROUTE_CHARS,
	ROUTE_MATCH_EXACT,
	ROUTE_MATCH_SIBLING,
	ROUTE_MATCH_LETTER,
	ROUTE_MATCH_DIGIT,
	S_TRAIN_SIBLING_LINES_ENABLED,
	S_TRAIN_SIBLING_LINES,
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

def shift_one_digit(normalized):
	"""
	The normalized name itself plus every name with exactly ONE digit moved by 1,
	suffix kept: '192' -> {'192', '292', '182', '191', '193'}, '150S' -> {'150S', '250S', '140S', '160S', '151S'}.
	No carry (a 9 does not become 10) and no leading zero, so the digit count never changes.
	"""
	digits = "".join(char for char in normalized if char.isdigit())
	suffix = normalized[len(digits):]
	shifted = {normalized}
	for position, digit in enumerate(digits):
		for new_digit in (int(digit) - 1, int(digit) + 1):
			if not 0 <= new_digit <= 9 or (position == 0 and new_digit == 0):
				continue
			shifted.add(digits[:position] + str(new_digit) + digits[position + 1:] + suffix)
	return shifted

def candidate_route_names(normalized, allow_digit_shift=False, allow_sibling=False):
	"""The normalized TU name plus its one-digit neighbours and/or S-train sibling line."""
	candidates = shift_one_digit(normalized) if allow_digit_shift else {normalized}
	if allow_sibling and normalized in S_TRAIN_SIBLING_LINES:
		candidates.add(S_TRAIN_SIBLING_LINES[normalized])
	return candidates

def resolve_tu_route_name(tu_route, route_name_index, allow_missing_letter=False, allow_digit_shift=False, allow_sibling=False):
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
	allow_digit_shift also takes every one-digit neighbour (see shift_one_digit),
	allow_sibling the S-train line sharing its track (S_TRAIN_SIBLING_LINES).
	"""
	if not route_name_index:
		return set()
	exact, digits_only = route_name_index
	normalized = normalize_route_name(tu_route)
	if not normalized:
		return set()
	candidates = candidate_route_names(normalized, allow_digit_shift, allow_sibling)
	resolved = set()
	for candidate in candidates:
		resolved |= exact.get(candidate, set())
		if allow_missing_letter and candidate.isdigit():
			resolved |= digits_only.get(candidate, set())
	return resolved

def route_names_match(tu_route, otp_route, allow_missing_letter=False, allow_digit_shift=False, allow_sibling=False) -> bool:
	"""
	Compare a TU route name against a GTFS/OTP one on their normalized forms.

	With allow_missing_letter (BUS only), a purely numeric TU value also matches
	a route that adds a letter suffix to it — '150' matches '150S'. The suffix
	must be all letters, so '15' still does not match '150S' and '150' does not
	match '1500'. allow_digit_shift also accepts every one-digit neighbour of the
	TU value (see shift_one_digit), with the same letter rule. allow_sibling also
	accepts the S-train line sharing its track ('A' matches 'E').
	"""
	tu_normalized = normalize_route_name(tu_route)
	otp_normalized = normalize_route_name(otp_route)
	if not tu_normalized or not otp_normalized:
		return False
	candidates = candidate_route_names(tu_normalized, allow_digit_shift, allow_sibling)
	for candidate in candidates:
		if candidate == otp_normalized:
			return True
		if allow_missing_letter and candidate.isdigit() and otp_normalized.startswith(candidate):
			if otp_normalized[len(candidate):].isalpha():
				return True
	return False

def route_match_flags(route_match, otp_mode):
	"""
	(allow_missing_letter, allow_digit_shift, allow_sibling) for one route_match level;
	the first two BUS only, allow_sibling S_TRAIN only (kept on at the later levels).
	"""
	is_bus = otp_mode == "BUS"
	return (
		is_bus and route_match in (ROUTE_MATCH_LETTER, ROUTE_MATCH_DIGIT),
		is_bus and route_match == ROUTE_MATCH_DIGIT,
		S_TRAIN_SIBLING_LINES_ENABLED and otp_mode == "S_TRAIN"
		and route_match in (ROUTE_MATCH_SIBLING, ROUTE_MATCH_LETTER, ROUTE_MATCH_DIGIT),
	)

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

def resolve_route_short_names(tu_deltur_sub, otp_mode_routes_cache, otp_route_name_index=None, route_match=ROUTE_MATCH_EXACT):
	"""
	Extracts and maps transit modes and routes for a given TurId.
	Handles fallback routes for RAIL, TRAM, and SUBWAY using a cache.

	otp_route_name_index maps "BUS"/"S_TRAIN" to a build_route_name_index() pair,
	used to translate TU's spelling of a route into the real GTFS one. Passing
	None skips that translation and sends TU's names as written.
	route_match (a ROUTE_MATCH_* level) sets how far a BUS name is widened.

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
	# carries values that exist in the graph. BUS is free-texted in TU and is widened
	# per route_match; S_TRAIN comes from a survey dropdown and is only widened to its
	# sibling line. A name that resolves to nothing is kept as written,
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
		allow_missing_letter, allow_digit_shift, allow_sibling = route_match_flags(route_match, otp_mode)
		for tu_route_name in tu_route_names:
			resolved = resolve_tu_route_name(
				tu_route_name,
				otp_route_name_index.get(otp_mode) if otp_route_name_index else None,
				allow_missing_letter=allow_missing_letter,
				allow_digit_shift=allow_digit_shift,
				allow_sibling=allow_sibling,
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
