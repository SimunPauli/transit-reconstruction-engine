import pandas as pd
import load_TU_data
from otp_client import get_all_routes_for_mode, load_all_candidates
from find_similar_trip import find_similar_trip
from otp_utils import has_invalid_route_name, resolve_route_short_names, get_via_stops
from tu_gtfs_stations_match import match_tu_gtfs_stations

def main():
    print("Loading TU data...")
    data_dir = "/home/simpal/O/TU_Rejseplan/Data/TU/"
    tu_session, tu_tur, tu_deltur, tu_stations = load_TU_data.load_tu(
        data_dir=data_dir,
        session_file="tu_session_secret_2015_2025.xlsx",
        tur_file="tu_tur_secret_2015_2025.xlsx",
        deltur_file="tu_deltur_2015_2025.xlsx",
        stations_file="Stationer_tudatabase.xlsx"
    )
    print("TU data loaded")

    #Configurartion
    mode_map = {
        #TU: OTP
        31: "BUS",
        32: "S_TRAIN",
        33: "RAIL",
        34: "SUBWAY",
        37: "TRAM",
        41: "FERRY",
        35: "BUS"
    }
    otp_url = "http://localhost:8080/otp/gtfs/v1"
    search_window = "PT30M"
    print(f"Search window: {search_window}")

    # RAIL, TRAM and SUBWAY are missing route name in TU.
    # So taking all routes for these modes. Which will be used when modes
    # that do include route name in TU only can access those routes, but for
    # those that do not, all routes will be used.
    otp_mode_routes_cache = {mode: get_all_routes_for_mode(mode) for mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"]}

    #Small processing of TU data
    tu_tur = tu_tur[tu_tur["PtPrimMode"].isin([31, 32, 33, 34, 37, 41])]
    tu_tur = tu_tur[(tu_tur["DiaryYear"] == 2024) & (tu_tur["DiaryMonth"] == 6)]
    tu_deltur = tu_deltur[tu_deltur["TurId"].isin(tu_tur["TurId"])]
    tu_deltur["otp_mode"] = tu_deltur["StageMode"].map(mode_map)

    #Map TU and GTFS stations
    tu_gtfs_station_df = match_tu_gtfs_stations(
        tu_stations,
        period=(tu_tur["DiaryDate"].min(), tu_tur["DiaryMonth"].max()),
        bbox_buffer_m=1000,
        name_match_threshold=0.6
    )
    print("Mapping of TU and GTFS station has been exported to", data_dir + "tu_gtfs_station_df.csv")
    tu_gtfs_station_df.to_csv(data_dir + "tu_gtfs_station_df.csv")

    time_based_matches = []
    for i, tu_tur_row in tu_tur.iterrows():
        i_TurId = tu_tur_row["TurId"]
        tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
        print(f"\n\nTurId: {i_TurId}. With SessionId: {tu_tur_row['SessionId']}.")

        #Print for debugging
        tu_deltur_sub_print_col = ["StageMode", "StageLength", "StageWaitMin", "StageDurationMin", "Route", "FromStation", "ToStation"]
        print(f"tu_deltur_sub: {tu_deltur_sub[tu_deltur_sub_print_col]}")
        route_names, route_names_ext, modes_json, modes_list = resolve_route_short_names(
            tu_deltur_sub,
            mode_map,
            otp_mode_routes_cache
        )
        print(f"route_short_name: {route_names}")
        print(f"route_names_ext: {route_names_ext}")

        if not modes_json:
            print(f"No valid public transport modes found for TurId: {i_TurId}")
            continue
        print(f"modes_json: {modes_json}")
        if any(mode in ["BUS", "S_TRAIN"] for mode in modes_list) and not route_names:
            print(f"No valid route found for TurId: {i_TurId}")
            continue
        if has_invalid_route_name(route_names) and route_names:
            print(f"Invalid route name: {route_names}")
            continue

        #Get the gtfs stop_ids for stations respondent travel through
        via_stopids = get_via_stops(tu_deltur_sub=tu_deltur_sub, tu_gtfs_station_df=tu_gtfs_station_df)

        # 2. Fetch all candidates (handles pagination & concat internally)
        is_bus_s_train = any(mode in ["BUS", "S_TRAIN"] for mode in modes_list)
        is_rail_tram_subway_ferry = any(mode in ["RAIL", "SUBWAY", "TRAM", "FERRY"] for mode in modes_list)

        if is_bus_s_train and is_rail_tram_subway_ferry:
            otp_candidates_df = load_all_candidates(
                tu_tur_row=tu_tur_row,
                modes_json=modes_json,
                route_short_name=route_names_ext,
                via_stopids=via_stopids,
                search_window=search_window,
                otp_url=otp_url)
        elif is_bus_s_train:
            otp_candidates_df = load_all_candidates(
                tu_tur_row=tu_tur_row,
                modes_json=modes_json,
                route_short_name=route_names,
                via_stopids=via_stopids,
                search_window=search_window,
                otp_url=otp_url)
        elif is_rail_tram_subway_ferry:
            otp_candidates_df = load_all_candidates(
                tu_tur_row=tu_tur_row,
                modes_json=modes_json,
                route_short_name=None,
                via_stopids=via_stopids,
                search_window=search_window,
                otp_url=otp_url)

        if otp_candidates_df.empty:
            print(f"No OTP trips found for TurId: {i_TurId}")
            continue

        required_routes = set(route_names_ext)
        iteration_ids_with_all_routes = (
            otp_candidates_df.groupby("iteration_id")["route_short_name"]
            .apply(lambda routes: required_routes.issubset(set(routes.astype(str))))
        )

        otp_candidates_df = otp_candidates_df[
            otp_candidates_df["iteration_id"].isin(
                iteration_ids_with_all_routes[iteration_ids_with_all_routes].index
            )
        ].reset_index(drop=True)
        if otp_candidates_df.empty:
            print(f"No OTP trips include all route_names_ext for TurId: {i_TurId}")
            continue

        time_based_match = find_similar_trip(tu_tur_row, otp_candidates_df, arrival_dev_weight=1)
        if time_based_match is None:
            print(f"No best trip found for TurId: {i_TurId}")
            continue
        time_based_match["TurId"] = i_TurId
        time_based_matches.append(time_based_match)

    if not time_based_matches:
        print("No time-based matches found. Nothing to save.")
        return

    all_time_based_matches = pd.concat(time_based_matches, ignore_index=True)
    all_time_based_matches.to_csv("~/O/TU_Rejseplan/Data/TU/time_based_matches.csv", index=False)
    print(f"Saved {len(all_time_based_matches)} time-based matches to time_based_matches.csv")

if __name__ == "__main__":
    import sys
    from contextlib import redirect_stdout

    #All prints will be written in both console and log file
    class Tee:
        def __init__(self, *files): self.files = files
        def write(self, data): [file.write(data) for file in self.files]
        def flush(self): [file.flush() for file in self.files]

    with open("time_based_matches.log", "w", encoding="utf-8") as log_file:
        with redirect_stdout(Tee(sys.stdout, log_file)):
            main()