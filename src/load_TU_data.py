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

	#Add Lat/Lon (destination)
	tu_tur[["tiladrlat", "tiladrlon"]] = tu_tur.apply(
		lambda row: pd.Series(utm.to_latlon(row["tiladre"], row["tiladrn"], zone_number=32, zone_letter='N')),
		axis=1
	)

	#Add Lat/Lon (origin)
	tu_tur[["orig_lat", "orig_lon"]] = tu_tur.apply(
		lambda row: pd.Series(utm.to_latlon(row["orig_e"], row["orig_n"], zone_number=32, zone_letter='N')),
		axis=1
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
	tu_deltur = tu_deltur[tu_deltur["TurId"].isin(tu_tur["TurId"])].copy()

	public_driver_TurId = tu_deltur[
		(tu_deltur["StageMode"].isin(transit_code_tu)) & (tu_deltur["StageDrivPass"]==1) #is driver
	]["TurId"] #remove drive of public transport (busdriver, train driver..)
	tu_deltur = tu_deltur[~tu_deltur["TurId"].isin(public_driver_TurId)] #exclude driver
	tu_tur = tu_tur[~tu_tur["TurId"].isin(public_driver_TurId)]
	tu_session = tu_session[tu_session["SessionId"].isin(tu_tur["SessionId"])]

	return [tu_session, tu_tur, tu_deltur, tu_stations]

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