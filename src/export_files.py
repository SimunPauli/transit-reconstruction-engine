import pandas as pd
from .constant import ROUTE_MATCH_LEVELS

RMSE_MATCH_COLUMN_ORDER = [
	"TurId",
	"leg_id",
	"iteration_id",
	"tu_Delturnr",
	"start_trip",
	"end_trip",
	"start_leg",
	"end_leg",
	"tu_deltur_depart_time",
	"otp_leg_depart_time",
	"waitingtime",
	"duration_min",
	"mode",
	"route_short_name",
	"interlined_route_short_names",
	"route_gtfs_id",
	"trip_short_name",
	"interlined_trip_short_names",
	"from",
	"to",
	"from_gtfs_id",
	"to_gtfs_id",
	"distance_km",
	"is_bike_placeholder",
	"generalized_cost",
	"system_notice_tag",
	"system_notice_text",
	"leg_geometry",
]


def _reorder_rmse_columns(df):
	return df[RMSE_MATCH_COLUMN_ORDER]


def _write_failures_file(trip_matching_summaries_df, path):
	"""
	Writes TurId + failure_reason (a short code, no coordinates/station names or other
	survey data) for every trip that wasn't reconstructed, so failure causes can be
	reviewed or shared without exposing personal information from the survey.
	"""
	failures_df = trip_matching_summaries_df.loc[
		trip_matching_summaries_df["trip_not_found"] == 1, ["TurId", "failure_reason"]
	]
	failures_df.to_csv(path, sep=",", decimal = ".", index=False)


def _build_summary_stats(trip_matching_summaries_df):
	"""
	Builds run-level summary tables from the per-trip matching summaries: an overview
	(counts and success rates for each of the four outcomes - found with the TU-recorded
	routes, found with a near route name, found only by ignoring them, not found) and a
	breakdown of failure_reason frequency among the trips not found.
	"""
	total = len(trip_matching_summaries_df)
	found = int(trip_matching_summaries_df["trip_found"].sum())
	near_route = int(trip_matching_summaries_df["trip_near_route"].sum())
	wrong_route = int(trip_matching_summaries_df["trip_wrong_route"].sum())
	not_found = int(trip_matching_summaries_df["trip_not_found"].sum())

	route_match_counts = trip_matching_summaries_df["route_match"].value_counts()
	pct = lambda count: round(100 * count / total, 1) if total else 0.0

	overview_df = pd.DataFrame([{
		"total_trips": total,
		"trips_found": found,
		"trips_near_route": near_route,
		"trips_wrong_route": wrong_route,
		"trips_not_found": not_found,
		# BUS/S_TRAIN trips found, by how far the TU route name had to be widened
		**{f"route_match_{level}": int(route_match_counts.get(level, 0)) for level in ROUTE_MATCH_LEVELS},
		"success_rate_pct": pct(found),
		"success_rate_incl_near_route_pct": pct(found + near_route),
		"success_rate_incl_wrong_route_pct": pct(found + near_route + wrong_route)
	}])

	failures_df = trip_matching_summaries_df.loc[trip_matching_summaries_df["trip_not_found"] == 1]
	failure_counts_df = (
		failures_df["failure_reason"]
		.value_counts()
		.rename_axis("failure_reason")
		.reset_index(name="count")
		.sort_values("count", ascending=False)
		.reset_index(drop=True)
	)
	failure_counts_df["pct_of_total"] = round(100 * failure_counts_df["count"] / total, 1) if total else 0.0
	failure_counts_df["pct_of_failures"] = round(100 * failure_counts_df["count"] / not_found, 1) if not_found else 0.0

	return overview_df, failure_counts_df


STATION_STAGE_MODES = [32, 33, 34]  # S-train, other train, metro: the TU legs that record stations


def _build_station_stats(trip_matching_summaries_df, tu_deltur, tu_gtfs_station_df):
	"""
	Match outcome per TU station and mode, over the trips boarding or alighting there, with the
	GTFS stop_ids the station maps to. A station failing far more often than others points at a
	station-mapping or GTFS problem there (e.g. a stop split into two stop_ids).
	"""
	legs = tu_deltur.loc[
		tu_deltur["StageMode"].isin(STATION_STAGE_MODES), ["TurId", "otp_mode", "FromStation", "ToStation"]
	]
	outcomes = trip_matching_summaries_df[
		["TurId", "trip_found", "trip_near_route", "trip_wrong_route", "trip_not_found", "failure_reason"]
	]
	visits = (
		legs.melt(id_vars=["TurId", "otp_mode"], value_vars=["FromStation", "ToStation"], value_name="tu_station_name")
		.dropna(subset=["tu_station_name"])
		.drop_duplicates(["TurId", "otp_mode", "tu_station_name"])
		.merge(outcomes, on="TurId")
	)
	keys = ["otp_mode", "tu_station_name"]
	stats = visits.groupby(keys).agg(
		trips=("TurId", "size"),
		trips_found=("trip_found", "sum"),
		trips_near_route=("trip_near_route", "sum"),
		trips_wrong_route=("trip_wrong_route", "sum"),
		trips_not_found=("trip_not_found", "sum"),
	).reset_index()
	stats["failure_rate_pct"] = (100 * stats["trips_not_found"] / stats["trips"]).round(1)

	top_failure_reason = (
		visits.loc[visits["trip_not_found"] == 1]
		.groupby(keys)["failure_reason"]
		.agg(lambda reasons: reasons.value_counts().index[0])
		.rename("top_failure_reason")
	)
	gtfs_station_ids = (  # empty if the station has no mapping
		tu_gtfs_station_df.groupby(keys)["gtfs_station_id"]
		.agg(lambda stop_ids: "/".join(dict.fromkeys(stop_ids)))
		.rename("gtfs_station_ids")
	)
	return (
		stats.join(top_failure_reason, on=keys)
		.join(gtfs_station_ids, on=keys)
		.sort_values(["trips_not_found", "failure_rate_pct"], ascending=False)
		.reset_index(drop=True)
	)


SESSION_FLAG_MIN_PROBLEM_TRIPS = 2  # sessions with at least this many near-route/wrong-route/not-found trips are flagged


def _build_session_stats(trip_matching_summaries_df):
	"""
	Match outcome per SessionId (one interview). Several problem trips (near/wrong route or not found) in
	one session point at that respondent's reporting rather than at OTP or the GTFS data.
	"""
	df = trip_matching_summaries_df
	problem_reason = (
		df.get("failure_reason", pd.Series(pd.NA, index=df.index))
		.where(df["trip_not_found"] == 1)
		.mask(df["trip_near_route"] == 1, "near_route")
		.mask(df["trip_wrong_route"] == 1, "wrong_route")
	)
	stats = df.groupby("SessionId").agg(
		trips=("TurId", "size"),
		trips_found=("trip_found", "sum"),
		trips_near_route=("trip_near_route", "sum"),
		trips_wrong_route=("trip_wrong_route", "sum"),
		trips_not_found=("trip_not_found", "sum"),
	).reset_index()
	stats["problem_trips"] = stats["trips_near_route"] + stats["trips_wrong_route"] + stats["trips_not_found"]
	stats["problem_rate_pct"] = (100 * stats["problem_trips"] / stats["trips"]).round(1)
	failure_reasons = (
		problem_reason.groupby(df["SessionId"])
		.agg(lambda reasons: "/".join(f"{reason}×{count}" for reason, count in reasons.value_counts().items()))
		.rename("failure_reasons")
	)
	stats = stats.join(failure_reasons, on="SessionId")
	stats["flagged"] = stats["problem_trips"] >= SESSION_FLAG_MIN_PROBLEM_TRIPS
	return stats.sort_values(["problem_trips", "problem_rate_pct"], ascending=False).reset_index(drop=True)


def _print_and_export_summary_stats(trip_matching_summaries_df, path, tu_deltur=None, tu_gtfs_station_df=None):
	overview_df, failure_counts_df = _build_summary_stats(trip_matching_summaries_df)
	session_stats_df = _build_session_stats(trip_matching_summaries_df)
	station_stats_df = None
	if tu_deltur is not None and tu_gtfs_station_df is not None:
		station_stats_df = _build_station_stats(trip_matching_summaries_df, tu_deltur, tu_gtfs_station_df)

	print("\nRun summary:")
	print(overview_df.to_string(index=False))
	if not failure_counts_df.empty:
		print("\nFailure reason breakdown:")
		print(failure_counts_df.to_string(index=False))
	if station_stats_df is not None and not station_stats_df.empty:
		print("\nStations with the most trips not found (all in the 'stations' sheet):")
		print(station_stats_df.head(10).to_string(index=False))
	n_flagged = int(session_stats_df["flagged"].sum())
	if n_flagged:
		print(f"\n{n_flagged} session(s) with {SESSION_FLAG_MIN_PROBLEM_TRIPS}+ problem trips; top 10 (all in the 'sessions' sheet):")
		print(session_stats_df.head(10).to_string(index=False))

	with pd.ExcelWriter(path) as writer:
		overview_df.to_excel(writer, sheet_name="overview", index=False)
		failure_counts_df.to_excel(writer, sheet_name="failure_reasons", index=False)
		session_stats_df.to_excel(writer, sheet_name="sessions", index=False)
		if station_stats_df is not None:
			station_stats_df.to_excel(writer, sheet_name="stations", index=False)
	print(f"Summary statistics exported to {path}")

