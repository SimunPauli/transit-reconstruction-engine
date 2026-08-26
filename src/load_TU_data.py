import pandas as pd
import utm
from pathlib import Path

def load_tu(data_dir,
            session_file,
            tur_file,
            deltur_file,
            stations_file):

	data_dir = Path(data_dir)

	tu_session = pd.read_excel(data_dir / session_file)
	tu_tur = pd.read_excel(data_dir / tur_file)
	tu_deltur = pd.read_excel(data_dir / deltur_file)
	tu_stations = pd.read_excel(data_dir / stations_file)

	tu_stations["id"] = tu_stations.index

	# --- Sort tu_session ---
	tu_session = tu_session.sort_values(by="SessionId")

	# --- Join tu_tur + tu_deltur ---
	tu_deltur = (
		tu_tur[["SessionId", "TurId"]]
		.merge(tu_deltur, on="TurId", how="right")
	)

	# --- Number of deltur ---
	tu_deltur["n_deltur"] = (
		tu_deltur.groupby("TurId")["Delturnr"]
		.transform("max")
	)

	# --- Add Lat/Lon (destination) ---
	tu_tur[["tiladrlat", "tiladrlon"]] = tu_tur.apply(
		lambda row: pd.Series(utm.to_latlon(row["tiladre"], row["tiladrn"], zone_number=32, zone_letter='N')),
		axis=1
	)

	# --- Add Lat/Lon (origin) ---
	tu_tur[["orig_lat", "orig_lon"]] = tu_tur.apply(
		lambda row: pd.Series(utm.to_latlon(row["orig_e"], row["orig_n"], zone_number=32, zone_letter='N')),
		axis=1
	)

	tu_tur = pd.merge(tu_tur, tu_session, on="SessionId", how="left")


	from datetime import datetime
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