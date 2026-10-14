# Surf quality, breaking height, uncertainty, confidence and swell events

Source: `backend/app/services/surf_quality/` and `backend/app/services/swell_detection/`.
Algorithm version: `ALGORITHM_VERSION = "2.0.0"` (stored on every prediction row, so a
changed algorithm never silently mixes with old scores; the live run is rescored after an
upgrade while the prediction archive keeps what was originally issued).

> **Calibration status.** The nearshore model, the uncertainty model, the score weights and
> every per-spot parameter are a reasoned, documented first version. None has been fitted to
> observed surf at these 50 spots: no open surf-observation dataset exists for them (see
> `docs/surf-forecast-model-research.md`, sections 8 and 9). Breaking heights are labelled
> estimates, forecast ranges are labelled "not yet verified", and the score is a transparent
> ranking signal, not ground truth. The schema supports per-spot calibration
> (`surf_spots.calibration`) and nearshore observations (`nearshore_observations`).

## 1. Inputs per forecast timestep

The authoritative forecast per spot is either the multi-model blend or the highest-priority
configured source (see `docs/FORECAST_SOURCES.md`). Per valid time it provides:

| Group | Variables |
|-------|-----------|
| Combined sea | significant wave height (Hs), period (with its definition: `tp`, `tm01`, `tm02`, `tm10`), direction |
| Swell partitions | up to three swell partitions (height / peak period / direction FROM); WAVEWATCH III ranks them by height |
| Wind sea | wind-wave height / period / direction |
| Wind (10 m) | speed (km/h), direction (from), gusts |
| Sea level | sea-surface height incl. tide, when available |

From the spot (`data/surf_spots/spots.json`): swell window, optimal swell direction range,
offshore wind direction, minimum and ideal period, working wave-height range, tide
preference and `height_factor`. From its physical profile (`spot_physical_profiles`): shore
normal, depths, focusing prior, shadow table and the provenance of each value.

## 2. Estimated breaking (face) height — `linear_wave_v2`

Offshore significant wave height is **not** the size of the surf. `nearshore.py`, per
partition i (swells 1–3 and wind sea):

1. **Deep-water equivalent.** If the depth h at the wave-model extraction point is known and
   shallower than half the deep-water wavelength: `H0 = H / Ks`, `Ks = sqrt(Cg0 / Cg(h))`,
   with the wavenumber from Fenton & McKee (1990) refined by Newton iterations on
   `ω² = g·k·tanh(k·h)`. Unknown depth (47 spots) → deep water assumed (up to ~9 % error
   for long swell over a 50–200 m deep grid point; documented assumption). Forecast-point
   depths from NOAA BlueTopo surveys exist for Mavericks (57.9 m), Ocean Beach (36.4 m) and
   Rincon (96.5 m); Trestles' forecast point lies outside any survey (see below).
2. **Directional factor** `R = Σ D(θ)·max(cos(θ − β), 0)·T(θ)·Δθ`: a wrapped-Gaussian
   spreading D (σ = 10° for T ≥ 13 s, 15° for 9–13 s, 30° for wind sea and shorter swell —
   assumptions, since GFS-Wave publishes no spread), the shore normal β and the energy
   transmission T(θ) from the spot's shadow table, or a smooth taper outside its swell
   window (`exp(−2·(d/15°)²)`). The cosine carries refraction: straight-contour refraction
   plus breaking gives `Hb ∝ (H0²·T·cos α0)^(2/5)` (checked within 1 % to 60° in tests).
3. **Breaker height** `Hb_i = 0.39·g^0.2·(T·H0²·R)^0.4·Kf(T)` (Komar & Gaughan 1972), with
   a focusing prior `Kf(T) = Kf_ref·(1 + κ·max(0, T − T*)/T*)`.
4. **Energy-flux combination** `Hb = (Σ Hb_i^2.5)^0.4` (the flux physics behind the
   formula; version 1's root-sum-square and ×0.7 wind-sea factor are gone).
5. **Depth limit** `Hb ≤ γ·d` only for a spot with a documented controlling depth d.

Reported heights: **typical** = Hb (the significant breaking face, trough to crest) and
**sets** = 1.27·Hb (Rayleigh H1/10; an upper bound in the surf zone). The API returns both,
plus the central estimate, the method and its assumptions. A spectral form
(`spectral_breaking_height`, for spots near a GFS-Wave station spectrum) is implemented and
tested but not yet fed by station-spectrum ingestion.

**Physical profiles.** Every spot has one. Unless curated values exist
(`data/surf_spots/physical_profiles.json`, optional), it is derived: β = centre of the
optimal swell window, Kf_ref = `height_factor`, κ = 0, depths unknown, no shadow table, and
each of these is flagged `estimated` or `unknown` in `data_quality`. No bathymetry is
invented.

**Measured values (NOAA BlueTopo, 2026-10-13).** `scripts/bathymetry_profiles.py` samples
the BlueTopo tiles on the transect from each continental-US break to its forecast point and
writes `physical_profiles.json` with `data_quality: "measured"` and a provenance object. Its
rules: the forecast-point depth is the median of the survey cells within 25 m of the point,
used only when at least half of the cells there come from a survey (BlueTopo's "NBS
Generalization" cells are a best-guess surface outside any survey with unknown uncertainty,
not a measurement); β is the downslope azimuth of a plane fitted to the underwater survey
cells within 250 m of the break, used only with at least 200 cells, R² ≥ 0.5, at least half
of them measured depths (not interpolations between soundings), and only when every
direction of the optimal swell window stays within 90° of it (the cos(θ − β) term assumes
straight contours: a β that the documented best swells cannot reach means the contours turn
around a point faster than this model can represent); the controlling depth is the depth at
the break coordinate, used only when it is under water with no cell at or above 0 m NAVD88
within 100 m. Outcome: three forecast-point depths (Mavericks' and Rincon's are
interpolations between the soundings of 1934 and 1977 surveys, Ocean Beach's measured depths
of a 2009 survey), one shore normal (Trestles, 210.9° instead of the estimated 197.5°), no
controlling depth (every break coordinate is a shoreline position). Trestles' forecast point
lies in NBS Generalization cells (758.7 m there, kept under `provenance.not_applied`; review
of 2026-10-13: the first run had written it as "measured"); at that depth every period is
deep water, so leaving it unknown changes nothing. Effect on the real 2026-10-11 blend run
(81 hours each; scores re-computed by the prediction job on a re-seeded clone, identical to
`scripts/profile_sensitivity.py rescore --physical`):

| Spot | Changed | Mean central breaking height (ft) | Mean \|Δ score\| | Labels changed |
|---|---|---|---:|---:|
| Mavericks | forecast-point depth 57.9 m | 9.48 → 9.82 | 1.40 | 1 |
| Ocean Beach | forecast-point depth 36.4 m | 8.14 → 8.60 | 0.21 | 1 |
| Trestles | β 197.5° → 210.9° (forecast-point depth not set) | 5.77 → 5.72 | 0.06 | 0 |
| Rincon | forecast-point depth 96.5 m | 1.08 → 1.09 | 0 | 0 |

Why: de-shoaling a 13-17 s swell from 36-58 m to deep water raises H0 by a few per cent
(Ks < 1 at those depths), so breaking heights rise by about 4-6 % at Mavericks and Ocean Beach;
at 96.5 m (Rincon) only the longest periods are affected; Trestles' measured β turns the
shore normal 13° towards the west, which favours SW swells and disfavours S ones
(cos(θ − β)); on this run the mean moved by −0.05 ft.

**Calibrated model** (only when `surf_spots.calibration` has `a`, `b`, `c`):
`Hb_ft = a·H0^b·T^c·R^d` (d defaults to 0.4).

Version 1 (`komar_gaughan_v1_uncalibrated`) is kept in `breaking.py` for backtest
comparison only.

## 2a. Forecast uncertainty of the breaking height

`uncertainty.py` treats the breaking height as log-normal around Hb:

```
σ_ln(Hb)² = (0.8·σ_ln(Hs))² + (0.4·σ_T/T)² + σ_m²        (Hb ∝ H0^0.8·T^0.4)
σ_ln(Hs)² = σ_f² + σ0²
```

* σ_f, the forecast-dependent part: 1.05 × GEFS-Wave spread/mean when an ensemble row
  exists for the hour (`ensemble_heuristic`); otherwise the lead-time table
  0.080/0.192/0.276/0.352/0.430 at days 1/3/5/7/10 (`heuristic`); never less than the
  spread between independent models in the blend. Both are measured against the model's
  own analysis — a lower bound on real error.
* σ0 = 0.15: assumed model-versus-reality floor.
* σ_T/T: ensemble period spread/mean, else 0.08 (assumed).
* σ_m = 0.30: the uncalibrated offshore-to-surf translation.

The **80 % range** `Hb·exp(∓1.2816·σ)` is shown as "80% range … not yet verified". The label
`calibrated` is reserved for ranges that passed a coverage check on held-out observations;
nothing sets it yet. Probabilities such as P(score ≥ threshold) are deliberately **not**
shown: under an unverified error model they would look more precise than they are.

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
Where sources disagree about which wind is offshore (forecast-confidence.md §3a) and the two
directions give different relations, the wind part reads *"wind 12 km/h from NE, offshore or
cross-shore: sources disagree"*; the score and `wind_relation` keep the profile's direction.

## 3a. Stored components contract (algorithm 2.1.0)

`surf_quality_predictions.components` (JSONB) is read by the spot timeline API ("Why this
forecast?") and by the confidence and text layer. Algorithm 2.0.0 stores the factor fits
(`swell_direction`, `swell_period`, `wave_size`, `wind`, `tide`, `consistency`, each 0–1
and present only when its inputs exist), the gates (`size_gate`, `period_gate`,
`wind_gate`), `run_agreement`, `tide_stage`, `uncertainty`, `breaking` and, when they
apply, `ml_correction` and `wind_dispute`. Algorithm 2.1.0 keeps all of these and adds the
keys below. The scoring change that writes them also raises `ALGORITHM_VERSION` from 2.0.0
to 2.1.0, so live runs are rescored; until then no stored row has these keys.

| Key | Shape | Meaning |
|---|---|---|
| `wind_detail` | object, below | The wind fit and its physical components |
| `weights_used` | `{factor: weight}` | The renormalised weights actually applied (only factors with inputs; they sum to 1) |
| `contributions` | `{factor_or_gate: points_lost}` | Points each factor or gate cost the score; the sum of `points_lost` plus `score` equals 100 within rounding |
| `direction_partitions` | `[{kind, weight, direction_fit, period_fit}]` | Swell direction and period scored per wave partition, with weight `Hb_i^2.5 / sum(Hb_j^2.5)` |
| `tide_detail` | `{stage, rising, range_m, source}` | Present only when the tide is scored |
| `tide_excluded_reason` | string | Present only when the tide is not scored, saying why (e.g. no tide prediction, or the tide preference is not well established) |

`wind_detail`:

| Field | Type | Meaning |
|---|---|---|
| `q` | float 0–1 | The wind fit used in the score |
| `v_offshore_kmh` | float | `V . n`, with `V` the wind velocity (where the air goes) and `n` the seaward unit normal (bearing `offshore_wind_direction + 180`): positive = offshore component, negative = onshore |
| `v_cross_kmh` | float | Size of the cross-shore component, `abs(V x n)` |
| `strength` | `glassy` \| `light` \| `moderate` \| `strong` | Speed class (Beaufort-based, km/h): glassy at most 5.0; light below 12.0; moderate below 29.0; strong from 29.0 |
| `favourability` | `favourable` \| `workable` \| `unfavourable` | From `q`: favourable at 0.75 or more, workable at 0.4 or more, otherwise unfavourable |
| `gust_used` | bool | Whether gusts entered `q` (only with `WIND_GUST_SCORING=scored` and a documented gust definition) |
| `params` | `{name: {value, provenance}}` | Every wind-model parameter used, with `provenance` `research`, `beaufort` or `heuristic` |

Rules for readers and writers:

* The six `wind_relation` strings (glassy, offshore, cross-offshore, cross-shore,
  cross-onshore, onshore; "unknown" without wind data) and its `String(16)` column do not
  change. `strength` and `favourability` are separate fields, not new relations.
* Readers treat every key above as optional and a missing key as None: rows scored by
  2.0.0 do not have them.
* Classes such as `strength`, `favourability` and the confidence classes are documented
  heuristic classes, not probabilities.

## 4. Daylight

Sunrise and sunset come from the NOAA solar position approximation (±2 min), extended by
20 minutes of twilight at each end. Only daylight steps count toward events and matches.

## 5. Confidence, separate from quality

Confidence answers *"how much should I trust this forecast?"*, not *"how good is it?"*. Every
forecast hour carries three things, none of them a probability:

**The 0–100 confidence index** (`confidence`, `confidence_label`; the "index" in the code and
docs, unchanged and still stored on every prediction, event and match):

```
confidence = 100·exp(−σ_f / 0.4)              # σ_f from §2a: ensemble spread, lead table,
                                               # or between-model disagreement
           × (0.6 + 0.4 · completeness)        # share of key variables present
           × (0.8 if the model run is stale)   # stale per source (ADR-015)
```

Index classes: High ≥ 70, Moderate ≥ 45, Low below. Without an ensemble a complete forecast is
High for about two days, Moderate to about day six and Low beyond — matching the published loss
of wave-forecast skill between days 4 and 10. With GEFS-Wave the index varies with the
situation (spread varies about fivefold at a fixed lead).

**Forecast confidence** (`forecast_confidence_class`, methodology `forecast-v1.1`: forecast-v1
plus a disputed-wind note that changes no class): starts from
the index class and applies documented caps and downgrades — lead time (beyond 7 days at most
Moderate, beyond 10 days Low, 3–7 days High only with a small ensemble spread), large GEFS-Wave
spread, disagreement between *independent* model families only (GFS vs ECMWF; GFS-Wave and
GEFS-Wave share NCEP forcing and never confirm each other), a large run-to-run change
(heuristic), poor verification against buoys, missing variables, staleness, demo data and a
single model beyond 3 days. A rule can only lower the class. Each hour stores the class, short
reasons ("An 8-day forecast — forecasts this far out often change.", "GFS and ECMWF disagree on
swell size.") and every input and rule (`confidence_inputs`).

**Local confidence** (`local_confidence_class`): confidence in the *breaking-wave* prediction
at this break = min(forecast confidence, research basis), research basis = min(swell
direction, swell size, max(bathymetry, local behaviour)) from the spot's research assessment;
no assessment gives Low ("How this break responds to swell is not well documented yet.").
When it is lower than forecast confidence, that one research sentence is also returned as
`local_confidence_reason`, so the UI never has to pick it out of `confidence_reasons`.

Rules, thresholds and the evidence behind them: [forecast-confidence.md](forecast-confidence.md).
Research confidence (how well the break is documented) is a separate assessment
([research-confidence.md](research-confidence.md)); the three are never merged into one number.

Run-to-run change is stored as a diagnostic (`components.run_agreement`) and is not an index
factor: it predicted error much more weakly than ensemble spread in the research analysis. It
enters forecast confidence only as a one-class downgrade for a large change (factor 1.4),
labelled heuristic. Nearshore translation is reported separately as uncalibrated.

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
  average quality, confidence (index), dominant swell height/period/direction, wind summary
  and the forecast run that produced it; the newest `history` snapshot also holds the peak
  hour's forecast and local confidence classes, reasons and lead time.

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

Calibration needs observed surf, which does not exist yet for these spots. The design
(research document, section 12): collect nearshore observations (`nearshore_observations`:
face-height band with an explicit convention, time, spot, source), then fit
`ln H_obs = ln a + b·ln H0 + c·ln T + d·ln R` per spot with shrinkage toward the physical
exponents (b = 0.8, c = 0.4, d = 0.4) and store `{"a", "b", "c", "d", "sigma_ln",
"validated": true, "n_observations": …}` in `surf_spots.calibration` only after the intervals
pass a coverage check on held-out observations. Offshore-Hs post-processing with ML is a
separate, buoy-verified step (`docs/ml-pipeline.md`).

The scoring weights live in one place (`WEIGHTS`, `LABEL_THRESHOLDS` in `scoring.py`).
