from datetime import datetime
from pathlib import Path
import pandas as pd
from src.config_loader import RUN_ID_FORMAT


def _is_run_id(name):
	try:
		datetime.strptime(name, RUN_ID_FORMAT)
		return True
	except ValueError:
		return False


def find_chosen_trips_file(config):
	"""
	Path to a reconstruction run's matches file: in choice_set.reconstruct_run_dir, or if that is null,
	in the newest output_dir/<year>/<run_id>/ that has one (crashed or running runs have none).
	Call before start_run, which repoints paths.output_dir into the choice-set run.
	"""
	matches_file = config["reconstruct"]["paths"]["rmse_based_matches_file"]
	run_dir = config["choice_set"]["reconstruct_run_dir"]
	if run_dir:
		return Path(run_dir).expanduser() / matches_file

	year_dir = Path(config["paths"]["output_dir"]).expanduser() / str(config["tu_subset"]["year"])
	# run_id is a sortable timestamp, so name order is time order
	run_dirs = sorted(
		(run_dir for run_dir in year_dir.iterdir() if run_dir.is_dir() and _is_run_id(run_dir.name)),
		reverse=True,
	)
	for run_dir in run_dirs:
		if (run_dir / matches_file).is_file():
			return run_dir / matches_file
	raise FileNotFoundError(f"No run in {year_dir} contains {matches_file}")


def read_chosen_trips(matches_path, summaries_file):
	"""
	The reconstructed (chosen) itineraries, one row per leg, with the trip's outcome column
	(trip_found or trip_near_route). Trips only found by ignoring the TU route (trip_wrong_route,
	per the run's summaries_file) are dropped: their route isn't the chosen one.
	"""
	# Logged, as the run's config.json can't tell which run reconstruct_run_dir: null picked
	print(f"Chosen trips from: {matches_path}")
	chosen_trips = pd.read_csv(matches_path)
	summaries = pd.read_excel(matches_path.parent / summaries_file)
	wrong_route = summaries.loc[summaries["trip_wrong_route"] == 1, "TurId"]
	print(f"Dropped {wrong_route.isin(chosen_trips['TurId']).sum()} wrong-route trip(s)")
	chosen_trips = chosen_trips[~chosen_trips["TurId"].isin(wrong_route)]
	outcome = summaries.set_index("TurId")["trip_near_route"].map({0: "trip_found", 1: "trip_near_route"})
	return chosen_trips.assign(outcome=chosen_trips["TurId"].map(outcome))
