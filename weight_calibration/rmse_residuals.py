"""
Spread of the RMSE components among a finished run's best matches, for setting squared_error_weights.

A weight acts as 1 / (expected deviation)², so the spreads give the weights' ratios. For street
distance it also fits spread = c * TU distance^p per mode, to see how the error grows with leg
length. Trips whose departure or arrival is off by more than LARGE_DEVIATION_MIN are listed, as
likely TU time errors. Only trips found with the TU-recorded route (trip_found == 1) are used.

The spreads are optimistic: the matches were picked by minimising these same deviations.

Run from tu_reconstruct_trips/:  .venv/bin/python weight_calibration/rmse_residuals.py <output_dir>/<year>/<run_id>
Writes rmse_residuals.xlsx into that run directory.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src import config_loader

if len(sys.argv) != 2:
	sys.exit("Usage: .venv/bin/python weight_calibration/rmse_residuals.py <output_dir>/<year>/<run_id>")
RUN_DIR = Path(sys.argv[1]).expanduser()
try:
	run_dir_exists = RUN_DIR.is_dir()
except OSError as error:  # e.g. a dropped network share
	sys.exit(f"Can't access {RUN_DIR}: {error}\nCheck that the share is mounted, e.g. with ls on the folder.")
if not run_dir_exists:
	sys.exit(f"Run directory not found: {RUN_DIR.resolve()}\nGive the full path, <output_dir>/<year>/<run_id>")

# Before the other src imports: they read get_config() at import time (src.constant).
# The run's own config, so TU files, weights and constants match the run; older runs have none.
RUN_CONFIG = RUN_DIR / "config.json"
CONFIG_PATH = RUN_CONFIG if RUN_CONFIG.is_file() else Path("config.json")
config_loader._config = config_loader.load_config(CONFIG_PATH)

from src.tu import load_TU_data
from src.matching.best_otp_candidate import street_length_bin
from src.constant import STREET_MODES
from src.tu.tu_utils import absorb_short_walk_into_car

DISTANCE_BINS_KM = [0, 0.5, 1, 2, 5, 10, 20, np.inf]
MIN_BIN_LEGS = 20  # bins with fewer legs are left out of the c * d^p fit
LARGE_DEVIATION_MIN = 60
PRINT_ROWS = 30  # longer tables are cut in the printout, not in the xlsx

# Printed above each table. All deviations are OTP − TU.
TABLE_DESCRIPTIONS = {
	"trip_times": "Departure, arrival and trip duration (arrival − departure) deviation per trip (OTP − TU, min).\n"
				  "TU arrival is derived from departure + leg durations + waits, so trip duration is scored, not arrival.",
	"time_correlation": "Correlation of departure and arrival deviation per trip. Spearman reflects typical trips, Pearson\n"
						"is driven by large deviations. As TU arrival = departure + duration, the expected rho is\n"
						"sd_departure / sd_arrival; a measured rho far from it means the clock and duration errors aren't independent.",
	"transit_duration": "Transit leg duration deviation per mode (OTP − TU, min).",
	"street_distance": "Street leg distance deviation per mode (OTP − TU, km).",
	"street_distance_bins": "Street leg distance deviation per mode and TU leg length bin (OTP − TU, km).",
	"street_distance_fit": f"Fit of robust_sd_deviation_km = c_km * median_tu_StageLength_km^p per mode, over bins with "
						   f">= {MIN_BIN_LEGS} legs.\np ≈ 0: one weight fits all lengths; p > 0: the weight should fall with length, as in w_street_mode_km_by_length.",
	"implied_weights": "Weights implied by the spreads, w = w_departure_min * (robust_sd_departure / robust_sd)²,\n"
					   "next to the configured ones; street distance per configured TU length bin (km).\n"
					   "Optimistic: the matches were picked with the configured weights.",
	"large_time_deviations": f"Trips with departure or arrival off by more than {LARGE_DEVIATION_MIN} min "
							 f"(OTP − TU), likely TU time errors.",
}


def robust_sd(values):
	"""1.4826 * MAD: equals the sd for normal errors, but a few bad matches can't inflate it."""
	values = values.dropna()
	return 1.4826 * (values - values.median()).abs().median() if len(values) else np.nan


def street_km_length_bins(weights):
	"""The run's street length bins; older runs had one w_street_mode_km, shown against the current config's bins."""
	if "w_street_mode_km_by_length" in weights:
		return weights["w_street_mode_km_by_length"]
	with open("config.json", encoding="utf-8") as file:
		current_bins = json.load(file)["squared_error_weights"]["w_street_mode_km_by_length"]
	return [{**length_bin, "w": weights["w_street_mode_km"]} for length_bin in current_bins]


def spread_table(df, value_col, by):
	return df.groupby(by, observed=True).agg(**{
		"n": (value_col, "count"),
		f"median_{value_col}": (value_col, "median"),
		f"robust_sd_{value_col}": (value_col, robust_sd),
		f"sd_{value_col}": (value_col, "std"),
		f"p90_abs_{value_col}": (value_col, lambda values: values.abs().quantile(0.9)),
	}).reset_index()


def fit_distance_spread(binned):
	"""Weighted log-log least squares of robust_sd_deviation_km = c * median_tu_StageLength_km^p per mode, over bins with enough legs."""
	rows = []
	for mode, bins in binned.groupby("mode"):
		bins = bins[(bins["n"] >= MIN_BIN_LEGS) & (bins["median_tu_StageLength_km"] > 0)]
		c, p = np.nan, np.nan
		if len(bins) >= 2:
			p, log_c = np.polyfit(np.log(bins["median_tu_StageLength_km"]), np.log(bins["robust_sd_deviation_km"]), 1, w=np.sqrt(bins["n"]))
			c = np.exp(log_c)
		rows.append({"mode": mode, "c_km": c, "p": p, "bins_used": len(bins)})
	return pd.DataFrame(rows)


def analyse(matches, summaries, tu_deltur, weights):
	found_ids = summaries.loc[summaries["trip_found"] == 1, "TurId"]

	found = summaries[summaries["TurId"].isin(found_ids)]
	# Scored instead of arrival, so a shifted clock time isn't counted twice
	found = found.assign(trip_duration_deviation_min=found["arrival_deviation_min"] - found["depart_deviation_min"])
	time_cols = ["depart_deviation_min", "arrival_deviation_min"]
	trip = found.melt(value_vars=[*time_cols, "trip_duration_deviation_min"], var_name="component", value_name="deviation_min")

	max_abs_deviation = found[time_cols].abs().max(axis=1)
	large_deviations = (
		found.loc[max_abs_deviation > LARGE_DEVIATION_MIN, ["TurId", *time_cols, "rmse", "used_anchor_fallback"]]
		.assign(max_abs_deviation_min=max_abs_deviation)
		.sort_values("max_abs_deviation_min", ascending=False)
	)

	# Score against the TU legs as matching saw them, after short walks were merged into car legs
	tu_legs = pd.concat(
		absorb_short_walk_into_car(legs, verbose=False)
		for _, legs in tu_deltur[tu_deltur["TurId"].isin(found_ids)].groupby("TurId")
	)
	legs = (
		matches[matches["TurId"].isin(found_ids) & matches["tu_Delturnr"].notna()]
		.astype({"tu_Delturnr": "Int64"})
		.merge(
			tu_legs[["TurId", "Delturnr", "StageLength", "StageDurationMin"]].astype({"Delturnr": "Int64"}),
			left_on=["TurId", "tu_Delturnr"], right_on=["TurId", "Delturnr"], how="left",
		)
	)
	legs["deviation_km"] = legs["distance_km"] - legs["StageLength"]
	legs["deviation_min"] = legs["duration_min"] - legs["StageDurationMin"]

	street = legs[legs["mode"].isin(STREET_MODES)]
	street = pd.concat([street, street.assign(mode="ALL_STREET")])
	transit = legs[~legs["mode"].isin(STREET_MODES)]

	binned = (
		street.assign(tu_km_bin=pd.cut(street["StageLength"], DISTANCE_BINS_KM, right=False).astype(str))
		.groupby(["mode", "tu_km_bin"], observed=True)
		.agg(
			n=("deviation_km", "count"),
			median_tu_StageLength_km=("StageLength", "median"),
			median_deviation_km=("deviation_km", "median"),
			robust_sd_deviation_km=("deviation_km", robust_sd),
		)
		.reset_index()
		.sort_values(["mode", "median_tu_StageLength_km"])
	)

	trip_spread = spread_table(trip, "deviation_min", "component")

	# Measured departure–arrival correlation, next to the one the trip duration term assumes
	dep, arr = found["depart_deviation_min"], found["arrival_deviation_min"]
	trip_robust_sd = trip_spread.set_index("component")["robust_sd_deviation_min"]
	time_correlation = pd.DataFrame([{
		"n": int((dep.notna() & arr.notna()).sum()),
		"pearson_rho": dep.corr(arr),
		"spearman_rho": dep.corr(arr, method="spearman"),
		"rho_assumed_by_duration_term": trip_robust_sd["depart_deviation_min"] / trip_robust_sd["arrival_deviation_min"],
	}])
	transit_spread = spread_table(transit, "deviation_min", "mode")
	street_spread = spread_table(street, "deviation_km", "mode")

	# Street distance per configured TU length bin, over all street modes, as the weights are shared by them
	length_bins = street_km_length_bins(weights)
	all_street = street[street["mode"] == "ALL_STREET"]
	street_by_length = (all_street.groupby(street_length_bin(all_street["StageLength"], length_bins).to_numpy())["deviation_km"]
						.agg(n="count", robust_sd=robust_sd).reindex(range(len(length_bins))))
	lower_km = [0, *[length_bin["below_km"] for length_bin in length_bins[:-1]]]
	street_by_length["length_km"] = [f"{lo}–{length_bin['below_km']}" if length_bin["below_km"] is not None else f">= {lo}"
									 for lo, length_bin in zip(lower_km, length_bins)]
	street_by_length["configured_weight"] = [length_bin["w"] for length_bin in length_bins]

	# Weights implied by the spreads, relative to w_departure_min, next to the configured ones
	implied = pd.concat([
		# Runs from before trip duration replaced arrival have no configured w_trip_duration_min
		pd.DataFrame({"weight": "w_trip_duration_min", "mode": "", "n": len(found),
					  "robust_sd": trip_robust_sd["trip_duration_deviation_min"],
					  "unit": "min", "configured_weight": weights.get("w_trip_duration_min", np.nan)}, index=[0]),
		transit_spread[["mode", "n", "robust_sd_deviation_min"]].rename(columns={"robust_sd_deviation_min": "robust_sd"})
			.assign(weight="w_transit_min", unit="min", configured_weight=weights["w_transit_min"]),
		street_by_length.assign(weight="w_street_mode_km_by_length", mode="ALL_STREET", unit="km"),
	], ignore_index=True)
	implied["implied_weight"] = (trip_robust_sd["depart_deviation_min"] / implied["robust_sd"]) ** 2 * weights["w_departure_min"]
	implied = implied[["weight", "mode", "length_km", "n", "robust_sd", "unit", "implied_weight", "configured_weight"]].fillna({"length_km": ""})

	return {
		"trip_times": trip_spread,
		"time_correlation": time_correlation,
		"transit_duration": transit_spread,
		"street_distance": street_spread,
		"street_distance_bins": binned,
		"street_distance_fit": fit_distance_spread(binned),
		"implied_weights": implied,
		"large_time_deviations": large_deviations,
	}


def main(run_dir):
	config = config_loader.get_config()
	paths = config["paths"]
	run_dir = Path(run_dir)
	# The run's TU year, from <output_dir>/<year>/<run_id>
	year_dir = run_dir.resolve().parent.name
	if not year_dir.isdigit():
		sys.exit(f"Expected <output_dir>/<year>/<run_id>, but the run's parent directory is '{year_dir}'")
	year = int(year_dir)

	if CONFIG_PATH == RUN_CONFIG:
		print(f"Using the run's config: {CONFIG_PATH}", flush=True)
	else:
		print(f"No config.json in the run directory, using the current one: {CONFIG_PATH.resolve()}\n"
			  f"configured_weight is left empty, as the run's weights are unknown.", flush=True)

	# Run files first: they're small, so a wrong path or dropped share fails before the slow TU load
	print(f"Reading run files from {run_dir}...", flush=True)
	matches = pd.read_csv(run_dir / paths["rmse_based_matches_file"])
	summaries = pd.read_excel(run_dir / paths["trip_matching_summaries_file"])

	print(f"Loading TU data for {year} from {paths['data_dir']}...", flush=True)
	_, _, tu_deltur, _ = load_TU_data.load_tu(
		data_dir=str(paths["data_dir"]),
		YEAR=year,
		**{name: config["tu_files"][name] for name in
		   ("session_file", "tur_file", "tur_secret_file", "deltur_file", "stations_file")},
	)
	# The run's own files were written by pandas, so match TurId's dtype to TU's
	turid_dtype = tu_deltur["TurId"].dtype
	matches = matches.astype({"TurId": turid_dtype})
	summaries = summaries.astype({"TurId": turid_dtype})

	print(f"Comparing {int((summaries['trip_found'] == 1).sum())} found trips...", flush=True)
	tables = analyse(matches, summaries, tu_deltur, config["squared_error_weights"])
	if CONFIG_PATH != RUN_CONFIG:
		# The current config's weights aren't necessarily the ones the run was matched with
		tables["implied_weights"]["configured_weight"] = np.nan

	with pd.option_context("display.width", 200, "display.float_format", "{:.3f}".format):
		for name, table in tables.items():
			cut = f" (first {PRINT_ROWS} of {len(table)} rows)" if len(table) > PRINT_ROWS else ""
			print(f"\n=== {name}{cut} ===\n{TABLE_DESCRIPTIONS[name]}\n\n{table.head(PRINT_ROWS).to_string(index=False)}")

	out_path = run_dir / "rmse_residuals.xlsx"
	with pd.ExcelWriter(out_path) as writer:
		for name, table in tables.items():
			table.to_excel(writer, sheet_name=name, index=False)
	print(f"\nWritten to {out_path}")


if __name__ == "__main__":
	main(RUN_DIR)
