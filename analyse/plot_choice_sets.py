"""
Choice sets as plots, one large page per trip: on top a timeline of the legs, the chosen itinerary's row
first, below it one map per shown alternative (leg_geometry over OpenStreetMap tiles fetched from
TILE_URL), each with the chosen underneath and the same extent. Up to MAX_ALTERNATIVES are shown: the
closest same-routes one (as other_stops_table picks it), the fastest, then by nearest departure
(choice-set score: departure gap to the chosen plus waiting; alt_id order). On each map, the stops of the
chosen and the alternative are labelled, those only one of them uses circled red.

Reads the choice-set run's public_choice_set_file and coverage_table.csv (run choice_set_coverage.py
first). Writes PLOTS_FILE to the choice-set run's folder, pages ordered by likely_cause.

Run from any directory:  .venv/bin/python analyse/plot_choice_sets.py [choice-set run dir] [--all] [--trips TurId ...]
Without a run dir: the newest choice-set run, as choice_set_coverage.py. Uncovered trips only, unless
--all (covered too) or --trips (only these).
"""
import argparse
import functools
import io
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from PIL import Image

import choice_set_coverage as coverage  # first: sets the working directory and sys.path for src
from src.config_loader import load_config
from src.constant import (STREET_MODES, ROUTE_MATCH_EXACT, ROUTE_MATCH_SIBLING, ROUTE_MATCH_LETTER,
	ROUTE_MATCH_DIGIT, ROUTE_MATCH_IGNORED)

MAX_ALTERNATIVES = 6
MAP_COLUMNS = 3
MAP_SIZE_IN = 8  # each map's width and height on the page
TIMELINE_HEIGHT_IN = 5
# The reconstruction's route_match: how the chosen's bus/S-train lines match the ones TU wrote
LINE_MATCH = {ROUTE_MATCH_EXACT: "exact line", ROUTE_MATCH_SIBLING: "near line (sibling)",
	ROUTE_MATCH_LETTER: "near line (letter)", ROUTE_MATCH_DIGIT: "near line (digit)", ROUTE_MATCH_IGNORED: "wrong line"}
NO_LINE_MATCH = "line not checked (no bus/S-train leg)"
PLOTS_FILE = "choice_set_plots.pdf"
TIMEZONE = "Europe/Copenhagen"
MODE_COLORS = {"WALK": "#808080", "BICYCLE": "#a6d96a", "BUS": "#f2c94c", "RAIL": "#c0392b",
	"S_TRAIN": "#f28e2b", "SUBWAY": "#4e79a7", "TRAM": "#76b7b2", "FERRY": "#9c755f"}
OTHER_MODE_COLOR = "#999999"
CHOSEN_STYLE = {"color": "magenta", "lw": 8, "alpha": 0.35}  # wide highlighter under the alternative, a colour no mode has
HIGHLIGHT_COLOR = "#1f4e9c"  # the same-routes alternative's frame and timeline band, for STOP_CAUSES trips
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
TILE_HEADERS = {"User-Agent": "trip_choice_pipeline plot_choice_sets"}  # OSM's tile policy requires one
MAP_PX = 1024  # OSM background's resolution across a map (×0.7–1.4, by zoom level)
MAX_ZOOM = 18
TILE_PX = 256
EARTH_RADIUS_M = 6378137  # Web Mercator's
HALF_WORLD_M = np.pi * EARTH_RADIUS_M


def _clock(ms):
	"""Epoch ms as matplotlib dates in local time."""
	local = pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert(TIMEZONE).dt.tz_localize(None)
	return mdates.date2num(local.to_numpy())


def _mercator(points):
	"""(lat, lon) points as Web Mercator x and y arrays, m (OSM tiles' projection)."""
	lat, lon = np.radians(np.array(points)).T
	return EARTH_RADIUS_M * lon, EARTH_RADIUS_M * np.log(np.tan(np.pi / 4 + lat / 2))


@functools.cache
def _tile(z, x, y):
	"""One OSM tile as an RGB array, cached for the run."""
	response = requests.get(TILE_URL.format(z=z, x=x, y=y), headers=TILE_HEADERS, timeout=30)
	response.raise_for_status()
	return np.asarray(Image.open(io.BytesIO(response.content)).convert("RGB"))


def add_osm_background(ax):
	"""OSM tiles behind ax's current (Web Mercator) extent; none, with a warning, if they can't be fetched."""
	(x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
	# The zoom closest to MAP_PX pixels across the view
	z = min(MAX_ZOOM, round(np.log2(MAP_PX * 2 * HALF_WORLD_M / (TILE_PX * (x1 - x0)))))
	size = 2 * HALF_WORLD_M / 2 ** z
	# Tile columns from the west, rows from the north
	cols = range(int((x0 + HALF_WORLD_M) // size), int((x1 + HALF_WORLD_M) // size) + 1)
	rows = range(int((HALF_WORLD_M - y1) // size), int((HALF_WORLD_M - y0) // size) + 1)
	try:
		image = np.vstack([np.hstack([_tile(z, col, row) for col in cols]) for row in rows])
	except requests.RequestException as exc:
		print(f"No OSM background: {exc}")
		return
	# Cropped to the view, lightened and at most 256 colours: the PDF stores it small (indexed), at full resolution
	west, north, px_m = cols[0] * size - HALF_WORLD_M, HALF_WORLD_M - rows[0] * size, size / TILE_PX
	c0, c1 = int((x0 - west) / px_m), int(np.ceil((x1 - west) / px_m))
	r0, r1 = int((north - y1) / px_m), int(np.ceil((north - y0) / px_m))
	image = Image.fromarray((image[r0:r1, c0:c1] * 0.6 + 255 * 0.4).astype(np.uint8)).quantize(256).convert("RGB")
	extent = (west + c0 * px_m, west + c1 * px_m, north - r1 * px_m, north - r0 * px_m)
	ax.imshow(np.asarray(image), extent=extent, interpolation="none", zorder=0)
	ax.set_xlim(x0, x1)
	ax.set_ylim(y0, y1)
	ax.text(0.99, 0.01, "© OpenStreetMap contributors", transform=ax.transAxes, ha="right", va="bottom", fontsize=6)


def shown_alternatives(trip, closest, line_match):
	"""
	alt_id -> label of the alternatives shown: the chosen (with line_match), closest (the closest
	same-routes alternative's alt_id, or None), the fastest, then by nearest departure up to
	MAX_ALTERNATIVES; chosen first, then by alt_id.
	"""
	alts = trip.groupby("alt_id")[["start_trip", "end_trip"]].first()
	duration = (pd.to_datetime(alts["end_trip"], utc=True) - pd.to_datetime(alts["start_trip"], utc=True)).dt.total_seconds() / 60
	transfers = (~trip["mode"].isin(STREET_MODES)).groupby(trip["alt_id"]).sum() - 1
	tags = {0: []}
	if closest is not None:
		tags.setdefault(closest, []).append("same routes")
	others = duration.drop(0)
	if not others.empty:
		tags.setdefault(others.idxmin(), []).append("fastest")
	for alt_id in others.index:
		if alt_id not in tags and len(tags) > MAX_ALTERNATIVES:  # tags holds the chosen too
			break
		tags.setdefault(alt_id, []).append("nearest departure")
	return {alt_id: f"{f'chosen: {line_match}' if alt_id == 0 else f'alt {alt_id}: ' + ', '.join(tags[alt_id])}\n"
		f"{duration[alt_id]:.0f} min, {transfers[alt_id]} transfer(s)" for alt_id in sorted(tags)}


def _legs(trip, alt_id):
	return trip[trip["alt_id"] == alt_id].sort_values("leg_id").to_dict("records")


def map_extent(trip, shown):
	"""Square Web Mercator extent (x0, x1, y0, y1) around the shown alternatives' legs, 5 % margin; None without geometry."""
	points = [point for geometry in trip.loc[trip["alt_id"].isin(shown), "leg_geometry"] for point in coverage.leg_points(geometry)]
	if not points:
		return None
	x, y = _mercator(points)
	half = max(np.ptp(x), np.ptp(y), 500) * 0.55
	x_mid, y_mid = (x.min() + x.max()) / 2, (y.min() + y.max()) / 2
	return x_mid - half, x_mid + half, y_mid - half, y_mid + half


def plot_map(ax, trip, alt_id, label, extent, highlight):
	"""
	Legs of alt_id, coloured by mode, over the chosen's (wide underneath; walk dashed) in Web Mercator over
	OSM, their transit stops named, those only one of them uses circled red. highlight: framed, bold title.
	"""
	stops = {}
	for shown_id in dict.fromkeys([0, alt_id]):
		used = stops.setdefault(shown_id, {})
		for leg in _legs(trip, shown_id):
			points = coverage.leg_points(leg["leg_geometry"])
			if not points:
				continue
			x, y = _mercator(points)
			walk = leg["mode"] in STREET_MODES
			if shown_id == 0:
				ax.plot(x, y, **CHOSEN_STYLE, ls=":" if walk else "-", zorder=1)
			else:
				ax.plot(x, y, color=MODE_COLORS.get(leg["mode"], OTHER_MODE_COLOR), lw=2.5, ls="--" if walk else "-", zorder=2)
			if not walk:
				used.setdefault(leg["from"], (x[0], y[0]))
				used.setdefault(leg["to"], (x[-1], y[-1]))

	differing = stops[0].keys() ^ stops[alt_id].keys()
	for name, (x, y) in (stops[alt_id] | stops[0]).items():
		ax.plot(x, y, "o", ms=5, mfc="white", mec="black", zorder=3)
		if name in differing:
			ax.plot(x, y, "o", ms=13, mfc="none", mec="red", mew=1.5, zorder=3)
		ax.annotate(name, (x, y), xytext=(5, 5), textcoords="offset points", fontsize=7, zorder=4)

	chosen = [point for leg in _legs(trip, 0) for point in coverage.leg_points(leg["leg_geometry"])]
	if chosen:
		x, y = _mercator([chosen[0], chosen[-1]])
		ax.plot(x[0], y[0], "^", ms=11, color="green", zorder=4)
		ax.plot(x[1], y[1], "s", ms=10, color="red", zorder=4)
	ax.set_aspect("equal")
	if extent:
		ax.set_xlim(extent[:2])
		ax.set_ylim(extent[2:])
		add_osm_background(ax)
	ax.set_xticks([])
	ax.set_yticks([])
	ax.set_title(label, fontsize=10, fontweight="bold" if highlight else "normal", color=HIGHLIGHT_COLOR if highlight else "black")
	if highlight:
		for spine in ax.spines.values():
			spine.set(color=HIGHLIGHT_COLOR, linewidth=4)


def plot_timeline(ax, trip, shown, highlight):
	"""
	One row per shown alternative: legs as bars coloured by mode, labelled by km (and route, transit).
	highlight (an alt_id, or None): its row banded, label bold.
	"""
	modes = set()
	for row, alt_id in enumerate(shown):
		legs = trip[trip["alt_id"] == alt_id].sort_values("leg_id")
		for leg, start, end in zip(legs.to_dict("records"), _clock(legs["start_leg"]), _clock(legs["end_leg"])):
			modes.add(leg["mode"])
			ax.barh(row, end - start, left=start, height=0.6, color=MODE_COLORS.get(leg["mode"], OTHER_MODE_COLOR),
				edgecolor="white")
			route = leg["route_short_name"] if isinstance(leg["route_short_name"], str) else leg["mode"]
			text = f"{leg['distance_km']:.1f} km" if leg["mode"] in STREET_MODES else f"{route}\n{leg['distance_km']:.1f} km"
			ax.text((start + end) / 2, row, text, ha="center", va="center", fontsize=8, clip_on=True)

	ax.set_yticks(range(len(shown)), list(shown.values()), fontsize=9)
	if highlight is not None:
		row = list(shown).index(highlight)
		ax.axhspan(row - 0.45, row + 0.45, color=HIGHLIGHT_COLOR, alpha=0.15, zorder=0)
		ax.get_yticklabels()[row].set(fontweight="bold", color=HIGHLIGHT_COLOR)
	ax.invert_yaxis()
	ax.axhline(0.5, color="black", lw=1)  # the chosen's row apart
	chosen_start = _clock(trip.loc[trip["alt_id"] == 0, "start_leg"]).min()
	ax.axvline(chosen_start, color="black", lw=0.8, ls=":")
	ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
	ax.grid(axis="x", alpha=0.3)
	ax.legend(handles=[Patch(color=MODE_COLORS.get(mode, OTHER_MODE_COLOR), label=mode) for mode in sorted(modes)],
		fontsize=9, loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=len(modes))


def plot_trip(pdf, tur_id, trip, info, closest):
	line_match = LINE_MATCH.get(info["route_match"], NO_LINE_MATCH)
	shown = shown_alternatives(trip, closest, line_match)
	# Where the chosen's stops are the likely cause, its closest same-routes alternative matters most: first
	highlight = closest if info["likely_cause"] in coverage.STOP_CAUSES and closest in shown else None
	if highlight is not None:
		shown = {0: shown.pop(0), highlight: shown.pop(highlight), **shown}
	map_ids = [alt_id for alt_id in shown if alt_id != 0] or [0]  # the chosen alone if no alternatives
	map_rows = -(-len(map_ids) // MAP_COLUMNS)
	fig = plt.figure(figsize=(MAP_COLUMNS * MAP_SIZE_IN, TIMELINE_HEIGHT_IN + map_rows * MAP_SIZE_IN), layout="constrained")
	grid = fig.add_gridspec(1 + map_rows, MAP_COLUMNS, height_ratios=[TIMELINE_HEIGHT_IN, *[MAP_SIZE_IN] * map_rows])
	plot_timeline(fig.add_subplot(grid[0, :]), trip, shown, highlight)
	extent = map_extent(trip, shown)
	for i, alt_id in enumerate(map_ids):
		plot_map(fig.add_subplot(grid[1 + i // MAP_COLUMNS, i % MAP_COLUMNS]), trip, alt_id,
			shown[alt_id].replace("\n", " · "), extent, alt_id == highlight)
	fig.legend(handles=[
		Line2D([], [], **CHOSEN_STYLE, label="chosen"),
		Line2D([], [], color=MODE_COLORS["WALK"], lw=2.5, ls="--", label="walk (chosen dotted)"),
		Line2D([], [], marker="^", color="green", ls="", label="origin"),
		Line2D([], [], marker="s", color="red", ls="", label="destination"),
		Line2D([], [], marker="o", mfc="none", mec="red", ls="", ms=10, label="stop only one of them uses"),
	], loc="outside lower center", ncol=5, fontsize=10)
	fig.suptitle(
		f"TurId {tur_id} · {info['likely_cause']} · {info['n_alternatives']:.0f} alternatives · chosen: {line_match}\n"
		f"duration {info['chosen_duration_min']:.0f} vs fastest {info['best_alt_duration_min']:.0f} min "
		f"(×{info['duration_vs_best']}) · transfers {info['chosen_transfers']:.0f} vs fewest {info['min_alt_transfers']:.0f} · "
		f"walk {info['chosen_walk_km']:.2f} km (direct {info['direct_walk_km']:.2f}) · "
		f"reconstructed departure gap {info['depart_gap_min']:.0f} min", fontsize=14)
	pdf.savefig(fig)
	plt.close(fig)


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("run_dir", nargs="?", type=Path)
	parser.add_argument("--all", action="store_true", help="covered trips too")
	parser.add_argument("--trips", nargs="+", type=int, help="only these TurIds")
	args = parser.parse_args()
	run_dir = args.run_dir.expanduser() if args.run_dir else coverage.newest_choice_set_run(load_config())
	run_config = load_config(run_dir / "config.json")

	choice_sets = pd.read_csv(run_dir / run_config["choice_set"]["paths"]["public_choice_set_file"])
	trips = pd.read_csv(run_dir / coverage.TABLE_FILE, index_col="TurId")
	trips["likely_cause"] = trips["likely_cause"].fillna("covered")
	if args.trips:
		trips = trips.loc[args.trips]
	elif not args.all:
		trips = trips[trips["coverage"] == 0]
	order = {cause: i for i, cause in enumerate([*coverage.CAUSE_ORDER, "unexplained", "covered"])}
	trips = trips.sort_index().sort_values("likely_cause", key=lambda cause: cause.map(order), kind="stable")

	differences = coverage.other_stops_differences(choice_sets, trips.index)
	closest = differences.groupby("TurId")["alt_id"].first()
	by_trip = dict(tuple(choice_sets.groupby("TurId")))
	with PdfPages(run_dir / PLOTS_FILE) as pdf:
		for tur_id, info in trips.iterrows():
			plot_trip(pdf, tur_id, by_trip[tur_id], info, closest.get(tur_id))
	print(f"Wrote {len(trips)} trip(s) to {run_dir / PLOTS_FILE}")


if __name__ == "__main__":
	main()
	