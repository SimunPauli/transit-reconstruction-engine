import json
import shutil
from datetime import datetime
from pathlib import Path

RUN_ID_FORMAT = "%Y%m%d_%H%M%S"


def load_config(config_path="config.json"):
	"""Reads config.json, in the current layout. No side effects, so importing modules that read it creates nothing."""
	with open(config_path, "r", encoding="utf-8") as file:
		return _upgrade_layout(json.load(file))


def _upgrade_layout(config):
	"""Config was restructured on 13-10-2026. This moves an older run's saved config.json
	into the current layout."""
	if "reconstruct" in config:
		return config
	matching = config.pop("matching")
	paths = config["paths"]
	config["tu_prep"] = {"walk_car_absorb_max_km": matching.pop("walk_car_absorb_max_km")}
	config["reconstruct"] = {
		"search_window": config["request"].pop("search_window"),
		"matching": matching,
		"reluctance_retries": config.pop("reluctance_retries", {}),
		"squared_error_weights": config.pop("squared_error_weights"),
		"walk_bike_time_ratio": config.pop("walk_bike_time_ratio"),
		"paths": {key: paths.pop(key) for key in list(paths) if key.endswith("_file")},
	}
	return config


def start_run(config, stage, subfolder=True, config_path="config.json"):
	"""
	Creates the run's output folder and points paths.output_dir and every <stage>.paths file into it.
	With subfolder, the run goes in its own <stage>/ folder (reconstruction runs don't, so the
	choice set and older runs find them directly under the year).
	Called once by the entry point, so only an actual run creates a folder.
	"""
	# Outputs are sorted into output_dir/<year>/[<stage>/]<run_id>/ so repeated runs (and different
	# TU years) don't overwrite each other. run_id is a sortable timestamp, same convention
	# tools like Hydra/MLflow use for per-run output folders.
	year = config["tu_subset"]["year"]
	run_id = datetime.now().strftime(RUN_ID_FORMAT)
	output_dir = Path(config["paths"]["output_dir"]).expanduser() / str(year)
	if subfolder:
		output_dir = output_dir / stage
	output_dir = output_dir / run_id
	output_dir.mkdir(parents=True, exist_ok=True)
	# Keep the run's settings with its output
	shutil.copyfile(config_path, output_dir / "config.json")  # contents only: the share rejects chmod

	config["paths"]["output_dir"] = output_dir
	paths = config[stage]["paths"]
	for key in paths:
		paths[key] = output_dir / paths[key]
	return config

_config = None
def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config