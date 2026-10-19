import pandas as pd
from src import otp
from src.constant import STREET_MODES, METRO_SIBLING_LINES, S_TRAIN_SIBLING_LINES

# Transit-leg columns that make two itineraries the same alternative (choice_set.alternative_key).
# Stops by name, not stop_id, so the same route from another platform isn't a new alternative.
ALTERNATIVE_KEYS = {
	"routes": ["mode", "route_short_name"],
	"routes_stops": ["mode", "route_short_name", "from", "to"],
	"trip": ["mode", "route_short_name", "from", "to", "start_leg"],
}

CHOICE_SET_COLUMNS = [
	"trip_id",
	"alt_id",
	"is_chosen",
	"leg_id",
	"start_trip",
	"end_trip",
	"start_leg",
	"end_leg",
	"mode",
	"route_short_name",
	"route_gtfs_id",
	"trip_short_name",
	"bikes_allowed",
	"trip_gtfs_id",
	"from",
	"to",
	"from_gtfs_id",
	"to_gtfs_id",
	"distance_km",
	"duration_min",
	"generalized_cost",
	"system_notice_tag",
	"leg_geometry",
]


# Lines sharing track count as one route
SIBLING_LINES = {"SUBWAY": METRO_SIBLING_LINES, "S_TRAIN": S_TRAIN_SIBLING_LINES}


def _sibling_route(mode, name):
	"""The route name shared by name and its sibling line: the lower of the two (M2 -> M1, Bx -> B)."""
	siblings = SIBLING_LINES.get(mode, {})
	if not isinstance(name, str) or name.upper() not in siblings:
		return name
	return min(name.upper(), siblings[name.upper()])


def _itinerary_keys(legs, key_columns):
	"""{iteration_id: tuple of its transit legs' key_columns, in leg order}."""
	transit = legs[~legs["mode"].isin(STREET_MODES)].sort_values("leg_id")
	transit = transit.assign(route_short_name=[
		_sibling_route(mode, name) for mode, name in zip(transit["mode"], transit["route_short_name"])
	])
	keys = transit[key_columns].fillna("").astype(str)
	return {
		iteration_id: tuple(map(tuple, rows.to_numpy()))
		for iteration_id, rows in keys.groupby(transit["iteration_id"])
	}


def walk_to_bicycle(candidates, bike_access, bike_egress, walk_bike_time_ratio):
	"""
	Bike access/egress is queried as WALK (OTP's BICYCLE needs both ends): each itinerary's first
	(bike_access) / last (bike_egress) WALK leg becomes BICYCLE, duration scaled by walk_bike_time_ratio.
	"""
	if candidates.empty or not (bike_access or bike_egress):
		return candidates
	leg_id = candidates["leg_id"]
	itinerary_legs = leg_id.groupby(candidates["iteration_id"])
	end_leg = pd.Series(False, index=candidates.index)
	if bike_access:
		end_leg |= leg_id == itinerary_legs.transform("min")
	if bike_egress:
		end_leg |= leg_id == itinerary_legs.transform("max")
	bike = end_leg & (candidates["mode"] == "WALK")
	candidates = candidates.copy()
	candidates.loc[bike, "duration_min"] = (candidates.loc[bike, "duration_min"] * walk_bike_time_ratio).round().astype(int)
	candidates.loc[bike, "mode"] = "BICYCLE"
	return candidates


def build_choice_set(trip_id, candidates, key_columns, depart_dt, chosen_legs, chosen_public_transport):
	"""
	One itinerary per alternative: per key, the candidate departing closest to depart_dt, ordered by
	that closeness. chosen_legs is alt_id 0; if chosen_public_transport, it replaces the candidates
	sharing its key. Columns chosen_legs lacks (e.g. a car trip's routes/stops) are left empty.
	"""
	alternatives = [chosen_legs]
	chosen_keys = list(_itinerary_keys(chosen_legs, key_columns).values()) if chosen_public_transport else []

	if not candidates.empty:
		starts = pd.to_datetime(candidates.groupby("iteration_id")["start_trip"].first(), utc=True)
		itineraries = pd.DataFrame({"key": pd.Series(_itinerary_keys(candidates, key_columns))})
		itineraries["gap"] = (starts - pd.to_datetime(depart_dt, utc=True)).abs()
		itineraries = itineraries[itineraries["key"].map(lambda key: key not in chosen_keys)]
		keep = itineraries.sort_values("gap").drop_duplicates("key").index
		alternatives += [candidates[candidates["iteration_id"] == iteration_id] for iteration_id in keep]

	return pd.concat(
		[legs.assign(trip_id=trip_id, alt_id=alt_id, is_chosen=alt_id == 0) for alt_id, legs in enumerate(alternatives)],
		ignore_index=True,
	).reindex(columns=CHOICE_SET_COLUMNS)


PRINT_COLUMNS = ["mode", "distance_km", "waitingtime", "duration_min", "route_short_name", "from", "to", "otp_leg_depart_time"]


def print_choice_set(trip_choice_set):
	"""Prints each alternative's legs, one at a time, as the reconstruction prints its match."""
	legs = otp.parser.add_leg_times(trip_choice_set.sort_values(["alt_id", "leg_id"]), itinerary_column="alt_id")
	for alt_id, alt_legs in legs.groupby("alt_id"):
		print(f"alt_id {alt_id}{' (chosen)' if alt_id == 0 else ''}:")
		print(alt_legs[PRINT_COLUMNS].to_string(index=False, max_colwidth=None))
