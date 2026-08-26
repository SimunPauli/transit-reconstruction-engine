import pandas as pd
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

