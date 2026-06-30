import pandas as pd
from constant import WALK_BIKE_TIME_RATIO

def add_tu_delturnr_to_otp_candidates(
		otp_candidates_df,
		tu_deltur_sub,
		tu_gtfs_station_df,
		bike_stage_modes=(2,8)
):
	"""
	Add a tu_Delturnr column to OTP legs by aligning each OTP itinerary with the TU leg sequence.

	OTP may contain legs missing from TU, especially transfer WALK legs between transit legs.
	Those unmatched OTP legs get pd.NA.

	Bicycle TU legs can be matched to OTP WALK legs as placeholders. For those legs,
	duration_min is multiplied by walk_bike_ratio to approximate cycling time.
	"""
	from tu_gtfs_stations_match import _normalise_name

	tu_deltur_sub = (
		tu_deltur_sub
		.sort_values("Delturnr")
		.reset_index(drop=True)
		.copy()
	)

	bike_stage_modes = set(bike_stage_modes)

	# Build a lookup: (otp_mode, tu_station_name) → set of gtfs_station_ids
	station_lookup = {}
	if tu_gtfs_station_df is not None and not tu_gtfs_station_df.empty:
		for _, row in tu_gtfs_station_df.iterrows():
			key = (row["otp_mode"],_normalise_name(str(row["tu_station_name"])))
			if key not in station_lookup:
				station_lookup[key] = set()
			station_lookup[key].add(row["gtfs_station_id"])

	def _route_matches(tu_route, otp_route):
		if tu_route is None or (isinstance(tu_route, float) and pd.isna(tu_route)):
			return True
		if otp_route is None or (isinstance(otp_route, float) and pd.isna(otp_route)):
			return False
		tu_route = str(tu_route).strip()
		otp_route = str(otp_route).strip()
		if not tu_route:
			return True
		return tu_route == otp_route

	def _station_matches(tu_station_name, otp_gtfs_id, mode):
		"""Check if TU station matches OTP stop using GTFS mapping."""
		if tu_station_name is None or (isinstance(tu_station_name, float) and pd.isna(tu_station_name)):
			return True
		if otp_gtfs_id is None and pd.isna(otp_gtfs_id):
			return False

		lookup_key = (mode, _normalise_name(str(tu_station_name)))
		if lookup_key in station_lookup:
			return otp_gtfs_id in station_lookup[lookup_key]

		return False

	def _leg_matches(otp_leg, tu_deltur_sub_leg):
		tu_mode = tu_deltur_sub_leg.get("otp_mode")
		tu_stage_mode = int(tu_deltur_sub_leg.get("StageMode"))

		if tu_stage_mode in bike_stage_modes:
			tu_mode = "WALK"
		elif pd.isna(tu_mode):
			tu_mode = "WALK"  #TODO: All missing modes are set to WALK!

		if otp_leg["mode"] != tu_mode:
			return False

		# For BUS/S_TRAIN, check route name
		if otp_leg["mode"] in {"BUS", "S_TRAIN"}:
			if not _route_matches(tu_deltur_sub_leg.get("Route"), otp_leg.get("route_short_name")):
				return False
		# For transit with stations, check station match using GTFS mapping
		elif otp_leg["mode"] in {"SUBWAY", "RAIL", "S_TRAIN"}:
			if not _station_matches(
				tu_deltur_sub_leg.get("FromStation"),
				otp_leg.get("from_gtfs_id"),
				otp_leg["mode"]
			):
				return False
			if not _station_matches(
				tu_deltur_sub_leg.get("ToStation"),
				otp_leg.get("to_gtfs_id"),
				otp_leg["mode"]
			):
				return False

			#TRAM and FERRY only have stops and route_name when it has been added manually during data-processing
		return True

	def _align_iteration(iteration_df):
		iteration_id = iteration_df.name
		iteration_df = iteration_df.sort_values("leg_id").copy()

		delturnrs = []
		is_bike_placeholders = []
		tu_pos = 0

		for _, otp_leg in iteration_df.iterrows():
			matched_delturnr = pd.NA
			is_bike_placeholder = False

			while tu_pos < len(tu_deltur_sub):
				tu_deltur_sub_leg = tu_deltur_sub.iloc[tu_pos]

				if _leg_matches(otp_leg, tu_deltur_sub_leg):
					matched_delturnr = tu_deltur_sub_leg["Delturnr"]
					is_bike_placeholder = (
						otp_leg["mode"] == "WALK" and (tu_deltur_sub_leg["StageMode"] in bike_stage_modes)
					)
					tu_pos += 1
					break

				# If the current TU leg does not match this OTP leg, do not consume the TU leg.
				# The OTP leg is treated as an extra OTP leg, e.g. a transfer walk missing in TU.
				break

			delturnrs.append(matched_delturnr)
			is_bike_placeholders.append(is_bike_placeholder)


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
