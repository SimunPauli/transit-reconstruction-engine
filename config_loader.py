import json
from pathlib import Path

def load_config(config_path="config.json"):
	with open(config_path, "r", encoding="utf-8") as file:
		config = json.load(file)

	output_dir = Path(config["paths"]["output_dir"]).expanduser()
	output_dir.mkdir(parents=True, exist_ok=True)

	config["paths"]["output_dir"] = output_dir
	config["paths"]["log_file"] = output_dir / config["paths"]["log_file"]
	config["paths"]["rmse_based_matches_file"] = output_dir / config["paths"]["rmse_based_matches_file"]
	config["paths"]["trip_matching_summaries_file"] = output_dir / config["paths"]["trip_matching_summaries_file"]
	config["paths"]["failures_file"] = output_dir / config["paths"]["failures_file"]
	config["paths"]["summary_stats_file"] = output_dir / config["paths"]["summary_stats_file"]
	config["paths"]["tu_gtfs_station_file"] = output_dir / config["paths"]["tu_gtfs_station_file"]
	config["paths"]["map_file"] = output_dir / config["paths"]["map_file"]

	return config

_config = None
def get_config():
    global _config
    if _config is None:
        _config = load_config()
    return _config