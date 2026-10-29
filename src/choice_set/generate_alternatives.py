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
	"bus_line_group",
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
	"crow_distance_km",
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


def _used_stops(legs):
	"""Sorted boarding/alighting stop ids of legs' transit legs."""
	transit = legs[~legs["mode"].isin(STREET_MODES)].reindex(columns=["from_gtfs_id", "to_gtfs_id"])
	return sorted({stop for stop in transit.to_numpy().ravel() if isinstance(stop, str)})


def exclude_stop_rounds(query, candidates, chosen_legs, key_columns, rounds, stops_per_round, rng):
	"""
	candidates plus `rounds` re-queries, each excluding stops_per_round stop ids drawn at random
	(rng) from the boarding/alighting stops of chosen_legs and the candidates so far; draws are
	independent per round. query(exclude_stopids) returns candidates. Each candidate is tagged with
	its exclude_round (0 = candidates) and that round's excluded_stops.
	"""
	candidates = candidates.assign(exclude_round=0, excluded_stops="")
	for exclude_round in range(1, rounds + 1):
		stops = _used_stops(pd.concat([chosen_legs, candidates]))
		if not stops:
			break
		exclude_stopids = rng.sample(stops, min(stops_per_round, len(stops)))
		round_candidates = query(exclude_stopids)
		#known_keys = set(_itinerary_keys(candidates, key_columns).values()) if not candidates.empty else set()
		#if round_candidates.empty or set(_itinerary_keys(round_candidates, key_columns).values()) <= known_keys:
		#	break
		if round_candidates.empty:
			continue
		# Past the ids so far; a query's ids go negative for backward pages
		offset = candidates["iteration_id"].max() + 1 - round_candidates["iteration_id"].min() if not candidates.empty else 0
		candidates = pd.concat([candidates, round_candidates.assign(iteration_id=round_candidates["iteration_id"] + offset,
			exclude_round=exclude_round, excluded_stops=" ".join(exclude_stopids))], ignore_index=True)
	return candidates


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


def _bus_line_groups(trip_choice_set, key_columns):
	"""
	Per row, the lowest alt_id of the alternatives the same as its alternative but for bus lines (e.g.
	parallel buses 143/144 between the same stops): its own alt_id if there is none.
	"""
	bus_blind = trip_choice_set.assign(iteration_id=trip_choice_set["alt_id"],
		route_short_name=trip_choice_set["route_short_name"].where(trip_choice_set["mode"] != "BUS"))
	keys = _itinerary_keys(bus_blind, list(dict.fromkeys([*key_columns, "from", "to"])))
	first_alt = {}
	for alt_id in sorted(keys):
		first_alt.setdefault(keys[alt_id], alt_id)
	# Alternatives without transit legs (e.g. the chosen car trip) have no key
	return trip_choice_set["alt_id"].map(lambda alt_id: first_alt[keys[alt_id]] if alt_id in keys else alt_id)


def build_choice_set(trip_id, candidates, key_columns, depart_dt, chosen_legs, chosen_public_transport):
	"""
	One itinerary per alternative: per key, the candidate departing closest to depart_dt, ordered by
	that closeness. chosen_legs is alt_id 0; if chosen_public_transport, it replaces the candidates
	sharing its key, and a coverage column is added. Columns chosen_legs lacks (e.g. a car trip's
	routes/stops) are left empty.
	"""
	alternatives = [chosen_legs]
	chosen_keys = set(_itinerary_keys(chosen_legs, key_columns).values()) if chosen_public_transport else set()
	coverage = 0

	if not candidates.empty:
		#Remove duplicates
		starts = pd.to_datetime(candidates.groupby("iteration_id")["start_trip"].first(), utc=True)
		itineraries = pd.DataFrame({"key": pd.Series(_itinerary_keys(candidates, key_columns))})
		itineraries["gap"] = (starts - pd.to_datetime(depart_dt, utc=True)).abs()
		in_chosen = itineraries["key"].map((lambda key: key in chosen_keys))
		coverage = int(in_chosen.any())
		itineraries = itineraries[~in_chosen]
		keep = itineraries.sort_values("gap").drop_duplicates("key").index
		alternatives += [candidates[candidates["iteration_id"] == iteration_id] for iteration_id in keep]

	trip_choice_set = pd.concat(
		[legs.assign(trip_id=trip_id, alt_id=alt_id, is_chosen= int(alt_id == 0)) for alt_id, legs in enumerate(alternatives)],
		ignore_index=True,
	).reindex(columns=CHOICE_SET_COLUMNS)
	trip_choice_set["bus_line_group"] = _bus_line_groups(trip_choice_set, key_columns)
	if chosen_public_transport:
		# 1 if the generated candidates included the chosen key, 0 otherwise
		trip_choice_set.insert(trip_choice_set.columns.get_loc("is_chosen") + 1, "coverage", coverage)

	return trip_choice_set


PRINT_COLUMNS = ["mode", "distance_km", "waitingtime", "duration_min", "route_short_name", "from", "to", "otp_leg_depart_time"]


def print_choice_set(trip_choice_set):
	"""Prints each alternative's legs, one at a time, as the reconstruction prints its match."""
	legs = otp.parser.add_leg_times(trip_choice_set.sort_values(["alt_id", "leg_id"]), itinerary_column="alt_id")
	for alt_id, alt_legs in legs.groupby("alt_id"):
		print(f"alt_id {alt_id}{' (chosen)' if alt_id == 0 else ''}:")
		print(alt_legs[PRINT_COLUMNS].to_string(index=False, max_colwidth=None))


def coverage_summary(choice_sets, groups=None):
	"""
	Number of trips whose candidates included the chosen alternative_key (coverage column), its share and
	the number with no OTP transit itinerary: row "all" and, if groups (Series trip_id -> label) is given,
	one per label.
	"""
	trips = choice_sets.groupby("trip_id").agg(coverage=("coverage", "first"), alternatives=("alt_id", "max"))
	# Uncovered and nothing besides the chosen: OTP returned no transit itinerary
	trips["no_candidates"] = (trips["coverage"] == 0) & (trips["alternatives"] == 0)

	def summary(group, trips):
		covered = trips["coverage"].sum()
		return {"group": group, "trips": len(trips), "covered": covered, "coverage_share": covered / len(trips),
			"no_candidates": trips["no_candidates"].sum()}

	rows = [summary("all", trips)]
	if groups is not None:
		rows += [summary(label, group_trips) for label, group_trips in trips.groupby(groups)]
	return pd.DataFrame(rows)


def print_coverage(summary, alternative_key):
	"""Prints coverage_summary's rows, "all" first."""
	for row in summary.itertuples():
		text = (f"{row.covered} of {row.trips} trips ({row.coverage_share:.1%}), "
			f"{row.no_candidates} with no OTP transit itinerary")
		print(f"Coverage (alternative_key={alternative_key}): {text}" if row.group == "all" else f"  {row.group}: {text}")
