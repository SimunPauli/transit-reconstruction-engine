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


# ... existing code ...

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

    # Ensure Route column is treated as string to prevent type issues
    # Modifying a copy or using .astype directly on the slice prevents pandas SettingWithCopy warnings

    # 2. Extract explicit routes for modes that provide them (BUS=31, S_TRAIN=32, FERRY=41)
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
    # 3. For RAIL, TRAM, SUBWAY, append cached routes if the mode is used in this trip
    if any(mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"] for mode in modes_list):
        route_names_ext = list(route_names)
        for mode in ["RAIL", "TRAM", "SUBWAY", "FERRY"]:
            if mode in modes_list:
                route_names_ext = route_names_ext + otp_mode_routes_cache.get(mode, [])

    # 4. Deduplicate and clean up
    route_names = list(set(route_names))
    route_names_ext = list(set(route_names_ext))

    return route_names, route_names_ext, modes_json, modes_list