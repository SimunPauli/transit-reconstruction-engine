from config_loader import load_config
config = load_config()

MODE_MAP = {
	# TU: OTP
	# Transit modes:
	31: "BUS",
	32: "S_TRAIN",
	33: "RAIL",
	34: "SUBWAY",
	37: "TRAM",
	41: "FERRY",
	35: "CAR", #Telebus, Flexbus. These are often essentially functioning as taxi.
	# Street modes:
	1: "WALK",
	2: "BIKE",
	7: "WALK",  # Electric wheelchair. Assumed to have same speed as regular walk. Very few observations
	8: "BIKE",  # Electric bike. Assumed to have same speed as regular bike. Few observations.
	11: "CAR",
	12: "CAR",  # van
	25: "CAR",  # Taxi
	26: "CAR",  # Non-public bus
	# TODO: Add all modes. For some modes, OTP need modification. Which might not be worth the time as they have almost no observations.
}

#BIKE doesn't work well with OTP. So setting it to walk, then later reducing the time by WALK_BIKE_TIME_RATIO
ACCESS_EGRESS_MODE_MAP = {
	1: "WALK",
	2: "WALK", #Bicycle
	7: "WALK",
	8: "WALK", #This is e-scooter
	11: "CAR_DROP_OFF",
	12: "CAR_DROP_OFF",
	25: "CAR_DROP_OFF",
	26: "CAR_DROP_OFF",
}

#Direct (street-only) mode used for the station-anchored fallback's access/egress leg.
#CAR_DROP_OFF/CAR_PICKUP are not valid PlanDirectMode values, so those TU stage modes
#collapse to CAR, same as the walk-as-bike-placeholder approximation used elsewhere.
DIRECT_ACCESS_MODE_MAP = {
	1: "WALK",
	2: "WALK",  #Bicycle
	7: "WALK",
	8: "WALK",  #E-scooter
	11: "CAR",
	12: "CAR",  # van
	25: "CAR",  # Taxi
	26: "CAR",  # Non-public bus
}

INVALID_ROUTE_CHARS = ("?", "&", "/", ".", ",")
LOCAL_TIMEZONE = "Europe/Copenhagen"

WALK_BIKE_TIME_RATIO = config["walk_bike_time_ratio"]

#Short failure-reason codes for match_tu_trip_to_otp's "last_print_if_not_found"/"failure_reason"
#trip-summary fields and the failures-only output file. Kept as single tokens (no embedded TurId,
#coordinates, or other survey data) so they stay short and safe to write to that file.
#Fallback-specific outcomes reuse these prefixed with "anchored_" (built at the call site) rather
#than declaring a separate constant per combination.
REASON_NO_VALID_MODES = "no_valid_modes"
REASON_INVALID_ROUTE_NAME = "invalid_route_name"
REASON_NO_OTP_CANDIDATES = "no_otp_candidates"
REASON_MISSING_ROUTE_SHORT_NAME_COLUMN = "missing_route_short_name_column"
REASON_MISSING_MODE_COLUMN = "missing_mode_column"
REASON_NO_REQUIRED_ROUTES = "no_required_routes"
REASON_NO_REQUIRED_MODES = "no_required_modes"
REASON_NO_MATCHING_LEG_SEQUENCE = "no_matching_leg_sequence"
REASON_NO_DIRECT_ACCESS_ROUTE = "no_direct_access_route"
REASON_NO_DIRECT_EGRESS_ROUTE = "no_direct_egress_route"
REASON_NO_BEST_MATCH = "no_best_match"