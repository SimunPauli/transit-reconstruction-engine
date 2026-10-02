import pandas as pd
import utm
from pathlib import Path
from datetime import datetime

# Access column types, as listed in read_write_TU_linux's <table>_types.csv.
# Integer types are left to pandas: int64, or float64 (NaN) if values are missing, as with read_excel.
DB_TEXT_TYPES = {"TEXT", "MEMO", "GUID"}
DB_FLOAT_TYPES = {"FLOAT", "DOUBLE", "NUMERIC", "MONEY"}
DB_DATE_TYPES = {"SHORT_DATE_TIME", "EXT_DATE_TIME"}


def _read_tu_table(path):
	"""Reads an Excel file, or a read_write_TU_linux csv typed by its <name>_types.csv."""
	if path.suffix in (".xlsx", ".xls"):
		return pd.read_excel(path)

	types = pd.read_csv(path.with_name(path.stem + "_types.csv"))
	db_types = dict(zip(types["column"], types["type"]))
	dtype = {col: "str" for col, t in db_types.items() if t in DB_TEXT_TYPES}
	dtype |= {col: "float64" for col, t in db_types.items() if t in DB_FLOAT_TYPES}
	dates = [col for col, t in db_types.items() if t in DB_DATE_TYPES]

	return pd.read_csv(path, dtype=dtype, parse_dates=dates).drop(columns="rowId")


def load_tu(data_dir,
            YEAR,
            session_file,
            tur_file,
            tur_secret_file,
            deltur_file,
            stations_file,
            transit_code_tu = [31, 32, 33, 34, 37]):

	data_dir = Path(data_dir)
	tu_session = _read_tu_table(data_dir / session_file)
	tu_tur = _read_tu_table(data_dir / tur_file)
	tu_tur_secret = _read_tu_table(data_dir / tur_secret_file)
	tu_deltur = _read_tu_table(data_dir / deltur_file)
	tu_stations = _read_tu_table(data_dir / stations_file)


	tu_stations["id"] = tu_stations.index

	#Sort tu_session
	tu_session = tu_session.sort_values(by="SessionId")

	tu_tur_secret = tu_tur_secret.rename(columns={"turid":"TurId"})
	tu_tur = tu_tur.merge(tu_tur_secret, on="TurId", how="right")
	tu_tur = tu_tur[[
		"TurId", "SessionId",
		"DepartHH", "DepartMM", "ArrivalHH", "ArrivalMM",
		"orig_e", "orig_n", "tiladre", "tiladrn",
		"OrigMuncode", "DestMuncode", "PtPrimMode",
	]]

	#Join tu_tur + tu_deltur
	tu_deltur = (
		tu_tur[["SessionId", "TurId"]]
		.merge(tu_deltur, on="TurId", how="right")
	)

	#Number of deltur
	tu_deltur["n_deltur"] = (
		tu_deltur.groupby("TurId")["Delturnr"]
		.transform("max")
	)

	tu_tur = pd.merge(tu_tur, tu_session, on="SessionId", how="left")


	tu_tur["date"] = datetime(1970, 1, 1) + pd.to_timedelta(tu_tur["DiaryDate"], unit="D")
	tu_tur["date_str"] = tu_tur["date"].dt.strftime("%Y-%m-%d")


	tu_tur["DiaryDate"] = pd.to_numeric(tu_tur["DiaryDate"], errors="coerce")
	tu_tur["DepartHH"] = pd.to_numeric(tu_tur["DepartHH"], errors="coerce")
	tu_tur["DepartMM"] = pd.to_numeric(tu_tur["DepartMM"], errors="coerce")
	tu_tur["ArrivalHH"] = pd.to_numeric(tu_tur["ArrivalHH"], errors="coerce") #arrivalHH goes beyond 24 if corosses midnight
	tu_tur["ArrivalMM"] = pd.to_numeric(tu_tur["ArrivalMM"], errors="coerce")

	# Depart as datetime
	tu_tur["depart_dt"] = _build_local_datetime(
		tu_tur,
		"DiaryDate",
		"DepartHH",
		"DepartMM"
	)

	tu_tur["depart_dt_str"] = tu_tur["depart_dt"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")

	# Arrival as datetime
	tu_tur["arrival_dt"] = _build_local_datetime(
		tu_tur,
		"DiaryDate",
		"ArrivalHH",
		"ArrivalMM"
	)

	tu_tur["arrival_dt_str"] = tu_tur["arrival_dt"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
	tu_tur.sort_values(by="DiaryDate")

	#processing of TU data
	if YEAR is not None:
		tu_tur = tu_tur[(tu_tur["DiaryYear"] == YEAR)]

	#remove trip outside of Denmark and/or which include border crossing
	outside_dk_code = [997, 998, 999]
	tu_tur = tu_tur[
		~tu_tur["DestMuncode"].isin(outside_dk_code) & ~tu_tur["OrigMuncode"].isin(outside_dk_code)
	]

	tu_tur = tu_tur[tu_tur["PtPrimMode"].isin(transit_code_tu)].copy() #Not ferry

	#Add Lat/Lon (destination, origin), dropping trips without valid coordinates
	invalid = (
		_add_lat_lon(tu_tur, "tiladre", "tiladrn", "tiladrlat", "tiladrlon")
		| _add_lat_lon(tu_tur, "orig_e", "orig_n", "orig_lat", "orig_lon")
	)
	if invalid.any():
		print(f"Warning: dropping {invalid.sum()} trips with missing/invalid UTM coordinates.")
		tu_tur = tu_tur[~invalid]

	tu_deltur =tu_deltur[tu_deltur["TurId"].isin(tu_tur["TurId"])].copy()

	public_driver_TurId = tu_deltur[
		(tu_deltur["StageMode"].isin(transit_code_tu)) & (tu_deltur["StageDrivPass"]==1) #is driver
	]["TurId"] #remove drive of public transport (busdriver, train driver..)
	tu_deltur = tu_deltur[~tu_deltur["TurId"].isin(public_driver_TurId)] #exclude driver
	tu_tur = tu_tur[~tu_tur["TurId"].isin(public_driver_TurId)]
	tu_session = tu_session[tu_session["SessionId"].isin(tu_tur["SessionId"])]

	return [tu_session, tu_tur, tu_deltur, tu_stations]

def _add_lat_lon(df, e_col, n_col, lat_col, lon_col):
	"""Adds lat/lon from UTM zone 32N; returns mask of rows with missing/out-of-range coordinates (left NaN)."""
	valid = df[e_col].between(100000, 999999) & df[n_col].between(0, 10000000)
	df[[lat_col, lon_col]] = float("nan")
	if valid.any():
		lat, lon = utm.to_latlon(df.loc[valid, e_col].to_numpy(), df.loc[valid, n_col].to_numpy(), zone_number=32, zone_letter="N")
		df.loc[valid, lat_col] = lat
		df.loc[valid, lon_col] = lon
	return ~valid

def _build_local_datetime(df, date_col, hour_col, minute_col, timezone="Europe/Copenhagen"):
	valid = df[[date_col, hour_col, minute_col]].notna().all(axis=1)

	result = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")

	result.loc[valid] = (
			pd.Timestamp("1970-01-01")
			+ pd.to_timedelta(df.loc[valid, date_col], unit="D")
			+ pd.to_timedelta(df.loc[valid, hour_col], unit="h")
			+ pd.to_timedelta(df.loc[valid, minute_col], unit="m")
	)

	return result.dt.tz_localize(
		timezone,
		nonexistent="shift_forward",
		ambiguous="NaT"
	)