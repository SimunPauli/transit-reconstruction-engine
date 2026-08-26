import pandas as pd

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
	(counts and success rate) and a breakdown of failure_reason frequency among the trips
	not found.
	"""
	total = len(trip_matching_summaries_df)
	found = int(trip_matching_summaries_df["trip_found"].sum())
	not_found = total - found

	overview_df = pd.DataFrame([{
		"total_trips": total,
		"trips_found": found,
		"trips_not_found": not_found,
		"success_rate_pct": round(100 * found / total, 1) if total else 0.0
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


def _print_and_export_summary_stats(trip_matching_summaries_df, path):
	overview_df, failure_counts_df = _build_summary_stats(trip_matching_summaries_df)

	print("\nRun summary:")
	print(overview_df.to_string(index=False))
	if not failure_counts_df.empty:
		print("\nFailure reason breakdown:")
		print(failure_counts_df.to_string(index=False))

	with pd.ExcelWriter(path) as writer:
		overview_df.to_excel(writer, sheet_name="overview", index=False)
		failure_counts_df.to_excel(writer, sheet_name="failure_reasons", index=False)
	print(f"Summary statistics exported to {path}")

