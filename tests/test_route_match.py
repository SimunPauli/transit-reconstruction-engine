"""
Synthetic end-to-end test of the route_match stages (exact -> letter -> digit -> ignored),
with OTP mocked out. Needs config.json (for the RMSE weights), no OTP server or TU data.

Run from tu_reconstruct_trips/:  python tests/test_route_match.py
"""
import contextlib
import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import otp
from src.matching import tu_otp_matching
from src.matching.candidate_search import SearchSettings
from src.matching.route_matching_utils import build_route_name_index

TZ = "Europe/Copenhagen"
DEPART = pd.Timestamp("2024-05-01 08:00", tz=TZ)
ms = lambda minutes: int((DEPART + pd.Timedelta(minutes=minutes)).tz_convert("UTC").timestamp() * 1000)

# Every bus route in the fake feed (the index OTP's route list would build)
FEED_BUS_ROUTES = ["191", "193", "171", "161", "150S", "6A", "200", "5C"]
ROUTE_INDEX = {"BUS": build_route_name_index(FEED_BUS_ROUTES), "S_TRAIN": build_route_name_index([])}

FAKE_NETWORK = []   # itineraries, each a list of bus routes ridden in order
query_log = []

def fake_load_all_candidates(route_short_name=None, **_):
	"""Return the fake network's itineraries that OTP's include filter would let through."""
	query_log.append(sorted(route_short_name) if route_short_name else None)
	rows = []
	for iteration_id, bus_routes in enumerate(FAKE_NETWORK):
		if route_short_name is not None and not set(bus_routes) <= set(route_short_name):
			continue
		legs = [("WALK", None, 0, 5, 0.4)]
		t = 5
		for route in bus_routes:
			legs.append(("BUS", route, t, t + 10, 5.0))
			t += 10
		legs.append(("WALK", None, t, t + 5, 0.4))
		for leg_id, (mode, route, start, end, km) in enumerate(legs):
			rows.append({
				"iteration_id": iteration_id, "leg_id": leg_id,
				"start_trip": ms(0), "end_trip": ms(t + 5),
				"start_leg": ms(start), "end_leg": ms(end),
				"mode": mode, "interline_with_previous": False,
				"route_short_name": route, "route_gtfs_id": None,
				"distance_km": km, "duration_min": end - start,
				"from": "A", "to": "B", "from_gtfs_id": None, "to_gtfs_id": None,
				"trip_short_name": None, "generalized_cost": 0, "leg_geometry": None,
				"system_notice_tag": [], "system_notice_text": [],
				"interlined_route_short_names": [], "interlined_trip_short_names": [],
			})
	return pd.DataFrame(rows)

otp.client.load_all_candidates = fake_load_all_candidates

def run_case(tu_routes, network):
	"""Match one TU trip of bus legs (10 min, 5 km each) against the given fake network."""
	global FAKE_NETWORK
	FAKE_NETWORK = network
	query_log.clear()
	total = 10 + 10 * len(tu_routes)
	tu_tur_row = pd.Series({
		"TurId": 1, "SessionId": 1, "orig_lat": 55.0, "orig_lon": 12.0,
		"tiladrlat": 55.1, "tiladrlon": 12.1,
		"depart_dt": DEPART, "arrival_dt": DEPART + pd.Timedelta(minutes=total),
		"depart_dt_str": "08:00", "arrival_dt_str": "",
	})
	tu_deltur = pd.DataFrame([{
		"TurId": 1, "Delturnr": i + 1, "StageMode": 31, "otp_mode": "BUS",
		"StageLength": 5.0, "StageWaitMin": 0, "StageDurationMin": 10,
		"Route": route, "FromStation": None, "ToStation": None,
	} for i, route in enumerate(tu_routes)])
	with contextlib.redirect_stdout(io.StringIO()) as log:
		result_df, summary = tu_otp_matching.match_tu_trip_to_otp(
			tu_tur_row, tu_deltur,
			otp_mode_routes_cache={}, otp_route_name_index=ROUTE_INDEX,
			tu_gtfs_station_df=pd.DataFrame(columns=["otp_mode", "tu_station_name", "gtfs_station_id", "gtfs_station_name"]),
			anchor_station_lookup={},
			settings=SearchSettings(otp_url="", search_window="PT30M", max_itinerary_candidates=10),
			print_deviation_details=False,
			return_trip_summary=True,
		)
	used = None if result_df is None else result_df.loc[result_df["mode"] == "BUS", "route_short_name"].tolist()
	stage_lines = [line for line in log.getvalue().splitlines() if line.startswith("ROUTE_MATCH")]
	return summary, used, stage_lines

CASES = [
	# (description, TU routes, fake network itineraries, expected route_match, expected bus routes used)
	("exact", ["191"], [["191"], ["193"]], "exact", ["191"]),
	("letter", ["150"], [["150S"]], "letter", ["150S"]),
	("digit 192->191", ["192"], [["191"]], "digit", ["191"]),
	("digit 171->161", ["171"], [["161"]], "digit", ["161"]),
	("digit + letter 5->6A", ["5"], [["6A"]], "digit", ["6A"]),
	("no carry 199->200", ["199"], [["200"]], "ignored", ["200"]),
	("two buses, 2nd wrong", ["191", "172"], [["191", "171"]], "digit", ["191", "171"]),
	("two buses, both 1 off", ["192", "172"], [["193", "171"]], "digit", ["193", "171"]),
	("two buses, 1st 2 off", ["191", "172"], [["193", "171"]], "ignored", ["193", "171"]),
]

def main():
	failures = 0
	for description, tu_routes, network, expected_level, expected_used in CASES:
		summary, used, stage_lines = run_case(tu_routes, network)
		ok = summary["route_match"] == expected_level and used == expected_used
		failures += not ok
		print(f"{'PASS' if ok else 'FAIL'}  {description:28s} route_match={summary['route_match']!s:8s} "
		      f"used={used} found={summary['trip_found']} near_route={summary['trip_near_route']} wrong_route={summary['trip_wrong_route']}")
		print(f"      OTP queries (routeShortNames): {query_log}")
		for line in stage_lines:
			print(f"      {line}")
	print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
	return failures

if __name__ == "__main__":
	sys.exit(1 if main() else 0)
