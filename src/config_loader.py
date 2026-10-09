import json
import shutil
from datetime import datetime
from pathlib import Path

def load_config(config_path="config.json"):
	"""Reads config.json. No side effects, so importing modules that read it creates nothing."""
	with open(config_path, "r", encoding="utf-8") as file:
		return json.load(file)


def start_run(config, config_path="config.json"):
	"""
	Creates the run's output folder and points paths.output_dir and every paths.*_file into it.
	Called once by the entry point, so only an actual run creates a folder.
	"""
	# Outputs are sorted into output_dir/<year>/<run_id>/ so repeated runs (and different
	# TU years) don't overwrite each other. run_id is a sortable timestamp, same convention
	# tools like Hydra/MLflow use for per-run output folders.
	year = config["tu_subset"]["year"]
	run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
	output_dir = Path(config["paths"]["output_dir"]).expanduser() / str(year) / run_id
	output_dir.mkdir(parents=True, exist_ok=True)
	# Keep the run's settings with its output
	shutil.copyfile(config_path, output_dir / "config.json")  # contents only: the share rejects chmod

	paths = config["paths"]
	paths["output_dir"] = output_dir
	for key in paths:
		if key.endswith("_file"):
			paths[key] = output_dir / paths[key]
	return config

_config = None
def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config