import pandas as pd
import load_TU_data
from otp_client import get_all_routes_for_mode, load_all_candidates
from find_similar_trip import find_similar_trip
from otp_utils import has_invalid_route_name, resolve_route_short_names

def main():
    print("Loading TU data...")
    tu_session, tu_tur, tu_deltur, tu_station = load_TU_data.load_tu(
        data_dir="/home/simpal/O/TU_Rejseplan/Data/TU/",
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


    tu_tur = tu_tur[tu_tur["PtPrimMode"].isin([31, 32, 33, 34, 37, 41])]
    tu_tur = tu_tur[(tu_tur["DiaryYear"] == 2024) & (tu_tur["DiaryMonth"] == 6)]

    time_based_matches = []
    for i, tu_tur_row in tu_tur.iterrows():
        i_TurId = tu_tur_row["TurId"]
        tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == i_TurId]
        print(f"\n\nTurId: {i_TurId}. With SessionId: {tu_tur_row['SessionId']}.")

        tu_deltur_sub_print_col = ["StageMode", "StageLength", "StageWait", "StageDuration", "Route", "FromStation", "ToStation"]
        print(f"tu_deltur_sub: {tu_deltur_sub[tu_deltur_sub_print_col]}")
        route_names, route_names_ext, modes_json, modes_list = resolve_route_short_names(tu_deltur_sub,
                                                                 mode_map,
                                                                 otp_mode_routes_cache)
        if not modes_json:
            print(f"No valid public transport modes found for TurId: {i_TurId}")
            continue
        print(f"modes_json: {modes_json}")
        if (modes_list.isin([31, 32])) & (not route_names):
            print(f"No valid route found for TurId: {i_TurId}")
            continue
        if has_invalid_route_name(route_names):
            print(f"Invalid route name: {route_names}")
            continue

        print(f"route_short_name: {route_names}")

        # 2. Fetch all candidates (handles pagination & concat internally)
        is_bus_s_train = any(mode in ["BUS", "S_TRAIN"] for mode in modes_list)
        is_rail_tram_subway_ferry = any(mode in ["RAIL", "SUBWAY", "TRAM", "FERRY"] for mode in modes_list)

        if is_bus_s_train and is_rail_tram_subway_ferry:
            otp_candidates_df = load_all_candidates(
                tu_tur_row=tu_tur_row,
                modes_json=modes_json,
                route_short_name=route_names_ext,  # extented
                search_window=search_window,
                otp_url=otp_url
            )
        elif is_bus_s_train:
            otp_candidates_df = load_all_candidates(
                tu_tur_row=tu_tur_row,
                modes_json=modes_json,
                route_short_name=route_names,  # not extented
                search_window=search_window,
                otp_url=otp_url
            )
        elif is_rail_tram_subway_ferry:
            otp_candidates_df = load_all_candidates(
                tu_tur_row=tu_tur_row,
                modes_json=modes_json,
                # route_short_name=route_names,
                search_window=search_window,
                otp_url=otp_url
            )

        if otp_candidates_df.empty:
            print(f"No OTP trips found for TurId: {i_TurId}")
            continue

        #TODO: Currently only considering trips that include all routes in route_short_name.
        #      But for RAIL, TRAM and SUBWAY, route_short_name includes all their routes.
        #      Either remove this check or handle it differently for these modes!
        required_routes = set(route_names)
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
            print(f"No OTP trips include all routes {route_names} for TurId: {i_TurId}")
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
    from contextlib import redirect_stdout

    with open("time_based_matches.log", "w", encoding="utf-8") as log_file:
        with redirect_stdout(log_file):
            main()