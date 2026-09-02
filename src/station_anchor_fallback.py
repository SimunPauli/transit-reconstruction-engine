import pandas as pd
from .constant import LOCAL_TIMEZONE
from .tu_gtfs_stations_match import _normalise_name

#Only S_TRAIN, RAIL, and SUBWAY reliably have station names populated in TU (bus has
#no named stops; tram station names aren't reliably populated), so anchor-station
#lookup is restricted to these modes.
ANCHOR_MODES = {"S_TRAIN", "RAIL", "SUBWAY"}

#Every mode the fallback's direct access/egress leg (a single street-mode OTP query) could be
#silently standing in for, if left unchecked. An anchor leg is only usable if every TU leg on
#its access/egress side is NOT one of these — otherwise the direct leg would collapse a real
#transit leg (e.g. a bus ride to the station) into a straight-line walk/car, and the swallowed
#leg's route/mode/Delturnr could never appear in the fallback's OTP candidates, guaranteeing a
#downstream reject in filter_candidates_by_requirements.
TRANSIT_MODES = {"BUS", "S_TRAIN", "RAIL", "SUBWAY", "TRAM", "FERRY"}

_TIME_COLS = ("start_trip", "end_trip", "start_leg", "end_leg")

#OTP's planConnection API gives trip-level start/end as ISO8601 strings but leg-level
#startTime/endTime as raw epoch milliseconds (see otp_parser.py) — the two groups need
#different pd.to_datetime parsing on the way in and different serialization on the way
#out, or downstream code (e.g. tu_otp_matching.py's waitingtime calc, which does raw
#ms arithmetic on start_leg/end_leg) breaks on stitched itineraries.
_ISO_TIME_COLS = ("start_trip", "end_trip")
_MS_TIME_COLS = ("start_leg", "end_leg")


def build_anchor_station_lookup(tu_gtfs_station_df):
	"""
	Builds the (otp_mode, normalised TU station name) -> gtfs_station_id lookup used by
	find_known_anchor_stations. Callers that invoke find_known_anchor_stations once per TU
	trip (tu_otp_matching.py) should build this once from the run's tu_gtfs_station_df and
	reuse it, rather than rebuilding it on every trip.
	"""
	if tu_gtfs_station_df is None or tu_gtfs_station_df.empty:
		ValueError(f"tu_gtfs_station does not exist")

	station_lookup = {}
	for _, row in tu_gtfs_station_df.iterrows():
		key = (row["otp_mode"], _normalise_name(str(row["tu_station_name"])))
		station_lookup.setdefault(key, row["gtfs_station_id"])
	return station_lookup


def find_known_anchor_stations(tu_deltur_sub, station_lookup):
	"""
	Resolve the first and/or last S_TRAIN/RAIL/SUBWAY TU leg's boarding/alighting
	station to a GTFS stop ID, for use as a query anchor when the normal full-route
	OTP search finds nothing.

	station_lookup is the dict built once by build_anchor_station_lookup.

	Returns (first_stop_id, last_stop_id); either may be None if the corresponding side isn't
	anchorable (no rail-type leg, another transit leg in the way, or the station doesn't
	resolve via station_lookup).
	"""
	tu_deltur_sorted = tu_deltur_sub.sort_values("Delturnr")
	anchor_legs = tu_deltur_sorted.loc[tu_deltur_sorted["otp_mode"].isin(ANCHOR_MODES)]
	if anchor_legs.empty:
		return None, None

	def _resolve(mode, station_name):
		if station_name is None or (isinstance(station_name, float) and pd.isna(station_name)):
			return None
		return station_lookup.get((mode, _normalise_name(str(station_name))))

	def _other_side_is_transit_free(delturnr, side):
		if side == "before":
			other_legs = tu_deltur_sorted.loc[tu_deltur_sorted["Delturnr"] < delturnr]
		else:
			other_legs = tu_deltur_sorted.loc[tu_deltur_sorted["Delturnr"] > delturnr]
		return not other_legs["otp_mode"].isin(TRANSIT_MODES).any()

	first_leg = anchor_legs.iloc[0]
	last_leg = anchor_legs.iloc[-1]

	first_stop_id = None
	if _other_side_is_transit_free(first_leg["Delturnr"], "before"):
		first_stop_id = _resolve(first_leg["otp_mode"], first_leg.get("FromStation"))

	last_stop_id = None
	if _other_side_is_transit_free(last_leg["Delturnr"], "after"):
		last_stop_id = _resolve(last_leg["otp_mode"], last_leg.get("ToStation"))

	return first_stop_id, last_stop_id


def _prep_leg_df(leg_df):
	if leg_df is None or leg_df.empty:
		return None
	leg_df = leg_df.sort_values("leg_id").reset_index(drop=True).copy()
	for col in _ISO_TIME_COLS:
		leg_df[col] = pd.to_datetime(leg_df[col], utc=True)
	for col in _MS_TIME_COLS:
		leg_df[col] = pd.to_datetime(leg_df[col], utc=True, unit="ms")
	return leg_df


def stitch_candidates(transit_df, access_leg_df=None, egress_leg_df=None, wait_min=0):
	"""
	Combine a street-only access leg and/or egress leg (each a single OTP direct-mode
	itinerary, queried once via otp_client.request_direct_leg) with every itinerary in
	transit_df (a normal transit OTP query anchored at a known station), producing one
	stitched itinerary per transit_df iteration_id.

	Street-mode (walk/car) travel time in OTP doesn't depend on time-of-day, so the
	access/egress leg's own timestamps are discarded and recomputed relative to each
	transit itinerary: the access leg ends `wait_min` minutes before the transit legs'
	earliest departure, and the egress leg starts `wait_min` minutes after the transit
	legs' latest arrival — back-to-back by default (wait_min=0).
	"""
	if transit_df is None or transit_df.empty:
		return transit_df

	transit_df = transit_df.copy()
	for col in _ISO_TIME_COLS:
		transit_df[col] = pd.to_datetime(transit_df[col], utc=True)
	for col in _MS_TIME_COLS:
		transit_df[col] = pd.to_datetime(transit_df[col], utc=True, unit="ms")

	access_leg_df = _prep_leg_df(access_leg_df)
	egress_leg_df = _prep_leg_df(egress_leg_df)
	wait_delta = pd.Timedelta(minutes=wait_min)

	def _stitch_iteration(group):
		iteration_id = group.name
		group = group.sort_values("leg_id").reset_index(drop=True)
		parts = []

		if access_leg_df is not None:
			shift = group["start_leg"].min() - wait_delta - access_leg_df["end_leg"].iloc[-1]
			shifted_access = access_leg_df.copy()
			for col in _TIME_COLS:
				shifted_access[col] = shifted_access[col] + shift
			parts.append(shifted_access)

		parts.append(group)

		if egress_leg_df is not None:
			shift = group["end_leg"].max() + wait_delta - egress_leg_df["start_leg"].iloc[0]
			shifted_egress = egress_leg_df.copy()
			for col in _TIME_COLS:
				shifted_egress[col] = shifted_egress[col] + shift
			parts.append(shifted_egress)

		stitched = pd.concat(parts, ignore_index=True)
		stitched["leg_id"] = range(len(stitched))
		stitched["start_trip"] = stitched["start_leg"].min()
		stitched["end_trip"] = stitched["end_leg"].max()
		stitched["iteration_id"] = iteration_id
		return stitched

	stitched_df = (
		transit_df
		.groupby("iteration_id", group_keys=False)
		.apply(_stitch_iteration)
		.reset_index(drop=True)
	)

	for col in _ISO_TIME_COLS:
		stitched_df[col] = (
			stitched_df[col].dt.tz_convert(LOCAL_TIMEZONE).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
		)
	for col in _MS_TIME_COLS:
		#Don't assume a specific underlying resolution (pandas 3.x promotes tz-aware
		#datetime64 to different resolutions - e.g. us here - depending on the exact
		#construction path), so cast to ms explicitly before pulling out the int.
		stitched_df[col] = stitched_df[col].astype("datetime64[ms, UTC]").astype("int64")

	return stitched_df


def compute_anchor_split_segments(tu_deltur_sub, station_lookup):
	"""
	Splits the Delturnr-sorted TU leg sequence into an ordered list of segments at every
	resolvable ANCHOR_MODES (S_TRAIN/RAIL/SUBWAY) station boundary - not just the trip's
	outer first/last leg like find_known_anchor_stations, and without requiring the other
	side to be transit-free. Used by the post-failure fallback in tu_otp_matching.py when the
	normal full-route query finds nothing, so each segment gets its own OTP query and
	leg-sequence verification instead of giving up whenever a known station has a transit leg
	(e.g. a bus with no named stop) on either side of it.

	Each ANCHOR_MODES leg contributes up to two independent boundaries: its FromStation (a
	cut immediately before the leg, if it resolves via station_lookup) and its ToStation (a
	cut immediately after the leg, if it resolves). A leg with both endpoints resolvable
	therefore becomes its own atomic single-leg segment, bounded on both sides - no
	special-casing needed.

	Returns an ordered list of segment dicts:
		{"legs": DataFrame, "origin_stop_id": str | None, "destination_stop_id": str | None,
		 "kind": "direct" | "transit"}
	origin_stop_id/destination_stop_id is None when that end is the TU trip's own true
	origin/destination coordinate (only possible for the first/last segment). kind is
	"direct" iff no leg in the segment has an otp_mode in TRANSIT_MODES - since every
	boundary sits directly next to an ANCHOR_MODES (hence TRANSIT_MODES) leg, a "direct"
	segment is always flanked by "transit" segments (or the trip's own edge), never another
	"direct" segment.

	Returns None if no boundary resolves at all - equivalent to find_known_anchor_stations
	returning (None, None), i.e. nothing to split/anchor on for this trip.
	"""
	tu_deltur_sorted = tu_deltur_sub.sort_values("Delturnr").reset_index(drop=True)
	n = len(tu_deltur_sorted)

	def _resolve(mode, station_name):
		if station_name is None or (isinstance(station_name, float) and pd.isna(station_name)):
			return None
		return station_lookup.get((mode, _normalise_name(str(station_name))))

	#position -> stop_id. Boundary position p means the cut sits between leg p-1 and leg p
	#(0 <= p <= n); positions 0/n coinciding with a boundary just means the very first/last
	#leg's own known-station endpoint anchors that end, same as find_known_anchor_stations
	#already allows today.
	boundaries = {}

	def _set_boundary(position, stop_id, source):
		existing = boundaries.get(position)
		if existing is None:
			boundaries[position] = stop_id
		elif existing != stop_id:
			print(
				f"STATION_ANCHOR_SPLIT: boundary tie at position {position} - keeping "
				f"stop {existing}, ignoring {source}'s stop {stop_id}"
			)

	for i in range(n):
		leg = tu_deltur_sorted.iloc[i]
		if leg["otp_mode"] not in ANCHOR_MODES:
			continue
		from_stop_id = _resolve(leg["otp_mode"], leg.get("FromStation"))
		if from_stop_id:
			_set_boundary(i, from_stop_id, f"Delturnr {leg['Delturnr']} FromStation")
		to_stop_id = _resolve(leg["otp_mode"], leg.get("ToStation"))
		if to_stop_id:
			_set_boundary(i + 1, to_stop_id, f"Delturnr {leg['Delturnr']} ToStation")

	if not boundaries:
		return None

	positions = sorted({0, n, *boundaries.keys()})
	segments = []
	for start, end in zip(positions[:-1], positions[1:]):
		legs = tu_deltur_sorted.iloc[start:end]
		kind = "transit" if legs["otp_mode"].isin(TRANSIT_MODES).any() else "direct"
		segments.append({
			"legs": legs,
			"origin_stop_id": boundaries.get(start),
			"destination_stop_id": boundaries.get(end),
			"kind": kind,
		})
	return segments


def stitch_segment_chains(chains, direct_leg_cache, wait_min=0):
	"""
	Combine every surviving chain built by tu_otp_matching._try_split_station_anchored_fallback
	into one stitched itinerary DataFrame, assigning each chain a fresh, contiguous
	iteration_id. Generalizes stitch_candidates (which hardcodes exactly 1 access leg + 1
	transit_df + 1 egress leg) to an arbitrary ordered list of segment parts, mixing "direct"
	and "transit" kinds.

	Each chain is {"parts": [(kind, segment_index, leg_df_or_None), ...], ...} in left-to-right
	segment order, where kind is "transit" (leg_df holds that segment's own OTP itinerary,
	already absolute-correct - it was queried seeded at its actual predicted departure time,
	see _try_split_station_anchored_fallback) or "direct" (leg_df is None; the segment's
	single cached itinerary is looked up in direct_leg_cache by segment_index instead, since
	street-mode travel time doesn't depend on time of day and was only queried once).

	"transit" parts are used as-is (after the same ISO/ms -> tz-aware parsing stitch_candidates
	already applies via _prep_leg_df). "direct" parts are time-shifted to butt up against
	their neighboring part, wait_min apart - generalizing stitch_candidates' fixed
	access/egress shifting to an arbitrary position within an N-part chain: a direct part
	after the chain's first transit part is shifted to start wait_min after the previous
	(already-resolved) part ends; a direct part before it is shifted to end wait_min before
	the next (already-resolved) part starts.
	"""
	if not chains:
		return pd.DataFrame()

	wait_delta = pd.Timedelta(minutes=wait_min)
	prepped_direct_cache = {
		idx: _prep_leg_df(df.sort_values("leg_id").reset_index(drop=True))
		for idx, df in direct_leg_cache.items()
	}

	stitched_chains = []
	for iteration_id, chain in enumerate(chains):
		parts = chain["parts"]
		resolved = [None] * len(parts)

		first_transit_i = next(i for i, (kind, _, _) in enumerate(parts) if kind == "transit")
		_, _, first_leg_df = parts[first_transit_i]
		resolved[first_transit_i] = _prep_leg_df(first_leg_df.sort_values("leg_id").reset_index(drop=True))

		#Forward pass: every part after the first transit part (further transit parts are
		#already absolute-correct; direct parts are shifted against the previous part).
		for i in range(first_transit_i + 1, len(parts)):
			kind, seg_idx, leg_df = parts[i]
			if kind == "transit":
				resolved[i] = _prep_leg_df(leg_df.sort_values("leg_id").reset_index(drop=True))
				continue
			cached = prepped_direct_cache[seg_idx].copy()
			shift = resolved[i - 1]["end_leg"].max() + wait_delta - cached["start_leg"].iloc[0]
			for col in _TIME_COLS:
				cached[col] = cached[col] + shift
			resolved[i] = cached

		#Backward pass: any leading direct part(s) before the first transit part, shifted
		#against the next (already-resolved) part instead.
		for i in range(first_transit_i - 1, -1, -1):
			_, seg_idx, _ = parts[i]
			cached = prepped_direct_cache[seg_idx].copy()
			shift = resolved[i + 1]["start_leg"].min() - wait_delta - cached["end_leg"].iloc[-1]
			for col in _TIME_COLS:
				cached[col] = cached[col] + shift
			resolved[i] = cached

		stitched = pd.concat(resolved, ignore_index=True)
		stitched["leg_id"] = range(len(stitched))
		stitched["start_trip"] = stitched["start_leg"].min()
		stitched["end_trip"] = stitched["end_leg"].max()
		stitched["iteration_id"] = iteration_id
		stitched_chains.append(stitched)

	stitched_df = pd.concat(stitched_chains, ignore_index=True)

	for col in _ISO_TIME_COLS:
		stitched_df[col] = (
			stitched_df[col].dt.tz_convert(LOCAL_TIMEZONE).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
		)
	for col in _MS_TIME_COLS:
		stitched_df[col] = stitched_df[col].astype("datetime64[ms, UTC]").astype("int64")

	return stitched_df
