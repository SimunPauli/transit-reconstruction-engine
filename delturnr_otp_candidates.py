import pandas as pd
from collections import Counter
from constant import WALK_BIKE_TIME_RATIO
from otp_utils import route_names_match

def add_tu_delturnr_to_otp_candidates(
		otp_candidates_df,
		tu_deltur_sub,
		tu_gtfs_station_df,
		bike_stage_modes=(2,8),
		diagnostics=None
):
	"""
	Add a tu_Delturnr column to OTP legs by aligning each OTP itinerary with the TU leg sequence.

	OTP may contain legs missing from TU, especially transfer WALK legs between transit legs.
	Those unmatched OTP legs get pd.NA.

	Bicycle TU legs can be matched to OTP WALK legs as placeholders. For those legs,
	duration_min is multiplied by walk_bike_ratio to approximate cycling time.

	If `diagnostics` is a list, one record is appended for every itinerary that failed
	to consume the whole TU leg sequence, saying which TU leg alignment stalled on and
	which OTP legs came close. Pass summarize_alignment_diagnostics() the same list to
	turn it into something printable.
	"""
	from tu_gtfs_stations_match import _normalise_name

	tu_deltur_sub = (
		tu_deltur_sub
		.sort_values("Delturnr")
		.reset_index(drop=True)
		.copy()
	)

	bike_stage_modes = set(bike_stage_modes)

	# Build a lookup: (otp_mode, tu_station_name) → set of gtfs_station_ids.
	# station_names keeps the GTFS name each id was matched to, purely so a failed
	# alignment can say *which* stop it expected rather than just an opaque id.
	station_lookup = {}
	station_names = {}
	if tu_gtfs_station_df is not None and not tu_gtfs_station_df.empty:
		for _, row in tu_gtfs_station_df.iterrows():
			key = (row["otp_mode"],_normalise_name(str(row["tu_station_name"])))
			if key not in station_lookup:
				station_lookup[key] = set()
			station_lookup[key].add(row["gtfs_station_id"])
			station_names[row["gtfs_station_id"]] = row.get("gtfs_station_name")

	def _route_matches(tu_route, otp_route, mode):
		if tu_route is None or (isinstance(tu_route, float) and pd.isna(tu_route)):
			return True
		if otp_route is None or (isinstance(otp_route, float) and pd.isna(otp_route)):
			return False
		if not str(tu_route).strip():
			return True
		# BUS route names are free-texted in TU, so respondents both misspace them
		# ('102 A') and drop the trailing letter ('150' for '150S'). S_TRAIN comes
		# from a survey dropdown, so it is compared without the letter fallback.
		return route_names_match(tu_route, otp_route, allow_missing_letter=(mode == "BUS"))

	def _station_mismatch(tu_station_name, otp_gtfs_id, otp_stop_name, mode):
		"""None if the TU station matches the OTP stop, else why it does not.

		The message names both sides and, when the TU station *is* in the mapping,
		the stop the mapping expected - the two failure modes look identical from
		the outside but need opposite fixes: a missing (mode, station) entry means
		the station match never ran or was filtered out, while a present entry
		pointing at a different id means the station match picked the wrong stop
		(typically the twin stop of an interchange, e.g. the S-tog platform where
		the metro platform was meant).
		"""
		if tu_station_name is None or (isinstance(tu_station_name, float) and pd.isna(tu_station_name)):
			return None
		if otp_gtfs_id is None and pd.isna(otp_gtfs_id):
			return f"OTP leg has no stop id to compare against TU station {tu_station_name!r}"

		lookup_key = (mode, _normalise_name(str(tu_station_name)))
		expected = station_lookup.get(lookup_key)
		if expected is None:
			return (
				f"TU station {tu_station_name!r} has no ({mode}) entry in the station mapping"
			)
		if otp_gtfs_id in expected:
			return None

		expected_desc = ", ".join(
			f"{sid} ({station_names.get(sid)})" if station_names.get(sid) else str(sid)
			for sid in sorted(expected)
		)
		otp_desc = f"{otp_gtfs_id} ({otp_stop_name})" if otp_stop_name else str(otp_gtfs_id)
		return (
			f"TU station {tu_station_name!r} maps to {expected_desc} for {mode}, "
			f"but the OTP leg uses {otp_desc}"
		)

	def _leg_mismatch_reason(otp_leg, tu_deltur_sub_leg):
		"""None if the legs match, else (kind, description) for the first failing check.

		kind is "mode" for the ordinary case of an OTP leg TU does not record at all
		(a transfer walk), and "route"/"station" when the modes agreed but the leg is
		still not the one TU described. Only the latter two are worth reporting: they
		mean OTP produced a leg of exactly the right shape that alignment nevertheless
		rejected, which is the case that looks like "OTP cannot find the trip" from
		the outside while OTP in fact returned it.
		"""
		tu_mode = tu_deltur_sub_leg.get("otp_mode")
		tu_stage_mode = int(tu_deltur_sub_leg.get("StageMode"))

		if tu_stage_mode in bike_stage_modes:
			tu_mode = "WALK"
		elif pd.isna(tu_mode):
			tu_mode = "WALK"  #TODO: All missing modes are set to WALK!

		if otp_leg["mode"] != tu_mode:
			return "mode", f"OTP leg is {otp_leg['mode']}, TU leg is {tu_mode}"

		# For BUS/S_TRAIN, check route name
		if otp_leg["mode"] in {"BUS", "S_TRAIN"}:
			if not _route_matches(tu_deltur_sub_leg.get("Route"), otp_leg.get("route_short_name"), otp_leg["mode"]):
				return "route", (
					f"{otp_leg['mode']} route {otp_leg.get('route_short_name')!r} "
					f"does not match TU route {tu_deltur_sub_leg.get('Route')!r}"
				)
		# For transit with stations, check station match using GTFS mapping
		elif otp_leg["mode"] in {"SUBWAY", "RAIL", "S_TRAIN"}:
			for tu_col, id_col, name_col, end in (
				("FromStation", "from_gtfs_id", "from", "boarding"),
				("ToStation", "to_gtfs_id", "to", "alighting"),
			):
				mismatch = _station_mismatch(
					tu_deltur_sub_leg.get(tu_col),
					otp_leg.get(id_col),
					otp_leg.get(name_col),
					otp_leg["mode"],
				)
				if mismatch is not None:
					return "station", f"{end}: {mismatch}"

			#TRAM and FERRY only have stops and route_name when it has been added manually during data-processing
		return None

	def _describe_tu_leg(tu_deltur_sub_leg):
		"""One-line description of a TU leg, for alignment diagnostics."""
		mode = tu_deltur_sub_leg.get("otp_mode")
		mode = str(mode) if not pd.isna(mode) else f"StageMode {tu_deltur_sub_leg.get('StageMode')}"
		parts = [f"Delturnr {tu_deltur_sub_leg.get('Delturnr')}", mode]
		route = tu_deltur_sub_leg.get("Route")
		if route is not None and not pd.isna(route) and str(route).strip():
			parts.append(f"route {route}")
		frm, to = tu_deltur_sub_leg.get("FromStation"), tu_deltur_sub_leg.get("ToStation")
		if not pd.isna(frm) or not pd.isna(to):
			parts.append(f"{frm} -> {to}")
		return ", ".join(parts)

	def _align_iteration(iteration_df):
		iteration_id = iteration_df.name
		iteration_df = iteration_df.sort_values("leg_id").copy()

		delturnrs = []
		is_bike_placeholders = []
		tu_pos = 0
		# Every OTP leg that was the right mode for the TU leg alignment is currently
		# stuck on, but was rejected on route or station. Reset whenever alignment
		# advances, so what survives describes the leg it never got past.
		near_misses = []

		for _, otp_leg in iteration_df.iterrows():
			matched_delturnr = pd.NA
			is_bike_placeholder = False

			while tu_pos < len(tu_deltur_sub):
				tu_deltur_sub_leg = tu_deltur_sub.iloc[tu_pos]

				mismatch = _leg_mismatch_reason(otp_leg, tu_deltur_sub_leg)
				if mismatch is None:
					matched_delturnr = tu_deltur_sub_leg["Delturnr"]
					is_bike_placeholder = (
						otp_leg["mode"] == "WALK" and (tu_deltur_sub_leg["StageMode"] in bike_stage_modes)
					)
					tu_pos += 1
					near_misses = []
					break

				kind, description = mismatch
				if kind != "mode":
					near_misses.append(description)

				# If the current TU leg does not match this OTP leg, do not consume the TU leg.
				# The OTP leg is treated as an extra OTP leg, e.g. a transfer walk missing in TU.
				break

			delturnrs.append(matched_delturnr)
			is_bike_placeholders.append(is_bike_placeholder)

		# Alignment is greedy and never backtracks, so running out of OTP legs with TU
		# legs left over means it stalled on tu_pos and every later TU leg went
		# unmatched as a consequence. Record that one leg, not the pile it dragged down.
		if diagnostics is not None and tu_pos < len(tu_deltur_sub):
			diagnostics.append({
				"iteration_id": iteration_id,
				"stalled_on": _describe_tu_leg(tu_deltur_sub.iloc[tu_pos]),
				"tu_legs_matched": tu_pos,
				"tu_legs_total": len(tu_deltur_sub),
				"near_misses": near_misses,
			})

		iteration_df["tu_Delturnr"] = delturnrs
		iteration_df["is_bike_placeholder"] = is_bike_placeholders
		iteration_df["iteration_id"] = iteration_id

		return iteration_df

	otp_candidates_df = (
		otp_candidates_df
		.groupby("iteration_id", group_keys=False)
		.apply(_align_iteration)
		.reset_index(drop=True)
	)
	otp_candidates_df["duration_min_otp"] = otp_candidates_df["duration_min"]
	otp_candidates_df["duration_min_otp"] = otp_candidates_df["duration_min"].astype(float)
	otp_candidates_df.loc[otp_candidates_df["is_bike_placeholder"], "duration_min"] = (
		otp_candidates_df.loc[otp_candidates_df["is_bike_placeholder"], "duration_min"]
		* WALK_BIKE_TIME_RATIO
	).round().astype(int)
	otp_candidates_df.loc[otp_candidates_df["is_bike_placeholder"], "mode"] = "BICYCLE"
	return otp_candidates_df


def summarize_alignment_diagnostics(diagnostics, max_stall_points=3, max_reasons=4):
	"""Turn add_tu_delturnr_to_otp_candidates() diagnostics into a printable summary.

	Returns "" when nothing failed. The point of the summary is to separate the two
	explanations that no_matching_leg_sequence otherwise collapses together:

	  * near misses listed  -> OTP *did* return a leg of the right mode and alignment
	    rejected it, so the fault is in the station mapping or the route comparison;
	  * no near misses      -> OTP never produced a leg of that mode at that point in
	    the itinerary, so the fault is upstream in the query or the feed.
	"""
	if not diagnostics:
		return ""

	total = len(diagnostics)
	by_stall = Counter(d["stalled_on"] for d in diagnostics)

	lines = [
		"  Leg alignment failed for the only itinerary; where it stopped:"
		if total == 1 else
		f"  Leg alignment failed for all {total} itineraries; where each one stopped:"
	]
	for stalled_on, count in by_stall.most_common(max_stall_points):
		matched = min(d["tu_legs_matched"] for d in diagnostics if d["stalled_on"] == stalled_on)
		total_legs = diagnostics[0]["tu_legs_total"]
		lines.append(
			f"    {count}/{total} stalled after {matched}/{total_legs} TU legs, on [{stalled_on}]"
		)
		reasons = Counter(
			reason
			for d in diagnostics if d["stalled_on"] == stalled_on
			for reason in set(d["near_misses"])
		)
		if not reasons:
			lines.append(
				"      no OTP leg of that mode reached this point - OTP did not return the leg"
			)
			continue
		for reason, reason_count in reasons.most_common(max_reasons):
			lines.append(f"      [{reason_count}x] {reason}")
		if len(reasons) > max_reasons:
			lines.append(f"      ... and {len(reasons) - max_reasons} other reason(s)")

	if len(by_stall) > max_stall_points:
		lines.append(f"    ... and {len(by_stall) - max_stall_points} other stall point(s)")

	return "\n".join(lines)
