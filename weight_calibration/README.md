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

**Street distance vs leg length.** `w_street_mode_km` is one weight for all street legs, which
assumes a 300 m walk and a 10 km bike ride are equally far off in km. To check this, street legs
are binned by TU distance, and the spread per bin is fitted as

```
sd_km = c * distance_km^p
```

- `c` is the typical deviation (km) of a 1 km leg: the size of the error.
- `p` is how the deviation grows with leg length: the shape of the error.

| `p` | Meaning | Consequence for the weight |
|---|---|---|
| ≈ 0 | Same km deviation at any length | A single `w_street_mode_km` fits |
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
| `transit_duration` | Spread of transit leg duration deviations (min), per mode |
| `street_distance` | Spread of street leg distance deviations (km), per mode |
| `street_distance_bins` | Street distance spread per mode and TU distance bin |
| `street_distance_fit` | Fitted `c` and `p` per mode |
| `implied_weights` | Weights implied by the spreads, next to the configured ones |
| `large_time_deviations` | Trips with departure or arrival off by more than 60 min, likely TU time errors |

Only `w_trip_duration_min`, `w_transit_min` and `w_street_mode_km` get implied values; `w_street_mode_min`
and `w_transit_km` are not estimated.

## Limitations

The spreads are optimistic: each match was picked by minimising these same deviations under the
current weights, so the residuals are smaller than the true noise. Components with high weights are
shrunk most, which pulls the implied weights towards the configured ones. Use them as a check on
direction and rough size, not as exact values; re-running with the implied weights and comparing
shows whether they settle.
