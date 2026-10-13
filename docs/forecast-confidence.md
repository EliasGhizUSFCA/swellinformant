# Forecast confidence and local confidence (methodology `forecast-v1.1`)

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
most of it measured against the model's own analysis or at buoys. The OFFSHORE classes of
single-source GFS-Wave hours, with and without a GEFS-Wave spread, were checked against
held-out NDBC buoy observations in 2026 (§8): their realised offshore errors are ordered
High < Moderate < Low. Neither class has been validated against observed surf, and the
rules for several independent model families have not been validated at all. Every input
and rule is archived (`prediction_archive.confidence_inputs`) so they can be evaluated
further as observations accumulate.

**forecast-v1.1** (2026-10-12) differs from forecast-v1 in one rule,
`single_source_no_ensemble`: without an ensemble row and without a second independent model
family an hour is at most Moderate at every lead (before: only beyond 3 days). Against the
buoys, forecast-v1's High and Moderate single-source hours had the same median error on the
training year and no distinguishable RMSE (§8).

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
| `single_source_no_ensemble` (v1.1) | lead ≤ 72 h, fewer than two independent model families and no ensemble row (or a flat-sea ensemble, mean ≤ 0.05 m) | at most Moderate | "Only one forecast model and no ensemble data to check it." |
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

Notes that never change the class: "No ensemble data for this hour." (inside 3 days with
two independent families: a reason, not a penalty), "Not yet checked against buoys near this spot." (no verification row
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
* **Run-to-run change** predicts error only weakly. The research document records run-to-run
  agreement as "a diagnostic only" (research notes §2), and the earlier analysis noted in the
  `confidence.py` docstring found a correlation with the error of about 0.3 against 0.5-0.6
  for the ensemble spread, adding nothing once the spread was known. Those two correlations
  are not reproduced in the research document or in [ml-evaluation.md](ml-evaluation.md), so
  treat them as unverified. Run change is therefore only a one-class downgrade at a large
  change (factor 1.4, the same ratio the blend uses for "disagree") and is labelled
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
  preferred over the global row. In the WA backtest the scatter index was 0.11-0.37 and the
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
* **Single deterministic source without an ensemble inside 3 days** (v1.1). At five NDBC
  buoys (§8) such GFS-Wave hours with a High index (leads up to 52 h) and with a Moderate
  index (52-150 h) had the same median offshore error (0.23 m) and an RMSE difference of
  0.017 m [90 % CI −0.010, 0.043] on the training year, and no High / Moderate lead
  boundary inside 3 days separated them. With nothing that indicates how certain the hour
  is, the lead time alone does not support High.
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
  `confidence_method` ("forecast-v1.1"; "forecast-v1" on rows scored before 2026-10-12),
  `confidence_inputs`, `issued_at`, `lead_hours`,
  `research_assessment_id`; the legacy `confidence` (index) and `confidence_label` (index
  class) are unchanged.
* `prediction_archive` (immutable): `forecast_confidence_class`, `local_confidence_class`,
  `research_class` (overall research class used), `confidence_method`, `confidence_inputs`.
  Rows archived before forecast-v1 existed keep these columns empty: the archive is never
  rewritten, so a run that was archived under the old code and is rescored afterwards keeps
  its original rows (in the 2026-10-11 live clone: 3,850 such rows).
* API: `Conditions` and `ForecastPoint` (map snapshot, spot summaries `current` /
  `best_upcoming`, spot detail, spot forecast) have `forecast_confidence_class`,
  `local_confidence_class`, `confidence_reasons` (forecast reasons first, then the research
  reason), `lead_hours` and `local_confidence_reason`; `EventOut` has the classes, reasons and
  local reason of the event's peak hour (stored at detection, with the read-time staleness cap
  below); `DailySummary` the classes of the day's best hour.
  `local_confidence_reason` is the single reason the local class is lower than the forecast
  class (always the research sentence, since local confidence is the min of the two), and
  `null` when the classes are equal (including after the read-time staleness cap below).
  `SpotSummary.research_class` and `SpotDetail.research` are research confidence, separate
  fields.
* Events: detection stores the peak hour's classes, reasons and lead in the event's newest
  `history` snapshot (no migration).
* Alerts: "Forecast confidence: Moderate — about 6 days away" (the class of the event's peak
  hour, as in the API; the distance is from when the alert is built) and "Confidence at this
  break: Low — How this break responds to swell is not well documented yet." The reason
  after the local class is `local_confidence_reason`, so it appears only when the local class
  is lower ("Confidence at this break: Low" when both are Low). Older snapshots keep the
  index wording.
* **Read-time staleness:** when the authoritative run has gone stale since it was scored, the
  API caps both classes at Moderate and puts the staleness reason first. The same cap applies
  to an event's peak-hour fields (`EventOut`) and to alert snapshots when the event's run is
  stale, so one hour never reads High in an event or email and Moderate in the forecast
  (`forecast_confidence.cap_for_staleness`).
* A run whose predictions were scored with another methodology, or whose local confidence
  used a research assessment that is no longer current (after `research load` or a seed), is
  rescored once it is shown to users (`predict.outdated_authoritative_runs`); archived rows
  are never rewritten.

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

## 6. Live check (real 2026-10-11 runs, re-run 2026-10-12 with the forecast-v1 code)

A fresh clone of the live database (`swell_live_d`, created from `swell_live`, which is never
modified) with the real runs NOAA GFS `noaa-gfs:2026101100`, ECMWF IFS `ecmwf-ifs:2026101012`
and GEFS-Wave `gefs-wave:2026101018`, migrated from 0003 to head (0005); research assessed
first with no evidence in the database (all 50 spots Low), then with the committed evidence
file; blend, predictions (rescoring), archive and detection run at a fixed "now" of
2026-10-11 08:30 UTC, the hour of the day at which ECMWF used to drop out
(`FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs`).

* **Staleness.** At 08:30 the ECMWF run was 20.5 h old: old rule (18 h) → blend members
  `[noaa_gfs_direct]` only, the existing blend run counted as stale and GFS alone was
  authoritative for all 50 spots (**0** spot-hours with ECMWF); per-source rule → members
  GFS + ECMWF, the blend authoritative for all 50 spots and **3,850 of 4,050** blended
  spot-hours (81 valid times × 50 spots) with ECMWF as a member (the remaining 200 are
  beyond ECMWF's 240 h). Hour by hour from 2026-10-10 20:00 to 2026-10-11 14:00 the old
  rule kept the ECMWF 12Z run in the blend for 11 of 19 hours (lost from 07:00), the new one
  for 18 (lost from 14:00, 25 h after the cycle).
* **Forecast classes by lead** (4,050 blend predictions):

  | Lead (days) | High | Moderate | Low |
  |---|---:|---:|---:|
  | 0-1 | 159 | 86 | 5 |
  | 1-3 | 544 | 239 | 17 |
  | 3-7 | 811 | 715 | 74 |
  | 7-10 | 0 | 920 | 280 |
  | > 10 | 0 | 0 | 200 |

  Median GEFS-Wave relative spread at these hours: 0.025 / 0.034 / 0.084 / 0.153 / 0.229.
  The forecast classes were identical before and after the research evidence was loaded
  (forecast confidence never reads research).
* **Local confidence.** Without evidence: Low for all 4,050 hours. With the committed
  evidence file (research-v1.1: 18 spots Moderate, 32 Low overall; research basis Moderate at
  19 spots, Low at 31) and the run rescored: High 0, Moderate 1,313, Low 2,737 (by lead
  0-1 / 1-3 / 3-7 / 7-10 / > 10 days: Moderate 90 / 290 / 572 / 361 / 0). No spot can be High
  locally while no research variable is High. `local_confidence_reason` was set on 2,593
  hours ("… not well documented yet." 2,161, "… only partly documented." 432) and empty on
  1,457 where the two classes are equal. The rescoring had to be forced in this check: the
  prediction job then rescored 0 runs although all 4,050 hours referenced the superseded
  assessment. Since the review fix it is automatic (§4); a re-run on the clone after
  `research assess --slug pipeline --force` rescored the blend run once (4,050 predictions)
  and then nothing.
* **Most common reasons:** "Not yet checked against buoys near this spot." (4,050; no
  verification metrics in the live data), "Ensemble members agree closely." (2,974), "GFS and
  ECMWF agree on swell size." (1,282), "Ensemble members differ somewhat on swell size." (734),
  "GFS and ECMWF disagree on swell period." (604), "… on swell size." (586), "… on swell
  direction." (564), and the lead-time reasons ("An 8-day forecast — forecasts this far out
  often change." and "A 9-day forecast — …", 400 each). Rules that capped or downgraded:
  `lead_7_10d` 1,200, `period_disagree` 604, `hs_disagree` 586, `direction_disagree` 564,
  `lead_3_7d_spread_not_small` 288, `lead_over_10d` 200, `single_source_over_3d` 200,
  `spread_large` 182, `wind_disagree` 181, `hs_strong_disagree` 119, `spread_very_large` 110.
* **Examples.** Rincon, valid 2026-10-11 18Z, 30 h: Low (index 29) — "GFS and ECMWF disagree
  strongly on swell size." (Hs log ratio 0.98, a factor 2.7), "… on swell period." (Tp
  4.2 s apart), although the GEFS spread was small (0.10); local Low with no separate local
  reason (both Low). Uluwatu, 2026-10-15 03Z, 111 h: High (index 95) — a 5-day forecast with
  GEFS spread 0.021 and GFS / ECMWF within 1 %; local Low ("How this break responds to swell
  is not well documented yet.": research Moderate overall, but its swell-size evidence is Low,
  so the basis is Low). Jaws, 2026-10-12 18Z, 54 h: High (index 70, spread 0.10); local
  Moderate ("… only partly documented."). Pipeline, 2026-10-18 21Z, 201 h: Moderate
  (index 72) — "An 8-day forecast — forecasts this far out often change.", "GFS and ECMWF
  disagree on swell period." (6.0 s) and "… on swell direction." (51°); local Low.
* **Events and archive.** Detection updated 49 events; their peak-hour classes were High /
  Low 27, Moderate / Low 18, Low / Low 4 (forecast / local, before the evidence was loaded).
  The clone's 3,850 archive rows were written by the old code and were not rewritten (the
  rescoring inserted none), so they keep empty confidence columns; a demo-mode pipeline on a
  fresh database archived 4,050 rows, all with both classes and the research class.
* Not exercised live: verification rows (none exist yet), run-to-run change (no previous run
  of the same source in the clone), missing variables and demo data (covered by tests).

## 7. Limitations and next steps

* All thresholds are heuristics anchored on the cited numbers. The offshore classes of
  single-source GFS-Wave hours (with and without a GEFS-Wave spread) are ordered by
  realised error at held-out NDBC buoys (§8); the rules that need two independent families
  (disagreement, agreement), run-to-run change at the app's 6-hourly cycle, staleness and
  the ECMWF-only case have no held-out evidence, and no class has been validated against
  observed surf. The archive keeps every input so that, once verification pairs
  accumulate, the classes can be checked for reliability and the thresholds recalibrated.
* The spread evidence is 7 weeks of GEFS-Wave against its own analysis; the lead-time
  evidence is one season (2021) of GFS v16 at three buoys in one region.
* Local confidence uses the research assessment only; the uncalibrated breaking-height
  translation (σ_m = 0.30, [SURF_SCORING.md](SURF_SCORING.md)) is not yet a separate factor.

## 8. Validation against buoys (2026-10-12)

Full method, plan (fixed before any result) and tables: [ml-evaluation.md](ml-evaluation.md)
section 12. Data: raw GFS-Wave station bulletins (00Z every second day, 2024-10-12 to
2026-10-12) paired with NDBC spectral Hm0 at the five offshore buoys of the US spots
(51201, 51205, 46026, 46224, 46053), and GEFS-Wave station statistics (mean and spread,
published with spreads only since 2026-02-26) at six buoys. TRAIN = the older year (valid
times up to 2025-09-29), TEST = cycles from 2025-10-13 (136,097 pairs); ensemble TEST from
2026-06-20 (22,380 pairs). The error is the OFFSHORE Hs error at the buoy, not the error of
a breaking-height prediction.

**What could be computed historically:** lead time, one deterministic family, the lead
table or the GEFS-Wave spread, completeness, and (variant V1) the verification row the
scheduled job would have produced from the 90 days before each cycle. Not available
historically: ECMWF (no second family), run-to-run change at 6-hourly cycles, staleness.

**Pre-registered test:** RMSE, median and p90 of |error| increase strictly High < Moderate
< Low, and the 90 % cycle-block bootstrap CIs of both RMSE differences exclude zero; a
boundary counts as misplaced only if the test fails on TRAIN, a change is derived on TRAIN
only and adopted only if it passes on TEST.

| | High | Moderate | Low |
|---|---|---|---|
| forecast-v1, single source, TRAIN (V0) | RMSE 0.387, median 0.23, p90 0.54 | 0.404, 0.23, 0.58 | 0.504, 0.26, 0.77 |
| forecast-v1, single source, TEST (V0) | 0.382, 0.25, 0.60 | 0.406, 0.25, 0.63 | 0.543, 0.29, 0.86 |
| forecast-v1.1, single source, TEST | — (no High) | 0.396, 0.25, 0.62 | 0.543, 0.29, 0.86 |
| v1 = v1.1 with GEFS-Wave spread, TEST | 0.340, 0.25, 0.54 | 0.490, 0.30, 0.77 | 0.752, 0.48, 1.06 |

(metres; n per class 25,543-110,554 for single source, 390-15,131 with an ensemble.)

* **forecast-v1 fails for single-source hours.** High (leads up to 52 h) and Moderate had
  the same median error on TRAIN and an RMSE difference of 0.017 m [−0.010, 0.043]; the
  same on TEST for the medians. No High / Moderate lead boundary between 24 and 72 h
  separated them on TRAIN. Low (single source beyond 150 h, or swell missing) was clearly
  worse in every split.
* **forecast-v1.1** removes High for such hours (`single_source_no_ensemble`), the only
  candidate that passed on TRAIN. On TEST its two classes are distinct (Low − Moderate RMSE
  0.146 m [0.123, 0.169]; medians 0.25 vs 0.29 m). Because the candidate has no High, its
  test is the same test on the two classes that exist. That held-out check was not blind:
  the candidates and the rule choosing among them were written after forecast-v1's TEST
  scores had been computed, and the new classes merge forecast-v1's High and Moderate.
* **By lead** (TEST, RMSE; table with medians and p90 in ml-evaluation.md 12.3): single
  source 0.378 m at 24 h, 0.383 at 48 h, 0.392 at 72 h, 0.415 at 120 h (Moderate in
  v1.1), 0.476 at 168 h and 0.603 at 240 h (Low). Without an ensemble the class of a
  complete hour is a function of its lead, so this validates the lead caps, not a separate
  certainty signal. With a GEFS-Wave spread: High 0.333-0.338 m at 24-72 h, Moderate 0.358
  at 72 h rising to 0.630 at 240 h, Low 0.675-0.887 at 168-240 h (59-88 pairs).
* **Effect on the live system:** rescoring the real 2026-10-11 runs with forecast-v1.1 (a
  scratch copy of `swell_live2`), the new rule fired on 0 of 4,050 NOAA GFS hours and 0 of
  4,050 blend hours, because every hour had a GEFS-Wave row. It changes classes only when
  the ensemble is missing and a single model family is available.
* **With a GEFS-Wave spread** the three classes are distinct on TRAIN and TEST, with and
  without verification rows (Moderate − High 0.150 m [0.125, 0.174], Low − Moderate 0.262 m
  [0.144, 0.368] on TEST). v1.1 does not change these hours.
* **Validated:** the ordering of offshore errors of single-family GFS-Wave hours by class,
  with and without an ensemble spread, at buoys off Hawaii and California. **Not
  validated:** every rule that needs two independent families (disagreement, agreement),
  ECMWF-only hours, run-to-run change, staleness, local confidence, and any probability:
  the classes remain ordinal labels.
* **80 % range** (same data, [ml-evaluation.md](ml-evaluation.md) 12.2). The app's offshore
  range for a single source covered 53 % at 24 h, 62 % at 48 h, 73 % at 72 h and 85-89 %
  beyond 5 days (most misses above p90). A lead-dependent two-tail scale fitted on TRAIN
  covered 75.1-80.2 % at every TEST lead bucket pooled over the five buoys (69 % at 46053
  and 91 % at 51201). It is implemented as the calibrated offshore component
  `ndbc-offshore-v1` in `uncertainty.py`, applied only to raw single-family GFS-Wave hours
  without an ensemble at jaws-peahi, mavericks, ocean-beach-sf and trestles (the spots
  whose buoy agrees), and not yet wired into the prediction job. The breaking range keeps
  its heuristic label, and the ensemble-based range stays unvalidated (pooled pass, but too
  wide at both paired buoys). The confidence index itself (σ_f) is unchanged.
