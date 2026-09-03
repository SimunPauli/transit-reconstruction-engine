# TU Trip Reproducer
### Reproducing TU Trips — using OTP and GTFS

This project reproduces public transport trips from the Danish National Travel Survey
(**Transportvaneundersøgelsen**, TU) using a modified version of
[OpenTripPlanner (OTP) - currently private repo -](<!-- Add link to modified OTP repo here -->) 
and GTFS data.

For each trip in TU, the tool queries OTP for candidate itineraries and selects the
best match using a weighted RMSE score across departure time, arrival time, leg
duration, and leg distance.

The GTFS data is updated about every 10, and is available to DTU back to 2015 (with 
degrading quality). Each GTFS feed extents 90 days into the future and are mostly 
indentical, therefore I've createed a code for merging feed across time 
[GTFS temporal merger - currently private repo -](<!-- Add link to modified OTP repo here -->) 


The repository is public so that researchers who use the reproduced trip data can
inspect how it was produced. Running the tool itself requires access to the
confidential TU data.

This project will be extended with choice-set generation for route choice modelling.

---

## Requirements

- Python 3.13+
- A running instance of the modified OTP (link above), loaded with the relevant GTFS
  data, accessible at the `otp_url` defined in `config.json`

---

## Setup




```bash 

python -m venv venv 

source venv/bin/activate 

pip install -r requirements.txt #TODO: create requirements.txt

```
---

## Configuration

The project is configured via a `config.json` file in the project root.
`config.json` is excluded from version control.

Create it based on the template below:
```
{
  "request": {
    "otp_url": "http://localhost:8080/otp/gtfs/v1",
    "search_window": "PT1H",
    "max_itinerary_candidates": 50,
    "request_timeout": 30
  },

  "matching": {
    "return_trip_summary": true,
    "print_deviation": true,
    "station_anchor_wait_min": 0
  },
  "reluctance_retries": {
    "transit": {
      "enabled": true,
      "sequence": [0.5, 0.25, 0.1]
    },
    "walk": {
      "enabled": true,
      "sequence": [3, 4, 6]
    }
  },
  "paths": {
    "data_dir": "/home/user/Reproducing/Data/TU/",
    "output_dir": "/home/user/Reproducing/Output/",
    "log_file": "rmse_based_matches.log",
    "rmse_based_matches_file": "rmse_based_matches.csv",
    "trip_matching_summaries_file": "trip_matching_summaries.xlsx",
    "failures_file": "trip_failures.tsv",
    "summary_stats_file": "trip_matching_summary_stats.xlsx",
    "tu_gtfs_station_file": "tu_gtfs_station_df.csv",
    "map_file": "map.htmlz"
  },

  "tu_files": {
    "session_file": "tu_session_secret_2024.xlsx",
    "tur_file": "tu_tur_secret_2024.xlsx",
    "deltur_file": "tu_deltur_2024.xlsx",
    "stations_file": "Stationer_tudatabase.xlsx"
  },
  "tu_subset": {
    "year": 2024,
    "tu_PtPrimMode": [31, 32, 33, 34, 37]
  },
  "station_matching": {
    "bbox_buffer_m": 1000,
    "station_name_threshold": 0.7
  },
  "squared_error_weights": {
    "w_departure_min": 1.0,
    "w_arrival_min": 1.0,
    "w_street_mode_min": 0.0,
    "w_street_mode_km": 1.0,
    "w_transit_min": 1.0,
    "w_transit_km": 0.0
  },
  "walk_bike_time_ratio": 0.266
}
```

### Configuration reference

| Key | Description |
|-----|-------------|
| `request.otp_url` | GraphQL endpoint of the running OTP instance |
| `request.search_window` | ISO 8601 duration — time window searched around departure time |
| `request.max_itinerary_candidates` | Maximum number of OTP itineraries to fetch per trip |
| `request.request_timeout` | HTTP timeout in seconds for OTP requests |
| `matching.return_trip_summary` | Whether to output a per-trip summary Excel file |
| `matching.print_deviation` | Whether to print RMSE deviation details per trip |
| `matching.station_anchor_wait_min` | Minutes of slack between the street-only access/egress leg and the transit leg in the station-anchored fallback (see below). `0` = back-to-back. |
| `reluctance_retries.transit.enabled` | Whether to retry with reduced per-mode transit reluctance when no candidate survives filtering (see "Fetching candidates from OTP" below) |
| `reluctance_retries.transit.sequence` | Transit reluctance values tried, per mode then per mode combination, when `reluctance_retries.transit.enabled` is true |
| `reluctance_retries.walk.enabled` | Whether to retry with increased `walk_reluctance` when no candidate survives filtering |
| `reluctance_retries.walk.sequence` | `walk_reluctance` values tried, in order, when `reluctance_retries.walk.enabled` is true |
| `paths.data_dir` | Directory containing the TU input Excel files |
| `paths.output_dir` | Base directory for output files; each run writes into `output_dir/<tu_subset.year>/<run_id>/`, where `run_id` is a `YYYYMMDD_HHMMSS` timestamp generated at startup |
| `paths.log_file` | Log file name (relative to `output_dir`) |
| `paths.rmse_based_matches_file` | Output file for the best-matched itineraries |
| `paths.trip_matching_summaries_file` | Output file for per-trip match summaries |
| `paths.failures_file` | Output file listing TurId + failure reason code for trips that weren't reconstructed (see "Failure reasons" below); contains no coordinates, station names, or other survey data |
| `paths.summary_stats_file` | Output file for run-level summary statistics (success rate, failure reason breakdown) |
| `paths.tu_gtfs_station_file` | Output file for the TU–GTFS station mapping |
| `paths.map_file` | Output file name for optional map visualisation |
| `station_matching.bbox_buffer_m` | Search radius in metres when matching TU stations to GTFS stops |
| `station_matching.station_name_threshold` | Minimum name-similarity score (0–1, rapidfuzz WRatio) for a station name match; stations with no candidate above this are left unmatched for that mode rather than guessed by distance |
| `squared_error_weights` | Weights applied to each component of the RMSE score |
| `walk_bike_time_ratio` | Factor applied to OTP walk time to approximate bicycle travel time |

---

## Input data

The TU data is confidential and not included in this repository.
The tool expects four Excel files, with at minimum the columns listed below.

### Session file (`tu_files.session_file`)

| Column | Description |
|--------|-------------|
| `SessionId` | |

### Trip file (`tu_files.tur_file`)

| Column | Description |
|--------|-------------|
| `TurId` | |
| `SessionId` | |
| `DiaryDate` | Days since 1970-01-01 |
| `DiaryYear` | |
| `PtPrimMode` | Primary public transport mode code |
| `DepartHH` | Departure hour |
| `DepartMM` | Departure minute |
| `ArrivalHH` | Arrival hour (may exceed 24 for trips crossing midnight) |
| `ArrivalMM` | Arrival minute |
| `orig_e` | Origin easting (UTM zone 32N) |
| `orig_n` | Origin northing (UTM zone 32N) |
| `tiladre` | Destination easting (UTM zone 32N) |
| `tiladrn` | Destination northing (UTM zone 32N) |

### Leg file (`tu_files.deltur_file`)

| Column | Description |
|--------|-------------|
| `TurId` | |
| `Delturnr` | Leg sequence number within a trip |
| `StageMode` | Transport mode code |
| `StageLength` | Leg distance (km) |
| `StageDurationMin` | Leg duration (minutes) |
| `StageWaitMin` | Waiting time before leg (minutes) |
| `Route` | Route short name (for bus and S-train legs) |
| `FromStation` | Boarding station name |
| `ToStation` | Alighting station name |

### Stations file (`tu_files.stations_file`)

| Column | Description |
|--------|-------------|
| `statnavn` | Station name |
| `e` | Easting (UTM zone 32N) |
| `n` | Northing (UTM zone 32N) |
| `OpenDate` | Days since 1970-01-01; blank if open before data collection period |
| `ClosedDate` | Days since 1970-01-01; blank if still open |
| `stog` | 1 if the station serves S-train |
| `metro` | 1 if the station serves metro |
| `andettog` | 1 if the station serves regional/intercity rail |
| `letbane` | 1 if the station serves light rail (tram) |

---

## Running

Ensure OTP is running and accessible at the configured `otp_url`, then:

> **Note:** OTP graphs can be built from multiple GTFS feeds (transit data)
> spanning different time periods for the same region. However, due to how OTP
> handles stops, loading multiple overlapping feeds for the same region will
> break this program.

bash python main.py

All console output is mirrored to the log file defined in `config.json`.

---

## Output

All output files are written to `paths.output_dir/<tu_subset.year>/<run_id>/` (created if it doesn't exist), where `run_id` is a `YYYYMMDD_HHMMSS` timestamp generated when the run starts — so re-running for the same year never overwrites a previous run's output, and different years never mix in the same folder.

| File | Description |
|------|-------------|
| `rmse_based_matches_file` | Best-matched OTP itinerary per TU trip, one row per leg |
| `trip_matching_summaries_file` | Per-trip summary including the trip's outcome (see "Trip outcomes" below), RMSE score and deviation metrics |
| `failures_file` | TurId + failure reason code for every trip that wasn't reconstructed (see "Failure reasons" below) |
| `summary_stats_file` | Run-level summary: counts and success rates per outcome, plus a breakdown of how often each `failure_reason` code occurred, as % of all trips and % of failures. Also printed to console/`log_file` at the end of the run. |
| `tu_gtfs_station_file` | Mapping between TU station names and GTFS stop IDs |
| `log_file` | Full console log of the run |

### Trip outcomes

Every TU trip ends in exactly one of three mutually exclusive outcomes, recorded as three 0/1
columns in `trip_matching_summaries_file` and counted in `summary_stats_file`'s overview sheet:

| Column | Meaning |
|---|---|
| `trip_found` | Matched, with every route name TU recorded for its bus/S-train legs present in the itinerary |
| `trip_wrong_route` | Matched only after the route-name filter was dropped — the itinerary uses the right modes in the right order, but not necessarily the routes TU recorded |
| `trip_not_found` | No itinerary matched; see `failure_reason` |

`trip_wrong_route` comes from the retry described in [candidate filtering](#5-candidate-filtering):
when a trip with a bus/S-train leg fails for a route-related reason (`invalid_route_name`,
`no_required_routes`, `no_otp_candidates`, or their `anchored_` variants), the search is run once
more with route names ignored and matching falls back to mode + leg order. Such a trip is still
written to `rmse_based_matches_file` — it's kept separate rather than dropped, because its route
assignment is the part that's unverified, not the trip itself. `summary_stats_file` reports
`success_rate_pct` (`trip_found` only) alongside `success_rate_incl_wrong_route_pct` (both).

### Failure reasons

Both `trip_matching_summaries_file`'s `failure_reason` column and `failures_file` use short codes
rather than full sentences, so failures can be grepped/counted without wading through prose (the
full sentence, with any relevant non-personal detail such as required routes, is still printed to
`log_file` and kept in `trip_matching_summaries_file`'s `last_print_if_not_found` column).

Anything produced while trying the [station-anchored fallback](#7-station-anchored-fallback) is
prefixed `anchored_` — this only appears when a fallback was actually attempted (i.e. an anchor
station was found); trips with no S_TRAIN/RAIL/SUBWAY leg to anchor on just keep the full-route
search's own reason, since a fallback was never possible.

For trips that *were* successfully reconstructed via the fallback, `trip_matching_summaries_file`'s
`used_anchor_fallback` column is `True` (it's `False` for every normal full-route match). Each such
trip also prints a `ANCHOR_FALLBACK_USED: TurId=<id>` line, so every fallback-reconstructed trip can
be found in `log_file` with a plain text search.

| Code | Meaning |
|------|---------|
| `no_valid_modes` | None of the TU trip's legs map to a usable OTP transit mode |
| `invalid_route_name` | A required BUS/S_TRAIN route name is missing/malformed in TU |
| `no_otp_candidates` | OTP returned zero itineraries for the full-route search |
| `no_required_routes` | OTP itineraries were found, but none used all the required BUS/S_TRAIN routes |
| `no_required_modes` | OTP itineraries were found, but none used all the required transit modes |
| `no_matching_leg_sequence` | OTP itineraries were found, but none matched the TU legs in the correct order |
| `no_direct_access_route` | Station-anchored fallback: no direct (walk/car) route found from the true origin to the anchor station |
| `no_direct_egress_route` | Station-anchored fallback: no direct (walk/car) route found from the anchor station to the true destination |
| `anchored_no_otp_candidates` | Station-anchored fallback: the direct access/egress leg(s) were found, but OTP returned zero transit itineraries from/to the anchor station |
| `anchored_no_required_routes` / `anchored_no_required_modes` / `anchored_no_matching_leg_sequence` | Station-anchored fallback: transit itineraries were found from/to the anchor station, but none passed the same route/mode/sequence checks as the full-route search |
| `no_best_match` | Candidates passed all filters, but RMSE ranking found no best trip |
| *(exception class name, e.g. `TimeoutError`)* | An unexpected error was raised while processing the trip; see `log_file` for the message |

---

## How to run

1. **Load TU data** — session, trip, leg, and station data are read from Excel.
2. **Match TU stations to GTFS** — each TU station is matched to a GTFS stop via
   bounding-box queries to OTP, filtered by name similarity and proximity.
3. **For each TU trip:**
   - OTP is queried via the `planConnection` GraphQL API for candidate itineraries
     within the search window, using bidirectional pagination.
   - Candidates are filtered to ensure all required transit modes and route names
     from TU are present, in the correct order.
   - Each OTP leg is aligned to the corresponding TU leg.
   - The best itinerary is selected by minimising a weighted RMSE across departure
     time, arrival time, leg duration, and leg distance.
4. **Results are exported** to Excel.




## How it works

### Overview

TU records each trip as an ordered sequence of legs (*delture*), where each leg
has a mode, duration, distance, and — for bus and S-train — route name.
For rail, metro, and S-train it also records station names.

The goal is to find, for each TU trip, the specific real-world transit itinerary
the respondent most likely took, expressed as a routable GTFS itinerary from OTP.

---

### 1. Pre-run: TU station → GTFS stop mapping

Before trip matching begins, every TU station in the stations file is mapped to
one or more GTFS stops in OTP. For each station:

1. OTP is queried for all GTFS stops within a configurable bounding box
   (`bbox_buffer_m`).
2. Stops are filtered to those that serve a mode relevant to that station (S-train,
   metro, rail, or tram).
3. Among the remaining stops, the one with the best name-similarity score (rapidfuzz
   `WRatio`, ties broken by distance) against the TU station name — or any of its
   known aliases (`TU_NAME_ALIASES`, for TU names that predate a later GTFS station
   rename) — is selected, provided it clears `station_name_threshold`. If no stop
   clears the threshold, that mode is left unmatched for that station rather than
   guessed by distance alone.

The result is a lookup table — `tu_gtfs_station_df` — mapping
`(otp_mode, tu_station_name)` → `gtfs_station_id`. This is used later to constrain
OTP queries and to verify that candidate itineraries pass through the correct
stations.

#### Known GTFS data quirk: mislabeled replacement-bus mode in early-2018 releases

Some early-2018 DTU feed releases tag rail-replacement-bus trips with the *original*
train's mode instead of `BUS`. Confirmed pattern in `routes.txt`: several DSB
"Togbus" routes are tagged `route_type=2` (RAIL) in releases dated `20171219`/
`20180215`, then the identical route reappears correctly tagged `route_type=3` (BUS)
in a release from `20180712` onward — the mislabeling was fixed partway through 2018.

Metroselskabet's metro-replacement buses go a step further: rather than using a
separate "Togbus"-style route at all, they run substitute trips directly under the
real M1/M2 `route_id`, with the real `SUBWAY` mode. Concretely, the GTFS stop
`Nørreport St. (Nørre Voldgade)` — an ordinary street-level bus stop (route 350S) —
also shows up as serving `SUBWAY`, but only because of 187 trips tied to a single
`service_id` whose `calendar_dates.txt` entries cover exactly 2018-04-30 through
2018-05-04 (a 5-day window, presumably a planned closure/diversion at Nørreport).
There's no separate route name to filter on for this case — it's only identifiable
by that anomalously short calendar span.

In practice this mostly doesn't matter: the name-similarity-first tie-break in step 3
above means the real platform stop (e.g. `Nørreport St. (Metro)`) is preferred for
matching almost year-round. It would only bite for a TU trip whose actual travel date
falls inside one of these short mislabeled windows, which per-mode station matching
(built once per whole TU period, not per exact date) doesn't currently detect.

---

### 2. Per-trip: building the OTP query

For each TU trip, the legs are inspected to determine what constraints to pass to
OTP:

**Transit modes** — the set of distinct transit modes in the trip's legs is extracted
and passed to OTP so that only itineraries using those modes are returned.

**Route names** — TU records route short names for bus and S-train legs. These are
passed to OTP as a route filter so that only itineraries containing those specific
routes are considered. For rail, metro, tram — where TU does not record
route names — all routes for those modes are fetched from OTP at startup and used
as the filter set.

**Via stops** — for S-train, rail, and metro legs, the boarding and alighting
stations recorded in TU are resolved to GTFS stop IDs using the pre-built station
mapping. These are passed to OTP as `via` constraints, forcing candidate itineraries
to pass through those stops in order.

**Access/egress mode** — the first and last legs of the TU trip determine the
access and egress mode passed to OTP (walk, car drop-off/pick-up, or bicycle).
OTP normally routes each mode only on infrastructure that mode is permitted to use.
However, because cyclists tend to take the shortest path even when it crosses
pedestrian-only infrastructure, the modified OTP used by this project allows
bicycles to use all pedestrian paths as well. The reverse — pedestrians using
bicycle paths — is also permitted, reflecting Danish traffic rules where
pedestrians may use a cycling path when no pedestrian alternative is available.
These adjustments are intentional: the goal is trip reproduction, not strict
mode-segregated routing.

---

### 3. Fetching candidates from OTP

OTP is queried via the `planConnection` GraphQL API with the constraints above,
starting from the TU departure time. Because OTP returns a paginated result, the
tool fetches itineraries both forward and backward in time until it has up to
`max_itinerary_candidates` itineraries within the `search_window`, or the `search_window`
boundary is reached. Duplicate itineraries from pagination overlap are removed.

If the initial query returns no valid candidates after filtering, the tool retries with two
independent, configurable mechanisms (`reluctance_retries` in `config.json`), each of which
can be toggled on/off and has a configurable value sequence:

- **Walk retries** (`reluctance_retries.walk`) — increases `walk_reluctance` (default `[3, 4,
  6]`, above the baseline of `2`), one value per attempt. This targets cases where OTP's own
  cost model prefers walking further to reach a different, "cheaper" stop/route than the one
  the respondent actually used — TU respondents often go to greater lengths to avoid walking
  than OTP's default cost function assumes, so this makes OTP itself avoid extra walking more.
- **Transit retries** (`reluctance_retries.transit`) — reduces reluctance for the trip's
  transit mode(s) (default `[0.5, 0.25, 0.1]`, below the baseline of `1`), first for
  individual modes and then for combinations. This targets cases where OTP's search found
  nothing at all in the search window, by making transit relatively cheaper against other
  options; it does not help when OTP already returned itineraries but none matched the
  required route/mode/leg sequence, since reluctance only affects which itineraries OTP's own
  search generates — not the requirement filtering that runs afterward.

Walk retries run before transit retries. All other query parameters (route constraints, via
stops, time window) stay fixed across every retry attempt. Access/egress which are car to
bus as transit still struggle, since bus has no station in TU — this is unrelated to
reluctance and should be looked at separately.

**Street reluctance** — OTP’s default street reluctance is set to `2`, which makes
time spent walking on street networks more expensive than in-vehicle time. This
value is somewhat arbitrary, but it stays within OTP’s recommended range for
walking reluctance, which is roughly between `2` and `4`.

---

### 4. Leg alignment

OTP itineraries often contain more legs than TU records — most commonly short
transfer walk legs between transit services that TU does not capture. To make the
two sequences comparable, each OTP itinerary is aligned against the TU leg sequence
using a greedy forward pass:

- Each OTP leg is matched to the current TU leg if mode, route name (for bus/S-train),
  and boarding/alighting stations (for rail/metro/S-train) all agree.
- If an OTP leg does not match the current TU leg, it is treated as an extra OTP leg
  (e.g. a transfer walk) and left unmatched — the TU leg position is not advanced.
- Due to OTP router requireing both access and egress to be bicycle if one is bicycle;
  they are matched to OTP walk legs, and the OTP walk duration is then scaled by
  `walk_bike_time_ratio` to approximate cycling time.

After alignment, each OTP leg carries the `Delturnr` of its matched TU leg, or
`NA` if unmatched.

---

### 5. Candidate filtering

After alignment, candidates are filtered to remove any itinerary where:

- Not all required bus/S-train route names are present.
- Not all required transit modes are present.
- The transit legs do not appear in the same order as in TU (verified as a
  subsequence match on matched `Delturnr` values).

If a trip with a bus/S-train leg ends up with no candidates for a route-related reason, the
whole search is retried once with the route-name filter dropped; a match found that way is
recorded as `trip_wrong_route` rather than `trip_found` (see [Trip outcomes](#trip-outcomes)).

---

### 6. Selecting the best match — weighted RMSE

The remaining candidates are ranked by a weighted rooted sum of squared error (WRSS).
In the code this is referred to as RMSE, because taking the average
does not change the ranking and RMSE is better known. The score compares
each OTP itinerary against the TU record across four dimensions:

| Dimension | Compared at |
|-----------|-------------|
| Departure time | Trip level (minutes deviation) |
| Arrival time | Trip level (minutes deviation) |
| Trainst duration | Per matched leg transit |
| Street distance | Per matched leg transit |

Each dimension has a configurable weight (`squared_error_weights` in `config.json`).
Unmatched OTP legs, such as extra transfer walks, incur no penalty. The current
weights set the street-duration and transit-distance terms to `0`, because
respondents’ reported transit distances are considered unreliable, and because
sum of duration on street and transit legs is already indirectly captured by the
departure- and arrival-time terms. The WSS is:

$$
\text{WSS} = 
  \sqrt{ 
    \sum_{\text{legs}} \left(
      w_\text{dep} \cdot \Delta t_\text{dep}^2 +
      w_\text{arr} \cdot \Delta t_\text{arr}^2 +
      w_\text{min} \cdot \Delta \text{dur}^2 +
      w_\text{km} \cdot \Delta \text{dist}^2
    \right)
  }
$$

The itinerary with the lowest RMSE is selected as the reproduced trip.

---

### 7. Station-anchored fallback

Sometimes the full-route query (steps 2–3) returns no valid itinerary at all, even
though TU records which S-train, regional/intercity rail, or metro station the
respondent boarded or alighted at — OTP's own street-access routing can prefer a
different, nearby station instead of the one actually used. (Only S-train, rail, and
metro legs have reliably populated station names in TU; bus stops aren't named and
tram names aren't reliable enough to anchor on.)

When the full-route search comes back empty and the first and/or last S-train/rail/
metro leg's station resolves via the TU–GTFS station mapping, the tool splits the
query instead of giving up. A side (access or egress) is only anchored this way if every TU leg
on that side is street-mode — if getting to the anchor station itself involved another transit
leg (e.g. a bus), that side is left alone, since a single direct walk/car leg can't stand in for
a real transit leg without breaking the route/mode/leg-order checks in step 5:

1. A street-only (walk or car) itinerary from the true origin to the known station,
   and/or from the known station to the true destination, is queried once — OTP's
   street-mode travel time doesn't depend on time of day, so this only needs to be
   requested once and its duration reused for every transit candidate.
2. A transit-only itinerary is queried from/to the known station instead of the true
   origin/destination, using the same mode/route/via filters as the normal query.
3. The two are stitched into a single itinerary per transit candidate, with the
   street-only leg placed `matching.station_anchor_wait_min` minutes before/after the
   transit leg (`0` = back-to-back, the default).
4. The stitched itinerary is run through the same leg alignment (step 4) and
   candidate filtering (step 5) as any other candidate.

