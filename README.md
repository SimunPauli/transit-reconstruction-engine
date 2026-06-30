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


bash python -m venv venv source venv/bin/activate pip install -r requirements.txt``` 

---

## Configuration

The project is configured via a `config.json` file in the project root.
`config.json` is excluded from version control.

Create it based on the template below:

json { "request": { "otp_url": "http://localhost:8080/otp/gtfs/v1", "search_window": "PT1H", "max_itinerary_candidates": 50, "request_timeout": 30 }, "matching": { "return_trip_summary": true, "print_deviation": true }, "paths": { "data_dir": "/path/to/tu/data/", "output_dir": "/path/to/output/", "log_file": "rmse_based_matches.log", "rmse_based_matches_file": "rmse_based_matches.xlsx", "trip_matching_summaries_file": "trip_matching_summaries.xlsx", "tu_gtfs_station_file": "tu_gtfs_station_df.xlsx", "map_file": "map.html" }, "tu_files": { "session_file": "tu_session.xlsx", "tur_file": "tu_tur.xlsx", "deltur_file": "tu_deltur.xlsx", "stations_file": "stations.xlsx" }, "station_matching": { "bbox_buffer_m": 1000, "station_name_threshold": 0.6 }, "squared_error_weights": { "w_departure_min": 1.0, "w_arrival_min": 1.0, "w_street_mode_min": 1.0, "w_street_mode_km": 1.0, "w_transit_min": 1.0, "w_transit_km": 1.0 }, "walk_bike_time_ratio": 0.266 }``` 

### Configuration reference

| Key | Description |
|-----|-------------|
| `request.otp_url` | GraphQL endpoint of the running OTP instance |
| `request.search_window` | ISO 8601 duration — time window searched around departure time |
| `request.max_itinerary_candidates` | Maximum number of OTP itineraries to fetch per trip |
| `request.request_timeout` | HTTP timeout in seconds for OTP requests |
| `matching.return_trip_summary` | Whether to output a per-trip summary Excel file |
| `matching.print_deviation` | Whether to print RMSE deviation details per trip |
| `paths.data_dir` | Directory containing the TU input Excel files |
| `paths.output_dir` | Directory where all output files are written |
| `paths.log_file` | Log file name (relative to `output_dir`) |
| `paths.rmse_based_matches_file` | Output file for the best-matched itineraries |
| `paths.trip_matching_summaries_file` | Output file for per-trip match summaries |
| `paths.tu_gtfs_station_file` | Output file for the TU–GTFS station mapping |
| `paths.map_file` | Output file name for optional map visualisation |
| `station_matching.bbox_buffer_m` | Search radius in metres when matching TU stations to GTFS stops |
| `station_matching.station_name_threshold` | Minimum cosine similarity (0–1) for a station name match |
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


bash python main.py

All console output is mirrored to the log file defined in `config.json`.

---

## Output

All output files are written to `paths.output_dir`.

| File | Description |
|------|-------------|
| `rmse_based_matches_file` | Best-matched OTP itinerary per TU trip, one row per leg |
| `trip_matching_summaries_file` | Per-trip summary including RMSE score and deviation metrics |
| `tu_gtfs_station_file` | Mapping between TU station names and GTFS stop IDs |
| `log_file` | Full console log of the run |

---

## How it works

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
