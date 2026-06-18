import json
from pathlib import Path

def load_config(config_path="config.json"):
	with open(config_path, "r", encoding="utf-8") as file:
		config = json.load(file)

	data_dir = Path(config["paths"]["data_dir"]).expanduser()

	config["paths"]["data_dir"] = data_dir
	config["paths"]["log_file"] = Path(config["paths"]["log_file"]).expanduser()
	config["paths"]["rmse_based_matches_file"] = data_dir / config["paths"]["rmse_based_matches_file"]
	config["paths"]["trip_matching_summaries_file"] = data_dir / config["paths"]["trip_matching_summaries_file"]
	config["paths"]["tu_gtfs_station_file"] = data_dir / config["paths"]["tu_gtfs_station_file"]
	config["paths"]["map_file"] = Path(config["paths"]["map_file"]).expanduser()

	return config