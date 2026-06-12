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
    tu_tur = tu_tur[tu_tur["PtPrimMode"].isin([31, 32, 33, 34, 37])] #Not ferry
    tu_tur = tu_tur[(tu_tur["DiaryYear"] == 2024) & (tu_tur["DiaryMonth"] == 6)]
    tu_deltur = tu_deltur[tu_deltur["TurId"].isin(tu_tur["TurId"])].copy()
    tu_deltur["otp_mode"] = tu_deltur["StageMode"].map(mode_map)

    #Map TU and GTFS stations
    tu_gtfs_station_df = match_tu_gtfs_stations(
        tu_stations,
        period=(tu_tur["DiaryDate"].min(), tu_tur["DiaryDate"].max()),
        bbox_buffer_m=1000,
        name_match_threshold=0.6
    )
    print("Mapping of TU and GTFS station has been exported to", data_dir + "tu_gtfs_station_df.csv")
    tu_gtfs_station_df.to_csv(data_dir + "tu_gtfs_station_df.csv")

    time_based_matches = []
    for i, tu_tur_row in tu_tur.iterrows():
        i_TurId = tu_tur_row["TurId"]
        tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
        print("\n\n____________________________________________________________________________________________")
        print(f"TurId: {i_TurId}. With SessionId: {tu_tur_row['SessionId']}.")
        print(f"Tur coordinates origin (lat lon) :     {tu_tur_row['orig_lat']} {tu_tur_row['orig_lon']}")
        print(f"Tur coordinates destination (lat lon): {tu_tur_row['tiladrlat']} {tu_tur_row['tiladrlon']}")
        print(f"Depart: {tu_tur_row['depart_dt_str']}. Arrival: {tu_tur_row['arrival_dt_str']}.")
        # Print for debugging
        tu_deltur_sub_print_col = ["StageMode", "StageLength", "StageWaitMin", "StageDurationMin", "Route","FromStation", "ToStation"]
        print("tu_deltur_sub:")
        print(tu_deltur_sub[tu_deltur_sub_print_col].to_string(index=False, max_colwidth=None))

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
        if not is_bus_s_train and not is_rail_tram_subway_ferry:
            raise ValueError("No valid transit modes found. TurId: ", i_TurId, ".")
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


        #TODO: Move next two if statements out of main():
        if route_names:
            if "route_short_name" not in otp_candidates_df.columns:
                print(f"OTP candidates are missing route_short_name for TurId: {i_TurId}")
                continue #TODO: this correct?
            required_routes = set(map(str, route_names))

            iteration_ids_with_required_routes = (
                otp_candidates_df.groupby("iteration_id")["route_short_name"]
                .apply(
                    lambda routes: required_routes.issubset(
                        set(routes.dropna().astype(str))
                    )
                )
            )

            otp_candidates_df = otp_candidates_df[
                otp_candidates_df["iteration_id"].isin(
                    iteration_ids_with_required_routes[
                        iteration_ids_with_required_routes
                    ].index
                )
            ].reset_index(drop=True)

            if otp_candidates_df.empty:
                print(f"No OTP trips include all required BUS/S_TRAIN routes for TurId: {i_TurId}")
                continue
        # Ensure all transit modes from the TU data are used in the OTP itinerary
        if modes_list:
            if "mode" not in otp_candidates_df.columns:
                print(f"OTP candidates are missing mode for TurId: {i_TurId}")
                continue
            required_modes = set(modes_list)

            iteration_ids_with_required_modes = (
                otp_candidates_df.groupby("iteration_id")["mode"]
                .apply(lambda modes: required_modes.issubset(set(modes)))
            )

            otp_candidates_df = otp_candidates_df[
                otp_candidates_df["iteration_id"].isin(
                    iteration_ids_with_required_modes[
                        iteration_ids_with_required_modes
                    ].index
                )
            ].reset_index(drop=True)

            if otp_candidates_df.empty:
                print(f"No OTP trips include all required transit modes ({modes_list}) for TurId: {i_TurId}")
                continue

        time_based_match = find_similar_trip(
            tu_tur_row,
            otp_candidates_df,
            arrival_dev_weight=1,
            print_devation_details=True
        )
        if time_based_match is None:
            print(f"No best trip found for TurId: {i_TurId}")
            continue
        time_based_match["TurId"] = i_TurId
        time_based_matches.append(time_based_match)

        time_based_match_print_col = ["mode", "distance_km", "waiting_time_min", "duration_min", "route_short_name", "from", "to"]
        print("time_based_match:")
        print(time_based_match[time_based_match_print_col].to_string(index=False, max_colwidth=None))

    if not time_based_matches:
        print("No time-based matches found. Nothing to save.")
        return

    all_time_based_matches = pd.concat(time_based_matches, ignore_index=True)
    all_time_based_matches.to_csv(f"{data_dir}time_based_matches.csv", index=False)
    print(f"\n\n\n____________________________________________________________________________________________")
    print(f"\n\n\nall_time_based_matches has been exported to {data_dir}time_based_matches.csv")
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