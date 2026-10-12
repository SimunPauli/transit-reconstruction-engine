import sys
from contextlib import redirect_stdout
from src.tu import load_TU_data
from src.config_loader import get_config, start_run
from src.run_utils import Tee, wait_for_output_dir


def main():
	config = start_run(get_config(), stage="choice_set")

	with open(config["choice_set"]["paths"]["log_file"], "w", encoding="utf-8") as log_file:
		tee = Tee(sys.stdout, log_file)
		with redirect_stdout(tee):
			_run(config, tee)


def _run(config, tee):
	print("Loading TU data...")
	tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu_from_config(config)
	print("TU data loaded")

	# TODO: choice-set generation (src/choice_set/)

	# Before writing: the output dir is on a network share
	wait_for_output_dir(config["paths"]["output_dir"])


if __name__ == "__main__":
	main()
