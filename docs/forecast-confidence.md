# Forecast confidence and local confidence (methodology `forecast-v1`)

Every forecast hour carries two confidence classes, each **High**, **Moderate** or **Low**
with short plain-language reasons:

| Class | Question it answers | Depends on |
|---|---|---|
| **Forecast confidence** (`forecast_confidence_class`) | How trustworthy is the OFFSHORE forecast (swell and wind) at this hour? | Lead time, GEFS-Wave ensemble spread, disagreement between independent model families, run-to-run change, verification against buoys, missing variables, staleness, source reliability |
| **Local confidence** (`local_confidence_class`) | How trustworthy is our BREAKING-wave prediction at this break? | The forecast confidence and how well the break is understood (its research assessment) |

Both are separate from **research confidence** (how well a break's ideal conditions are
documented, [research-confidence.md](research-confidence.md)). The three are stored and shown
separately and are never merged into one number; the only link is the documented `min()`
that defines local confidence (ADR-014 in [decisions.md](decisions.md)).

**These are documented classifications, not calibrated probabilities.** "High" does not mean
"x % likely to verify". The thresholds below come from the evidence cited next to each one,
most of it measured against the model's own analysis or at three buoys in one region, so the
classes have not been validated against observed surf. Every input and rule is archived
(`prediction_archive.confidence_inputs`) so they can be evaluated once observations exist.

Code: `backend/app/services/surf_quality/forecast_confidence.py` (pure rules),
`confidence_context.py` (database inputs, read once per model run), `predict.py` (writes
them on every prediction).

## 1. Starting point: the 0-100 confidence index

The existing index (`surf_quality_predictions.confidence`, `confidence_label`; kept unchanged
and still shown where it was) is

```
index = 100 · exp(−σ_f / 0.4) · (0.6 + 0.4 · completeness) · (0.8 if the run is stale)
```

where σ_f is the forecast-dependent spread of ln(Hs): 1.05 × GEFS-Wave spread / mean when an
ensemble row exists, else the lead-time table 0.080 / 0.192 / 0.276 / 0.352 / 0.430 at days
1 / 3 / 5 / 7 / 10, and never less than half the largest log ratio between independent model
families ([SURF_SCORING.md](SURF_SCORING.md) §2a and §5). Its class (High ≥ 70, Moderate ≥ 45,
Low below) is where forecast confidence starts.

## 2. Forecast confidence: caps and downgrades

```
forecast class = max(Low, min(index class, every cap that fired) − number of downgrades)
```

A rule can therefore only lower the class. The single conditional, "3-7 days: High only when
an ensemble spread exists and is small", is a cap of Moderate when its condition fails; it
never lifts the index class. Agreement between models never raises a class, and agreement
between models that share NCEP forcing (GFS-Wave, GEFS-Wave) does not even count as
agreement.

| Rule (`rules` entry) | Condition | Effect | Reason shown |
|---|---|---|---|
| `demo` | demo provider (synthetic data) | Low | "Demo data, not a real forecast." |
| `missing_swell` | no swell period or no swell direction (and no bulk-sea fallback) | Low | "Swell period or direction missing for this hour." |
| `missing_wind` | wind speed or direction missing | at most Moderate | "Wind data missing for this hour." |
| `lead_over_10d` | lead > 240 h | Low | "An 11-day forecast — too far ahead to be dependable." |
| `lead_7_10d` | 168 h < lead ≤ 240 h | at most Moderate | "An 8-day forecast — forecasts this far out often change." |
| `lead_3_7d_no_ensemble` | 72 h < lead ≤ 168 h, no ensemble | at most Moderate | "A 5-day forecast with no ensemble data to check it." |
| `lead_3_7d_spread_not_small` | 72 h < lead ≤ 168 h, spread / mean > 0.15 | at most Moderate | "A 5-day forecast, and ensemble members do not agree closely." |
| `single_source_over_3d` | lead > 72 h and fewer than two independent model families | at most Moderate | "Only one independent forecast model is available this far out." |
| `spread_large` | GEFS-Wave spread / mean > 0.30 | one class down | "Ensemble members disagree on swell size." |
| `spread_very_large` | spread / mean > 0.40 | Low | "Ensemble members disagree widely on swell size." |
| `hs_disagree` | independent families' Hs differ by more than a factor 1.4 | at most Moderate | "GFS and ECMWF disagree on swell size." |
| `hs_strong_disagree` | … by more than a factor 2 | Low | "GFS and ECMWF disagree strongly on swell size." |
| `period_disagree`, `direction_disagree`, `wind_disagree` | the blend's flags (> 3 s; > 30°, or 45° peak vs mean direction; > 15 km/h) | at most Moderate each | "GFS and ECMWF disagree on swell period." etc. |
| `same_family_mismatch` | two copies of one model differ by > 20 % (integrity check) | at most Moderate | "Two copies of the same model differ here; one data feed may be faulty." |
| `run_change` | offshore Hs at this valid time changed by more than a factor 1.4 since the previous run of the same source (heights floored at 0.5 m) | one class down (heuristic) | "Forecast changed a lot since the previous model run." |
| `verification_poor` | the matching verification row has scatter index > 0.5 or MSE skill vs persistence ≤ 0 | at most Moderate | "Recent checks against buoys show large errors." |
| `stale` | the run is older than its source's stale-after age (§5) | at most Moderate | "Forecast data is out of date; a newer model run is overdue." |

Notes that never change the class: "No ensemble data for this hour." (inside 3 days: a
reason, not a penalty), "Not yet checked against buoys near this spot." (no verification row
for a buoy paired with the spot), "Ensemble members agree closely." (spread / mean ≤ 0.15),
"GFS and ECMWF agree on swell size." (independent families within 15 % and no flags), "A
2-day forecast." (the lead), and why the index itself is below High ("Typical forecast error
at this lead time is moderate.", "Ensemble members differ somewhat on swell size.", "Forecast
models differ on swell size.").

**Lead time** is valid time − issue time of the run the prediction comes from. A blend's issue
time is its *oldest* member's (conservative), so a blend hour 30 h after the newest GFS cycle
can count as a 2-day forecast when the ECMWF member is 12 h older. Reasons therefore say "A
7-day forecast", not "7 days away": they are shown long after scoring.

**Reasons** are ordered by a fixed importance (rules that force Low first, then the other
caps and downgrades, then notes, with positive notes last), are at most 90 characters, and
the API appends the local-confidence reason last.

### Evidence behind each threshold

* **Lead time.** Raw GFS-Wave against spectral Hm0 at three Western Australian buoys
  (209,658 pairs, 472 cycles, [ml-evaluation.md](ml-evaluation.md) §3): RMSE 0.40 m at 24 h,
  0.47 m at 72 h, 0.57 m at 120 h, 0.75 m at 168 h and 1.01 m at 240 h; correlation 0.96 →
  0.63. MSE skill against the long-term monthly climatology of the same buoys: 0.84 at 24 h,
  0.79 at 72 h, 0.69 at 120 h, 0.47 at 168 h and **0.03 [90 % CI −0.09, 0.15] at 240 h, i.e.
  no skill**. Hence: beyond 240 h Low; beyond 168 h (error almost twice the 24 h error, skill
  halved) at most Moderate; between 72 h and 168 h High needs more than the lead time alone,
  namely evidence that this particular forecast is unusually certain.
* **Ensemble spread** is the best available predictor of forecast error. In the 51-cycle
  GEFS-Wave analysis (research notes §11, source [2]; 2026-08-20 to 2026-10-09, against the
  model's own analysis, not peer reviewed) spread/RMSE was 1.36 / 1.04 / 1.03 / 1.00 / 0.94 at
  days 1 / 3 / 5 / 7 / 10 and the ±1.645σ interval covered 90-91 %; the unfiltered log error
  0.080 / 0.192 / 0.276 / 0.352 / 0.430 tracked the relative spread 0.098 / 0.181 / 0.245 /
  0.313 / 0.392. The thresholds are anchored on those typical spreads: **small ≤ 0.15**
  (tighter than a typical 3-day ensemble), **large > 0.30** (as wide as a typical 7-day
  ensemble) and **very large > 0.40** (as wide as a typical 10-day ensemble, where the
  deterministic forecast has no skill against climatology).
* **Run-to-run change** predicts error only weakly: in the research sample its correlation
  with the error was about 0.3 against 0.5-0.6 for the ensemble spread, and it added nothing
  once the spread was known (`confidence.py`). It is therefore only a one-class downgrade at
  a large change (factor 1.4, the same ratio the blend uses for "disagree") and is labelled
  heuristic.
* **Independent families only.** The source dependency analysis (research notes §6) finds
  NOAA GFS-Wave, Open-Meteo's and Windy's GFS-Wave to be one model; GEFS-Wave shares the
  WAVEWATCH III physics and the NCEP forcing core with GFS-Wave ("related, not
  independent"); ECMWF ecWAM is the only verified independent chain. So only pairs that
  `families.are_independent` separates (today ncep_gfs vs ecmwf_ifs) count as disagreement
  or agreement, GEFS-Wave is used only for its spread, and GFS + GEFS counts as one family
  (the single-source cap still applies). A test checks that listing GEFS as a second model
  that agrees perfectly never raises the class.
* **Disagreement thresholds** are the blend's documented flags (heuristic,
  [FORECAST_SOURCES.md](FORECAST_SOURCES.md)): Hs ratio 1.4, Tp 3 s, direction 30° (45°
  between a peak and a mean direction), wind 15 km/h. A 1.41× Hs spread becomes about 1.31×
  in breaking height because Hb ∝ H^0.8 (research notes §5). A factor of 2 is a heuristic
  "strong" level. In the first live comparison (2026-10-11) 33 % of spot-hours had a flag.
* **Verification.** `forecast_accuracy_metrics` (variable `hs`, raw model, at least 200
  pairs, newest window) for the verification sources of each family behind the hour
  (`blend.FAMILY_VERIFICATION_SOURCES`) and the nearest lead bucket (24, 48, 72, 120, 168,
  240 h); a buoy paired with the spot (`spot_physical_profiles.reference_stations`) is
  preferred over the global row. In the WA backtest the scatter index was 0.12-0.37 and the
  skill against persistence 0.48-0.90 at every lead, so **SI > 0.5 or no skill against
  persistence** marks a clearly worse forecast than any measured there. Without a row for a
  paired buoy the reason "Not yet checked against buoys near this spot." is added, without a
  penalty.
* **Missing variables.** Swell direction and period carry 0.20 each of the quality score
  and drive the breaking-height model; without them the prediction has no basis (Low). Wind
  carries 0.30 of the score but the swell forecast itself stands (at most Moderate).
* **Single deterministic source beyond 3 days.** Without a second, independent model nothing
  can reveal a forecast that has gone wrong in the 3-10 day range, where wind-forcing errors
  for storms that have not formed yet dominate (research notes §13).
* **Demo data** is synthetic and is always Low.

## 3. Local confidence

```
research basis   = min(swell_direction, swell_size, max(bathymetry, local_behavior))
local confidence = min(forecast confidence, research basis)
```

using the per-variable classes of the spot's **current** research assessment
(`app.services.research.current_assessments`; a variable that was not assessed counts as
Low). Swell direction and size must both be understood, and either measured bathymetry or a
documented local behaviour can explain how the break transforms the swell. Without any
assessment local confidence is Low with the reason "How this break responds to swell is not
well documented yet." (the same sentence is used for a Low research basis; Moderate: "… is
only partly documented."; High: "… is well documented."). The assessment used is stored in
`surf_quality_predictions.research_assessment_id` (assessments are never updated, so the id
is a stable reference) and its overall class in `prediction_archive.research_class`.

Consequences: a well-researched break never gets more local confidence than the forecast
allows (a 9-day forecast stays Moderate at best), and a break without research evidence
never gets High local confidence, however good the forecast is.

## 4. Where the classes are stored and shown

* `surf_quality_predictions`: `forecast_confidence_class`, `local_confidence_class`,
  `confidence_method` ("forecast-v1"), `confidence_inputs`, `issued_at`, `lead_hours`,
  `research_assessment_id`; the legacy `confidence` (index) and `confidence_label` (index
  class) are unchanged.
* `prediction_archive` (immutable): `forecast_confidence_class`, `local_confidence_class`,
  `research_class` (overall research class used), `confidence_method`, `confidence_inputs`.
* API: `Conditions` and `ForecastPoint` (map snapshot, spot summaries `current` /
  `best_upcoming`, spot detail, spot forecast) have `forecast_confidence_class`,
  `local_confidence_class`, `confidence_reasons` (forecast reasons first, then the research
  reason) and `lead_hours`; `EventOut` has the classes and reasons of the event's peak hour;
  `DailySummary` the classes of the day's best hour. `SpotSummary.research_class` and
  `SpotDetail.research` are research confidence, separate fields.
* Events: detection stores the peak hour's classes, reasons and lead in the event's newest
  `history` snapshot (no migration).
* Alerts: "Forecast confidence: Moderate — about 6 days away" (the class of the event's peak
  hour, as in the API; the distance is from when the alert is built) and "Confidence at this
  break: Low — How this break responds to swell is not well documented yet." Older snapshots
  keep the index wording.
* **Read-time staleness:** when the authoritative run has gone stale since it was scored, the
  API caps both classes at Moderate and puts the staleness reason first.
* A run whose predictions were scored with another methodology is rescored once it is shown
  to users (`predict.outdated_authoritative_runs`); archived rows are never rewritten.

`confidence_inputs` (JSON): `method`, `index`, `index_class`, `sigma_source`, `lead_h`,
`source`, `demo`, `families`, `independent_families`, `ensemble` {`rel_spread`, `members`},
`disagreement` {`hs_log_ratio`, `tp_diff_s`, `dir_diff_deg`, `wind_diff_kmh`, `flags`},
`run_change_ratio`, `verification` [{`scope`, `key`, `source`, `bucket_h`, `n`, `si`,
`ss_persistence`, `near_spot`, `poor`}], `missing`, `stale`, `stale_after_h`, `rules`
(e.g. `"cap:moderate:lead_7_10d"`, `"downgrade:run_change"`, `"note:spread_small"`),
`class`, `reasons`, and `local` {`class`, `reason`, `basis`, `research_class`,
`assessment_id`, `variables`}.

## 5. Per-source staleness

A run is stale once it is older than cycle interval + publication lag + acquisition interval
+ 2 h margin of its source (ADR-015): NOAA GFS 6 + 5 + 3 + 2 = **16 h**, GEFS-Wave 6 + 7.5 + 3
+ 2 = **18.5 h**, ECMWF 12 + 8 + 3 + 2 = **25 h**; demo, Open-Meteo and Windy keep
`FORECAST_STALE_HOURS` (18 h). A blend is stale when any member is stale by its own rule.
With the old single 18 h limit, ECMWF's newest 12-hourly cycle (published about 8 h after
the cycle and fetched by the next 3-hourly acquisition) was stale for several hours every
day and silently dropped out of the blend.

## 6. Live check (2026-10-11, real data)

A clone of the live database (`swell_live_d`, never `swell_live` itself) with the real runs
NOAA GFS `noaa-gfs:2026101100`, ECMWF IFS `ecmwf-ifs:2026101012` and GEFS-Wave
`gefs-wave:2026101018`, migrated to head; research assessed with `research load` (no
evidence file yet: all 50 spots Low); blend, predictions, archive and detection run at a fixed
"now" of 2026-10-11 08:30 UTC, the hour of the day at which ECMWF used to drop out.

* **Staleness.** At 08:30 the ECMWF run was 20.5 h old: old rule (18 h) → blend members
  `[noaa_gfs_direct]` only, so no blend was built and GFS alone was authoritative (**0**
  spot-hours with ECMWF); per-source rule → members GFS + ECMWF and **3,850 of 4,050**
  blended spot-hours (77 valid times × 50 spots) with ECMWF as a member (the remaining 200
  are beyond ECMWF's 240 h). Hour by hour from 2026-10-10 20:00 to 2026-10-11 14:00 the old
  rule kept ECMWF for 11 of 19 hours (lost from 07:00), the new one for 18 (lost from 14:00,
  25 h after the cycle).
* **Forecast classes by lead** (4,050 blend predictions; local confidence was Low for all,
  as every spot's research is Low):

  | Lead (days) | High | Moderate | Low |
  |---|---:|---:|---:|
  | 0-1 | 159 | 86 | 5 |
  | 1-3 | 544 | 239 | 17 |
  | 3-7 | 811 | 715 | 74 |
  | 7-10 | 0 | 920 | 280 |
  | > 10 | 0 | 0 | 200 |

  Median GEFS-Wave relative spread at these hours: 0.025 / 0.034 / 0.084 / 0.153 / 0.229.
* **Most common reasons:** "Not yet checked against buoys near this spot." (4,050; no
  verification metrics in the live data), "Ensemble members agree closely." (2,974), "GFS and
  ECMWF agree on swell size." (1,282), "Ensemble members differ somewhat on swell size." (734),
  "GFS and ECMWF disagree on swell period." (604), "… on swell size." (586), "… on swell
  direction." (564), and the lead-time reasons ("An 8-day forecast — forecasts this far out
  often change.", 400).
* **Examples.** Rincon, valid 2026-10-11 18Z, 30 h: Low (index 29) — "GFS and ECMWF disagree
  strongly on swell size." (Hs log ratio 0.98, a factor 2.7), "… on swell period." (Tp
  4.2 s apart), although the GEFS spread was small (0.10). Uluwatu, 2026-10-15 03Z, 111 h:
  High (index 95) — a 5-day forecast with GEFS spread 0.021 and GFS / ECMWF within 1 %; local
  Low ("How this break responds to swell is not well documented yet."). Pipeline,
  2026-10-18 21Z, 201 h: Moderate (index 72) — "An 8-day forecast — forecasts this far out
  often change.", "GFS and ECMWF disagree on swell period." (6.0 s) and "… on swell
  direction." (51°).
* Not exercised live: verification rows (none exist yet), run-to-run change (no previous run
  of the same source in the clone), missing variables and demo data (covered by tests).

## 7. Limitations and next steps

* All thresholds are heuristics anchored on the cited numbers; none of the classes has been
  validated against observed surf or against held-out buoy data. The archive keeps every
  input so that, once verification pairs accumulate, the classes can be checked for
  reliability (observed error by class and lead) and the thresholds recalibrated.
* The spread evidence is 7 weeks of GEFS-Wave against its own analysis; the lead-time
  evidence is one season (2021) of GFS v16 at three buoys in one region.
* Local confidence uses the research assessment only; the uncalibrated breaking-height
  translation (σ_m = 0.30, [SURF_SCORING.md](SURF_SCORING.md)) is not yet a separate factor.
