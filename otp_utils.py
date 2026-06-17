import pandas as pd

INVALID_ROUTE_CHARS = ("?", "&", "/", ".", ",")

def has_invalid_route_name(route_names) -> bool:
	if not route_names:
		return True

	for route_name in route_names:
		if pd.isna(route_name):
			return True
		route_name = str(route_name)
		if any(char in route_name for char in INVALID_ROUTE_CHARS):
			return True

	return False

def resolve_route_short_names(tu_deltur_sub, mode_map, otp_mode_routes_cache):
	"""
	Extracts and maps transit modes and routes for a given TurId.
	Handles fallback routes for RAIL, TRAM, and SUBWAY using a cache.

	Returns:
		tuple: (route_names, modes_json)
			   where route_names is a list of strings,
			   and modes_json is a list of dicts like [{"mode": "BUS"}].
	"""
	# 1. Identify all valid modes for this TurId
	valid_stage_modes = [31, 32, 33, 34, 37, 41]
	modes_list = (
		tu_deltur_sub.loc[tu_deltur_sub["StageMode"].isin(valid_stage_modes),
		"StageMode"]
		.drop_duplicates()
		.map(mode_map)
		.tolist()
	)
	if not modes_list:
		return [], [], [], []

	modes_json = [{"mode": mode} for mode in modes_list]

	# 2. Extract explicit routes for modes that provide them (BUS=31, S_TRAIN=32)
	if not any(mode in ["BUS", "S_TRAIN"] for mode in modes_list):
		return [], [], modes_json, modes_list

	modes_with_route_names = [31, 32]
	route_names = (
		tu_deltur_sub.loc[
			tu_deltur_sub["StageMode"].isin(modes_with_route_names),
			"Route"
		]
		.dropna()
		.astype(str)
		.str.strip()
		.loc[lambda routes: ~routes.str.lower().isin(["", "nan", "none", "?"])]
		.drop_duplicates()
		.tolist()
	)
	# 3. For RAIL, TRAM, SUBWAY, FERRY, append cached routes if the mode is used in this trip
	route_names_ext = list(route_names)
	if any(mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"] for mode in modes_list):
		for mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"]:
			if mode in modes_list:
				route_names_ext = route_names_ext + otp_mode_routes_cache.get(mode, [])

	# 4. Deduplicate and clean up
	route_names = list(set(route_names))
	route_names_ext = list(set(route_names_ext))

	return route_names, route_names_ext, modes_json, modes_list

def get_via_stops(tu_deltur_sub, tu_gtfs_station_df):
	stops_row = []
	for _, row in tu_deltur_sub.loc[tu_deltur_sub["StageMode"].isin([32, 33, 34, 37])].iterrows():
		stops_row.append({"otp_mode": row["otp_mode"], "tu_station_name": row["FromStation"]})
		stops_row.append({"otp_mode": row["otp_mode"], "tu_station_name": row["ToStation"]})
	if not stops_row:
		return None
	via_stopid = (
		pd.DataFrame(stops_row)
		.dropna(subset=["tu_station_name"])
		.drop_duplicates(subset=["otp_mode", "tu_station_name"], keep="first")
		.reset_index(drop=True)
	)
	if via_stopid.empty:
		return None

	required_columns = ["otp_mode", "tu_station_name", "gtfs_station_id"]
	missing_columns = [column for column in required_columns if column not in tu_gtfs_station_df.columns]
	if missing_columns:
		raise KeyError(f"tu_gtfs_station_df is missing required columns: {missing_columns}")


	# Merge and maintain order
	merged = (
		via_stopid
		.merge(
			tu_gtfs_station_df[["otp_mode", "tu_station_name", "gtfs_station_id"]],
			on=["otp_mode", "tu_station_name"],
			how="left"  # preserve order of via_stopid
		)
	)
	# Keep only rows with non-null gtfs_station_id and remove duplicates while preserving order
	via_stopids = (
		merged[merged["gtfs_station_id"].notna()]
		.drop_duplicates(subset=["gtfs_station_id"], keep='first')
		["gtfs_station_id"]
		.tolist()
	)
	return via_stopids


def filter_candidates_by_requirements(
		otp_candidates_df,
		route_names: list = None,
		modes_list: list = None,
		tur_id: int = None
):
	"""
	Filter OTP candidates to ensure all required routes and modes are present.

	Args:
		otp_candidates_df: DataFrame with OTP candidate trips
		route_names: List of required route short names (optional)
		modes_list: List of required transit modes (optional)
		tur_id: Trip ID for logging purposes

	Returns:
		tuple: (success: bool, filtered_df: DataFrame, message: str)
			   - success: True if filtering succeeded, False if candidates became empty
			   - filtered_df: The filtered DataFrame (or empty if failed)
			   - message: Status or error message for logging
	"""
	filtered_df = otp_candidates_df.copy()

	# Filter by required routes (for BUS/S_TRAIN)
	if route_names:
		if "route_short_name" not in filtered_df.columns:
			print(f"OTP candidates are missing route_short_name for TurId: {tur_id}")
			return None

		required_routes = set(map(str, route_names))

		iteration_ids_with_required_routes = (
			filtered_df.groupby("iteration_id")["route_short_name"]
			.apply(
				lambda routes: required_routes.issubset(
					set(routes.dropna().astype(str))
				)
			)
		)

		filtered_df = filtered_df[
			filtered_df["iteration_id"].isin(
				iteration_ids_with_required_routes[
					iteration_ids_with_required_routes
				].index
			)
		].reset_index(drop=True)

		if filtered_df.empty:
			print(f"No OTP trips include all required BUS/S_TRAIN routes for TurId: {tur_id}")
			return None

	# Filter by required transit modes
	if modes_list:
		if "mode" not in filtered_df.columns:
			print(f"OTP candidates are missing mode for TurId: {tur_id}")
			return None

		required_modes = set(modes_list)

		iteration_ids_with_required_modes = (
			filtered_df.groupby("iteration_id")["mode"]
			.apply(lambda modes: required_modes.issubset(set(modes)))
		)

		filtered_df = filtered_df[
			filtered_df["iteration_id"].isin(
				iteration_ids_with_required_modes[
					iteration_ids_with_required_modes
				].index
			)
		].reset_index(drop=True)

		if filtered_df.empty:
			print(f"No OTP trips include all required transit modes ({modes_list}) for TurId: {tur_id}")
			return None

	#TODO: Add filter that checks order of transit match. Also need to filter trips that e.g. only
	# include BUS once bus TU includes two separate BUS deltur

	return filtered_df