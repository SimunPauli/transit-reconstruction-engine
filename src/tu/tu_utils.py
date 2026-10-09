import pandas as pd
from src import otp
from src.constant import WALK_STAGE_MODES, CAR_STAGE_MODES, WALK_CAR_ABSORB_MAX_KM, ACCESS_EGRESS_MODE_MAP


def tu_endpoints(tu_tur_row, origin_stop_id=None, destination_stop_id=None):
	"""OTP origin/destination locations: the given GTFS stop, else the TU trip's own coordinate."""
	origin = otp.client.stop_location(origin_stop_id) if origin_stop_id else otp.client.coordinate_location(tu_tur_row["orig_lat"], tu_tur_row["orig_lon"])
	destination = otp.client.stop_location(destination_stop_id) if destination_stop_id else otp.client.coordinate_location(tu_tur_row["tiladrlat"], tu_tur_row["tiladrlon"])
	return origin, destination


def get_access_egress(tu_deltur_sub: pd.DataFrame):
	first_mode = int(tu_deltur_sub["StageMode"].iloc[0])
	last_mode = int(tu_deltur_sub["StageMode"].iloc[-1])

	def _normalise_access_egress_mode(stage_mode: int, side: str):
		if stage_mode < 27:  # Street modes are less than 27 in TU StageMode
			mode = ACCESS_EGRESS_MODE_MAP.get(stage_mode, "WALK")
		else:
			mode = "WALK"  # fallback

		if mode == "CAR_DROP_OFF":
			if side == "access":
				return ["WALK", "CAR_DROP_OFF"]
			if side == "egress":
				return ["WALK", "CAR_PICKUP"]

		return mode

	tu_access = _normalise_access_egress_mode(first_mode, "access")
	tu_egress = _normalise_access_egress_mode(last_mode, "egress")

	return tu_access, tu_egress


def absorb_short_walk_into_car(tu_deltur_sub, max_walk_km=WALK_CAR_ABSORB_MAX_KM, verbose=True):
	"""
	Merges a short WALK leg (e.g. walking to/from a parked car) into an adjacent CAR-family
	leg. Must run before anything derives access/egress mode from tu_deltur_sub's outer legs
	(is_car_access/is_car_egress and friends), so a short WALK doesn't hide an adjacent CAR
	leg from them.
	"""
	rows = tu_deltur_sub.sort_values("Delturnr").to_dict("records")

	merged_any = True
	while merged_any:
		merged_any = False
		for i, row in enumerate(rows):
			length = row.get("StageLength")
			#If StageMode is walk (or wheelchair) check if CAR is in nabor deltur
			if int(row["StageMode"]) not in WALK_STAGE_MODES or pd.isna(length) or length > max_walk_km:
				continue

			if i > 0 and int(rows[i - 1]["StageMode"]) in CAR_STAGE_MODES:
				car_idx = i - 1
			elif i + 1 < len(rows) and int(rows[i + 1]["StageMode"]) in CAR_STAGE_MODES:
				car_idx = i + 1
			else:
				continue

			car_row = rows[car_idx]
			car_row["StageLength"] = (car_row.get("StageLength") or 0) + length
			car_row["StageDurationMin"] = (car_row.get("StageDurationMin") or 0) + (row.get("StageDurationMin") or 0)
			if car_idx > i:  # WALK came first, so its wait is the wait before the merged leg
				car_row["StageWaitMin"] = row.get("StageWaitMin")

			if verbose:
				print(
					f"WALK_CAR_ABSORBED: merged Delturnr {row['Delturnr']} "
					f"(WALK, {length} km) into Delturnr {car_row['Delturnr']} (CAR)"
				)
			del rows[i]
			merged_any = True
			break

	return pd.DataFrame(rows, columns=tu_deltur_sub.columns).reset_index(drop=True)

def add_tu_deltur_depart_times(tu_deltur_sub, depart_dt):
	"""
	Computes each TU leg's departure/arrival time as local HH:MM strings.
	"""
	tu_deltur_sub = tu_deltur_sub.sort_values("Delturnr").copy()
	wait = tu_deltur_sub["StageWaitMin"].fillna(0).astype(float)
	duration = tu_deltur_sub["StageDurationMin"].fillna(0).astype(float)
	wait_after_first = wait.copy()
	wait_after_first.iloc[0] = 0

	offset_min = duration.cumsum().shift(fill_value=0) + wait_after_first.cumsum()
	depart_time = depart_dt + pd.to_timedelta(offset_min, unit="m")
	arrival_time = depart_time + pd.to_timedelta(duration, unit="m")

	tu_deltur_sub["tu_deltur_depart_time"] = depart_time.dt.strftime("%H:%M")
	tu_deltur_sub["tu_deltur_arrival_time"] = arrival_time.dt.strftime("%H:%M")
	return tu_deltur_sub

