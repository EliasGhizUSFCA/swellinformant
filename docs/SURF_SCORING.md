# Surf quality, breaking height, confidence and swell events

Source: `backend/app/services/surf_quality/` and `backend/app/services/swell_detection/`.
Algorithm version: `ALGORITHM_VERSION = "1.0.0"` (stored on every prediction row, so a
changed algorithm never silently mixes with old scores).

> **Calibration status.** All weights, thresholds and per-spot factors below are a reasoned
> first version. None has been fitted to observed surf. Treat scores as a transparent,
> reproducible ranking signal, not ground truth. The schema supports per-spot calibration
> (`surf_spots.calibration` JSONB), and the code uses it automatically once coefficients exist.

## 1. Inputs per forecast timestep (hourly)

From the forecast provider (Open-Meteo GFS-Wave + GFS, Windy, or the demo generator):

| Group | Variables |
|-------|-----------|
| Combined sea | significant wave height, mean period, mean direction |
| Swell partitions | primary + secondary swell height / period / direction |
| Wind sea | wind-wave height / period / direction |
| Wind (10 m) | speed (km/h), direction (from), gusts |
| Sea level | sea-surface height incl. tide (Open-Meteo `sea_level_height_msl`), when available |

From the spot (`data/surf_spots/spots.json`): swell window, optimal swell direction range,
offshore wind direction, minimum and ideal period, working wave-height range, tide
preference, `height_factor`, and the coordinates used for the forecast (a point just
offshore, not the beach itself).

## 2. Estimated breaking (face) height

Offshore significant wave height is **not** the size of the surf. `breaking.py`:

1. **Calibrated model** (only if the spot's `calibration` JSON has `a`, `b`, `c`):
   `Hb_ft = a · H0^b · T^c × exposure(direction)`.
2. **Approximate model** otherwise, labelled `komar_gaughan_v1_uncalibrated` in the UI/API:
   * For each partition: Komar & Gaughan (1972) `Hb = 0.39 · g^0.2 · (T · H0²)^0.4` (metres).
   * × exposure: `sqrt(cos(distance outside the optimal window))`. A further Gaussian
     falloff (σ = 15°) applies once the direction leaves the swell window (blocked by
     headlands or islands).
   * Wind sea × 0.7 (short-period energy breaks weaker than its Hs suggests).
   * Partitions combine by energy: `Hb = sqrt(Σ Hb_i²)`.
   * × `height_factor` (heuristic per spot, e.g. > 1 for Nazaré's canyon focusing).
   * Reported range 0.8–1.3 × the central estimate, rounded to ½ ft.

## 3. Quality score (0–100)

Each component maps to [0, 1]. The base score is the weighted mean over the components
whose inputs exist; weights are renormalised so missing data does not count as zero.

| Component | Weight | Mapping |
|-----------|-------:|---------|
| Swell direction | 0.20 | 0 outside the swell window; 1 inside the optimal range; linear to 0 at 45° from it |
| Swell period | 0.20 | 0 below `min − 3 s`; ramps to 0.4 at `min`, then to 1.0 at the ideal period |
| Wave size | 0.20 | Breaking-height mid vs. the spot's working range: 0 at 40 % of min; 0.75 at min; 1.0 at the sweet spot (65 % of the range); 0.85 at max; falls to 0 at 1.5 × max |
| Wind | 0.30 | Glassy ≤ 5 km/h → 1. Above that, direction quality `(1 + cos Δ)/2` relative to offshore phases in fully by 20 km/h. A further penalty applies above 35 km/h |
| Tide | 0.05 | Only when the spot has a tide preference *and* sea level is available: `1 − 1.6·|stage − target|`, floor 0.2 |
| Consistency | 0.05 | Share of wave energy in organised swell vs. local wind sea |

Tide stage is the position in the local tidal cycle (0 = low, 1 = high), from a ±12.5 h
rolling min/max of sea level. It is left empty when the range is < 0.15 m, so it never
invents a tide.

**Limiting-factor gates.** A weighted mean would let glassy wind make up for flat or
weak surf, so the score is multiplied by:

* size gate `clamp(size / 0.6)`
* period gate `0.5 + 0.5·clamp(period / 0.5)`
* wind gate `0.5 + 0.5·clamp(wind / 0.5)`

`score = round(100 × base × size_gate × period_gate × wind_gate)`

**Labels** (lower bound inclusive):

| Score | Label |
|------:|-------|
| 95 | Exceptional |
| 85 | Excellent |
| 72 | Very Good |
| 57 | Good |
| 40 | Fair |
| 0 | Poor |

Every prediction stores the component values, the weights actually used, the wind
relation (glassy / offshore / cross-offshore / cross-shore / cross-onshore / onshore)
and a plain-English explanation, e.g.
*"1.8 m @ 15 s SW swell, well aligned with the break; offshore wind 12 km/h from NE;
approx. 5–8 ft faces vs. a working range of 3–12 ft; favourable tide."*

## 4. Daylight

Sunrise and sunset come from the NOAA solar position approximation (±2 min), extended by
20 minutes of twilight at each end. Only daylight steps count toward events and matches.

## 5. Forecast confidence (0–100), separate from quality

Confidence answers *"how much should I trust this?"*, not *"how good is it?"*.

```
confidence = (25 + 75·exp(−lead_days / 9))
           × (0.6 + 0.4 · completeness)          # share of key variables present
           × (0.7 + 0.3 · run_agreement)         # vs. previous model run; 0.9 if none
           × (0.8 if the model run is stale)
```

Labels: High ≥ 70, Moderate ≥ 45, Low below that. At 7 days a complete, consistent
forecast scores about 60 (Moderate). This is deliberately conservative: global wave
models lose skill with lead time, and swell arrival timing and size shift between runs.

## 6. Swell events (`swell_detection/`)

* Steps are hourly; **only daylight steps** are considered. Nights neither qualify nor
  break an event, so a swell that is good on consecutive mornings is *one* event.
* A daylight step qualifies when `score ≥ SWELL_EVENT_MIN_SCORE` (default 40 = Fair).
  This is deliberately permissive. Each saved search later extracts its own sub-window
  using its own thresholds (see MATCHING_AND_FLIGHTS.md).
* An event ends after `SWELL_EVENT_BREAK_DAYLIGHT_HOURS` (9) consecutive non-qualifying
  daylight hours, e.g. a full day of onshore wind splits two swells.
* An event needs at least `SWELL_EVENT_MIN_HOURS` (3) qualifying hours.
* Each event stores start/end, peak time/score/label, estimated face-height range,
  average quality, confidence, dominant swell height/period/direction, wind summary and
  the forecast run that produced it.

**Lifecycle on every new run:**

* A cluster that overlaps an existing active event updates it in place (`version + 1`,
  previous values appended to `history`).
* A cluster overlapping several events merges them; the extras become `cancelled` with
  `merged_into_id`.
* Active events no longer supported by the latest run become `downgraded`.
* Events whose end has passed become `passed`.

A PostgreSQL exclusion constraint (`btree_gist`, `tstzrange && per spot WHERE
status='active'`) makes overlapping active events at one spot impossible, even under
concurrent workers.

## 7. Calibrating later

Setting `surf_spots.calibration` to `{"a": …, "b": …, "c": …}` (extra keys such as sample
count or fit date are allowed and ignored) switches that spot to the calibrated
breaking-height model, and the UI stops showing the "uncalibrated" note. A realistic calibration source is buoy
data paired with surf observations or reports. The scoring weights live in one place
(`WEIGHTS`, `LABEL_THRESHOLDS` in `scoring.py`). Every prediction is stamped with the
algorithm version.
