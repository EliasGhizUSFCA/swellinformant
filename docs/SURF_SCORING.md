# Surf quality, breaking height, uncertainty, confidence and swell events

Source: `backend/app/services/surf_quality/` and `backend/app/services/swell_detection/`.
Algorithm version: `ALGORITHM_VERSION = "2.1.0"` (stored on every prediction row, so a
changed algorithm never silently mixes with old scores; the live run is rescored after an
upgrade while the prediction archive keeps what was originally issued).

> **Calibration status.** The nearshore model, the uncertainty model, the score weights, the
> wind model and every per-spot parameter are a reasoned, documented first version. None has
> been fitted to observed surf at these 50 spots: no open surf-observation dataset exists for
> them (see `docs/surf-forecast-model-research.md`, sections 8 and 9). Breaking heights are
> labelled estimates, forecast ranges are labelled "not yet verified", and the score is a
> transparent ranking signal, not ground truth. Every wind-model constant is labelled with
> its provenance (`research`, `beaufort` or `heuristic`) in the code, in each stored
> prediction (`components.wind_detail.params`) and in section 3b. The schema supports
> per-spot calibration (`surf_spots.calibration`) and nearshore observations
> (`nearshore_observations`).

## 1. Inputs per forecast timestep

The authoritative forecast per spot is either the multi-model blend or the highest-priority
configured source (see `docs/FORECAST_SOURCES.md`). Per valid time it provides:

| Group | Variables |
|-------|-----------|
| Combined sea | significant wave height (Hs), period (with its definition: `tp`, `tm01`, `tm02`, `tm10`), direction |
| Swell partitions | up to three swell partitions (height / peak period / direction FROM); WAVEWATCH III ranks them by height |
| Wind sea | wind-wave height / period / direction |
| Wind (10 m) | speed (km/h), direction (FROM), gusts with the source's gust definition (`extra.gust_kind`); gusts are shown but not scored unless `WIND_GUST_SCORING=scored` |
| Tide | the stage (0 at the preceding low water, 1 at the next high water) and rising or falling, from the stored tide predictions (`services/tides/stage.tide_stages_for`); model sea level is never used as a tide. Demo runs without tide predictions use their synthetic sea level, labelled source `demo` |

From the spot (`data/surf_spots/spots.json`): swell window, optimal swell direction range,
offshore wind direction, minimum and ideal period, working wave-height range, tide
preference and `height_factor`. From its physical profile (`spot_physical_profiles`): shore
normal, depths, focusing prior, shadow table, an optional researched wind profile (section
3b) and the provenance of each value. From its research assessment: the tide research class
(section 3d).

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
   spreading D (σ = 30° up to 8 s and for wind sea, 15° from 10 to 12 s, 10° from 14 s,
   linear in between — assumptions, since GFS-Wave publishes no spread; 2.0.0 stepped at
   9 s and 13 s, which made breaking heights jump by 4–8 %), the shore normal β and the
   energy transmission T(θ) from the spot's shadow table, or a smooth taper outside its
   swell window (`exp(−2·(d/15°)²)`). The cosine carries refraction: straight-contour
   refraction plus breaking gives `Hb ∝ (H0²·T·cos α0)^(2/5)` (checked within 1 % to 60° in
   tests).
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
invented. β is a *wave-approach* normal for refraction; it is not used for wind, which is
measured against the researched offshore direction (section 3b): at points and wrapping
reefs the two differ by more than 60° (19 of 50 spots).

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
(81 steps each; scores re-computed by the prediction job on a re-seeded clone, identical to
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
| Swell direction | 0.20 | Per wave partition: 1 inside the optimal range, linear to 0 at 45° from it; outside the swell window the value at the nearest window edge times `exp(−2·(d/15°)²)` (d = degrees outside; the nearshore model's taper). The partitions are combined with weights `w_i = Hb_i^2.5 / Σ Hb_j^2.5`, their shares of the breaking energy flux (section 3e) |
| Swell period | 0.20 | Per partition: 0 below `min − 3 s`; ramps to 0.4 at `min`, then to 1.0 at the ideal period; combined with the same weights |
| Wave size | 0.20 | Breaking-height mid vs. the spot's working range: 0 at 40 % of min; 0.75 at min; 1.0 at the sweet spot (65 % of the range); 0.85 at max; falls to 0 at 1.5 × max |
| Wind | 0.30 | `Q_wind` from the offshore and cross-shore components of the wind against the spot's seaward normal (section 3b) |
| Tide | 0.05 | `1 − 1.6·|stage − target|`, floor 0.2 (targets: low 0.1, low_to_mid 0.3, mid 0.5, mid_to_high 0.7, high 0.9; "all" = 1), only when the tide preference is established (section 3d) |
| Consistency | 0.05 | Share of the breaking energy flux (`Σ Hb_i^2.5`) carried by the three swell partitions rather than the wind sea; without a breaking breakdown, the swells' share of the offshore energy (H²) |

The tide mapping (slope 1.6, floor 0.2) and the weights are heuristic. Without a partition
breakdown (bulk sea state only, or the calibrated breaking model) direction and period come
from the primary swell, else the bulk sea, as in 2.0.0.

**Limiting-factor gates.** A weighted mean would let glassy wind make up for flat or
weak surf, so the score is multiplied by:

* size gate `clamp(size / 0.6)`
* period gate `0.5 + 0.5·clamp(period / 0.5)`
* wind gate `0.5 + 0.5·clamp(wind / 0.75)`

`score = round(100 × base × size_gate × period_gate × wind_gate)`

The wind gate starts where the wind stops being favourable (fit 0.75); in 2.0.0 it started
at 0.5, when the wind fit was 0.5 for any cross-shore wind from 20 to 35 km/h. With the
continuous wind fit of 2.1.0 a 0.5 start left an ideal swell "Good" in a 35 km/h cross-shore
wind (59) and "Very Good" in a 45 km/h offshore wind (77); with 0.75 they score 52 and 65.
The 0.75 is heuristic.

**Labels** (lower bound inclusive):

| Score | Label |
|------:|-------|
| 95 | Exceptional |
| 85 | Excellent |
| 72 | Very Good |
| 57 | Good |
| 40 | Fair |
| 0 | Poor |

Every prediction stores the component values, the weights actually used, the points each
factor and gate cost, the wind breakdown (section 3a), the wind relation (glassy / offshore /
cross-offshore / cross-shore / cross-onshore / onshore) and a plain-English explanation, e.g.
*"0.9 m @ 15 s WNW secondary swell, well aligned with the break; light offshore wind 9 km/h
from ENE; approx. 7.5–9.5 ft faces vs. a working range of 3–15 ft."* The swell part names
the partition with the largest share of the breaking energy flux ("swell" for the primary,
otherwise "secondary swell", "tertiary swell" or "wind sea"); the wind part starts with the
strength class (section 3b). Where sources disagree about which wind is offshore
(forecast-confidence.md §3a) and the two directions give different relations, the wind part
reads *"moderate wind 12 km/h from NE, offshore or cross-shore: sources disagree"*; the score
and `wind_relation` keep the profile's direction. When a tide stage exists but the tide is
not scored, the explanation ends with *"tide not scored (tide preference not well
established: research confidence is low)"* or similar.

## 3a. Stored components contract (algorithm 2.1.0)

`surf_quality_predictions.components` (JSONB) is read by the spot timeline API ("Why this
forecast?") and by the confidence and text layer. Algorithm 2.0.0 stores the factor fits
(`swell_direction`, `swell_period`, `wave_size`, `wind`, `tide`, `consistency`, each 0–1
and present only when its inputs exist), the gates (`size_gate`, `period_gate`,
`wind_gate`), `run_agreement`, `tide_stage`, `uncertainty`, `breaking` and, when they
apply, `ml_correction` and `wind_dispute`. Algorithm 2.1.0 keeps all of these and adds the
keys below (`scoring.evaluate` returns them as `QualityResult.details`; the prediction job
writes `QualityResult.stored_components()`).

| Key | Shape | Meaning |
|---|---|---|
| `wind_detail` | object, below | The wind fit and its physical components (absent without a wind speed) |
| `weights_used` | `{factor: weight}` | The renormalised weights actually applied (only factors with inputs; they sum to 1) |
| `contributions` | `{factor_or_gate: points_lost}` | Points each factor (`swell_direction`, …) or gate (`size_gate`, `period_gate`, `wind_gate`) cost the score; the sum of `points_lost` plus `score` equals 100 within rounding |
| `direction_partitions` | `[{kind, weight, direction_fit, period_fit}]` | Swell direction and period scored per wave partition (`primary_swell`, `secondary_swell`, `tertiary_swell`, `wind_sea` or `combined`), with weight `Hb_i^2.5 / sum(Hb_j^2.5)`; absent when no partition breaks |
| `tide_detail` | `{stage, rising, range_m, source}` | Present only when the tide is scored |
| `tide_excluded_reason` | string | Present only when the tide is not scored, saying why: "No tide prediction for this time." or "Tide preference not well established: …" (research confidence is low, not assessed for this break, sources disagree, or no preference recorded) |

Contributions: factor k with weight `w_k` and fit `c_k` costs `100·w_k·(1 − c_k)` of the
base; the gates then remove `100·base·(1 − G)` with G the product of the gates, shared
between them in proportion to `−ln(g)` (a gate of 0 takes the whole loss). The points plus
the unrounded score are exactly 100.

`wind_detail`:

| Field | Type | Meaning |
|---|---|---|
| `q` | float 0–1 | The wind fit used in the score |
| `v_offshore_kmh` | float | `V . n`, with `V` the forecast mean wind velocity (where the air goes) and `n` the seaward unit normal (bearing `offshore_wind_direction + 180`): positive = offshore component, negative = onshore. Shelter and scored gusts change only the speed the fit uses, not these components (section 3b); None for an unknown direction |
| `v_cross_kmh` | float | Size of the cross-shore component of the same wind, `abs(V x n)` |
| `strength` | `glassy` \| `light` \| `moderate` \| `strong` | Speed class (Beaufort-based, km/h): glassy at most 5.0; light below 12.0; moderate below 29.0; strong from 29.0 |
| `favourability` | `favourable` \| `workable` \| `unfavourable` | From `q`: favourable at 0.75 or more, workable at 0.4 or more, otherwise unfavourable |
| `gust_used` | bool | Whether gusts entered `q` (only with `WIND_GUST_SCORING=scored` and a documented gust definition) |
| `params` | `{name: {value, provenance[, sources]}}` | Every wind-model parameter used, with `provenance` `research`, `beaufort` or `heuristic`; research-based spot values also list their evidence keys (`sources`) |

Rules for readers and writers:

* The six `wind_relation` strings (glassy, offshore, cross-offshore, cross-shore,
  cross-onshore, onshore; "unknown" without wind data) and its `String(16)` column do not
  change. `strength` and `favourability` are separate fields, not new relations.
* Readers treat every key above as optional and a missing key as None: rows scored by
  2.0.0 do not have them.
* Classes such as `strength`, `favourability` and the confidence classes are documented
  heuristic classes, not probabilities.

## 3b. Offshore wind (`wind.py`)

**Definition.** Offshore wind blows from the land toward the ocean; onshore wind from the
ocean toward the land. All wind directions are nautical degrees the wind blows FROM
(0 = north, clockwise). Each spot's `offshore_wind_direction` (spots.json, researched; at
points and wrapping reefs it is the wind that blows straight onto the wave face, see
`data/surf_spots/README.md`) is the FROM bearing of a straight-offshore wind.

**Vectors.** A wind of speed V from bearing θ has the velocity (where the air goes)
`u = −V·sin θ` (east) and `v = −V·cos θ` (north), the inverse of `geo.wind_from_uv`, which
every provider uses. The **seaward unit normal** points from the land to the ocean, at
bearing `φ = offshore_wind_direction + 180°`: `n = (sin φ, cos φ)`. Then

* `V_off = V · n = u·sin φ + v·cos φ = V·cos(θ − offshore)`: positive offshore, negative
  onshore, zero for a cross-shore wind;
* `V_cross = |V × n| = |u·cos φ − v·sin φ|`, the cross-shore speed;
* `ψ = atan2(V_cross, V_off)` in [0°, 180°], the angle from straight offshore
  (`cos ψ = V_off / V`).

West-facing beach (offshore from the east, 90°), 20 km/h:

| Wind from | V · n (km/h) | Relation |
|---|---:|---|
| E (90°) | +20.0 | offshore |
| NE (45°) or SE (135°) | +14.1 | offshore (ψ = 45°) |
| N (0°) or S (180°) | 0.0 | cross-shore |
| NW (315°) or SW (225°) | −14.1 | cross-onshore (ψ = 135°) |
| W (270°) | −20.0 | onshore |

**Two normals.** The nearshore model's β (section 2) is a wave-approach normal, usually the
centre of the optimal swell window. The wind normal is built only from
`offshore_wind_direction`, never from β. Examples (β + 180° is the offshore wind β would
imply): Chicama 30° vs the researched 150°; Jeffreys Bay 35° vs 290°; Snapper Rocks 297.5°
vs 200°; Kirra 305° vs 225° (east wind is cross-onshore at Kirra, not offshore); Uluwatu 40°
vs 120°; Teahupoo 27.5° vs 30°. Tests check every spot: `V · n = +V` at its offshore
direction and `−V` at the opposite one.

**The wind fit.**

```
V_on     = max(0, −V_off)
V_e      = hypot(V_on, cross_weight · V_cross)              onshore-equivalent chop speed
Q_chop   = 1 / (1 + (V_e / v_half)^p)
Q_strong = 1 / (1 + (max(0, V_off − tolerance) / soft)^2)
Q_wind   = Q_chop · Q_strong
```

Calm (speed 0, no direction) and straight offshore wind up to the tolerance (28 km/h) give
1; an offshore wind at an angle loses a little to its cross-shore component (0.85 at 20 km/h
and ψ = 45°). `Q_wind` is continuous in speed and direction, non-increasing in speed at
every direction, and has no floor (2.0.0 kept 1.0 for offshore wind up to 35 km/h and never
went below 0.4, and kept 0.5 for cross-shore wind from 20 to 35 km/h). Its steepest slope is
that of the chop curve, `x^(p−1)·(p+1)²/(4·p·v_half)` per km/h with `x^p = (p−1)/(p+1)`:
0.067 per km/h at the defaults (onshore near 10 km/h). A wind with a speed but no direction
is scored as pure cross-shore. With extra offshore directions (section 3b, spot profiles)
the best of them counts. Shelter and scored gusts change only the speed the fit uses
(`(1 − s)·V`, `V + gust_weight·(gust − V)`); `wind_detail` still reports the components of
the forecast mean wind.

Values at the defaults, west-facing beach (offshore from 90°; ψ in brackets), computed by
`wind.q_wind`:

| Wind FROM (ψ) | Relation | V·n, V×n at 10 km/h | 0 | 5 | 10 | 15 | 20 | 25 | 30 | 35 | 40 | 45 | 50 | 60 | 80 | 100 km/h |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| E 90 (0) | offshore | +10.0, 0.0 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.98 | 0.82 | 0.61 | 0.44 | 0.32 | 0.18 | 0.08 | 0.04 |
| ENE 67.5 (22.5) | offshore | +9.2, 3.8 | 1.00 | 1.00 | 1.00 | 0.99 | 0.97 | 0.95 | 0.91 | 0.80 | 0.60 | 0.41 | 0.28 | 0.13 | 0.03 | 0.01 |
| NE 45 (45) | offshore | +7.1, 7.1 | 1.00 | 1.00 | 0.98 | 0.93 | 0.85 | 0.74 | 0.62 | 0.51 | 0.41 | 0.31 | 0.21 | 0.09 | 0.02 | 0.00 |
| NNE 22.5 (67.5) | cross-offshore | +3.8, 9.2 | 1.00 | 0.99 | 0.95 | 0.85 | 0.71 | 0.56 | 0.42 | 0.32 | 0.24 | 0.18 | 0.14 | 0.08 | 0.04 | 0.01 |
| N 0 (90) | cross-shore | +0.0, 10.0 | 1.00 | 0.99 | 0.94 | 0.82 | 0.66 | 0.50 | 0.37 | 0.27 | 0.20 | 0.15 | 0.11 | 0.07 | 0.03 | 0.02 |
| NNW 337.5 (112.5) | cross-onshore | −3.8, 9.2 | 1.00 | 0.99 | 0.90 | 0.73 | 0.53 | 0.37 | 0.25 | 0.17 | 0.12 | 0.09 | 0.07 | 0.04 | 0.02 | 0.01 |
| NW 315 (135) | cross-onshore | −7.1, 7.1 | 1.00 | 0.97 | 0.80 | 0.54 | 0.33 | 0.20 | 0.13 | 0.08 | 0.06 | 0.04 | 0.03 | 0.02 | 0.01 | 0.00 |
| WNW 292.5 (157.5) | onshore | −9.2, 3.8 | 1.00 | 0.95 | 0.70 | 0.41 | 0.23 | 0.13 | 0.08 | 0.05 | 0.04 | 0.02 | 0.02 | 0.01 | 0.00 | 0.00 |
| W 270 (180) | onshore | −10.0, 0.0 | 1.00 | 0.94 | 0.66 | 0.37 | 0.20 | 0.11 | 0.07 | 0.04 | 0.03 | 0.02 | 0.02 | 0.01 | 0.00 | 0.00 |

Score of an ideal swell (2.0 m, 16 s, from 215° at a Jeffreys Bay-like spot: offshore from
290°, window 180–250°, optimal 200–230°, 4–12 ft, minimum and ideal period 11 and 15 s,
height factor 0.9: `JBAY` in `tests/unit/test_surf_quality.py`), computed by
`scoring.evaluate`:

| ψ (relation) | 0 | 5 | 10 | 15 | 20 | 25 | 30 | 35 | 40 | 45 | 50 | 60 | 80 km/h |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 (offshore) | 100 | 100 | 100 | 100 | 100 | 100 | 99 | 94 | 79 | 65 | 56 | 46 | 39 |
| 60 (cross-offshore) | 100 | 99 | 98 | 96 | 92 | 79 | 67 | 59 | 52 | 48 | 44 | 40 | 36 |
| 90 (cross-shore) | 100 | 99 | 98 | 94 | 84 | 70 | 59 | 52 | 47 | 43 | 41 | 38 | 36 |
| 120 (cross-onshore) | 100 | 99 | 96 | 84 | 66 | 54 | 47 | 43 | 40 | 38 | 37 | 36 | 35 |
| 180 (onshore) | 100 | 98 | 84 | 59 | 47 | 41 | 38 | 37 | 36 | 35 | 35 | 35 | 34 |

**Parameters and their provenance.** `research` = from the spot's researched profile or
evidence; `beaufort` = the Beaufort wind scale; `heuristic` = a reasoned default, not fitted
(no observations exist to fit it). The defaults below are checked against
`wind.WindParams` by `tests/unit/test_surf_scoring_doc.py`.

| Parameter | Default | Provenance | Meaning |
|---|---:|---|---|
| `offshore_wind_direction` | per spot | research | FROM bearing of straight offshore (spots.json) |
| `v_half_kmh` | 12.5 | heuristic | Onshore-equivalent speed that halves `Q_chop` (about 6.7 kt) |
| `p` | 3.0 | heuristic | Steepness of the chop curve |
| `cross_weight` | 0.5 | heuristic | Share of the cross-shore component counted as chop |
| `offshore_tolerance_kmh` | 28.0 | heuristic | Offshore speed that does not hurt (about 15 kt); replaced by a spot profile's value where one exists, stored with that value's own provenance (`tolerance_provenance`) |
| `soft_kmh` | 15.0 | heuristic | How quickly offshore wind beyond the tolerance hurts |
| `gust_weight` | 0.25 | heuristic | Share of the gust excess (gust − mean) added to the speed when gusts are scored |
| `glassy_max_kmh` | 5.0 | beaufort | "glassy" relation and strength (forces 0–1) |
| strength bounds | 5.0 / 12.0 / 29.0 | beaufort | glassy / light / moderate / strong (below) |
| favourability bounds | 0.75 / 0.4 | heuristic | favourable / workable / unfavourable |
| relation sector bounds | 45 / 75 / 105 / 135° | heuristic | below |
| wind gate start | 0.75 | heuristic | section 3 |
| shelter factor | 0.3 | heuristic | strength of a qualitative shelter claim (below) |
| shelter taper | 15° | heuristic | fade-out outside a sheltered sector |

The practitioner guidance behind the heuristic defaults (low-tier sources, read during the
audit): strong offshore from about 15–20 kt makes paddling into waves hard, and onshore or
cross-shore wind above about 10 kt chops the surface quickly. Their numbers are not fitted.

**Relation sectors** (by ψ, upper bounds inclusive, symmetric about cross-shore): offshore up
to 45°, cross-offshore up to 75°, cross-shore up to 105°, cross-onshore up to 135°, onshore
beyond; "glassy" at 5.0 km/h or less whatever the direction (and for a calm wind, whose
direction is None); "unknown" without a speed, or with a speed above 5 km/h and no
direction. 2.0.0 used 45 / 80 / 110 / 135°: its cross-shore sector reached 20° onshore of
perpendicular but only 10° offshore of it (422 steps of the 2026-10-11 run lay 0–20° onshore
of perpendicular); 2.1.0's cross-shore sector is 15° either side.

**Strength** (`wind_detail.strength`, `wind.wind_strength`), the km/h form of the Beaufort
scale: glassy up to 5.0 km/h (forces 0–1, "calm (glassy)" and "calm (rippled)" seas), light
below 12.0 (force 2), moderate below 29.0 (forces 3–4), strong from 29.0 (force 5 and up).
The Met Office table (https://www.metoffice.gov.uk/weather/guides/coast-and-sea/beaufort-scale,
read 2026-10-14) gives the same forces in knots: force 2 4–6 kt, forces 3–4 7–16 kt, force 5
17–21 kt; converted from knots the force-5 boundary is 31.5 km/h, 2.5 km/h above the km/h
form used here.

**Favourability** (`wind_detail.favourability`): favourable when `q ≥ 0.75`, workable when
`q ≥ 0.4`, otherwise unfavourable. A 5.8 km/h onshore breeze is "light", "onshore" and
"favourable" (q 0.91); a 45 km/h offshore wind is "strong", "offshore" and "workable".

**Spot wind profiles** (`spot_physical_profiles.wind_profile`, from
`data/surf_spots/physical_profiles.json`). Optional per-spot knowledge, filled only from
claims read directly from a source (evidence `retrieval` "fetched"), with the evidence key of
every value; the seed rejects malformed profiles and research values whose evidence key does
not exist or was not read directly. `python -m app.cli research lint` reports the same
problems for the stored profiles (`decisions.wind_profile_issues`, given the stored wind
profiles and each evidence key's retrieval). Fields: `offshore_tolerance_kmh` (with
`sources`, and `tolerance_provenance`: `research` when the source states the number,
`beaufort` when a word such as "strong offshore" is mapped to the Beaufort scale, `heuristic`
otherwise; without it, `research` for a research profile), `shelter` sectors
`{from_min, from_max, factor, sources}` (the fit scales winds FROM the clockwise sector by
`1 − factor`; outside it the shelter fades as
`factor·exp(−2·(d/15°)²)`), `extra_offshore_deg` (further straight-offshore bearings of
breaks with several wave faces), `data_quality` (`research` or `heuristic default`) and
`notes`. A factor f scales the wind from the sector by 1 − f (0.5 would halve it, so the
same fit would need twice the speed). The committed profiles:

| Spot | Claim read (evidence key) | Encoded |
|---|---|---|
| Burleigh Heads | "some shelter here from south winds" (surf-forecast-burleigh) | shelter S (157.5–202.5°), 0.3 |
| G-Land | "some shelter here from southeast winds" (surfforecast-g-land) | shelter SE (112.5–157.5°), 0.3 |
| Mundaka | "some shelter here from west winds" (surf-forecast-mundaka) | shelter W (247.5–292.5°), 0.3 |
| Ocean Beach SF | "some shelter here from northwest winds" (surf-forecast-ocean-beach) | shelter NW (292.5–337.5°), 0.3 |
| Punta de Lobos | "sheltered from the dreaded, and almost daily, SW wind by the cliffs" (wavelength-wsr-punta-de-lobos) | shelter SW (202.5–247.5°), 0.3 |
| Padang Padang | "it can cope with a strong offshore very well" (isg-padang) | offshore tolerance 39 km/h (lower bound of force 6, "strong breeze", in the km/h form; provenance `beaufort`: the claim is research, the number is not) |

The sector of a compass word is that point ±22.5° (an 8-point compass sector) and every
qualitative shelter gets the same heuristic factor 0.3: the direction is research, the
strength is not. The factor is deliberately conservative: none of the claims says how much
shelter there is. With 0.5, one Ocean Beach step of the 2026-10-11 run with a 19 km/h
northwest wind (ψ = 135°, the onshore edge of cross-onshore) went from Fair (41) to Very
Good (80) when only the wind part changed; with 0.3 it stays Fair (44 under all of 2.1.0).
Not encoded: Macaronis ("performs well at any size, any tide, and any wind", one community
page that also names a best wind; no direction or strength to encode, left for owner
review), and Rifles and Skeleton Bay ("no shelter here from cross shore breezes"), which is
the default. The other 44 spots use the defaults. No spot has extra
offshore directions yet.

**Gusts.** Stored and shown, not scored: `WIND_GUST_SCORING=off` (default). With `scored`,
the fit uses `V + gust_weight·max(0, gust − V)`, and only for gusts whose source documents
the definition (`extra.gust_kind` `gfs_instantaneous`, `ecmwf_max_3h` or
`ecmwf_max_since_pp`; never `unknown`). The definitions differ (on the 2026-10-11 runs the
median gust/speed ratio was 1.15 for GFS and 1.47 for ECMWF), so scoring them would make the
score depend on the model behind the blend; that is why the default is off.

**What is not calibrated.** Every heuristic parameter above; the sector bounds; the shelter
strength; the wind gate. The wind comes from the model at the forecast point (up to 40 km
from the break, on a 0.25° grid, 1- to 6-hourly): it misses dawn land breezes, sea breezes,
cliff shelter and channelling that the spot profiles do not record. Disputed offshore
directions (11 spots, forecast-confidence.md §3a) cost more points under the sharper curve
when the profile's direction is wrong. No local observations exist to fit any of it.

## 3c. Wind requirements in matching (`matching/evaluate.py`)

Saved searches keep working on the direction classes: "offshore" accepts glassy, offshore
and cross-offshore steps (`GOOD_WIND`); "not onshore" rejects onshore and cross-onshore
(`BAD_WIND`). Strong offshore is penalised through the score (a search's minimum quality),
not by redefining "offshore" (owner decision, 2026-10-14). Two optional rules exist behind
constants, both off: `EXCLUDE_STRONG_OFFSHORE` (a "strong" offshore or cross-offshore hour,
29 km/h or more, fails "offshore") and `LIGHT_ONSHORE_IS_NOT_ONSHORE` (an onshore or
cross-onshore hour below 12 km/h passes "not onshore"). A 60 km/h offshore hour on an ideal
swell is still "offshore" but scores 46 (Fair), so a "Very Good" search does not match it; a
6 km/h onshore hour fails "not onshore".

## 3d. Tide (`scoring.tide_excluded_reason`)

The tide stage comes from tide predictions only (section 1): 0 at the preceding low water, 1
at the next high water, with rising or falling and the range. The tide is scored only when
all three hold:

* the spot's tide research class (its current research assessment) is moderate or high;
* its tide decision (`profile_decisions.json`) is not "disputed";
* it has a tide preference.

Otherwise the component is left out (weights renormalise) and `tide_excluded_reason` says
why; this is the same rule as the tide block's "Tide preference not well established." Steps
without a tide prediction are scored exactly as before, with "No tide prediction for this
time." Until tide predictions are stored (the tides package), no real step scores its tide.
On the committed research and decisions 18 of 50 spots pass (Dungeons, Mundaka, Teahupoo
and Thurso East at high, 14 at moderate; 8 of the 18 prefer "all" tides, which always fit
1); 21 are excluded because sources disagree, 6 for a low research class and 5 without a
preference.

## 3e. Multiple swell systems and the effect on the real run

**Partition weighting.** WAVEWATCH III ranks swell partitions by offshore height, so the
"primary" swell can come from a blocked direction while a smaller, well-aligned groundswell
makes the surf. The breaking model already combines every partition by energy flux
(`Hb = (Σ Hb_i^2.5)^0.4`); 2.1.0 scores direction and period the same way, with
`w_i = Hb_i^2.5 / Σ Hb_j^2.5` over the partitions that break (the wind sea included, so surf
made by wind sea scores its short period). A single partition gives exactly the 2.0.0
direction and period fits; a tiny blocked partition changes the direction fit by less than
0.02.

**Effect on the real blend run of 2026-10-11** (blend of NOAA GFS 18Z and ECMWF 12Z, 4,050
3-hourly steps, 50 spots), rescored by the prediction job with 2.0.0 and then 2.1.0 on a copy of the
database whose spot profiles were re-seeded from `data/surf_spots` (2026-10-14):

| | 2.0.0 | 2.1.0 |
|---|---:|---:|
| Labels: poor / fair / good / very good / excellent / exceptional | 1,342 / 485 / 570 / 681 / 801 / 171 | 1,238 / 618 / 531 / 674 / 880 / 109 |
| Offshore steps at 45 km/h or more, Very Good or better | 0 of 3 | 0 of 3 |
| Offshore steps at 30 km/h or more, Very Good or better | 37 | 19 |
| Cross-shore steps at 30 km/h or more, Good or better | 37 of 127 | 0 of 151 |

1,313 labels change (32.4 %; 696 of them in daylight; 623 up, 690 down); the mean absolute
score change is 7.7 points (mean +0.25, 25 % of steps by 10 points or more). 205 steps change
relation, only through the new sector bounds (124 cross-offshore to cross-shore, 81
cross-shore to cross-onshore). Switching one part on at a time (the others at 2.0.0;
the parts overlap, so the counts do not add up): partition weighting 966 labels (mean
|Δ| 5.6), wind fit with its gate, sectors and spot profiles 687 (3.4), consistency 130
(0.5), window taper 26 (0.1), continuous spreading 7 (0.03). The six spot wind profiles
alone change 96 steps at five spots (mean +4.2, at most +19) and 29 labels (Ocean Beach 11,
Punta de Lobos 10, Mundaka 4, G-Land 4); Padang Padang's tolerance changes nothing on this
run. The wind part's largest changes are strong cross-shore steps that 2.0.0 rated Very Good,
now Fair: Sunset Beach 2026-10-19 06Z (38 km/h from 75°, offshore 135°: 80 → 47), Bells Beach
2026-10-13 00Z (35 km/h from 231°, offshore 300°: 82 → 49), Raglan 2026-10-16 03Z (41 km/h
from 197°, offshore 140°: 77 → 44), Teahupoo 2026-10-12 12Z (36 km/h from 118°, offshore 30°:
80 → 48). The largest changes are partition effects: steps
whose primary swell is a short local sea or a blocked remnant while a well-aligned secondary
or tertiary groundswell makes the surf rise by up to 64 points (La Graviere 2026-10-20 09Z:
primary 0.9 m 6 s from 357°, secondary 0.9 m 15 s WNW with 85 % of the breaking flux, 28 →
92), and steps whose tiny long-period primary swell hid a dominant wind sea fall by up to 63
(Anchor Point 2026-10-11 18Z: primary 0.09 m 14 s, wind sea 1.5 m 5 s with 98 % of the flux,
36 km/h cross-offshore, 77 → 14). These changes reach events, matches and alerts at the
next rescoring.

**Limitation: wind sea raised by an offshore wind.** The nearshore model decides how much of
a partition reaches the break from the wave-approach normal β (section 2), not from the wind
normal. Where β + 180° is far from the offshore direction (points and wrapping reefs), wind
sea coming from close to the offshore direction, which travels away from the coast, can keep
a large share of the breaking flux. On the 2026-10-11 run, wind sea from within 45° of the
offshore direction carried the largest share in 44 of 4,050 steps at six spots (Cloudbreak
19, Restaurants 17, Snapper Rocks 5, Burleigh Heads, Kirra and Safi 1 each). Example:
Cloudbreak 2026-10-20 03Z, 36 km/h from 133° (offshore 135°), a 2.6 m 9.6 s wind sea from
156° with 94 % of the breaking flux, 92 → 31. There the wind sea sets the breaking height (as
in 2.0.0) and now also the direction and period fits, which usually lowers the score. 2.1.0 does not correct this;
it needs a decision on how wind sea is transmitted at such breaks.

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

* Steps are the run's stored valid times at the source's native spacing (hourly, 3-hourly
  or 6-hourly, depending on the source and the lead time); nothing is interpolated. Event
  and match hour counts currently count every step as one hour (`clustering.Step.duration`
  defaults to one hour; the detection service does not yet pass each step's length), so
  they undercount 3-hourly runs. **Only daylight steps** are considered. Nights neither
  qualify nor break an event, so a swell that is good on consecutive mornings is *one*
  event.
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

The scoring weights live in one place (`WEIGHTS`, `LABEL_THRESHOLDS`, `WIND_GATE_FULL_Q` in
`scoring.py`), the wind model's defaults in `WindParams` (`wind.py`).
