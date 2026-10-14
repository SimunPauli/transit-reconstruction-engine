import pandas as pd
from src import otp
from src.constant import STREET_MODES, METRO_SIBLING_LINES, S_TRAIN_SIBLING_LINES
from src.tu.tu_utils import tu_endpoints, get_access_egress, absorb_short_walk_into_car

# Transit-leg columns that make two itineraries the same alternative (choice_set.alternative_key).
# Stops by name, not stop_id, so the same route from another platform isn't a new alternative.
ALTERNATIVE_KEYS = {
	"routes": ["mode", "route_short_name"],
	"routes_stops": ["mode", "route_short_name", "from", "to"],
	"trip": ["mode", "route_short_name", "from", "to", "start_leg"],
}

CHOICE_SET_COLUMNS = [
	"TurId",
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
	"from",
	"to",
	"from_gtfs_id",
	"to_gtfs_id",
	"distance_km",
	"duration_min",
	"generalized_cost",
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


def query_candidates(tu_tur_row, tu_deltur, settings):
	"""OTP itineraries for the TU trip's origin, destination, departure and access/egress, on any route."""
	tu_deltur_sub = tu_deltur.loc[tu_deltur["TurId"] == tu_tur_row["TurId"]].sort_values("Delturnr")
	tu_deltur_sub = absorb_short_walk_into_car(tu_deltur_sub, verbose=False)
	access_mode, egress_mode = get_access_egress(tu_deltur_sub)
	origin, destination = tu_endpoints(tu_tur_row)
	return otp.client.load_all_candidates(
		origin=origin,
		destination=destination,
		depart_dt=tu_tur_row["depart_dt"],
		access_mode=access_mode,
		egress_mode=egress_mode,
		**settings,
	)


def build_choice_set_with_chosen(chosen_legs, candidates, key_columns):
	"""
	The chosen itinerary (alt_id 0) plus one itinerary per other alternative: per key, the
	candidate departing closest to the chosen one. Alternatives are ordered by that closeness.
	"""
	chosen_key = next(iter(_itinerary_keys(chosen_legs, key_columns).values()))
	# TODO: Should alternatives time gap from chosen be the gap from TU respondent or reconstructed trip?
	#  currently using "chosen_start" to be start of reconstructed trip.
	#  For TU trip with chosen mode being car chosen start needs to be TU respondent answer.
	chosen_start = pd.to_datetime(chosen_legs["start_trip"].iloc[0], utc=True)

	alternatives = [chosen_legs]
	if not candidates.empty:
		starts = pd.to_datetime(candidates.groupby("iteration_id")["start_trip"].first(), utc=True)
		itineraries = pd.DataFrame({"key": pd.Series(_itinerary_keys(candidates, key_columns))})
		itineraries["gap"] = (starts - chosen_start).abs()
		itineraries = itineraries[itineraries["key"].map(lambda key: key != chosen_key)]
		keep = itineraries.sort_values("gap").drop_duplicates("key").index
		alternatives += [candidates[candidates["iteration_id"] == iteration_id] for iteration_id in keep]

	return pd.concat(
		[legs.assign(TurId=chosen_legs["TurId"].iloc[0], alt_id=alt_id, is_chosen=alt_id == 0) for alt_id, legs in enumerate(alternatives)],
		ignore_index=True,
	)[CHOICE_SET_COLUMNS]
