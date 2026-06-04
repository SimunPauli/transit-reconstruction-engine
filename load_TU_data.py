import pandas as pd
import geopandas as gpd

def load_tu(data_dir = "/home/simpal/O/TU_Rejseplan/Data/TU/",
            session_file = "tu_session_secret_2015_2025.xlsx",
            tur_file = "tu_tur_secret_2015_2025.xlsx",
            deltur_file = "tu_deltur_2015_2025.xlsx"):
    tu_session = pd.read_excel(data_dir + session_file)
    tu_tur = pd.read_excel(data_dir + tur_file)
    tu_deltur = pd.read_excel(data_dir + deltur_file)

    # --- Sort tu_session ---
    tu_session = tu_session.sort_values(by="DiaryDate")

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
    gdf_dest = gpd.GeoDataFrame(
        tu_tur,
        geometry=gpd.points_from_xy(tu_tur["tiladre"], tu_tur["tiladrn"]),
        crs="EPSG:32632"
    )
    gdf_dest = gdf_dest.to_crs("EPSG:4326")

    tu_tur["tiladrlon"] = gdf_dest.geometry.x
    tu_tur["tiladrlat"] = gdf_dest.geometry.y

    # --- Add Lat/Lon (origin) ---
    gdf_orig = gpd.GeoDataFrame(
        tu_tur,
        geometry=gpd.points_from_xy(tu_tur["orig_e"], tu_tur["orig_n"]),
        crs="EPSG:32632"
    )
    gdf_orig = gdf_orig.to_crs("EPSG:4326")

    tu_tur["orig_lon"] = gdf_orig.geometry.x
    tu_tur["orig_lat"] = gdf_orig.geometry.y

    tu_tur = pd.merge(tu_tur, tu_session, on="SessionId", how="left")


    from datetime import datetime
    tu_tur["date"] = datetime(1970, 1, 1) + pd.to_timedelta(tu_tur["DiaryDate"], unit="D")
    tu_tur["date_str"] = tu_tur["date"].dt.strftime("%Y-%m-%d")


    tu_tur["DiaryDate"] = pd.to_numeric(tu_tur["DiaryDate"], errors="coerce")
    tu_tur["DepartHH"] = pd.to_numeric(tu_tur["DepartHH"], errors="coerce")
    tu_tur["DepartMM"] = pd.to_numeric(tu_tur["DepartMM"], errors="coerce")
    tu_tur["ArrivalHH"] = pd.to_numeric(tu_tur["ArrivalHH"], errors="coerce")
    tu_tur["ArrivalMM"] = pd.to_numeric(tu_tur["ArrivalMM"], errors="coerce")

    # Depart as datetime
    tu_tur["depart_dt"] = (
        pd.Timestamp("1970-01-01")
        + pd.to_timedelta(tu_tur["DiaryDate"], unit="D", errors="coerce")
        + pd.to_timedelta(tu_tur["DepartHH"], unit="h", errors="coerce")
        + pd.to_timedelta(tu_tur["DepartMM"], unit="m", errors="coerce")
    ) #This will give warnings due to missing values.

    tu_tur["depart_dt"] = tu_tur["depart_dt"].dt.tz_localize(
        "Europe/Copenhagen",
        nonexistent="shift_forward",
        ambiguous="NaT"
    )
    tu_tur["depart_dt_str"] = tu_tur["depart_dt"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    # Arrival as datetime
    tu_tur["arrival_dt"] = (
        pd.Timestamp("1970-01-01")
        + pd.to_timedelta(tu_tur["DiaryDate"], unit="D", errors="coerce")
        + pd.to_timedelta(tu_tur["ArrivalHH"], unit="h", errors="coerce")
        + pd.to_timedelta(tu_tur["ArrivalMM"], unit="m", errors="coerce")
    ) #This will give warnings due to missing values.

    tu_tur["arrival_dt"] = tu_tur["arrival_dt"].dt.tz_localize(
        "Europe/Copenhagen",
        nonexistent="shift_forward",
        ambiguous="NaT"
    )
    tu_tur["arrival_dt_str"] = tu_tur["arrival_dt"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")

    return [tu_session, tu_tur, tu_deltur]