import requests
import json
import textwrap
import pandas as pd
import numpy as np
from typing import Optional, Any
from otp_parser import json_to_df

LOCAL_TIMEZONE = "Europe/Copenhagen"
def parse_otp_datetime(series, timezone = LOCAL_TIMEZONE):
	"""
	Parse OTP datetime strings as absolute instants and convert them to local time.

	OTP returns ISO datetime strings with offsets, e.g. '2024-06-17T05:57:24+02:00'.
	Using utc=True guarantees timezone-aware parsing before tz_convert.
	"""
	return pd.to_datetime(series, utc=True, errors="coerce").dt.tz_convert(timezone)

def get_response(url, query, variables, timeout=60):
	try:
		response = requests.post(
			url,
			json={
				"query": query,
				"variables": variables
			},
			timeout=timeout
		)
	except Timeout as exc:
		raise TimeoutError(f"OTP request timed out after {timeout} seconds.") from exc
	except RequestException as exc:
		raise ConnectionError(f"OTP request failed: {exc}") from exc
	if response.status_code != 200:
		raise Exception(response.text)
	if "errors" in response.json():
		raise Exception(response.json()["errors"])
	return response


# If user want
def print_for_graphiql(query, variables):
	query_clean = textwrap.dedent(query).strip()
	variables_clean = json.dumps(variables, indent=2)

	print("GRAPHQL QUERY:")
	print("```graphql")
	print(query_clean)
	print("```")

	print("\nQUERY VARIABLES:")
	print("```json")
	print(variables_clean)
	print("```")


def graphql_json_request(
		tu_tur_row: Optional[pd.Series] = None,
		modes_json: Optional[list] = None,
		route_short_name_json: Optional[list] = None,
		direct: Optional[list] = None,
		via_stopids: Optional[list] = None,
		before: Optional[str] = None,
		last: Optional[int] = None,
		after: Optional[str] = None,
		first: Optional[int] = None,
		direct_only: bool = False,
		transit_only: bool = False,
		search_window: str = "PT30M",
		url: str = "http://localhost:8080/otp/gtfs/v1",
		print_query: bool = False,
		timeout: int = 60
) -> requests.Response:
	if tu_tur_row is None:
		raise ValueError("tu_tur_row must be specified")

	# Determine which optional features are being used
	has_pagination = any(x is not None for x in [before, last, after, first])
	has_search_window = search_window is not None
	has_itinerary_filter = True  # You always include this, but could make it conditional
	has_via = via_stopids is not None

	# Build query dynamically
	query = build_graphql_query(
		include_pagination=has_pagination,
		include_search_window=has_search_window,
		include_itinerary_filter=has_itinerary_filter,
		include_via=has_via,
	)

	# Build variables dict
	variables = {
		"origin": {
			"location": {"coordinate": {"latitude": tu_tur_row["orig_lat"], "longitude": tu_tur_row["orig_lon"]}}},
		"destination": {
			"location": {"coordinate": {"latitude": tu_tur_row["tiladrlat"], "longitude": tu_tur_row["tiladrlon"]}}},
		"dateTime": {"earliestDeparture": tu_tur_row["depart_dt_str"]},
		"modes": {"directOnly": direct_only, "transitOnly": transit_only},
		"itineraryFilter": {"itineraryFilterDebugProfile": "LIST_ALL"},
		"preferences": {"transit": {"alight": {"slack": "PT0M"}}},
	}

	if direct is not None:
		variables["modes"]["direct"] = direct

	# Add optional variables only if they're needed
	if has_pagination:
		if before is not None:
			variables["before"] = before
		if last is not None:
			variables["last"] = last
		if after is not None:
			variables["after"] = after
		if first is not None:
			variables["first"] = first

	if has_search_window:
		variables["searchWindow"] = search_window

	if route_short_name_json is not None:
		variables["preferences"].setdefault("transit", {}).setdefault("filters", []).append({
			"include": {"routeShortNames": route_short_name_json}
		})

	if via_stopids is not None:
		variables["via"] = [{"visit": {"stopLocationIds": [stop_id]}} for stop_id in via_stopids]

	if modes_json is not None:
		variables["modes"]["transit"] = {"transit": modes_json}

	if print_query:
		print_for_graphiql(query, variables)

	return get_response(url, query, variables, timeout=timeout)


def build_graphql_query(
		include_pagination: bool = False,
		include_search_window: bool = False,
		include_itinerary_filter: bool = False,
		include_via: bool = False,
) -> str:
	"""
	Dynamically build the GraphQL query string based on which features are needed.
	"""
	# Base query structure
	query = """
    query
    ($origin: PlanLabeledLocationInput!,
    $destination: PlanLabeledLocationInput!,
    $modes: PlanModesInput!,
    $preferences: PlanPreferencesInput,
    $dateTime: PlanDateTimeInput"""

	# Add optional variable declarations
	if include_pagination:
		query += """,
    $before: String,
    $last: Int,
    $after: String,
    $first: Int"""

	if include_search_window:
		query += """,
    $searchWindow: Duration"""

	if include_itinerary_filter:
		query += """,
    $itineraryFilter: PlanItineraryFilterInput"""

	if include_via:
		query += """,
    $via: [PlanViaLocationInput!]"""

	query += """)
    {
      planConnection(
        origin: $origin
        destination: $destination
        dateTime: $dateTime
        modes: $modes
        preferences: $preferences"""

	# Add optional arguments to the planConnection call
	if include_pagination:
		query += """,
        before: $before,
        last: $last,
        after: $after,
        first: $first"""

	if include_search_window:
		query += """,
        searchWindow: $searchWindow"""

	if include_itinerary_filter:
		query += """,
        itineraryFilter: $itineraryFilter"""

	if include_via:
		query += """,
        via: $via"""

	# semanticHash:
	# Hash code of the pattern. This value is stable and not dependent on the
	# pattern id, i.e. this value can be used to check whether two patterns are the
	# same, even if their ids have changed.


	# Rest of the query (edges, nodes, etc.)
	query += """
      ) {
        edges {
          cursor
          node {
            start
            end
            numberOfTransfers
            emissionsPerPerson { co2 }
            systemNotices { tag text }
            legs {
              mode startTime endTime distance duration
              from { name stop { gtfsId id parentStation { gtfsId id name } } }
              to { name stop { gtfsId id parentStation { id name } } }
              route { shortName gtfsId}
              trip { tripShortName semanticHash }
              generalizedCost
              legGeometry { length points }
            }
          }
        }
        pageInfo {
          startCursor endCursor hasPreviousPage hasNextPage
        }
      }
    }
    """

	return query

def _candidate_start_bounds(otp_candidates_df: pd.DataFrame):
	if otp_candidates_df.empty or "start_trip" not in otp_candidates_df.columns:
		return None, None

	start_times = (
		pd.to_datetime(otp_candidates_df["start_trip"], utc=True, errors="coerce")
		.dt.tz_convert(LOCAL_TIMEZONE)
		.dropna()
		.drop_duplicates()
	)

	if start_times.empty:
		return None, None

	return start_times.min(), start_times.max()


def _deduplicate_itineraries(otp_candidates_df: pd.DataFrame) -> pd.DataFrame:
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
		.groupby("iteration_id")[["start_trip", "end_trip"]]
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


def load_all_candidates(tu_tur_row: pd.Series | None = None,
                        modes_json: list | None = None,
                        route_short_name: list | None = None,
                        via_stopids: list | None = None,
                        search_window: str = "PT30M",
						max_itinerary_candidates = 50,
                        otp_url: str = "http://localhost:8080/otp/gtfs/v1",
                        request_timeout: int = 60,
                        print_query: bool = False):
	"""
	Fetch all OTP itinerary candidates within the search_window using bidirectional pagination.

	Performs initial query, then iteratively fetches forward and backward itineraries until
	reaching max_itinerary_candidates or search window boundaries. Deduplicates results before
	returning.

	Returns:
		DataFrame with columns: iteration_id, leg_id, start_trip, end_trip, mode,
		route_short_name, distance_km, duration_min, and other leg attributes.
	"""
	response = graphql_json_request(
		tu_tur_row=tu_tur_row,
		modes_json=modes_json,
		route_short_name_json=route_short_name,
		via_stopids=via_stopids,
		direct=["WALK"],
		first=max_itinerary_candidates,
		direct_only=False,
		transit_only=True,
		search_window=search_window,
		url=otp_url,
		print_query=print_query,
		timeout=request_timeout
	)

	otp_candidates_df = json_to_df(response)
	response_data = response.json()

	resp_depart_dt = tu_tur_row["depart_dt"]
	search_delta = pd.Timedelta(search_window)
	window_start = resp_depart_dt - search_delta
	window_end = resp_depart_dt + search_delta

	n_forward = len(response_data["data"]["planConnection"]["edges"])
	hasNextPage = response_data["data"]["planConnection"]["pageInfo"]["hasNextPage"]
	while n_forward < max_itinerary_candidates and hasNextPage:
		_, latest_start_df = _candidate_start_bounds(otp_candidates_df)
		if latest_start_df is None or (latest_start_df >= window_end):
			break

		endCursor = response_data["data"]["planConnection"]["pageInfo"]["endCursor"]
		if not endCursor:
			break
		# Send forward request to OTP
		response = graphql_json_request(
			tu_tur_row=tu_tur_row,
			modes_json=modes_json,
			route_short_name_json=route_short_name,
			via_stopids=via_stopids,
			direct=["WALK"],
			after=endCursor,
			first=max_itinerary_candidates - n_forward,
			direct_only=False,
			transit_only=True,
			search_window=search_window,
			url=otp_url,
			print_query=print_query,
			timeout=request_timeout
		)

		otp_candidates_forward_df = json_to_df(response)
		if otp_candidates_forward_df.empty:
			break

		# Add iteration_id to the forward dataframe
		offset = otp_candidates_df["iteration_id"].max() + 1
		otp_candidates_forward_df["iteration_id"] = otp_candidates_forward_df["iteration_id"] + offset
		otp_candidates_df = pd.concat([otp_candidates_df, otp_candidates_forward_df]).reset_index(drop=True)

		response_data = response.json()
		hasNextPage = response_data["data"]["planConnection"]["pageInfo"]["hasNextPage"]
		n_forward += len(response_data["data"]["planConnection"]["edges"])

	n_backward = 0
	hasPreviousPage = response_data["data"]["planConnection"]["pageInfo"]["hasPreviousPage"]

	while n_backward < max_itinerary_candidates and hasPreviousPage:
		earliest_start_df, _ = _candidate_start_bounds(otp_candidates_df)
		if earliest_start_df is None or (earliest_start_df <= window_start):
			break

		startCursor = response_data["data"]["planConnection"]["pageInfo"]["startCursor"]
		if not startCursor:
			break

		# Send backward request to OTP
		response = graphql_json_request(
			tu_tur_row=tu_tur_row,
			modes_json=modes_json,
			route_short_name_json=route_short_name,
			via_stopids=via_stopids,
			direct=["WALK"],
			before=startCursor,
			last=max_itinerary_candidates - n_backward,
			direct_only=False,
			transit_only=True,
			search_window=search_window,
			url=otp_url,
			print_query=print_query,
			timeout=request_timeout
		)

		otp_candidates_backward_df = json_to_df(response)
		if otp_candidates_backward_df.empty:
			break
		# Add iteration_id to the backward dataframe
		offset = otp_candidates_df["iteration_id"].min() - otp_candidates_backward_df["iteration_id"].max() - 1
		otp_candidates_backward_df["iteration_id"] = otp_candidates_backward_df["iteration_id"] + offset
		otp_candidates_df = pd.concat([otp_candidates_backward_df, otp_candidates_df]).reset_index(drop=True)

		response_data = response.json()
		hasPreviousPage = response_data["data"]["planConnection"]["pageInfo"]["hasPreviousPage"]
		n_backward += len(response_data["data"]["planConnection"]["edges"])

	otp_candidates_df = _deduplicate_itineraries(otp_candidates_df)

	return otp_candidates_df

##Get all route names for a specific mode
#This is because RAIL, TRAM and SUBWAY do not have route names in TU
def get_all_routes_for_mode(mode: str, url: str = "http://localhost:8080/otp/gtfs/v1") -> list:
	"""Fetch all available route shortNames for a specific mode from OTP."""
	query = """
    query {
      routes(transportModes: [%s]) {
        shortName
      }
    }
    """ % mode
	try:
		response = requests.post(url, json={"query": query})
		response.raise_for_status()
		data = response.json()
		routes = [
			r["shortName"] for r in data.get("data", {}).get("routes", [])
			if r.get("shortName")
		]
		return routes
	except Exception as e:
		print(f"Error fetching routes for mode {mode}: {e}")
		return []


def get_stops_by_bbox_query(lat: float,
                            lon: float,
                            bbox_buffer_m: int,
                            otp_url = "http://localhost:8080/otp/gtfs/v1"):
	if bbox_buffer_m <= 0:
		raise ValueError(f"bbox_buffer_m must be positive, got {bbox_buffer_m}")

	maxLat = lat + bbox_buffer_m / 111320
	minLat = lat - bbox_buffer_m / 111320
	maxLon = lon + bbox_buffer_m / (111320 * np.abs(np.cos(np.radians(lat))))
	minLon = lon - bbox_buffer_m / (111320 * np.abs(np.cos(np.radians(lat))))
	query = """
    query GetStopsByBbox($maxLat: Float!, $minLat: Float!, $maxLon: Float!, $minLon: Float!) {
    stopsByBbox(
        maxLat: $maxLat,
        minLat: $minLat,
        maxLon: $maxLon,
        minLon: $minLon
      ) {
        id
        gtfsId
        name
        lat
        lon
        routes {
          shortName
          mode
        }
      }
    }
    """

	#Variables of query
	variables = {
		"maxLat": maxLat,
		"minLat": minLat,
		"maxLon": maxLon,
		"minLon": minLon
	}

	response = get_response(otp_url, query, variables)
	return response
