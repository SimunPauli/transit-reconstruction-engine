# Choice-set check

`synthetic_od_check.py` checks whether the choice-set generation misses real alternatives, in
particular slower routes that a regular commuter might take out of habit but that OTP drops because
a faster route dominates them.

## Running

From `tu_reconstruct_trips/`:

```bash
.venv/bin/python choice_set_check/synthetic_od_check.py <out_dir>
```

It needs a running OTP at `config.json`'s `request.otp_url`, serving a graph that covers `DEPART`
(set in the script, 2025-09-16 07:30). No TU data is needed. The choice-set settings
(`choice_set.exclude_stops`, `exclude_routes`, `search_window`, ...) come from the current
`config.json`.

## Method

The script uses a fixed list of synthetic origin–destination pairs (`ODS`): commuter corridors with
known slower alternatives, such as bus 5C vs. metro M3 or S-train E vs. regional train RE. For each pair:

- **Production:** the choice set from `run_choice_set.trip_candidates` and `build_choice_set`, as in
  a real run, with walk access/egress and no chosen trip.
- **Oracle:** one OTP query per route, and per pair of routes, serving stops within `RADIUS_M` of the
  origin and destination. Each query allows only those routes, so it finds alternatives that OTP's
  search would otherwise drop because a faster route dominates them. Rail replacement and night buses
  are skipped. Only alternatives with at most one transfer are covered.

An oracle alternative counts as plausible if it takes at most `PLAUSIBLE_RATIO` times the fastest
travel time and walks at most `PLAUSIBLE_WALK_KM`. Alternatives are compared by their sequence of
transit routes, ignoring the stops. A missed alternative is *dominated* if, for each of its
departures, a production itinerary leaves at the same time or later, arrives earlier and has no more
transfers. A route that only shows up in one or two exclude rounds was found by luck in the random
draw, and may be missed with another `seed`.

## Output

Written to `<out_dir>`:

| File | Content |
|---|---|
| `summary.csv` | Per pair: fastest time, plausible alternatives found / total, also within 1.3× the fastest, and how many were found in the plain query (round 0) |
| `misses.csv` | Plausible alternatives the choice set missed: time ratio, transfers, walk, whether a sibling line was found, and the dominating itinerary |
| `<od>_choice_set.csv` | The production choice set |
| `<od>_itineraries.csv` | All production and oracle itineraries, with the exclude round each was found in |
| `<od>_production.log` | The production run's printed output |
