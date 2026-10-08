# Weight calibration

`rmse_residuals.py` measures how far a finished run's best matches deviate from TU on each RMSE
component, and suggests `squared_error_weights` from those deviations. See the main README's
"Selecting the best match — weighted RMSE" section for how the weights are used.

## Running

From `tu_reconstruct_trips/`, pointing at a finished run directory:

```bash
.venv/bin/python weight_calibration/rmse_residuals.py <output_dir>/<year>/<run_id>
```

It reads the run's `rmse_based_matches_file` and `trip_matching_summaries_file`, and reloads the TU
leg file to compare against. The TU year is taken from the run directory's `<year>`. The TU files,
weights and other settings come from the `config.json` saved in the run directory; runs from before
it was saved fall back to the current `config.json`, whose TU files and settings must then match the
run. Their run weights are unknown, so `implied_weights`' `configured_weight` is left empty. No OTP
server is needed.

The tables are printed and written to `rmse_residuals.xlsx` in the run directory.

## Method

Only trips found with the TU-recorded route (`trip_found == 1`) are used.

**Weights from spreads.** A weight acts as 1 / (expected deviation)², so a noisy component counts
less. Only the weights' ratios affect the ranking, so each implied weight is scaled to the
configured `w_departure_min`:

```
implied_k = w_departure_min * (sd_departure / sd_k)²
```

**Robust sd.** Spreads use `1.4826 * MAD` (median absolute deviation). It equals the sd for normal
errors, but a few bad matches or TU time errors can't inflate it. The plain `sd` is reported next to
it; a much larger `sd` points to outliers.

**Street distance vs leg length.** One weight for all street legs would assume a 300 m walk and a
10 km bike ride are equally far off in km, so `w_street_mode_km_by_length` sets it per TU length
bin. `implied_weights` gives one implied weight per configured bin. To see how the spread grows,
street legs are binned by TU distance, and the spread per bin is fitted as

```
sd_km = c * distance_km^p
```

- `c` is the typical deviation (km) of a 1 km leg: the size of the error.
- `p` is how the deviation grows with leg length: the shape of the error.

| `p` | Meaning | Consequence for the weight |
|---|---|---|
| ≈ 0 | Same km deviation at any length | One weight fits all lengths |
| ≈ 0.5 | Grows like √length (many small independent detours) | Long legs dominate the score |
| ≈ 1 | Same % deviation at any length | Long legs dominate the score |

A street leg of length `d` adds `w * Δ²` to the score. With mean deviation ≈ 0, its expected
contribution is

```
E[w * Δ²] = w * sd_km² = w * c² * d^(2p)
```

With one constant `w`, a leg of length `d₁` counts `(d₁/d₂)^(2p)` times as much as one of length
`d₂`. For example, with `c = 0.1` and `p = 1` (legs about 10% off), a 10 km leg counts 100× a 1 km
leg. A per-leg weight `w(d) = 1 / (c * d^p)²` makes every leg's expected contribution 1.

`c` and `p` are fitted per mode as a straight line in logs,

```
log sd_km = log c + p * log d
```

by least squares over the bins, weighted by leg count, leaving out bins under 20 legs. `ALL_STREET`
pools all street modes.

## Output sheets

| Sheet | Contents |
|---|---|
| `trip_times` | Spread of departure, arrival and trip duration (arrival − departure) deviations (min) |
| `time_correlation` | Measured departure–arrival correlation (Pearson, Spearman), next to the one the trip duration term assumes |
| `transit_duration` | Spread of transit leg duration deviations (min), per mode |
| `street_distance` | Spread of street leg distance deviations (km), per mode |
| `street_distance_bins` | Street distance spread per mode and TU distance bin |
| `street_distance_fit` | Fitted `c` and `p` per mode |
| `implied_weights` | Weights implied by the spreads, next to the configured ones |
| `large_time_deviations` | Trips with departure or arrival off by more than 60 min, likely TU time errors |

Only `w_trip_duration_min`, `w_transit_min` and `w_street_mode_km_by_length` (per bin, over all
street modes) get implied values; `w_street_mode_min` and `w_transit_km` are not estimated. For runs
from before the length bins, the run's single `w_street_mode_km` is shown against the current
config's bins.

## Limitations

The spreads are optimistic: each match was picked by minimising these same deviations under the
current weights, so the residuals are smaller than the true noise. Components with high weights are
shrunk most, which pulls the implied weights towards the configured ones. Use them as a check on
direction and rough size, not as exact values; re-running with the implied weights and comparing
shows whether they settle.

## Comparing two runs

The residuals can't show whether different weights pick better itineraries, only that they pick
different ones. `compare_runs.py` lists which trips changed, e.g. to hand-check a sample:

```bash
.venv/bin/python weight_calibration/compare_runs.py <run_dir_a> <run_dir_b>
```

Each trip's transit legs are joined into two signatures and compared per TurId. Street legs are left
out, as with the same graph they follow from the transit legs.

| Change | Meaning |
|---|---|
| `route/transfer` | Different mode, route, or boarding/alighting stop on some leg |
| `departure only` | Same route and stops, different train number or leg times |
| `unchanged` | Same itinerary |
| `status change` | Different outcome or route match level, or the trip is in one run only |

The first three count only trips found (`trip_found`) in both runs. Status is decided before the
weights rank the candidates, so between runs that differ only in weights it should be 0; otherwise
something else differed, such as OTP timeouts or the config. Written to
`compare_<run_id_a>_vs_<run_id_b>.xlsx` in `run_dir_b`, with sheets `summary`, `changed_trips` (both runs' signatures and RMSE components) and
`status_changes`.
