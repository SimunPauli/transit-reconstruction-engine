import pandas as pd
import numpy as np
from .config_loader import get_config
from .constant import STREET_MODES, TRANSIT_STAGE_MODES

def find_best_match_by_rmse(
		tu_tur_row,
		tu_deltur_sub,
		otp_candidates_df):
	"""
	Find the best matching OTP trip using weighted sum of squares (we call it rmse).

	Calculates weighted squared differences for:
	- Departure time (minutes)
	- Trip duration (minutes): arrival minus departure deviation, so a shifted clock time isn't counted twice
	- Duration per leg (minutes) - split by street_mode vs transit
	- Distance per leg (km) - split by street_mode vs transit

	Parameters "w_" are weights for each of the metrics.
	"""
	config = get_config()
	config_weights = config["squared_error_weights"]
	w_departure_min   = config_weights["w_departure_min"]
	w_trip_duration_min = config_weights["w_trip_duration_min"]
	w_street_mode_min = config_weights["w_street_mode_min"]
	w_transit_min     = config_weights["w_transit_min"]
	w_street_mode_km  = config_weights["w_street_mode_km"]
	w_transit_km      = config_weights["w_transit_km"]

	expected_depart  = tu_tur_row["depart_dt"]
	expected_arrival = tu_tur_row["arrival_dt"]

	# Add the initial wait time to expected_depart. expected_depart is used to pick the
	# best-fitting candidate trip from OTP, but OTP always departs at a time that
	# leaves no wait time, so the wait must be added for the comparison to be fair.
	# A missing wait counts as 0, as in add_tu_deltur_depart_times.
	tu_deltur_sub = tu_deltur_sub.sort_values('Delturnr', ascending=True)
	first_wait = tu_deltur_sub.StageWaitMin[
		tu_deltur_sub.StageMode.isin(TRANSIT_STAGE_MODES)
	].fillna(0).iloc[0]
	expected_depart = expected_depart +  pd.to_timedelta(first_wait, unit = "min")

	legs = otp_candidates_df.copy()

	# Convert trip-level times to local datetime
	legs["start_trip"] = pd.to_datetime(legs["start_trip"], utc=True).dt.tz_convert("Europe/Copenhagen")
	legs["end_trip"]   = pd.to_datetime(legs["end_trip"],   utc=True).dt.tz_convert("Europe/Copenhagen")

	# Map TU leg attributes by Delturnr
	tu_leg_duration = tu_deltur_sub.set_index("Delturnr")["StageDurationMin"].astype(float)
	tu_leg_dist     = tu_deltur_sub.set_index("Delturnr")["StageLength"].astype(float)

	legs["tu_duration_min"] = legs["tu_Delturnr"].map(tu_leg_duration)
	legs["tu_distance_km"]  = legs["tu_Delturnr"].map(tu_leg_dist)

	matched_mask     = legs["tu_Delturnr"].notna()
	street_mode_mask = legs["mode"].isin(STREET_MODES) & matched_mask
	transit_mask     = ~legs["mode"].isin(STREET_MODES) & matched_mask

	# Weighted squared differences — 0 for unmatched legs (no penalty for extra OTP legs)
	legs["weighted_sq_deviation_duration"] = 0.0
	legs["weighted_sq_deviation_distance"] = 0.0

	for mask, w_min, w_km in [
		(street_mode_mask, w_street_mode_min, w_street_mode_km),
		(transit_mask,     w_transit_min,     w_transit_km),
	]:
		legs.loc[mask, "deviation_duration_min"] = (
			(legs.loc[mask, "duration_min"] - legs.loc[mask, "tu_duration_min"]).abs()
		)
		legs.loc[mask, "weighted_sq_deviation_duration"] = (
			w_min * ((legs.loc[mask, "duration_min"] - legs.loc[mask, "tu_duration_min"]) ** 2)
		)
		legs.loc[mask, "deviation_distance_km"] = (
				(legs.loc[mask, "distance_km"] - legs.loc[mask, "tu_distance_km"]).abs()
		)
		legs.loc[mask, "weighted_sq_deviation_distance"] = (
			w_km * ((legs.loc[mask, "distance_km"] - legs.loc[mask, "tu_distance_km"]) ** 2)
		)

	# Aggregate per iteration
	trips = legs.groupby("iteration_id").agg(
		start_trip = ("start_trip", "first"),
		end_trip = ("end_trip", "first"),
		deviation_duration_min = ("deviation_duration_min", "sum"), #sum of abs leg differences, for reporting only, not used in rmse
		weighted_sq_deviation_duration = ("weighted_sq_deviation_duration", "sum"),
		deviation_distance_km = ("deviation_distance_km", "sum"), #sum of abs leg differences, for reporting only, not used in rmse
		weighted_sq_deviation_distance = ("weighted_sq_deviation_distance", "sum"),
		system_notice_tag = ("system_notice_tag", "first"),
	).reset_index()

	# Trip-level time deviations
	trips["depart_deviation_min"]  = (trips["start_trip"] - expected_depart).dt.total_seconds() / 60
	trips["arrival_deviation_min"] = (trips["end_trip"]   - expected_arrival).dt.total_seconds() / 60
	trips["trip_duration_deviation_min"] = trips["arrival_deviation_min"] - trips["depart_deviation_min"]

	trips["weighted_sq_deviation_depart"]        = w_departure_min     * (trips["depart_deviation_min"] ** 2)
	trips["weighted_sq_deviation_trip_duration"] = w_trip_duration_min * (trips["trip_duration_deviation_min"] ** 2)

	trips["sum_weighted_sq_diff"] = (
		trips["weighted_sq_deviation_depart"]    +
		trips["weighted_sq_deviation_trip_duration"] +
		trips["weighted_sq_deviation_duration"]  +
		trips["weighted_sq_deviation_distance"]
	)

	# root for interpretability. Doesn't affect the ranking across trips.
	trips["rmse"] = np.sqrt(trips["sum_weighted_sq_diff"])

	if trips.empty or trips["rmse"].isna().all():
		print("No trips found with valid Weighted Sum of Squares (rmse) values.")
		return None

	return trips