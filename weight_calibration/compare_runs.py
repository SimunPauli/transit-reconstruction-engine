"""
Which trips got a different itinerary between two runs, e.g. with different squared_error_weights.

Each trip's transit legs are joined into two signatures, compared per TurId:
- route: mode, route and boarding/alighting stop per leg. A change is a different route or transfer.
- departure: the route signature plus train number and leg times. A change with the same route
  is a different departure of the same route.
Street legs are left out: with the same graph they follow from the transit legs.

Only trips found (trip_found) in both runs are compared. A trip's status (outcome and route match
level) is decided before the weights rank the candidates, so status changes should be 0 between runs
that differ only in weights; any are listed, as they point to e.g. OTP timeouts or a changed config.

Run from tu_reconstruct_trips/:  .venv/bin/python weight_calibration/compare_runs.py <run_dir_a> <run_dir_b>
Writes compare_<run_id_a>_vs_<run_id_b>.xlsx into run_dir_b.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "Europe/Copenhagen"
PRINT_ROWS = 30  # longer tables are cut in the printout, not in the xlsx
SUMMARY_COLS = ["rmse", "depart_deviation_min", "arrival_deviation_min"]
TEXT_COLS = ["mode", "route_gtfs_id", "from_gtfs_id", "to_gtfs_id", "trip_short_name", "interlined_trip_short_names"]
NEAR_ROUTE_LEVELS = ["sibling", "letter", "digit"]  # route_match levels counted as trip_near_route

# Printed above each table
TABLE_DESCRIPTIONS = {
	"summary": "Trips per change level. Route/departure/unchanged count trips found in both runs; status changes\n"
			   "should be 0 for runs that differ only in weights.",
	"changed_trips": "Trips found in both runs with a different route/transfer or departure, with both runs'\n"
					 "transit leg signatures (mode route from>to, then #train number and leg times) and RMSE components.",
	"status_changes": "Trips whose outcome or route match level differs between the runs, or that are in one run only.",
}


def run_file(run_dir, key):
	"""Path of a run output file, named as in the run's own config.json (or the current one for older runs)."""
	config_path = run_dir / "config.json"
	if not config_path.is_file():
		config_path = Path("config.json")
	with open(config_path, encoding="utf-8") as file:
		return run_dir / json.load(file)["paths"][key]


def load_run(run_dir):
	# As text, so a train number isn't read as e.g. 1234.0
	matches = pd.read_csv(run_file(run_dir, "rmse_based_matches_file"), dtype={col: str for col in TEXT_COLS})
	summaries = pd.read_excel(run_file(run_dir, "trip_matching_summaries_file"))
	# From route_match rather than the outcome columns, whose meaning changed between runs
	summaries["status"] = np.select(
		[summaries["trip_not_found"] == 1,
		 summaries["route_match"].isin(NEAR_ROUTE_LEVELS),
		 summaries["route_match"] == "ignored"],
		["trip_not_found", "trip_near_route", "trip_wrong_route"], default="trip_found",
	)
	# The route match level is set before ranking too, so it's part of the status
	summaries["status"] = summaries["status"] + summaries["route_match"].map(
		lambda level: f" ({level})" if pd.notna(level) else "")
	return matches.astype({"TurId": "Int64"}), summaries.astype({"TurId": "Int64"})


def signatures(matches):
	"""Route and departure signature per TurId, from its transit legs (the ones with a route) in leg order."""
	legs = matches[matches["route_gtfs_id"].notna()].sort_values(["TurId", "leg_id"])
	legs[TEXT_COLS] = legs[TEXT_COLS].fillna("")
	clock = lambda ms: pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert(TZ).dt.strftime("%H:%M")
	train = legs["trip_short_name"] + legs["interlined_trip_short_names"]
	legs["route_leg"] = legs["mode"] + " " + legs["route_gtfs_id"] + " " + legs["from_gtfs_id"] + ">" + legs["to_gtfs_id"]
	legs["departure_leg"] = (legs["route_leg"] + (" #" + train).where(train != "", "")
							 + " " + clock(legs["start_leg"]) + "-" + clock(legs["end_leg"]))
	return legs.groupby("TurId").agg(route=("route_leg", " | ".join), departure=("departure_leg", " | ".join))


def compare(run_a, run_b):
	status = run_a[1][["TurId", "status", *SUMMARY_COLS]].merge(
		run_b[1][["TurId", "status", *SUMMARY_COLS]], on="TurId", how="outer", suffixes=("_a", "_b"))
	status = status.fillna({"status_a": "not in run", "status_b": "not in run"})
	status_changes = status.loc[status["status_a"] != status["status_b"], ["TurId", "status_a", "status_b"]]

	found_ids = status.loc[(status["status_a"].str.startswith("trip_found")) & (status["status_a"] == status["status_b"]), "TurId"]
	trips = (signatures(run_a[0]).join(signatures(run_b[0]), lsuffix="_a", rsuffix="_b", how="inner")
			 .loc[lambda df: df.index.isin(found_ids)])
	trips["change"] = np.select(
		[trips["route_a"] != trips["route_b"], trips["departure_a"] != trips["departure_b"]],
		["route/transfer", "departure only"], default="unchanged",
	)

	summary = (trips["change"].value_counts()
			   .reindex(["route/transfer", "departure only", "unchanged"], fill_value=0)
			   .rename_axis("change").reset_index(name="trips"))
	summary = pd.concat([summary, pd.DataFrame([{"change": "status change", "trips": len(status_changes)}])], ignore_index=True)
	summary["pct_of_compared"] = (100 * summary["trips"] / len(trips)).round(1)
	summary.loc[summary["change"] == "status change", "pct_of_compared"] = np.nan

	changed = (trips[trips["change"] != "unchanged"].reset_index()
			   .merge(status[["TurId", *[f"{col}_{run}" for run in "ab" for col in SUMMARY_COLS]]], on="TurId", how="left")
			   .sort_values(["change", "TurId"]))
	changed = changed[["TurId", "change", "route_a", "route_b", "departure_a", "departure_b",
					   *[f"{col}_{run}" for col in SUMMARY_COLS for run in "ab"]]]

	return {"summary": summary, "changed_trips": changed, "status_changes": status_changes}


def main(run_dir_a, run_dir_b):
	print(f"Run A: {run_dir_a}\nRun B: {run_dir_b}", flush=True)
	tables = compare(load_run(run_dir_a), load_run(run_dir_b))

	with pd.option_context("display.width", 250, "display.max_colwidth", 80):
		for name, table in tables.items():
			cut = f" (first {PRINT_ROWS} of {len(table)} rows)" if len(table) > PRINT_ROWS else ""
			print(f"\n=== {name}{cut} ===\n{TABLE_DESCRIPTIONS[name]}\n\n{table.head(PRINT_ROWS).to_string(index=False)}")

	out_path = run_dir_b / f"compare_{run_dir_a.name}_vs_{run_dir_b.name}.xlsx"
	with pd.ExcelWriter(out_path) as writer:
		for name, table in tables.items():
			table.to_excel(writer, sheet_name=name, index=False)
	print(f"\nWritten to {out_path}")


if __name__ == "__main__":
	if len(sys.argv) != 3:
		sys.exit("Usage: .venv/bin/python weight_calibration/compare_runs.py <run_dir_a> <run_dir_b>")
	run_dirs = [Path(arg).expanduser() for arg in sys.argv[1:]]
	for run_dir in run_dirs:
		if not run_dir.is_dir():
			sys.exit(f"Run directory not found: {run_dir.resolve()}\nGive the full path, <output_dir>/<year>/<run_id>")
	main(*run_dirs)
