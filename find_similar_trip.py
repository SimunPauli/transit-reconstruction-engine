import pandas as pd
def find_similar_trip(
        tu_tur_row,
        candidate_df,
        arrival_dev_weight=1,
        print_devation_details=False):
    candidate_df = candidate_df.copy()
    expected_depart = tu_tur_row["depart_dt"]
    expected_arrival = tu_tur_row["arrival_dt"]

    # Convert candidate_df times to datetime
    candidate_df["start_dt"] = pd.to_datetime(candidate_df["start"], utc=True).dt.tz_convert("Europe/Copenhagen")
    candidate_df["end_dt"] = pd.to_datetime(candidate_df["end"], utc=True).dt.tz_convert("Europe/Copenhagen")

    # Group by iteration_id and get start/end times for each trip
    trips = candidate_df.groupby("iteration_id").agg({
        "start_dt": "first",
        "end_dt": "first", #start/end are start/stop of the whole trip not that leg (deltur)
        "system_notice_tag": "first"
    }).reset_index()

    # Calculate total deviation (in minutes) for each trip
    trips["depart_deviation"] = (trips["start_dt"] - expected_depart).dt.total_seconds() / 60
    trips["arrival_deviation"] = (trips["end_dt"] - expected_arrival).dt.total_seconds() / 60

    trips["total_deviation"] = abs(trips["depart_deviation"]) + abs(trips["arrival_deviation"])*arrival_dev_weight

    if trips.empty or trips["total_deviation"].isna().all():
        print("No trips found with similar departure and arrival times.")
        return None

    # Find the best matching trip
    best_iteration = trips.loc[trips["total_deviation"].idxmin(), "iteration_id"]
    if print_devation_details:
        print(f"Best matching trip: iteration_id = {best_iteration}")
        print(f"Deviation details:")
        print(trips[["iteration_id", "depart_deviation", "arrival_deviation", "total_deviation"]].head(10).sort_values("total_deviation"))

    # Filter candidate_df to get only the best trip
    best_trip_candidate_df = candidate_df[candidate_df["iteration_id"] == best_iteration].copy()

    return best_trip_candidate_df