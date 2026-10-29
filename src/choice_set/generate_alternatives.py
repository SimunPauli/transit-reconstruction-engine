from itertools import combinations
import pandas as pd
from src import otp
from src.constant import STREET_MODES, METRO_SIBLING_LINES, S_TRAIN_UNGROUPED_LINES

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
	"line_group",
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





def _line_group_route(mode, name):
	"""
	The route name line_group compares: none for BUS, the lower of metro sibling lines (M2 -> M1),
	one name for S-train lines but F; name otherwise.
	"""
	if mode == "BUS":
		return None
	if not isinstance(name, str):
		return name
	if mode == "SUBWAY" and name.upper() in METRO_SIBLING_LINES:
		return min(name.upper(), METRO_SIBLING_LINES[name.upper()])
	if mode == "S_TRAIN" and name.upper() not in S_TRAIN_UNGROUPED_LINES:
		return "S_TRAIN"
	return name


def _itinerary_keys(legs, key_columns):
	"""{iteration_id: tuple of its transit legs' key_columns, in leg order}."""
	transit = legs[~legs["mode"].isin(STREET_MODES)].sort_values("leg_id")
	keys = transit[key_columns].fillna("").astype(str)
	return {
		iteration_id: tuple(map(tuple, rows.to_numpy()))
		for iteration_id, rows in keys.groupby(transit["iteration_id"])
	}


def _used_ids(legs, columns):
	"""Sorted ids in columns of legs' transit legs."""
	transit = legs[~legs["mode"].isin(STREET_MODES)].reindex(columns=columns)
	return sorted({value for value in transit.to_numpy().ravel() if isinstance(value, str)})


# Per kind of exclude round: the transit-leg columns its ids are drawn from, and the query argument they go to
EXCLUDE_KINDS = {
	"stops": (["from_gtfs_id", "to_gtfs_id"], "exclude_stopids"),
	"routes": (["route_gtfs_id"], "exclude_routeids"),
}


def exclude_rounds(query, candidates, chosen_legs, rounds):
	"""
	candidates plus one re-query per round of rounds, a list of (kind, n_ids, rng) run in order: each
	excludes a set of n_ids ids of kind (EXCLUDE_KINDS) drawn at random (rng) from chosen_legs' and the
	candidates so far's transit legs, never a set an earlier round excluded; a round with no such set
	left is skipped. query(**{argument: ids}) returns candidates. Each candidate is tagged with its
	exclude_round (0 = candidates) and that round's excluded ids ("kind: ids").
	"""
	candidates = candidates.assign(exclude_round=0, excluded="")
	excluded_sets = set()
	for exclude_round, (kind, n_ids, rng) in enumerate(rounds, start=1):
		columns, argument = EXCLUDE_KINDS[kind]
		ids = _used_ids(pd.concat([chosen_legs, candidates]), columns)
		if not ids:
			break
		# ids is sorted, so each set has one tuple
		unused_sets = [ids_set for ids_set in combinations(ids, min(n_ids, len(ids)))
			if (kind, ids_set) not in excluded_sets]
		if not unused_sets:
			continue
		ids_set = rng.choice(unused_sets)
		excluded_sets.add((kind, ids_set))
		exclude_ids = list(ids_set)
		round_candidates = query(**{argument: exclude_ids})
		if round_candidates.empty:
			continue
		# Past the ids so far; a query's ids go negative for backward pages
		offset = candidates["iteration_id"].max() + 1 - round_candidates["iteration_id"].min() if not candidates.empty else 0
		candidates = pd.concat([candidates, round_candidates.assign(iteration_id=round_candidates["iteration_id"] + offset,
			exclude_round=exclude_round, excluded=f"{kind}: {' '.join(exclude_ids)}")], ignore_index=True)
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


def _line_groups(trip_choice_set, key_columns):
	"""
	Per row, the lowest alt_id of the alternatives the same as its alternative but for parallel lines
	(e.g. buses 143/144, metro M1/M2 or S-train A/C between the same stops; see _line_group_route):
	its own alt_id if there is none.
	"""
	line_blind = trip_choice_set.assign(iteration_id=trip_choice_set["alt_id"], route_short_name=[
		_line_group_route(mode, name) for mode, name in zip(trip_choice_set["mode"], trip_choice_set["route_short_name"])
	])
	keys = _itinerary_keys(line_blind, list(dict.fromkeys([*key_columns, "from", "to"])))
	first_alt = {}
	for alt_id in sorted(keys):
		first_alt.setdefault(keys[alt_id], alt_id)
	# Alternatives without transit legs (e.g. the chosen car trip) have no key
	return trip_choice_set["alt_id"].map(lambda alt_id: first_alt[keys[alt_id]] if alt_id in keys else alt_id)


def build_choice_set(trip_id, candidates, key_columns, depart_dt, chosen_legs, chosen_public_transport):
	"""
	One itinerary per alternative: per key, the candidate with the least gap to depart_dt plus waiting
	(ties to less waiting), ordered by that score. chosen_legs is alt_id 0; if chosen_public_transport, it replaces the candidates
	sharing its key, and a coverage column is added. Columns chosen_legs lacks (e.g. a car trip's
	routes/stops) are left empty.
	"""
	alternatives = [chosen_legs]
	chosen_keys = set(_itinerary_keys(chosen_legs, key_columns).values()) if chosen_public_transport else set()
	coverage = 0

	if not candidates.empty:
		#Remove duplicates
		starts = pd.to_datetime(candidates.groupby("iteration_id")["start_trip"].first(), utc=True)
		# Minutes between each leg and the previous one's end, summed per itinerary
		legs = candidates.sort_values(["iteration_id", "leg_id"])
		waits = (legs["start_leg"] - legs.groupby("iteration_id")["end_leg"].shift()).groupby(legs["iteration_id"]).sum() / 60000
		itineraries = pd.DataFrame({"key": pd.Series(_itinerary_keys(candidates, key_columns))})
		itineraries["gap"] = (starts - pd.to_datetime(depart_dt, utc=True)).abs().dt.total_seconds() / 60
		itineraries["wait"] = waits
		itineraries["score"] = itineraries["gap"] + itineraries["wait"]
		in_chosen = itineraries["key"].map((lambda key: key in chosen_keys))
		coverage = int(in_chosen.any())
		itineraries = itineraries[~in_chosen]
		# Ties (same arrival, earlier start) go to the run waiting least
		keep = itineraries.sort_values(["score", "wait"]).drop_duplicates("key").index
		alternatives += [candidates[candidates["iteration_id"] == iteration_id] for iteration_id in keep]

	trip_choice_set = pd.concat(
		[legs.assign(trip_id=trip_id, alt_id=alt_id, is_chosen= int(alt_id == 0)) for alt_id, legs in enumerate(alternatives)],
		ignore_index=True,
	).reindex(columns=CHOICE_SET_COLUMNS)
	trip_choice_set["line_group"] = _line_groups(trip_choice_set, key_columns)
	if chosen_public_transport:
		# 1 if the generated candidates included the chosen key or another alternative of its line_group, 0 otherwise
		coverage = int(coverage or (trip_choice_set["line_group"].eq(0) & trip_choice_set["alt_id"].ne(0)).any())
		trip_choice_set.insert(trip_choice_set.columns.get_loc("is_chosen") + 1, "coverage", coverage)

	return trip_choice_set


PRINT_COLUMNS = ["mode", "distance_km", "waitingtime", "duration_min", "route_short_name", "from", "to", "otp_leg_depart_time"]


def print_choice_set(trip_choice_set):
	"""Prints each alternative's legs, one at a time, as the reconstruction prints its match."""
	legs = otp.parser.add_leg_times(trip_choice_set.sort_values(["alt_id", "leg_id"]), itinerary_column="alt_id")
	for alt_id, alt_legs in legs.groupby("alt_id"):
		print(f"alt_id {alt_id}{' (chosen)' if alt_id == 0 else ''}:")
		print(alt_legs[PRINT_COLUMNS].to_string(index=False, max_colwidth=None))


N_ALTERNATIVES_PERCENTILES = [0, 5, 10, 25, 50, 75, 90, 95, 100]


def choice_set_summary(choice_sets, groups=None):
	"""
	Row "all" plus, if groups (Series trip_id -> label) is given, one per label: trips, covered (coverage
	column, NA without one) and its share, trips with no OTP transit itinerary, and the mean and
	percentiles of n_alternatives (alternatives besides the chosen).
	"""
	trips = choice_sets.reindex(columns=["trip_id", "alt_id", "coverage"]).groupby("trip_id").agg(
		n_alternatives=("alt_id", "max"), coverage=("coverage", "first"))

	def summary(group, trips):
		n = trips["n_alternatives"]
		return {"group": group, "trips": len(trips),
			"covered": trips["coverage"].sum(min_count=1), "coverage_share": trips["coverage"].mean(),
			# Uncovered and nothing besides the chosen: OTP returned no transit itinerary
			"no_otp_itinerary": ((trips["coverage"] != 1) & (n == 0)).sum(),
			"n_alternatives_mean": n.mean(),
			# "lower": an actual trip's value, not interpolated
			**{f"n_alternatives_p{p}": n.quantile(p / 100, interpolation="lower") for p in N_ALTERNATIVES_PERCENTILES}
		    }

	rows = [summary("all", trips)]
	if groups is not None:
		rows += [summary(label, group_trips) for label, group_trips in trips.groupby(groups)]
	return pd.DataFrame(rows)
