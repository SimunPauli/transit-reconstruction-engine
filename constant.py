MODE_MAP = {
	# TU: OTP
	# Transit modes:
	31: "BUS",
	32: "S_TRAIN",
	33: "RAIL",
	34: "SUBWAY",
	37: "TRAM",
	41: "FERRY",
	35: "BUS",
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

INVALID_ROUTE_CHARS = ("?", "&", "/", ".", ",")
LOCAL_TIMEZONE = "Europe/Copenhagen"