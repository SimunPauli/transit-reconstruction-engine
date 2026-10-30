"""
Synthetic-OD recall check of the choice-set generator.

Per OD: the production choice set (run_choice_set.trip_candidates + build_choice_set, config.json's
settings) vs an oracle that forces each route / route pair serving stops near origin and destination
(include filter), so alternatives Raptor prunes as dominated still come out. Reports which plausible
oracle alternatives the choice set misses, and whether a found itinerary dominates them.

Needs a running OTP (config.json's request.otp_url) serving a graph that covers DEPART; no TU data.
Run from tu_reconstruct_trips/:  .venv/bin/python choice_set_check/synthetic_od_check.py <out_dir>
Writes summary.csv, misses.csv and per OD its choice set, itineraries and production log into out_dir.
"""
import io
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from math import radians, cos, sqrt
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run_choice_set
from src import otp
from src.config_loader import get_config
from src.constant import STREET_MODES
from src.choice_set.generate_alternatives import build_choice_set, ALTERNATIVE_KEYS, _line_group_route

if len(sys.argv) != 2:
	sys.exit("Usage: .venv/bin/python choice_set_check/synthetic_od_check.py <out_dir>")
OUT = Path(sys.argv[1]).expanduser()
DEPART = pd.Timestamp("2025-09-16 07:30", tz="Europe/Copenhagen")  # a normal Tuesday
RADIUS_M = 700
ORACLE_MAX_CANDIDATES = 15
PLAUSIBLE_RATIO = 2.0
PLAUSIBLE_WALK_KM = 1.5
REPLACEMENT_BUSES = {"BLÅ", "GRØN", "GUL", "ORANGE", "PINK", "RØD", "LILLA", "BRUN"}

ODS = {
	"herlevhosp_norreport": ((55.7300, 12.4460), (55.6828, 12.5735)),
	"runddel_raadhus": ((55.6950, 12.5480), (55.6760, 12.5690)),
	"amagerbro_kgsnytorv": ((55.6630, 12.6000), (55.6800, 12.5870)),
	"valby_norreport": ((55.6655, 12.5130), (55.6828, 12.5735)),
	"koege_kbhh": ((55.4590, 12.1830), (55.6745, 12.5640)),
	"lyngby_dtu": ((55.7695, 12.5030), (55.7860, 12.5230)),
	"ballerup_norreport": ((55.7310, 12.3600), (55.6828, 12.5735)),
	"glostrup_kbhh": ((55.6645, 12.3990), (55.6745, 12.5640)),
	"frederiksberg_kgsnytorv": ((55.6825, 12.5300), (55.6800, 12.5870)),
	"roskilde_kbhh": ((55.6395, 12.0850), (55.6745, 12.5640)),
	"aarhush_skejby": ((56.1500, 10.2040), (56.1915, 10.1725)),
	"odense_sdu": ((55.4010, 10.3860), (55.3690, 10.4270)),
}


class ThreadStdout(io.TextIOBase):
	"""Per-thread print buffer, so parallel ODs' prints don't interleave."""
	local = threading.local()

	def __init__(self, real):
		self.real = real

	def write(self, text):
		buffer = getattr(self.local, "buffer", None)
		return (buffer or self.real).write(text)


def synthetic_trip(turid, orig, dest):
	"""A TU trip row and walk/bus/walk legs for an OD, as trip_candidates reads them."""
	row = pd.Series({"TurId": turid, "SessionId": turid, "orig_lat": orig[0], "orig_lon": orig[1],
		"tiladrlat": dest[0], "tiladrlon": dest[1], "depart_dt": DEPART, "arrival_dt": DEPART + pd.Timedelta("1h"),
		"depart_dt_str": str(DEPART), "arrival_dt_str": ""})
	deltur = pd.DataFrame({"TurId": turid, "Delturnr": [1, 2, 3], "StageMode": [1, 31, 1],
		"otp_mode": ["WALK", "BUS", "WALK"], "StageLength": [0.3, 5, 0.3], "StageDurationMin": [4, 20, 4],
		"StageWaitMin": [0, 2, 0], "Route": None, "FromStation": None, "ToStation": None})
	return row, deltur


def production(name, orig, dest, config):
	row, deltur = synthetic_trip(name, orig, dest)
	chosen_legs = pd.DataFrame({"leg_id": [0], "start_trip": DEPART, "end_trip": DEPART, "mode": "CAR",
		"distance_km": 0.0, "duration_min": 0})
	settings = run_choice_set.otp_settings(config)
	ThreadStdout.local.buffer = io.StringIO()
	try:
		candidates = run_choice_set.trip_candidates(row, deltur, settings, chosen_legs,
			config["choice_set"]["exclude_stops"], config["choice_set"]["exclude_routes"])
	finally:
		log = ThreadStdout.local.buffer.getvalue()
		ThreadStdout.local.buffer = None
	choice_set = build_choice_set(name, candidates, ALTERNATIVE_KEYS[config["choice_set"]["alternative_key"]],
		DEPART, chosen_legs, chosen_public_transport=False)
	return candidates, choice_set, log


def near_routes(lat, lon, url):
	"""{(mode, short name)} of routes at stops within RADIUS_M."""
	stops = otp.client.get_stops_by_bbox_query(lat, lon, RADIUS_M, url).json()["data"]["stopsByBbox"]
	routes = set()
	for stop in stops:
		dy = (stop["lat"] - lat) * 111320
		dx = (stop["lon"] - lon) * 111320 * cos(radians(lat))
		if sqrt(dx * dx + dy * dy) <= RADIUS_M:
			routes |= {(r["mode"], r["shortName"]) for r in stop["routes"] if r["shortName"]}
	# Rail replacement buses and night buses don't run in a weekday morning
	return {(mode, name) for mode, name in routes
		if name.upper() not in REPLACEMENT_BUSES and not (name.endswith("N") and name[:-1].isdigit())}


def oracle_query(orig, dest, route_set, config):
	request = config["request"]
	return otp.client.load_all_candidates(
		origin=otp.client.coordinate_location(*orig), destination=otp.client.coordinate_location(*dest),
		depart_dt=DEPART, access_mode="WALK", egress_mode="WALK",
		modes_json=[{"mode": mode} for mode in sorted({mode for mode, _ in route_set})],
		route_short_name=sorted({name for _, name in route_set}),
		search_window=config["choice_set"]["search_window"], max_itinerary_candidates=ORACLE_MAX_CANDIDATES,
		otp_url=request["otp_url"], request_timeout=request["request_timeout"])


def itinerary_table(legs, source):
	"""One row per itinerary: times, duration, transfers, walk, routes key, routes_stops key."""
	if legs.empty:
		return pd.DataFrame()
	rows = []
	for iteration_id, it in legs.sort_values(["iteration_id", "leg_id"]).groupby("iteration_id"):
		transit = it[~it["mode"].isin(STREET_MODES)]
		if transit.empty:
			continue
		start, end = pd.to_datetime(it["start_trip"].iloc[0]), pd.to_datetime(it["end_trip"].iloc[0])
		interlined = transit["interline_with_previous"].fillna(False).astype(bool).sum() if "interline_with_previous" in transit else 0
		rows.append({
			"source": source, "iteration_id": iteration_id,
			"exclude_round": it["exclude_round"].iloc[0] if "exclude_round" in it else None,
			"start": start, "end": end, "duration_min": (end - start).total_seconds() / 60,
			"transfers": len(transit) - 1 - interlined,
			"walk_km": it.loc[it["mode"] == "WALK", "distance_km"].sum(),
			"routes": " > ".join(f"{m}:{r}" for m, r in zip(transit["mode"], transit["route_short_name"])),
			"line_group": " > ".join(f"{m}:{_line_group_route(m, r) or r}" for m, r in zip(transit["mode"], transit["route_short_name"])),
			"routes_stops": " > ".join(f"{m}:{r}[{f}-{t}]" for m, r, f, t in zip(transit["mode"], transit["route_short_name"], transit["from"], transit["to"])),
		})
	return pd.DataFrame(rows)


def dominator(miss_instances, prod):
	"""A production itinerary dominating every instance of a missed alternative on departure (later),
	arrival (earlier) and transfers (fewer or equal), else None."""
	found = None
	for _, m in miss_instances.iterrows():
		dom = prod[(prod["start"] >= m["start"]) & (prod["end"] <= m["end"]) & (prod["transfers"] <= m["transfers"])]
		if dom.empty:
			return None
		found = found or dom.sort_values("duration_min")["routes"].iloc[0]
	return found


def main():
	config = get_config()
	url = config["request"]["otp_url"]
	sys.stdout = ThreadStdout(sys.stdout)
	OUT.mkdir(parents=True, exist_ok=True)

	route_sets = {}
	for name, (orig, dest) in ODS.items():
		o, d = near_routes(*orig, url), near_routes(*dest, url)
		sets = {frozenset([r]) for r in o & d} | {frozenset([r1, r2]) for r1 in o for r2 in d if r1 != r2}
		route_sets[name] = sorted(sets, key=lambda s: sorted(s))
		print(f"{name}: {len(o)} origin routes, {len(d)} destination routes, {len(sets)} oracle queries", flush=True)

	with ThreadPoolExecutor(max_workers=config["run"]["max_workers"]) as pool:
		prod_futures = {name: pool.submit(production, name, *od, config) for name, od in ODS.items()}
		oracle_futures = {name: [(s, pool.submit(oracle_query, *ODS[name], s, config)) for s in sets]
			for name, sets in route_sets.items()}

		summaries, misses = [], []
		for name in ODS:
			candidates, choice_set, log = prod_futures[name].result()
			(OUT / f"{name}_production.log").write_text(log)
			choice_set.to_csv(OUT / f"{name}_choice_set.csv", index=False)
			prod_raw = itinerary_table(candidates, "production")
			cs = itinerary_table(choice_set[choice_set["alt_id"] > 0].rename(columns={"alt_id": "iteration_id"}), "choice_set") \
				if (choice_set["alt_id"] > 0).any() else pd.DataFrame(columns=["routes", "line_group"])
			oracle_legs, errors, offset = [], 0, 0
			for s, future in oracle_futures[name]:
				try:
					legs = future.result()
				except Exception:
					errors += 1
					continue
				if not legs.empty:
					ids = legs["iteration_id"] - legs["iteration_id"].min() + offset
					offset = ids.max() + 1
					oracle_legs.append(legs.assign(iteration_id=ids))
			oracle = itinerary_table(pd.concat(oracle_legs, ignore_index=True), "oracle") if oracle_legs else pd.DataFrame()
			pool_its = pd.concat([prod_raw, oracle], ignore_index=True)
			pool_its.to_csv(OUT / f"{name}_itineraries.csv", index=False)

			best = pool_its.sort_values("duration_min").drop_duplicates("routes")
			t_min = best.loc[best["walk_km"] <= PLAUSIBLE_WALK_KM, "duration_min"].min()
			best = best.assign(ratio=best["duration_min"] / t_min)
			plausible = best[(best["ratio"] <= PLAUSIBLE_RATIO) & (best["walk_km"] <= PLAUSIBLE_WALK_KM)]
			cs_routes, cs_groups = set(cs["routes"]), set(cs["line_group"])
			plausible = plausible.assign(found=plausible["routes"].isin(cs_routes),
				group_found=plausible["line_group"].isin(cs_groups))
			first_round = prod_raw.groupby("routes")["exclude_round"].min() if not prod_raw.empty else pd.Series(dtype=float)
			for _, alt in plausible[~plausible["found"]].iterrows():
				instances = oracle[oracle["routes"] == alt["routes"]] if not oracle.empty else pd.DataFrame()
				misses.append({"od": name, "routes": alt["routes"], "duration_min": alt["duration_min"],
					"ratio": round(alt["ratio"], 2), "transfers": alt["transfers"], "walk_km": round(alt["walk_km"], 2),
					"sibling_found": alt["group_found"],
					"dominated_by": dominator(instances, prod_raw) if not instances.empty and not prod_raw.empty else None})
			summaries.append({"od": name, "t_min": t_min, "choice_set_alts": len(cs),
				"oracle_queries": len(route_sets[name]), "oracle_errors": errors,
				"plausible": len(plausible), "found": int(plausible["found"].sum()),
				"found_or_sibling": int((plausible["found"] | plausible["group_found"]).sum()),
				"plausible_le_1.3": int((plausible["ratio"] <= 1.3).sum()),
				"found_le_1.3": int((plausible["found"] & (plausible["ratio"] <= 1.3)).sum()),
				"found_round0": int(plausible["found"].where(plausible["routes"].map(first_round).eq(0), False).sum())})
			print(f"done {name}", flush=True)

	summary, misses = pd.DataFrame(summaries), pd.DataFrame(misses)
	summary.to_csv(OUT / "summary.csv", index=False)
	misses.to_csv(OUT / "misses.csv", index=False)
	pd.set_option("display.width", 250)
	pd.set_option("display.max_colwidth", 80)
	print(summary.to_string(index=False))
	print(misses.sort_values(["od", "ratio"]).to_string(index=False))


if __name__ == "__main__":
	main()
