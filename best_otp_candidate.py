import pandas as pd
from config import get_config

def find_best_match_by_rmse(
		tu_tur_row,
		tu_deltur_sub,
		otp_candidates_df):
	"""
	Find the best matching OTP trip using weighted sum of squares (we call it rmse).

	Calculates weighted squared differences for:
	- Departure time (minutes)
	- Arrival time (minutes)
	- Duration per leg (minutes) - split by street_mode vs transit
	- Distance per leg (km) - split by street_mode vs transit

	Parameters "w_" are weights for each of the metrics.
	"""
	config = get_config()
	config_weights = config["squared_error_weights"]
	w_departure_min   = config_weights["w_departure_min"]
	w_arrival_min     = config_weights["w_arrival_min"]
	w_street_mode_min = config_weights["w_street_mode_min"]
	w_transit_min     = config_weights["w_transit_min"]
	w_street_mode_km  = config_weights["w_street_mode_km"]
	w_transit_km      = config_weights["w_transit_km"]

	expected_depart  = tu_tur_row["depart_dt"]
	expected_arrival = tu_tur_row["arrival_dt"]

	legs = otp_candidates_df.copy()

	# Convert trip-level times to local datetime
	legs["start_trip"] = pd.to_datetime(legs["start_trip"], utc=True).dt.tz_convert("Europe/Copenhagen")
	legs["end_trip"]   = pd.to_datetime(legs["end_trip"],   utc=True).dt.tz_convert("Europe/Copenhagen")

	# Map TU leg attributes by Delturnr
	tu_leg_duration = tu_deltur_sub.set_index("Delturnr")["StageDurationMin"].astype(float)
	tu_leg_dist     = tu_deltur_sub.set_index("Delturnr")["StageLength"].astype(float)

	legs["tu_duration_min"] = legs["tu_Delturnr"].map(tu_leg_duration)
	legs["tu_distance_km"]  = legs["tu_Delturnr"].map(tu_leg_dist)

	street_modes = {
		"WALK", "BIKE", "BIKE_RENTAL", "BIKE_TO_PARK", "CAR", "CARPOOL",
		"CAR_HAILING", "CAR_RENTAL", "CAR_TO_PARK", "FLEXIBLE", "SCOOTER_RENTAL"
	}

	matched_mask     = legs["tu_Delturnr"].notna()
	street_mode_mask = legs["mode"].isin(street_modes) & matched_mask
	transit_mask     = ~legs["mode"].isin(street_modes) & matched_mask

	# Weighted squared differences — 0 for unmatched legs (no penalty for extra OTP legs)
	legs["weighted_sq_diff_duration"] = 0.0
	legs["weighted_sq_diff_distance"] = 0.0

	for mask, w_min, w_km in [
		(street_mode_mask, w_street_mode_min, w_street_mode_km),
		(transit_mask,     w_transit_min,     w_transit_km),
	]:
		legs.loc[mask, "weighted_sq_diff_duration"] = (
			w_min * ((legs.loc[mask, "duration_min"] - legs.loc[mask, "tu_duration_min"]) ** 2)
		)
		legs.loc[mask, "weighted_sq_diff_distance"] = (
			w_km * ((legs.loc[mask, "distance_km"] - legs.loc[mask, "tu_distance_km"]) ** 2)
		)

	# Aggregate per iteration
	trips = legs.groupby("iteration_id").agg(
		start_trip = ("start_trip", "first"),
		end_trip = ("end_trip", "first"),
		weighted_sq_diff_duration = ("weighted_sq_diff_duration", "sum"),
		weighted_sq_diff_distance = ("weighted_sq_diff_distance", "sum"),
		system_notice_tag = ("system_notice_tag", "first"),
	).reset_index()

	# Trip-level time deviations
	trips["depart_deviation_min"]  = (trips["start_trip"] - expected_depart).dt.total_seconds() / 60
	trips["arrival_deviation_min"] = (trips["end_trip"]   - expected_arrival).dt.total_seconds() / 60

	trips["weighted_sq_diff_depart"]  = w_departure_min * (trips["depart_deviation_min"] ** 2)
	trips["weighted_sq_diff_arrival"] = w_arrival_min   * (trips["arrival_deviation_min"] ** 2)

	trips["sum_weighted_sq_diff"] = (
		trips["weighted_sq_diff_depart"]    +
		trips["weighted_sq_diff_arrival"]   +
		trips["weighted_sq_diff_duration"]  +
		trips["weighted_sq_diff_distance"]
	)

	# root for interpretability. Doesn't affect the ranking across trips.
	trips["rmse"] = np.sqrt(trips["sum_weighted_sq_diff"])

	if trips.empty or trips["rmse"].isna().all():
		print("No trips found with valid Weighted Sum of Squares (rmse) values.")
		return None

	return trips