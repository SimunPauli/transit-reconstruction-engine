import pandas as pd

def decode_polyline(encoded):
	"""
	Decode a Google encoded polyline string into a list of (latitude, longitude) tuples.

	This replaces the external `polyline` package so the script does not fail with:
	ModuleNotFoundError: No module named 'polyline'
	"""
	if not encoded:
		return []

	coordinates = []
	index = 0
	lat = 0
	lng = 0

	while index < len(encoded):
		result = 0
		shift = 0

		while True:
			byte = ord(encoded[index]) - 63
			index += 1
			result |= (byte & 0x1F) << shift
			shift += 5
			if byte < 0x20:
				break

		delta_lat = ~(result >> 1) if result & 1 else result >> 1
		lat += delta_lat

		result = 0
		shift = 0

		while True:
			byte = ord(encoded[index]) - 63
			index += 1
			result |= (byte & 0x1F) << shift
			shift += 5
			if byte < 0x20:
				break

		delta_lng = ~(result >> 1) if result & 1 else result >> 1
		lng += delta_lng

		coordinates.append((lat / 1e5, lng / 1e5))

	return coordinates

def json_to_df(response):
	if response.status_code != 200:
		raise Exception(response.text)
	if "errors" in response.json():
		raise Exception(response.json()["errors"])
	data = response.json()["data"]["planConnection"]["edges"]
	rows = []
	columns = [
		"iteration_id",
		"leg_id",


		"start_trip",
		"end_trip",
		"start_leg",
		"end_leg",

		"mode",
		"interline_with_previous",
		"route_short_name",
		"route_gtfs_id",
		"distance_km",
		"duration_min",
		"from",
		"to",
		"from_gtfs_id",
		"to_gtfs_id",

		"trip_short_name",
		"generalized_cost",
		"leg_geometry",
		"system_notice_tag",
		"system_notice_text",
	]
	for j, edge in enumerate(data):
		node = edge["node"]

		system_notice = node.get("systemNotices") or []
		system_notice_tag = [notice.get("tag") for notice in system_notice]
		system_notice_text = [notice.get("text") for notice in system_notice]

		for k, leg in enumerate(node["legs"]):
			route = leg.get("route") or {} #for WALK "shortName" is null
			trip = leg.get("trip") or {}
			from_location = leg.get("from") or {}
			to_location = leg.get("to") or {}
			from_stop = from_location.get("stop") or {}
			to_stop = to_location.get("stop") or {}
			rows.append({
				"iteration_id": j,
				"leg_id": k,

				"start_trip": node["start"],
				"end_trip": node["end"],
				"start_leg": leg["startTime"],
				"end_leg": leg["endTime"],

				"mode": leg["mode"],
				"interline_with_previous": bool(leg.get("interlineWithPreviousLeg")),
				"route_short_name": route.get("shortName"),
				"route_gtfs_id": route.get("gtfsId"),
				"distance_km": round(leg["distance"]/1000,3),
				"duration_min": int(round(leg["duration"]/60,0)),
				"from": from_location["name"],
				"to": to_location["name"],
				"from_gtfs_id": from_stop.get("gtfsId"),
				"to_gtfs_id": to_stop.get("gtfsId"),

				"trip_short_name": trip.get("tripShortName"),
				"generalized_cost": leg["generalizedCost"],
				"leg_geometry": decode_polyline(leg["legGeometry"]["points"]),
				"system_notice_tag": system_notice_tag,
				"system_notice_text": system_notice_text
			})
	df = pd.DataFrame(rows, columns=columns)
	return _collapse_interlined_legs(df)


def _collapse_interlined_legs(df):
	"""Fold each stay-seated continuation into the leg it continues.

	OTP reports an interlined transfer (GTFS block_id) as two legs, but the traveller
	never left the vehicle and TU records it as one leg. Merging them here keeps the
	station checks in delturnr_otp_candidates.py comparing TU's boarding and alighting
	stops against the whole ride instead of half of it, and stops the second half from
	being scored as an unmatched extra leg.

	The merged leg keeps the boarding leg's route and trip; the continuation's are kept
	in interlined_route_short_names (so the route filter still recognises either line)
	and interlined_trip_short_names (the continuation's train number, for the export).
	"""
	if "interline_with_previous" not in df.columns:
		return df

	if df.empty or not df["interline_with_previous"].any():
		for column in ("interlined_route_short_names", "interlined_trip_short_names"):
			df[column] = [[] for _ in range(len(df))]
		return df

	rows = []
	for row in df.sort_values(["iteration_id", "leg_id"]).to_dict("records"):
		merges_into_previous = (
			rows
			and row["interline_with_previous"]
			and rows[-1]["iteration_id"] == row["iteration_id"]
		)
		if merges_into_previous:
			previous = rows[-1]
			previous["end_leg"] = row["end_leg"]
			previous["to"] = row["to"]
			previous["to_gtfs_id"] = row["to_gtfs_id"]
			previous["duration_min"] += row["duration_min"]
			previous["distance_km"] = round(previous["distance_km"] + row["distance_km"], 3)
			if previous["generalized_cost"] is not None and row["generalized_cost"] is not None:
				previous["generalized_cost"] += row["generalized_cost"]
			previous["leg_geometry"] = list(previous["leg_geometry"]) + list(row["leg_geometry"])
			previous["interlined_route_short_names"].append(row["route_short_name"])
			previous["interlined_trip_short_names"].append(row["trip_short_name"])

			print(f"OTP iteration_id={row["iteration_id"]} original leg_id={row["leg_id"]} collapsed with leg={previous['leg_id']}")
			continue

		row["interlined_route_short_names"] = []
		row["interlined_trip_short_names"] = []
		rows.append(row)

	return pd.DataFrame(
		rows,
		columns=list(df.columns) + ["interlined_route_short_names", "interlined_trip_short_names"],
	)


def deduplicate_itineraries(otp_candidates_df: pd.DataFrame) -> pd.DataFrame:
	"""
	Remove duplicate itineraries caused by pagination overlap.
	Two itineraries are considered the same if they share the same start_trip and end_trip.

	When duplicates exist, keeps the iteration_id with:
	1. Lowest absolute value
	2. In case of tie, the non-negative one (e.g., keep 0 over -0, keep 1 over -1)

	Then reassigns iteration_id to be contiguous (0, 1, 2, ...).
	"""
	if otp_candidates_df.empty:
		return otp_candidates_df

	# Get the first occurrence of each trip (by start_trip and end_trip)
	trip_keys = (
		otp_candidates_df
		.groupby("iteration_id")[["start_trip", "end_trip", "route_gtfs_id"]]
		.first()
		.reset_index()
	)

	# Group by (start_trip, end_trip) to find duplicates
	trip_key_to_ids = {}
	for _, row in trip_keys.iterrows():
		key = (row["start_trip"], row["end_trip"])
		if key not in trip_key_to_ids:
			trip_key_to_ids[key] = []
		trip_key_to_ids[key].append(row["iteration_id"])

	# For each duplicate group, select the best iteration_id
	keep_ids = []
	for iteration_ids in trip_key_to_ids.values():
		if len(iteration_ids) == 1:
			# No duplicate
			keep_ids.append(iteration_ids[0])
		else:
			# Multiple duplicates: pick by lowest |id|, then prefer >= 0
			best_id = min(iteration_ids, key=lambda x: (abs(x), x < 0))
			keep_ids.append(best_id)

	# Filter to keep only selected iteration_ids
	deduped = otp_candidates_df[otp_candidates_df["iteration_id"].isin(keep_ids)].copy()

	# # Reassign iteration_id to be contiguous based on sorted order
	# id_map = {old_id: new_id for new_id, old_id in enumerate(sorted(keep_ids))}
	# deduped["iteration_id"] = deduped["iteration_id"].map(id_map)

	return deduped.reset_index(drop=True)


def stops_json_to_df(response):
	"""One row per stop in a stopsByBbox response; modes/routes as comma-joined strings."""
	stops = response.json().get("data", {}).get("stopsByBbox", [])

	rows = []
	for stop in stops:
		routes = stop.get("routes", [])
		route_modes = [r.get("mode") for r in routes if r.get("mode")]
		route_names = [r.get("shortName") for r in routes if r.get("shortName")]

		rows.append({
			"stop_gtfsId": stop.get("gtfsId"),
			"name": stop.get("name"),
			"lat": stop.get("lat"),
			"lon": stop.get("lon"),
			"modes": ", ".join(set(route_modes)),
			"routes": ", ".join(route_names)
		})

	return pd.DataFrame(rows, columns=["stop_gtfsId", "name", "lat", "lon", "modes", "routes"])
