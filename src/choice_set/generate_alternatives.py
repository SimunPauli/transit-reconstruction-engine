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


def build_choice_set(tur_id, candidates, key_columns, depart_dt, chosen_legs=None):
	"""
	One itinerary per alternative: per key, the candidate departing closest to depart_dt, ordered by
	that closeness. chosen_legs (a reconstructed itinerary), if given, is alt_id 0 and replaces the
	candidates sharing its key; without it, alt_ids start at 1, so alt_id 0 is always the chosen.
	"""
	alternatives = [] if chosen_legs is None else [chosen_legs]
	chosen_keys = [next(iter(_itinerary_keys(legs, key_columns).values())) for legs in alternatives]

	if not candidates.empty:
		starts = pd.to_datetime(candidates.groupby("iteration_id")["start_trip"].first(), utc=True)
		itineraries = pd.DataFrame({"key": pd.Series(_itinerary_keys(candidates, key_columns))})
		itineraries["gap"] = (starts - pd.to_datetime(depart_dt, utc=True)).abs()
		itineraries = itineraries[itineraries["key"].map(lambda key: key not in chosen_keys)]
		keep = itineraries.sort_values("gap").drop_duplicates("key").index
		alternatives += [candidates[candidates["iteration_id"] == iteration_id] for iteration_id in keep]

	first_alt_id = 0 if chosen_legs is not None else 1
	return pd.concat(
		[legs.assign(TurId=tur_id, alt_id=alt_id, is_chosen=alt_id == 0) for alt_id, legs in enumerate(alternatives, start=first_alt_id)],
		ignore_index=True,
	)[CHOICE_SET_COLUMNS]
