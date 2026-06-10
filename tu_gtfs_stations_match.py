import pandas as pd
import geopandas as gpd
from otp_client import get_stops_by_bbox_query
import re
from collections import Counter
import numpy as np


def match_tu_gtfs_stations(tu_stations: pd.DataFrame,
                           bbox_buffer_m=400,
                           period=None,
                           name_match_threshold = 0.5):
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
    gdf_dest = gpd.GeoDataFrame(
        tu_stations,
        geometry=gpd.points_from_xy(tu_stations["e"], tu_stations["n"]),
        crs="EPSG:32632"
    )
    gdf_dest = gdf_dest.to_crs("EPSG:4326")
    tu_stations["lon"] = gdf_dest.geometry.x
    tu_stations["lat"] = gdf_dest.geometry.y

    # Collect results
    matches_list = []

    for i, row in tu_stations.iterrows():
        print(f"\n {i} Processing TU station: {row['statnavn']}")
        matches = find_gtfs_stations_for_tu_station(
            tu_station=row,
            name_match_threshold=name_match_threshold,
            bbox_buffer_m=bbox_buffer_m,
        )

        for mode, stop in matches.items():
            print(f"{mode}: {stop['name']} (name similarity: {stop.get('name_similarity', 'N/A')})")
            # Append match to list
            matches_list.append({
                'tu_station_id': row['id'],
                'tu_station_name': row['statnavn'],
                'gtfs_station_id': stop['stop_gtfsId'],
                'gtfs_station_name': stop['name'],
                'mode': mode,
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

def cosine_similarity(s1, s2):
    # Convert strings to character frequency vectors
    vec1 = Counter(s1)
    vec2 = Counter(s2)

    # Calculating cosine similarity
    dot_product = sum(vec1[ch] * vec2[ch] for ch in vec1)
    magnitude1 = np.sqrt(sum(count ** 2 for count in vec1.values()))
    magnitude2 = np.sqrt(sum(count ** 2 for count in vec2.values()))
    res = dot_product / (magnitude1 * magnitude2)
    return(res)

def find_gtfs_stations_for_tu_station(
    tu_station: pd.Series,
    gtfs_df: pd.DataFrame = None,
    bbox_buffer_m: int = 400,
    otp_url: str = "http://localhost:8080/otp/gtfs/v1",
    name_match_threshold: float = 0.6,
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
        Minimum similarity score (0–100) to accept a name match. Default 0.6.

    Returns
    -------
    dict mapping GTFS mode string → matched stop (pd.Series).
    e.g. {"S_TRAIN": <stop row>, "SUBWAY": <stop row>}
    If both modes share one stop, the same stop appears under both keys.
    """
    # 1. Active modes for this TU station
    active_modes = [
        gtfs_mode
        for tu_col, gtfs_mode in TU_MODE_TO_GTFS.items()
        if tu_station.get(tu_col, 0) == 1
    ]

    if not active_modes:
        print(f"No active modes for {tu_station['statnavn']}")
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
        print(f"No relevant_stops GTFS stops found for {tu_station['statnavn']} in {bbox_buffer_m}m radius for active_modes: {active_modes}")
        return {}

    tu_name = str(tu_station.get("statnavn", ""))

    # 4. For each mode, pick the closest stop with "good" name match
    result: dict[str, pd.Series] = {}

    for mode in active_modes:
        mode_stops = relevant_stops[
            relevant_stops["modes"].apply(lambda m: stop_serves_mode(m, mode))
        ].copy()

        if mode_stops.empty:
            print(f"No GTFS stops found for {tu_station['statnavn']} in {bbox_buffer_m}m radius for mode: {mode}")
            continue

        # Score by name similarity (token_sort_ratio handles word order differences)
        if tu_name:
            mode_stops["name_similarity"] = mode_stops["name"].apply(
                lambda n: cosine_similarity(_normalise_name(tu_name), _normalise_name(n)) * 100
            )
            name_filtered = mode_stops[mode_stops["name_similarity"] >= name_match_threshold]

            if not name_filtered.empty:
                mode_stops = name_filtered
            else:
                print(
                    f"  Warning: no name match (threshold={name_match_threshold}) "
                    f"for '{tu_name}' in {mode} stops, using distance only"
                )

        # Closest stop by distance
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