"""
Why choice sets miss the chosen itinerary. One row per trip of a public-transport choice-set run:
its coverage (0/1) and features of the chosen itinerary and how reconstruction found it, plus each
uncovered trip's likely cause (the first of CAUSE_ORDER that applies). Also the coverage share per
feature group, and for "other_stops" (access/egress) and "other_transfer" trips which stops differ from
the closest same-routes alternative.

Reads the choice-set run's public_choice_set_file and the trip_matching_summaries_file of the
reconstruction run it took the chosen trips from (its log's "Chosen trips from:" line). Writes
coverage_table.csv, coverage_by_feature.csv and other_stops_table.csv to the choice-set run's folder.

Run from any directory:  .venv/bin/python analyse/choice_set_coverage.py [choice-set run dir] (e.g. /home/simpal/O/TU_Rejseplan/Output/2025/choice_set/20261116_144144)
Without a run dir: the newest choice-set run of config.json's tu_subset.year.
"""
import ast
import os
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)  # src.constant reads config.json from the working directory
from src.config_loader import load_config
from src.otp.client import STREET_MODES
from src.otp.parser import decode_polyline

WORSE_DURATION_RATIO = 1.25  # chosen counts as slower than the best alternative from this ratio
GAP_BINS = [0, 15, 30, 60, 90, float("inf")]  # |chosen departure - TU departure|, minutes
CAUSE_ORDER = ["no_alternatives", "anchored", "outside_window", "walks_past_direct", "reluctance_retry",
	"near_route", "other_stops", "other_transfer", "worse_than_best"]
STOP_CAUSES = ["other_stops", "other_transfer"]
TRANSFER_BINS = [-0.5, 0.5, 1.5, float("inf")]  # chosen transfers: 0, 1, 2+
WALK_BINS = [0, 0.5, 1, 1.5, float("inf")]  # chosen walk, km
DIRECT_WALK_MARGIN_KM = 0.01  # trip_candidates' margin for dropping itineraries by direct walk
TU_STATION_MODES = {"S_TRAIN", "RAIL", "SUBWAY"}  # TU records their stations (StageMode 32-34)
STOP_KM_BINS = [0, 0.05, 0.2, 0.5, 1, float("inf")]  # chosen's stop to the alternative's, km
TABLE_FILE = "coverage_table.csv"
BY_FEATURE_FILE = "coverage_by_feature.csv"
OTHER_STOPS_FILE = "other_stops_table.csv"


def newest_choice_set_run(config):
	runs = Path(config["paths"]["output_dir"]).expanduser() / str(config["tu_subset"]["year"]) / "choice_set"
	file = config["choice_set"]["paths"]["public_choice_set_file"]
	# run_id is a sortable timestamp
	for run_dir in sorted(runs.iterdir(), reverse=True):
		if (run_dir / file).is_file():
			return run_dir
	raise FileNotFoundError(f"No run in {runs} contains {file}")


def reconstruct_run_dir(log_path):
	"""The reconstruction run a choice-set run took its chosen trips from, as its log records."""
	match = re.search(r"^Chosen trips from: (.+)$", log_path.read_text(encoding="utf-8"), re.MULTILINE)
	if not match:
		raise ValueError(f"No 'Chosen trips from:' line in {log_path}")
	return Path(match.group(1).strip()).parent


def alternative_stats(choice_sets):
	"""Per (TurId, alt_id): transfers, door-to-door duration in minutes and walk distance."""
	legs = choice_sets.assign(transit=~choice_sets["mode"].isin(STREET_MODES),
		walk_km=choice_sets["distance_km"].where(choice_sets["mode"] == "WALK", 0))
	alts = legs.groupby(["TurId", "alt_id"]).agg(
		transit_legs=("transit", "sum"), start=("start_trip", "first"), end=("end_trip", "first"),
		walk_km=("walk_km", "sum"))
	alts["transfers"] = alts["transit_legs"] - 1
	duration = pd.to_datetime(alts["end"], utc=True) - pd.to_datetime(alts["start"], utc=True)
	alts["duration_min"] = duration.dt.total_seconds() / 60
	return alts[["transfers", "duration_min", "walk_km"]]


def chosen_routes_found(choice_sets):
	"""Per TurId: "all", "some" or "none" of the chosen itinerary's transit routes used by an alternative."""
	transit = choice_sets[~choice_sets["mode"].isin(STREET_MODES)]
	route = list(zip(transit["TurId"], transit["mode"], transit["route_short_name"]))
	chosen = (transit["alt_id"] == 0).to_numpy()
	alt_routes = {r for r, is_chosen in zip(route, chosen) if not is_chosen}
	found = pd.Series([r in alt_routes for r, is_chosen in zip(route, chosen) if is_chosen],
		index=transit["TurId"][chosen])
	share = found.groupby(level=0).mean()
	return pd.Series(np.select([share == 1, share == 0], ["all", "none"], "some"), index=share.index)


def leg_points(geometry):
	"""
	leg_geometry as (lat, lon) tuples, [] if missing or unreadable. Runs before leg_geometry became an
	encoded polyline wrote it as a list of (lat, lon) tuples.
	"""
	if not isinstance(geometry, str):
		return []
	try:
		points = ast.literal_eval(geometry) if geometry.startswith("[") else decode_polyline(geometry)
		return [tuple(point) for point in points]
	except (IndexError, ValueError, SyntaxError):
		return []


def _end_point(geometry, point):
	"""geometry's first (point 0) or last (-1) (lat, lon); None if missing or unreadable."""
	points = leg_points(geometry)
	return points[point] if points else None


def _km(a, b):
	"""Great-circle km between two (lat, lon) points, NaN if either is missing."""
	if not a or not b:
		return np.nan
	(lat1, lon1), (lat2, lon2) = np.radians(a), np.radians(b)
	h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
	return 2 * 6371 * np.arcsin(np.sqrt(h))


def other_stops_differences(choice_sets, trip_ids):
	"""
	For trip_ids, one row per stop where the chosen itinerary differs (by name, as alternative_key compares)
	from its same-routes alternative (route_group 0) with the fewest such stops: position (access: first
	boarding, egress: last alighting, else transfer), side (board/alight), the leg's mode, both stop
	names, the km between them (from leg_geometry's ends) and n_differing, the trip's count of them.
	"""
	transit = choice_sets[choice_sets["TurId"].isin(trip_ids) & ~choice_sets["mode"].isin(STREET_MODES)
		& choice_sets["route_group"].eq(0)].sort_values(["TurId", "alt_id", "leg_id"])
	rows = []
	for tur_id, trip in transit.groupby("TurId"):
		chosen = trip[trip["alt_id"] == 0].to_dict("records")
		best = None
		for alt_id, alt in trip[trip["alt_id"] != 0].groupby("alt_id"):
			diffs = []
			for i, (c, a) in enumerate(zip(chosen, alt.to_dict("records"))):
				for side, column, point, end in (("board", "from", 0, 0), ("alight", "to", -1, len(chosen) - 1)):
					if c[column] == a[column]:
						continue
					diffs.append(dict(TurId=tur_id, alt_id=alt_id,
						position=("access" if side == "board" else "egress") if i == end else "transfer",
						side=side, mode=c["mode"], chosen_stop=c[column], alt_stop=a[column],
						stop_km=_km(_end_point(c["leg_geometry"], point), _end_point(a["leg_geometry"], point))))
			if diffs and (best is None or len(diffs) < len(best)):
				best = diffs
		rows += [dict(row, n_differing=len(best)) for row in best or []]
	table = pd.DataFrame(rows, columns=["TurId", "alt_id", "position", "side", "mode", "chosen_stop", "alt_stop",
		"stop_km", "n_differing"])
	table["tu_station"] = table["mode"].isin(TU_STATION_MODES)
	return table


def coverage_table(choice_sets, summaries, window_min, differences):
	"""differences: other_stops_differences of the uncovered trips."""
	alts = alternative_stats(choice_sets)
	chosen = alts.xs(0, level="alt_id")
	others = alts.drop(index=0, level="alt_id").groupby(level="TurId")

	trips = choice_sets.groupby("TurId").agg(
		coverage=("coverage", "first"), trip_near_route=("trip_near_route", "first"),
		n_alternatives=("alt_id", "max"), direct_walk_km=("direct_walk_km", "first"))
	trips = trips.join(summaries.reindex(
		columns=["route_match", "used_anchor_fallback", "reluctance_retry", "depart_deviation_min"]))
	trips["depart_gap_min"] = trips.pop("depart_deviation_min").abs()
	trips["chosen_transfers"] = chosen["transfers"]
	trips["min_alt_transfers"] = others["transfers"].min()
	trips["chosen_duration_min"] = chosen["duration_min"].round(1)
	trips["best_alt_duration_min"] = others["duration_min"].min().round(1)
	trips["duration_vs_best"] = (trips["chosen_duration_min"] / trips["best_alt_duration_min"]).round(2)
	trips["chosen_walk_km"] = chosen["walk_km"].round(3)
	# Itineraries like the chosen are dropped by the choice-set search's direct-walk filter
	trips["walks_past_direct"] = trips["chosen_walk_km"] + DIRECT_WALK_MARGIN_KM >= trips["direct_walk_km"]
	# An alternative with the chosen's routes but other stops: covered under alternative_key "routes"
	trips["same_routes_alt"] = (choice_sets["route_group"].eq(0) & choice_sets["alt_id"].ne(0)).groupby(choice_sets["TurId"]).any()
	# Coverage with other_stops and other_transfer trips counted as covered
	trips["coverage_routes"] = (trips["coverage"].eq(1) | trips["same_routes_alt"]).astype(int)
	# Where the closest same-routes alternative differs: at any transfer stop, else access/egress only
	transfer = differences["position"].eq("transfer").groupby(differences["TurId"]).any()
	trips["same_routes_differ_at"] = transfer.map({True: "transfer", False: "access_egress"})
	trips["chosen_routes_found"] = chosen_routes_found(choice_sets)

	causes = {
		"no_alternatives": trips["n_alternatives"] == 0,
		"anchored": trips["used_anchor_fallback"] == True,
		"outside_window": trips["depart_gap_min"] > window_min,
		"walks_past_direct": trips["walks_past_direct"],
		"reluctance_retry": trips["reluctance_retry"].notna(),
		"near_route": trips["trip_near_route"] == 1,
		"other_stops": trips["same_routes_differ_at"] == "access_egress",
		"other_transfer": trips["same_routes_differ_at"] == "transfer",
		"worse_than_best": (trips["chosen_transfers"] > trips["min_alt_transfers"])
			| (trips["duration_vs_best"] >= WORSE_DURATION_RATIO),
	}
	trips["likely_cause"] = np.select([causes[name] for name in CAUSE_ORDER], CAUSE_ORDER, default="unexplained")
	trips.loc[trips["coverage"] == 1, "likely_cause"] = ""
	return trips


def coverage_by_feature(trips):
	"""Trips, uncovered trips and coverage share (also as coverage_routes) per group of each feature."""
	features = {
		"trip_near_route": trips["trip_near_route"],
		"route_match": trips["route_match"].fillna("no bus/S-train leg"),
		"used_anchor_fallback": trips["used_anchor_fallback"],
		"reluctance_retry": trips["reluctance_retry"].notna().map({True: "retry", False: "baseline"}),
		"depart_gap_min": pd.cut(trips["depart_gap_min"], GAP_BINS, include_lowest=True),
		"walks_past_direct": trips["walks_past_direct"].where(trips["direct_walk_km"].notna(), "no direct walk"),
		"chosen_more_transfers": trips["chosen_transfers"] > trips["min_alt_transfers"],
		"duration_vs_best": pd.cut(trips["duration_vs_best"], [0, 1, WORSE_DURATION_RATIO, float("inf")]),
		"chosen_transfers": pd.cut(trips["chosen_transfers"], TRANSFER_BINS, labels=["0", "1", "2+"]),
		"chosen_walk_km": pd.cut(trips["chosen_walk_km"], WALK_BINS, include_lowest=True),
		"same_routes_alt": trips["same_routes_alt"],
		"chosen_routes_found": trips["chosen_routes_found"].fillna("no transit leg"),
	}
	rows = []
	for name, values in features.items():
		grouped = trips[["coverage", "coverage_routes"]].groupby(values.astype(str), dropna=False)
		rows.append(pd.DataFrame({
			"feature": name,
			"trips": grouped.size(),
			"uncovered": grouped["coverage"].apply(lambda coverage: (coverage == 0).sum()),
			"coverage_share": grouped["coverage"].mean().round(3),
			"coverage_share_routes": grouped["coverage_routes"].mean().round(3),
		}).rename_axis("group").reset_index())
	return pd.concat(rows, ignore_index=True)[["feature", "group", "trips", "uncovered", "coverage_share",
		"coverage_share_routes"]]


def main():
	config = load_config()
	run_dir = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else newest_choice_set_run(config)
	run_config = load_config(run_dir / "config.json")
	recon_dir = reconstruct_run_dir(run_dir / run_config["choice_set"]["paths"]["log_file"])
	recon_config = load_config(recon_dir / "config.json")
	print(f"Choice-set run: {run_dir}\nReconstruction run: {recon_dir}")

	choice_sets = pd.read_csv(run_dir / run_config["choice_set"]["paths"]["public_choice_set_file"])
	summaries = pd.read_excel(recon_dir / recon_config["reconstruct"]["paths"]["trip_matching_summaries_file"])
	if "reluctance_retry" not in summaries.columns:
		print("Note: the reconstruction run predates reluctance_retry, so that column is empty")
	if "direct_walk_km" not in choice_sets.columns:
		print("Note: the choice-set run predates direct_walk_km, so that column is empty")
		choice_sets["direct_walk_km"] = np.nan
	window_min = pd.Timedelta(run_config["choice_set"]["search_window"]).total_seconds() / 60

	coverage = choice_sets.groupby("TurId")["coverage"].first()
	differences = other_stops_differences(choice_sets, coverage.index[coverage == 0])
	trips = coverage_table(choice_sets, summaries.set_index("TurId"), window_min, differences)
	by_feature = coverage_by_feature(trips)
	trips.to_csv(run_dir / TABLE_FILE)
	by_feature.to_csv(run_dir / BY_FEATURE_FILE, index=False)

	uncovered = trips[trips["coverage"] == 0]
	print(f"\n{len(uncovered)} of {len(trips)} trips uncovered ({len(uncovered) / len(trips):.1%})")
	print(f"Coverage share {trips['coverage'].mean():.1%}, {trips['coverage_routes'].mean():.1%} with other_stops "
		f"and other_transfer covered (coverage_routes)")
	print(f"\nLikely cause (first that applies, in order {', '.join(CAUSE_ORDER)}):")
	print(uncovered["likely_cause"].value_counts().to_string())
	print("\nCoverage by feature:")
	print(by_feature.to_string(index=False))

	differences.insert(1, "likely_cause", differences["TurId"].map(trips["likely_cause"]))
	other_stops = differences[differences["likely_cause"].isin(STOP_CAUSES)]
	other_stops.to_csv(run_dir / OTHER_STOPS_FILE, index=False)
	per_trip = other_stops.drop_duplicates("TurId")
	print(f"\n{' and '.join(STOP_CAUSES)} trips vs their closest same-routes alternative ({len(per_trip)} trips, {len(other_stops)} differing stops)")
	print("\nTrips by differing stops:")
	print(pd.crosstab(per_trip["n_differing"].clip(upper=3).map({1: "1", 2: "2", 3: "3+"}), per_trip["likely_cause"],
		margins=True).to_string())
	print("\nDiffering stops by position and mode (tu_station: S_TRAIN/RAIL/SUBWAY, stations known from TU):")
	print(pd.crosstab([other_stops["position"], other_stops["side"]], [other_stops["tu_station"], other_stops["mode"]],
		margins=True).to_string())
	print("\nDiffering stops by km between them:")
	stop_km = pd.cut(other_stops["stop_km"], STOP_KM_BINS, include_lowest=True)
	print(pd.crosstab(stop_km.cat.add_categories("no valid geometry").fillna("no valid geometry"),
		other_stops["position"], margins=True).to_string())
	print("\nTrips with one differing stop, by position and mode:")
	print(other_stops[other_stops["n_differing"] == 1].groupby(["position", "mode"]).size().to_string())
	print(f"\nWrote {TABLE_FILE}, {BY_FEATURE_FILE} and {OTHER_STOPS_FILE} to {run_dir}")


if __name__ == "__main__":
	main()
