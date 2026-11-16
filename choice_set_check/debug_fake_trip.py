"""
Runs one made-up TU trip through run_choice_set's choice-set generation, for debugging in PyCharm.
Edit the OD, departure and legs below. No TU data or reconstruction run is needed: the fake
legs are the chosen alternative, as for a car trip (run_choice_set.car_chosen).
"""
import sys
from contextlib import redirect_stdout
import pandas as pd
from src.config_loader import get_config, start_run
from src.constant import MODE_MAP, LOCAL_TIMEZONE
from src.run_utils import Tee
from run_choice_set import generate_choice_sets, car_chosen

ORIGIN = (55.66660, 12.60356)       # lat, lon
DESTINATION = (55.78022, 12.51670)  # lat, lon
DEPART = "2025-05-13 08:00"       # local time; must be inside the OTP graph's GTFS period
# (StageMode, StageLength km, StageDurationMin, StageWaitMin), in order. StageMode codes: see MODE_MAP
LEGS = [
	(11, 14.0, 25, 0),  # car
]
TUR_ID = -1  # seeds the random exclusion rounds, like a real TurId


def fake_tu_trip():
	"""tu_tur and tu_deltur for the fake trip, with the columns load_TU_data.load_tu_from_config gives."""
	depart_dt = pd.Timestamp(DEPART, tz=LOCAL_TIMEZONE)
	# First leg's wait is before departure (as add_tu_deltur_depart_times)
	arrival_dt = depart_dt + pd.Timedelta(minutes=sum(duration + wait for _, _, duration, wait in LEGS) - LEGS[0][3])
	tu_tur = pd.DataFrame([{
		"TurId": TUR_ID, "SessionId": TUR_ID, "PrimMode": LEGS[0][0],
		"orig_lat": ORIGIN[0], "orig_lon": ORIGIN[1],
		"tiladrlat": DESTINATION[0], "tiladrlon": DESTINATION[1],
		"depart_dt": depart_dt, "depart_dt_str": depart_dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
		"arrival_dt": arrival_dt, "arrival_dt_str": arrival_dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
	}])
	tu_deltur = pd.DataFrame([{
		"TurId": TUR_ID, "SessionId": TUR_ID, "Delturnr": i, "n_deltur": len(LEGS),
		"StageMode": mode, "StageLength": length, "StageDurationMin": duration, "StageWaitMin": wait,
		"Route": None, "FromStation": None, "ToStation": None, "otp_mode": MODE_MAP[mode],
	} for i, (mode, length, duration, wait) in enumerate(LEGS, start=1)])
	return tu_tur, tu_deltur


def main():
	config = start_run(get_config(), stage="choice_set")
	config["run"]["max_workers"] = 1
	tu_tur, tu_deltur = fake_tu_trip()
	with open(config["choice_set"]["paths"]["log_file"], "w", encoding="utf-8") as log_file:
		tee = Tee(sys.stdout, log_file)
		with redirect_stdout(tee):
			# WALK access/egress, as car_choice_set
			generate_choice_sets(config, tee, tu_tur, tu_deltur, lambda tu_tur_row: car_chosen(tu_tur_row, tu_deltur),
				chosen_public_transport=False,
				choice_set_file=config["choice_set"]["paths"]["car_choice_set_file"], access_egress_mode="WALK")


if __name__ == "__main__":
	main()
