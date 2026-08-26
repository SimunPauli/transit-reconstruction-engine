import pandas as pd
import utm
from .otp_client import get_stops_by_bbox_query
import re
from rapidfuzz import fuzz
import numpy as np
from datetime import date

_EPOCH = date(1970, 1, 1)

def _days_since_epoch(d: date) -> int:
	return (d - _EPOCH).days


def match_tu_gtfs_stations(tu_stations: pd.DataFrame,
                           bbox_buffer_m=400,
                           period=None,
                           name_match_threshold = 0.7):
	tu_stations["id"] = tu_stations.index
	# period should be: period = (int(tu_tur["DiaryDate"].min()), int(tu_tur["DiaryDate"].max()))
	if period is None:
		print("Warning: No period specified. Matching all stations. Also station not open this period will be matched.")
	if period is not None:
		period_start, period_end = period
		mask = tu_stations.apply(
			_station_active_in_period, axis=1,
			period_start=period_start, period_end=period_end
		)
		tu_stations = tu_stations[mask].copy()

	# --- Add Lat/Lon (destination) ---
	lat_lon = [
		utm.to_latlon(e, n, zone_number=32, northern=True)
		for e, n in zip(tu_stations["e"], tu_stations["n"])
	]

	tu_stations[["lat", "lon"]] = pd.DataFrame(
		lat_lon,
		index=tu_stations.index,
	)

	# Collect results
	matches_list = []

	for i, row in tu_stations.iterrows():
		#print(f"\n {i} Processing TU station: {row['statnavn']}")
		matches = find_gtfs_stations_for_tu_station(
			tu_station=row,
			name_match_threshold=name_match_threshold,
			bbox_buffer_m=bbox_buffer_m,
			period=period,
		)

		for mode, stop in matches.items():
			#print(f"{mode}: {stop['name']} (name similarity: {stop.get('name_similarity', 'N/A')})")
			# Append match to list
			matches_list.append({
				'tu_station_id': row['id'],
				'tu_station_name': row['statnavn'],
				'gtfs_station_id': stop['stop_gtfsId'],
				'gtfs_station_name': stop['name'],
				'otp_mode': mode,
				'name_similarity': stop.get('name_similarity', None),
				'distance_degree': stop['distance_degree']
			})

	result_df = pd.DataFrame(matches_list)
	return result_df


TU_MODE_TO_GTFS = {
	"stog": "S_TRAIN",
	"metro":  "SUBWAY",
	"andettog":    "RAIL",
	"letbane":    "TRAM",
}

# TU station names that were renamed in GTFS at some point and no longer resemble
# the current GTFS name closely enough for name_similarity to bridge on its own
# (found by scanning all TU stations for a same-mode GTFS stop within 250m scoring
# below name_match_threshold against the plain TU name). Each TU name maps to every
# known former/alternate GTFS name that should also be tried during matching.
TU_NAME_ALIASES = {
	"København Syd": ["Ny Ellebjerg St."],
}

# Stations where a mode was added to an already-existing station later than the
# station row's own OpenDate reflects. tu_stations only has one OpenDate/ClosedDate
# pair per row, so it can't represent "this particular mode started later than the
# rest of the station" - a NaN OpenDate here correctly means the STATION itself
# (its other, older modes) predates TU, but _station_active_in_period's "NaN = open
# since -inf" reading would otherwise also apply to the newer mode. Checked here
# against `period` for just the (station, mode) pairs listed; every other mode at
# these stations, and every other station, keeps using the row-level dates as before.
# Dates cross-checked against this dataset's own correctly-dated Cityringen sibling
# stations (which don't have this ambiguity, since for them station-open-date ==
# metro-open-date) and Copenhagen Metro's real M3/M4 rollout history.
MODE_OPEN_DATE_OVERRIDES = {
	("Østerport", "SUBWAY"): date(2019, 9, 29),
	("Nordhavn", "SUBWAY"): date(2019, 9, 29),
	("Nørrebro", "SUBWAY"): date(2019, 9, 29),
}

# Rejseplanen encodes a stop's mode in the stop name itself wherever one station is
# split into several physically separate stops: the metro platforms of an interchange
# are their own stop named "X St. (Metro)", and a rail-replacement bus calls at a
# street-level stop named "X St. (togbus)" / "X Station (Bus / ...)". _normalise_name
# has to strip those markers for the TU name to be comparable to the GTFS name at all,
# but the marker is the single strongest piece of mode evidence in the feed: it is what
# distinguishes the metro platform from the S-tog platform when both are called
# "Nørreport St." and both therefore score identically on name similarity.
#
# Checked against the merged feeds: in 2020 all 39 "(Metro)"-marked stops are served by
# a Metroselskabet route and no metro-served stop with a station-like name is unmarked;
# in 2018, 160 of the 184 bus-marked stops are served by a route OTP types as rail-like
# (rail-replacement services keep the parent line's route_type), which is exactly the
# case where mode alone cannot tell a station apart from the bus stop outside it.
_MODE_MARKER_PATTERNS = [
	("SUBWAY", re.compile(r"\(\s*metro\b", re.IGNORECASE)),
	("BUS", re.compile(r"\(\s*(?:tog)?bus\b|/\s*bus\b|\bbus\s*$", re.IGNORECASE)),
]

def _name_mode_marker(name: str) -> str | None:
	"""The mode a GTFS stop name explicitly claims, or None if it claims nothing."""
	for mode, pattern in _MODE_MARKER_PATTERNS:
		if pattern.search(name):
			return mode
	return None

def _normalise_name(name: str) -> str:
	"""Lowercase, remove punctuation, collapse whitespace."""
	name = name.lower()
	# Replace Å/å with aa
	name = name.replace("å", "aa")
	# Remove common station type suffixes (case-insensitive)
	pattern = r'\s+(station|st\.|st|metro|s-tog|stog)\s*$|\s*\((station|st\.|st|metro|s-tog|stog)\)\s*$'
	while re.search(pattern, name):
		name = re.sub(pattern, '', name)
	# Keep Danish letters, remove other punctuation
	name = re.sub(r"[^\w\søæå]", " ", name)
	name = re.sub(r"\s+", " ", name).strip()
	return name

def find_gtfs_stations_for_tu_station(
		tu_station: pd.Series,
		gtfs_df: pd.DataFrame = None,
		bbox_buffer_m: int = 400,
		otp_url: str = "http://localhost:8080/otp/gtfs/v1",
		name_match_threshold: float = 0.7,
		period: tuple = None,
) -> dict[str, pd.Series]:
	"""
	Find matching GTFS station(s) for a single TU station row.

	A TU station may correspond to multiple GTFS stops when GTFS splits
	modes into separate stops, OR to a single stop that serves all modes.

	Parameters
	----------
	tu_station : pd.Series
		One row from tu_stations with 'lat', 'lon', mode flags, and 'statnavn'.
	gtfs_df : pd.DataFrame, optional
		Candidate GTFS stops (from parse_stops_to_df). If None, fetches from OTP.
	bbox_buffer_m : int
		Search radius in metres.
	otp_url : str
		OTP endpoint.
	name_match_threshold : float
		Minimum similarity score (0–1) to accept a name match, compared against a
		rapidfuzz WRatio score scaled to the same 0–1 range. Default 0.7.
	period : tuple, optional
		(period_start, period_end) in days since 1970-01-01, same convention as
		_station_active_in_period. Used only to check MODE_OPEN_DATE_OVERRIDES
		entries for this station; if None, overrides are not applied (every mode
		flagged on the row is treated as active, same as before this parameter
		existed).

	Returns
	-------
	dict mapping GTFS mode string → matched stop (pd.Series).
	e.g. {"S_TRAIN": <stop row>, "SUBWAY": <stop row>}
	If both modes share one stop, the same stop appears under both keys.
	"""
	# 1. Active modes for this TU station. Most stations are fully covered by the
	# row-level OpenDate/ClosedDate check the caller already applied; a station
	# listed in MODE_OPEN_DATE_OVERRIDES additionally needs that specific mode's
	# real opening date checked against the period, since the row-level dates
	# reflect the station's oldest mode, not this one.
	statnavn = tu_station.get("statnavn")
	active_modes = []
	for tu_col, gtfs_mode in TU_MODE_TO_GTFS.items():
		if tu_station.get(tu_col, 0) != 1:
			continue
		override_open = MODE_OPEN_DATE_OVERRIDES.get((statnavn, gtfs_mode))
		if override_open is not None and period is not None:
			_, period_end = period
			if _days_since_epoch(override_open) > period_end:
				print(
					f"  Note: {gtfs_mode} at {statnavn} didn't open until "
					f"{override_open.isoformat()} (MODE_OPEN_DATE_OVERRIDES); excluding it "
					f"for this period despite the station row's own OpenDate"
				)
				continue
		active_modes.append(gtfs_mode)

	if not active_modes:
		print(f"Warning: No active modes for {tu_station['statnavn']}")
		return {}

	# 2. Fetch nearby GTFS stops if not provided
	if gtfs_df is None or gtfs_df.empty:
		response = get_stops_by_bbox_query(
			lat=tu_station["lat"],
			lon=tu_station["lon"],
			bbox_buffer_m=bbox_buffer_m,
			otp_url=otp_url,
		)
		gtfs_df = parse_stops_to_df(response, tu_station["lat"], tu_station["lon"])
	# 3. Keep only stops that serve at least one relevant mode
	def stop_serves_mode(modes_str: str, mode: str) -> bool:
		return mode in [m.strip() for m in modes_str.split(",")]

	relevant_stops = gtfs_df[
		gtfs_df["modes"].apply(
			lambda m: any(stop_serves_mode(m, mode) for mode in active_modes)
		)
	].copy()

	if relevant_stops.empty:
		print(f"Warning: No relevant_stops GTFS stops found for {tu_station['statnavn']} in {bbox_buffer_m}m radius for active_modes: {active_modes}")
		return {}

	tu_name = str(tu_station.get("statnavn", ""))
	tu_name_variants = [tu_name] + TU_NAME_ALIASES.get(tu_name, [])
	tu_name_variants_norm = [_normalise_name(n) for n in tu_name_variants]

	# 4. For each mode, pick the closest stop with "good" name match
	result: dict[str, pd.Series] = {}

	for mode in active_modes:
		mode_stops = relevant_stops[
			relevant_stops["modes"].apply(lambda m: stop_serves_mode(m, mode))
		].copy()

		if mode_stops.empty:
			print(f"Warning: No GTFS stops found for {tu_station['statnavn']} in {bbox_buffer_m}m radius for mode: {mode}")
			continue

		# Let the stop name's own mode marker decide before name similarity or distance
		# get a say. A stop that claims this mode wins outright; a stop that claims a
		# different one is set aside unless nothing else is left. This is what keeps
		# (SUBWAY, "Nørreport") on "Nørreport St. (Metro)" instead of the identically-
		# normalised S-tog stop next door, and keeps rail modes off the "(togbus)" stop
		# outside a station - both of which the mode flags alone cannot rule out, since
		# a mis-typed or rail-replacement route makes the wrong stop advertise the mode.
		markers = mode_stops["name"].apply(_name_mode_marker)
		claims_this_mode = (markers == mode)
		claims_other_mode = markers.notna() & (markers != mode)
		if claims_this_mode.any():
			mode_stops = mode_stops[claims_this_mode].copy()
		elif (~claims_other_mode).any():
			dropped = mode_stops[claims_other_mode]
			if not dropped.empty:
				print(
					f"  Note: ignoring {len(dropped)} stop(s) whose name claims another mode "
					f"when matching {mode} for '{tu_station['statnavn']}': "
					f"{', '.join(dropped['name'].astype(str))}"
				)
			mode_stops = mode_stops[~claims_other_mode].copy()

		# Score by name similarity (WRatio handles word order and the common case
		# where the GTFS name is the TU name plus a cross-street suffix, e.g.
		# "Forum" vs "Forum St. (Rosenørns Allé)"). Also tries any known former/
		# alternate GTFS name for this TU station (TU_NAME_ALIASES), since some
		# TU names predate a later GTFS station rename and no longer resemble it.
		if tu_name:
			mode_stops["name_similarity"] = mode_stops["name"].apply(
				lambda n: max(
					fuzz.WRatio(variant, _normalise_name(n))
					for variant in tu_name_variants_norm
				)
			)
			name_filtered = mode_stops[mode_stops["name_similarity"] >= name_match_threshold * 100]

			if name_filtered.empty:
				print(
					f"  Warning: no name match (threshold={name_match_threshold}) "
					f"for '{tu_name}' in {mode} stops within {bbox_buffer_m}m; skipping rather than "
					f"guessing by distance alone"
				)
				continue
			mode_stops = name_filtered
			# Prefer the best name match among everything that cleared the threshold;
			# picking by distance alone can prefer a technically-nearer but worse-named
			# stop (e.g. a street-level stop that happens to carry a rare/anomalous
			# trip pattern under the right mode) over the actual station platform, so
			# distance only breaks ties between equally-good name matches.
			best = mode_stops.sort_values(
				["name_similarity", "distance_degree"], ascending=[False, True]
			).iloc[0]
		else:
			# No TU name available to compare against -- fall back to nearest by distance.
			best = mode_stops.loc[mode_stops["distance_degree"].idxmin()]
		result[mode] = best

	return result

def _station_active_in_period(row, period_start, period_end):
	"""
	Returns True if the station is open at any point within [period_start, period_end].
	Dates are days since 1970-01-01.
	NaN OpenDate  → station was already open at collection time (treat as open from -inf).
	NaN ClosedDate → station is still open (treat as open until +inf).
	"""
	open_date = row.get("OpenDate")
	closed_date = row.get("ClosedDate")

	# Effective open/close bounds
	effective_open = open_date if pd.notna(open_date) else float("-inf")
	effective_close = closed_date if pd.notna(closed_date) else float("inf")

	# Overlap condition: station interval [effective_open, effective_close]
	# overlaps [period_start, period_end]
	return effective_open <= period_end and effective_close >= period_start

def parse_stops_to_df(response_data, station_lat: float, station_lon: float):
	response_data = response_data.json()
	# Retrieve the list of edges
	data = response_data.get("data", {})

	rows = []
	stops = data.get("stopsByBbox", [])

	for stop in stops:
		routes = stop.get("routes", [])
		route_modes = [r.get("mode") for r in routes if r.get("mode")]
		route_names = [r.get("shortName") for r in routes if r.get("shortName")]

		rows.append({
			"distance_degree": None,  # No distance in bbox response
			"stop_gtfsId": stop.get("gtfsId"),
			"name": stop.get("name"),
			"lat": stop.get("lat"),
			"lon": stop.get("lon"),
			"modes": ", ".join(set(route_modes)),
			"routes": ", ".join(route_names)
		})

	df = pd.DataFrame(rows)
	if df.empty:
		df = pd.DataFrame(columns=["distance_degree", "stop_gtfsId", "name", "lat", "lon", "modes", "routes"])
	if not df.empty:
		# Approximate distance to station
		df["distance_degree"] = np.sqrt((df["lat"] - station_lat)**2 + (df["lon"] - station_lon)**2)
	df = df.sort_values(by="distance_degree")
	return pd.DataFrame(df)