"""
Compares two choice-set runs (output_dir/<year>/choice_set/<run_id>/), e.g. reference runs before and
after a code change: the runs' config.json, which reconstruction run each took its chosen trips from,
and the choice-set and summary csv files (run_time ignored), trip by trip where they differ.

Run from any directory:  .venv/bin/python choice_set_check/compare_choice_set_runs.py <run dir a> <run dir b>
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config_loader import load_config

CHOICE_SET_FILES = ["public_choice_set_file", "car_choice_set_file", "bike_choice_set_file"]
SORT_KEYS = ["TurId", "alt_id", "leg_id"]
MAX_LISTED = 10  # differing trips printed per file


def flat(config, prefix=""):
	"""Nested config as {"choice_set.search_window": value, ...}."""
	items = {}
	for key, value in config.items():
		if isinstance(value, dict):
			items |= flat(value, f"{prefix}{key}.")
		else:
			items[prefix + key] = value
	return items


def chosen_from(run_dir, config):
	log = run_dir / config["choice_set"]["paths"]["log_file"]
	lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
	return next((line for line in lines if line.startswith("Chosen trips from:")), "no 'Chosen trips from:' line")


def equal(a, b):
	try:
		pd.testing.assert_frame_equal(a, b, check_dtype=False)
		return True
	except AssertionError:
		return False


def per_trip(choice_set):
	"""Per TurId: number of alternatives (chosen included) and, for public transport, coverage."""
	trips = choice_set.groupby("TurId")
	stats = trips.agg(alternatives=("alt_id", "nunique"))
	if "coverage" in choice_set:
		stats["coverage"] = trips["coverage"].first()
	return stats


def compare_choice_sets(a, b):
	"""Prints how two choice-set files differ, per trip. True if they are equal."""
	same = list(a.columns) == list(b.columns)
	if not same:
		print(f"  columns only in a: {sorted(set(a.columns) - set(b.columns))}, "
			f"only in b: {sorted(set(b.columns) - set(a.columns))}")
	columns = [column for column in a.columns if column in b.columns]
	keys = [key for key in SORT_KEYS if key in columns]
	trips_a = dict(tuple(a.sort_values(keys).groupby("TurId")))
	trips_b = dict(tuple(b.sort_values(keys).groupby("TurId")))
	for side, only in [("a", trips_a.keys() - trips_b.keys()), ("b", trips_b.keys() - trips_a.keys())]:
		if only:
			same = False
			print(f"  {len(only)} trips only in {side}, e.g. {sorted(only)[:MAX_LISTED]}")
	common = sorted(trips_a.keys() & trips_b.keys())
	differing = [tur_id for tur_id in common if not equal(
		trips_a[tur_id][columns].reset_index(drop=True), trips_b[tur_id][columns].reset_index(drop=True))]
	if differing:
		rows = per_trip(a).loc[differing].join(per_trip(b).loc[differing], suffixes=("_a", "_b"))
		print(f"  {len(differing)} of {len(common)} common trips differ, "
			f"{(rows['alternatives_a'] != rows['alternatives_b']).sum()} of them in number of alternatives:")
		print(rows.head(MAX_LISTED).to_string())
	return same and not differing


def main():
	if len(sys.argv) != 3:
		sys.exit(__doc__)
	runs = [Path(arg).expanduser() for arg in sys.argv[1:]]
	configs = [load_config(run / "config.json") for run in runs]

	print("== config.json (a -> b)")
	flat_a, flat_b = flat(configs[0]), flat(configs[1])
	for key in sorted(flat_a.keys() | flat_b.keys()):
		if flat_a.get(key) != flat_b.get(key):
			print(f"  {key}: {flat_a.get(key)!r} -> {flat_b.get(key)!r}")
	print("== chosen trips")
	for side, run, config in zip("ab", runs, configs):
		print(f"  {side}: {chosen_from(run, config)}")

	n_differ = 0
	for key in CHOICE_SET_FILES + ["summary_file"]:
		files = [run / config["choice_set"]["paths"][key] for run, config in zip(runs, configs)]
		print(f"== {files[1].name}")
		if missing := [side for side, file in zip("ab", files) if not file.is_file()]:
			print(f"  missing in {' and '.join(missing)}")
			n_differ += len(missing) == 1
			continue
		a, b = (pd.read_csv(file) for file in files)
		if key == "summary_file":
			a, b = (summary.drop(columns="run_time", errors="ignore") for summary in (a, b))
			same = equal(a, b)
			if not same:
				print(f"  a:\n{a.to_string(index=False)}\n  b:\n{b.to_string(index=False)}")
		else:
			same = compare_choice_sets(a, b)
		print("  same" if same else "  DIFFERENT")
		n_differ += not same
	print(f"{n_differ} of {len(CHOICE_SET_FILES) + 1} files differ")


if __name__ == "__main__":
	main()
