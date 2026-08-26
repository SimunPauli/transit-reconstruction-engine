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


def find_known_anchor_stations(tu_deltur_sub, tu_gtfs_station_df):
	"""
	Resolve the first and/or last S_TRAIN/RAIL/SUBWAY TU leg's boarding/alighting
	station to a GTFS stop ID, for use as a query anchor when the normal full-route
	OTP search finds nothing.

	A side is only resolved if every TU leg before it (for the access/first side) or after it
	(for the egress/last side) is street-mode — otherwise the fallback's direct access/egress
	leg would have to stand in for a real bus/rail/etc. leg it can't reproduce, and that leg's
	route/mode/Delturnr could never satisfy the requirements checked downstream. See
	TRANSIT_MODES.

	Returns (first_stop_id, last_stop_id); either may be None if the corresponding side isn't
	anchorable (no rail-type leg, another transit leg in the way, or the station doesn't
	resolve via tu_gtfs_station_df).
	"""
	if tu_gtfs_station_df is None or tu_gtfs_station_df.empty:
		return None, None

	station_lookup = {}
	for _, row in tu_gtfs_station_df.iterrows():
		key = (row["otp_mode"], _normalise_name(str(row["tu_station_name"])))
		station_lookup.setdefault(key, row["gtfs_station_id"])

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
