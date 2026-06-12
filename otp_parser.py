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
        "route_short_name",
        "distance_km",
        "duration_min",
        "waiting_time_min",
        "from",
        "to",

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
            rows.append({
                "iteration_id": j,
                "leg_id": k,

                "start_trip": node["start"],
                "end_trip": node["end"],
                "start_leg": leg["startTime"],
                "end_leg": leg["endTime"],

                "mode": leg["mode"],
                "route_short_name": route.get("shortName"),
                "distance_km": round(leg["distance"]/1000,3),
                "duration_min": int(round(leg["duration"]/60,0)),
                "waiting_time_min": round((node.get("waitingTime_total") or 0)/60,2),
                "from": leg["from"]["name"],
                "to": leg["to"]["name"],

                "trip_short_name": trip.get("tripShortName"),
                "generalized_cost": leg["generalizedCost"],
                "leg_geometry": decode_polyline(leg["legGeometry"]["points"]),
                "system_notice_tag": system_notice_tag,
                "system_notice_text": system_notice_text
            })
    df = pd.DataFrame(rows, columns=columns)
    return df