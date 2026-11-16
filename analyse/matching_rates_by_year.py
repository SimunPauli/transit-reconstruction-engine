"""
Trip reconstruction outcome as plots: per TU year, the share of trips found (with the TU route name,
a near one or a "wrong" one) as stacked bars. For YEAR_DETAIL also the outcome incl. failure reasons
by origin city size, and the number of transfers of the trips found, found with a wrong route and
not found.

Reads trip_matching_summary_stats.xlsx from each year's newest reconstruction run under
config.json's paths.output_dir, and saves the plots there. The YEAR_DETAIL plots also read that
run's trip_matching_summaries and the TU tur/deltur files.

Run from any directory:  .venv/bin/python analyse/matching_rates_by_year.py
"""
import os
import re
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import PercentFormatter

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
os.chdir(PROJECT_DIR)  # src.constant reads config.json from the working directory
from src.config_loader import load_config
from src.constant import TRANSIT_STAGE_MODES
from src.tu.load_TU_data import _read_tu_table

YEARS = [str(year) for year in range(2016, 2026)]
YEARS_TO_DISPLAY = [str(year) for year in range(2017, 2026)]  # edit this line
YEAR_DETAIL = "2025"  # year of the city size and transfers plots
STATS_NAME = "trip_matching_summary_stats.xlsx"
RUN_ID_PATTERN = re.compile(r"\d{8}_\d{6}")
MIN_FAIL_SHARE = 0.01  # failure categories below this share of all trips are left out
MAX_FAIL_CATEGORIES = 7

# OrigCitySize interval edges (inhabitants), right-closed
CITY_SIZE_BINS = [0, 1_000, 10_000, 50_000, 100_000, 500_000, float("inf")]
TRANSIT_LEG_MODES = TRANSIT_STAGE_MODES | {41}  # + ferry
MAX_TRANSFERS = 3  # this many or more are one bar

# Merged failure categories
CATEGORY_MAP = {
	"TypeError": "code_error",
	"AttributeError": "code_error",
	"no_required_routes": "no_required_route_names",
	"anchored_no_required_routes": "no_required_route_names",
	"no_otp_candidates": "no_otp_candidates",
	"anchored_no_otp_candidates": "no_otp_candidates",
	"no_matching_leg_sequence": "no_matching_leg_sequence",
	"anchored_no_matching_leg_sequence": "no_matching_leg_sequence",
	"no_required_modes": "no_required_modes",
	"anchored_no_required_modes": "no_required_modes",
	"no_direct_access_route": "no_direct_access_or_egress_route",
	"no_direct_egress_route": "no_direct_access_or_egress_route",
	"invalid_route_name": "invalid_route_name",
	"car_leg_not_satisfied": "car_leg_not_satisfied",
}

# Unknown categories are labelled with their raw name
DK_LABELS = {
	"found": "Genskabt",
	"near_route": "Genskabt med lignende linjenummer",
	"wrong_route": "Genskabt med \"forkert\" linjenummer",
	"not_found": "Ikke genskabt",
	"code_error": "Kodefejl",
	"no_required_route_names": "Linjenumre ikke fundet",
	"no_otp_candidates": "Ingen rejseforslag",
	"no_matching_leg_sequence": "Delture matcher ikke",
	"no_required_modes": "Transportmidler ikke fundet",
	"no_direct_access_or_egress_route": "Ingen direkte rute til/fra station",
	"invalid_route_name": "Ugyldigt linjenummer",
	"car_leg_not_satisfied": "Bildeltur ikke opfyldt",
}

# Success: one blue ramp (dark = best match); failures: non-blue qualitative hues
SUCCESS_COLORS = {"found": "#08519c", "near_route": "#4292c6", "wrong_route": "#9ecae1"}
FAIL_PALETTE = ["#D95F02", "#1B9E77", "#E7298A", "#E6AB02", "#66A61E", "#A6761D", "#7570B3"]


def latest_run(year_dir):
	"""Newest run (highest run_id) in year_dir that has a stats file."""
	run_ids = [path.name for path in year_dir.iterdir()
		if RUN_ID_PATTERN.fullmatch(path.name) and (path / STATS_NAME).exists()]
	if not run_ids:
		raise FileNotFoundError(f"No finished run in {year_dir}")
	return max(run_ids)


def read_stats(output_root, runs, sheet):
	"""One stats sheet of every year's run, stacked, with a year column."""
	return pd.concat([pd.read_excel(output_root / year / run_id / STATS_NAME, sheet_name=sheet).assign(year=year)
		for year, run_id in runs.items()], ignore_index=True)


def read_trips(run_dir):
	"""The run's per-trip outcomes, with the TU OrigCitySize and number of transfers (transit legs - 1)."""
	config = load_config(run_dir / "config.json")  # the run's own, so file names and TU data match that run
	trips = pd.read_excel(run_dir / config["reconstruct"]["paths"]["trip_matching_summaries_file"])
	data_dir = Path(config["paths"]["data_dir"]).expanduser()
	tu_tur = _read_tu_table(data_dir / config["tu_files"]["tur_file"])
	tu_deltur = _read_tu_table(data_dir / config["tu_files"]["deltur_file"])

	transit_legs = tu_deltur.loc[tu_deltur["StageMode"].isin(TRANSIT_LEG_MODES)].groupby("TurId").size()
	trips["OrigCitySize"] = trips["TurId"].map(tu_tur.set_index("TurId")["OrigCitySize"])
	trips["n_transfers"] = (trips["TurId"].map(transit_legs).fillna(0) - 1).clip(lower=0).astype(int)
	return trips


def stats_by(trips, by):
	"""The stats file's overview and failure_reasons tables per value of by, from per-trip outcomes."""
	overview = trips.groupby(by, observed=True).agg(
		total_trips=("TurId", "size"),
		trips_found=("trip_found", "sum"),
		trips_near_route=("trip_near_route", "sum"),
		trips_wrong_route=("trip_wrong_route", "sum"),
	)
	failures = (trips.loc[trips["trip_not_found"] == 1].groupby([by, "failure_reason"], observed=True)
		.size().reset_index(name="count"))
	return overview, failures


def outcome_rates(overview, failures, by, top_fails=None):
	"""
	Share of trips per outcome for each value of by (overview's index), columns in stacking order:
	found at the bottom, failures largest first. Failure categories are top_fails, or the largest
	ones if None; the rest are left out. Returns the rates and the failure categories.
	"""
	# Older runs may lack near/wrong route
	success = (overview.reindex(columns=["trips_found", "trips_near_route", "trips_wrong_route"])
		.fillna(0).set_axis(list(SUCCESS_COLORS), axis=1))

	failures = failures.assign(category=failures["failure_reason"].map(CATEGORY_MAP).fillna(failures["failure_reason"]))
	if top_fails is None:
		fail_totals = failures.groupby("category")["count"].sum().sort_values(ascending=False)
		top_fails = fail_totals[fail_totals / overview["total_trips"].sum() >= MIN_FAIL_SHARE].index[:MAX_FAIL_CATEGORIES].tolist()
	fail_counts = failures.pivot_table(index=by, columns="category", values="count", aggfunc="sum", observed=True)

	counts = (pd.concat([success, fail_counts], axis=1)
		.reindex(index=overview.index, columns=list(SUCCESS_COLORS) + top_fails).fillna(0))
	return counts.div(overview["total_trips"], axis=0), top_fails


def city_size_labels(bins):
	"""Two-line interval labels with Danish thousands separators, e.g. '1.000-\\n10.000'."""
	number = lambda value: f"{value:,.0f}".replace(",", ".")
	return [f"{number(low)}-\n{number(high)}" if high != float("inf") else f">\n{number(low)}"
		for low, high in zip(bins[:-1], bins[1:])]


def percent_axis(ax, xlabel):
	"""0-100 % y axis, light horizontal grid, no frame."""
	ax.set_ylim(0, 1)
	ax.set_yticks([tick / 10 for tick in range(11)])
	ax.yaxis.set_major_formatter(PercentFormatter(1, decimals=0))
	ax.set_xlabel(xlabel)
	ax.set_ylabel("Andel af ture")
	ax.grid(axis="y", color="#ebebeb")
	ax.set_axisbelow(True)
	ax.spines[:].set_visible(False)
	ax.tick_params(length=0)


def stacked_bar_plot(rates, colors, xlabel, title=None, legend_below=False):
	"""Stacked bars per row of rates, the found share labelled at the bottom of each bar, 0-100 % axis."""
	fig, ax = plt.subplots(figsize=(10, 6))
	bottom = pd.Series(0.0, index=rates.index)
	for category in rates.columns:
		ax.bar(rates.index, rates[category], width=0.7, bottom=bottom, color=colors[category],
			edgecolor="white", linewidth=0.3, label=DK_LABELS.get(category, category))
		bottom += rates[category]
	for x, rate in enumerate(rates["found"]):
		ax.text(x, 0.01, f"{rate:.0%}", ha="center", va="bottom", color="white", fontweight="bold")
	percent_axis(ax, xlabel)
	if title:
		ax.set_title(title)

	handles, labels = ax.get_legend_handles_labels()
	if legend_below:
		ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=len(labels), frameon=False)
	else:
		# Top to bottom as stacked
		ax.legend(handles[::-1], labels[::-1], loc="center left", bbox_to_anchor=(1, 0.5), frameon=False)
	return fig


def transfer_shares(trips, outcomes):
	"""Per outcome (trip_<outcome> column), the share of its trips by number of transfers, and its trips."""
	n_transfers = trips["n_transfers"].clip(upper=MAX_TRANSFERS)
	shares = (pd.DataFrame({outcome: n_transfers[trips[f"trip_{outcome}"] == 1].value_counts(normalize=True)
		for outcome in outcomes}).T.reindex(columns=range(MAX_TRANSFERS + 1)).fillna(0))
	shares.columns = [str(n) for n in range(MAX_TRANSFERS)] + [f"{MAX_TRANSFERS}+"]
	n = pd.Series({outcome: int(trips[f"trip_{outcome}"].sum()) for outcome in outcomes})
	return shares, n


def transfers_plot(shares, n, outcome_colors, title):
	"""transfer_shares' rows side by side per number of transfers."""
	width = 0.8 / len(shares)
	fig, ax = plt.subplots(figsize=(10, 6))
	for i, (outcome, row) in enumerate(shares.iterrows()):
		offset = (i - (len(shares) - 1) / 2) * width
		ax.bar([x + offset for x in range(len(row))], row, width=width, color=outcome_colors[outcome],
			label=f"{DK_LABELS[outcome]} (n={n[outcome]})")
	ax.set_xticks(range(len(shares.columns)), shares.columns)
	percent_axis(ax, "Antal skift (TU-delture med kollektiv transport - 1)")
	ax.set_title(title)
	ax.legend(loc="upper right", frameon=False)
	return fig


def print_table(title, shares, n):
	"""shares in percent, with each row's number of trips first."""
	table = (100 * shares).round(1)
	table.insert(0, "n", n)
	table.index = table.index.astype(str).str.replace("\n", "")
	print(f"\n{title} (%):\n{table.to_string()}")


def main():
	output_root = Path(load_config()["paths"]["output_dir"]).expanduser()
	runs = {year: latest_run(output_root / year) for year in YEARS}
	print(pd.Series(runs, name="run_id").rename_axis("year").to_string())  # check which runs were picked

	overview = read_stats(output_root, runs, 0).set_index("year")
	rates, top_fails = outcome_rates(overview, read_stats(output_root, runs, 1), "year")
	rates = rates.loc[YEARS_TO_DISPLAY, list(SUCCESS_COLORS)]
	print_table("Found by year", rates, overview["total_trips"])
	rates.index = [f"{year}\nn={overview.loc[year, 'total_trips']}" for year in rates.index]
	colors = {**SUCCESS_COLORS, **dict(zip(top_fails, FAIL_PALETTE))}

	trips = read_trips(output_root / YEAR_DETAIL / runs[YEAR_DETAIL])
	trips["city_size"] = (pd.cut(trips["OrigCitySize"], CITY_SIZE_BINS, labels=city_size_labels(CITY_SIZE_BINS))
		.cat.add_categories("Ukendt").fillna("Ukendt"))
	city_overview, city_failures = stats_by(trips, "city_size")
	# Same failure categories as the year plot, so the colors match
	city_rates, _ = outcome_rates(city_overview, city_failures, "city_size", top_fails)
	print_table(f"Outcome by origin city size, {YEAR_DETAIL}", city_rates, city_overview["total_trips"])
	city_rates.index = [f"{size}\nn={n}" for size, n in city_overview["total_trips"].items()]

	outcome_colors = {"found": SUCCESS_COLORS["found"], "wrong_route": SUCCESS_COLORS["wrong_route"], "not_found": FAIL_PALETTE[0]}
	transfers, transfers_n = transfer_shares(trips, outcome_colors)
	print_table(f"Number of transfers by outcome, {YEAR_DETAIL}", transfers, transfers_n)

	plots = {
		"trip_matching_found_by_year.png": stacked_bar_plot(
			rates, colors, "År", legend_below=True),
		f"trip_matching_by_city_size_{YEAR_DETAIL}.png": stacked_bar_plot(
			city_rates, colors, "Indbyggere i startbyen (OrigCitySize)",
			title=f"Genskabelse af ture efter startbyens størrelse, {YEAR_DETAIL}"),
		f"trip_matching_transfers_{YEAR_DETAIL}.png": transfers_plot(
			transfers, transfers_n, outcome_colors, title=f"Antal skift efter udfald, {YEAR_DETAIL}"),
	}
	for name, fig in plots.items():
		fig.savefig(output_root / name, dpi=200, bbox_inches="tight")
		print(f"Saved to {output_root / name}")
	plt.show()


if __name__ == "__main__":
	main()
