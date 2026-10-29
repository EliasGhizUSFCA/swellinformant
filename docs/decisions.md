# Architectural decisions

Short records of the decisions that shape the forecasting, notification and map systems:
context, the decision, the alternatives considered and the consequences. Newest last.

## ADR-001 Centralised acquisition, one fetch per model run

**Context.** Every user's searches depend on the same 50 spots.
**Decision.** Forecasts are acquired by background jobs once per upstream model run
(`run_key`), scored once, and every API request reads cached rows. No user action calls a
forecast provider.
**Alternatives.** Per-request provider calls: slow, rate-limited, inconsistent between users.
**Consequences.** Idempotent runs, quotas under control, the same numbers for everyone.

## ADR-002 Read NOAA and ECMWF open data directly, by byte range

**Context.** Open-Meteo republishes NOAA GFS-Wave, but its default marine model is a European
blend, its free tier is non-commercial, and its wave/wind requests could not be reproduced
from the sandbox. NOAA (NODD on AWS) and ECMWF publish GRIB2 with per-message indexes.
**Decision.** Ingest GFS-Wave (native `global.0p16` / `gsouth.0p25`, `global.0p25` fallback),
GFS 10 m wind, GEFS-Wave ensemble statistics and ECMWF IFS wave/wind directly, fetching only
the needed messages by HTTP Range (`forecasts/grib.py`) and decoding with ecCodes. Point
values come from the nearest valid sea cell; nothing is interpolated across land. Open-Meteo
stays as a configurable source.
**Alternatives.** Open-Meteo only (one opaque dependency, licence limits); full-file downloads
(10–550 MB per step); NOMADS grib filter (blocked here, rate-limited).
**Consequences.** Measured: ~24 MB per GFS/GFS-Wave forecast step for all 50 spots (about
2 GB per 10-day run at 3-hourly steps), ~14 MB per ECMWF step and ~5 MB per GEFS-Wave
statistics step; an ecCodes dependency (binary wheel); file-layout risk at the GFS v17
upgrade, so bucket URLs and GFS path templates come from settings.

## ADR-003 Source dependency families; no equal averaging

**Context.** Agreement between two copies of the same model is not evidence.
**Decision.** Every source has a family (`forecasts/families.py`). The blend keeps one member
per family; same-family sources become integrity cross-checks (a disagreement flags a data
problem). Members are weighted by the best linear unbiased combination for correlated errors,
using verification statistics when they exist and otherwise a documented lead-time prior
(older runs count less); prior error correlations are 0.6 for related and 0.3 for
independent families. Disagreement between independent families is flagged and widens the
forecast range.
**Alternatives.** Simple average of all sources (double-counts NOAA); single source
(no cross-check, no disagreement signal).
**Consequences.** With no verification yet, two equally fresh independent models get equal
weight by construction of the prior; this changes automatically once accuracy metrics exist.

## ADR-004 A physical nearshore baseline before any ML

**Context.** Offshore Hs is not surf height, and there are no surf observations to train on.
**Decision.** `linear_wave_v2` (linear wave theory + Komar–Gaughan with directional spreading,
shore normal, shadowing, focusing prior and energy-flux combination) is the baseline. No
breaking-height or score model is trained until nearshore observations exist; no model is
trained on another website's surf score.
**Alternatives.** Neural or tree models on breaking height (no labels); SWAN/XBeach per spot
(needs bathymetry and compute for 50 spots).
**Consequences.** Every output is labelled as an uncalibrated estimate; calibration has a
defined path (SURF_SCORING.md §7).

## ADR-005 No invented bathymetry: provenance on every physical parameter

**Decision.** `spot_physical_profiles` stores the shore normal, depths, focusing prior and
shadow table with a provenance flag each (`measured`, `literature`, `calibrated`,
`estimated`, `unknown`). Unknown depths stay NULL and the model then assumes deep water and
no depth limit, saying so in the prediction's assumptions.
**Consequences.** Until 2026-10-13 all 50 spots ran on estimated profiles, shown on the spot
page. Since ADR-018 four US spots carry measured values from NOAA BlueTopo
(`data/surf_spots/physical_profiles.json`, provenance per value): survey depths at the
forecast points of Mavericks, Ocean Beach and Rincon and Trestles' shore normal (Trestles'
forecast point lies in BlueTopo's NBS Generalization surface, outside any survey, so its
depth stays unknown); no spot has a measured controlling depth.

## ADR-006 Uncertainty as labelled ranges, not probabilities

**Context.** The error model is assembled from verification against model analysis plus
assumed floors; it has not been checked against observations.
**Decision.** Predictions carry p10/p90 and an `uncertainty_kind` (`heuristic`,
`ensemble_heuristic`; `calibrated` reserved for ranges that passed a held-out coverage check).
The UI says "80% range … not yet verified". P(score ≥ threshold) is not shown. The 0–100
confidence is an index of offshore forecast spread, explicitly not a probability; run-to-run
change is a diagnostic only.
**Alternatives.** Monte Carlo exceedance probabilities (recommended by the research once
calibrated).
**Consequences.** Honest but less precise-looking communication until verification exists.

## ADR-007 An immutable prediction archive outside forecast retention

**Status (Swell 2.0).** Kept but off by default: `PREDICTION_ARCHIVE_ENABLED=false`, on in
the full-retention profile (ADR-042).
**Context.** Forecast runs are deleted after `RETENTION_FORECAST_DAYS`; what was forecast at a
given lead cannot be reconstructed afterwards.
**Decision.** After each scoring job the authoritative run is copied, 3-hourly to 240 h, into
`prediction_archive`, keyed by (source, run, spot, valid time), never updated, kept for
`RETENTION_ARCHIVE_DAYS` (730). Model forecasts at buoys go to `point_forecasts` for the same
reason.
**Consequences.** Roughly 4,000 rows per run; verification of breaking heights becomes
possible as soon as nearshore observations arrive.

## ADR-008 ML only as verified post-processing of offshore Hs

**Decision.** ML corrects the offshore Hs residual (log space) at buoys, with lead time as a
feature and no station-identity features. Training uses chronological splits with a gap of
at least the maximum lead plus three days, rolling-origin evaluation and an explicit leakage
guard. A model becomes active only if, on held-out data, it beats the raw model by at least
5 % RMSE with a cycle-block-bootstrap interval excluding zero, does not degrade any lead
bucket by more than 3 %, beats or ties the linear baseline and has at least 300 test pairs;
it is applied only to spots paired with its training stations and within its lead range.
Every decision, including rejections, is stored in `ml_model_versions.decision`.
**Alternatives.** LSTM/transformer forecasters (published gains are within ~2 days); train on
reanalysis (not forecast errors).
**Consequences.** It is acceptable — and expected in some regions — that no ML model is
deployed.

## ADR-009 Honest delivery states for every email

**Decision.** Account and alert emails move through `queued → sending → accepted | captured`
and reach `delivered`, `bounced` or `complained` only from a signed provider webhook. Local
development uses Mailpit (visible inbox); production refuses capture-only providers.
Suppressed alerts are stored with a reason and sent once the reason clears.
**Consequences.** The UI never says "delivered" because an API call returned 200.

## ADR-010 OpenStreetMap tiles by default; CSP from the configured tile host

**Decision.** OSM raster tiles with attribution and a Referer by default; CARTO only with a
key; the Content-Security-Policy is generated from the configured tile URL.
**Consequences.** The map works without credentials; switching providers is configuration.
**Update (2026-10-11, map redesign).** The world map's default basemap is a dark map drawn
from bundled Natural Earth data (no tiles); OSM tiles stay the default of the optional
Streets basemap and of the spot page's mini map, and the CSP is still generated from the
configured tile URL.

## ADR-011 Migration 0003 corrected before release

**Context.** The prediction archive's unique key initially used the issue time, which let
two runs issued in the same hour collide.
**Decision.** Migration `0003_forecast_engine` had not been merged or deployed, so it was
corrected in place (key on `run_key`) rather than followed by a new migration.
**Consequences.** Development databases created from the first draft must be downgraded to
`0002_email_delivery` and upgraded again (`alembic check` detects the difference).

## ADR-012 An ML correction is applied to the model it was validated on, once

**Context.** A correction is trained and accepted (ADR-008) on one model family's raw
forecasts at buoys (GFS-Wave station bulletins, family `ncep_gfs`). With two independent
families the authoritative forecast is the blend, whose family is `blend`.
**Decision.** `ml/hook.py` applies the active correction in exactly one place per value:
the blend corrects each *member* of the trained family before combining (cross-checks stay
raw, so integrity checks still compare raw data); a single-source run of that family is
corrected in the prediction job; a blend run is never corrected again. Only offshore heights
are scaled (Hs and every partition by the same factor); periods, directions and wind are
untouched, and the breaking-height model runs on the corrected offshore values. Features are
built as in training: the paired buoy's spectral Hm0 at or before the cycle time (within
1 h) and the analysis error against the archived lead-0 bulletin of the same cycle. When a
required feature is missing the value stays raw. Predictions and the archive record the
model version; blended hours record which member was corrected (`wave_forecasts.extra`).
**Alternatives.** Correcting the blended value (not what was validated); correcting
breaking heights (no observations to validate against).
**Consequences.** A correction that requires cycle-time observations only works where such
observations arrive in real time; models meant for production are trained with
`--no-obs-features` unless the paired buoy has a real-time Hm0 feed. A blend built before a
correction was activated is not rebuilt; the next upstream run uses it.

## ADR-013 The first passing correction ships inactive

**Context.** The backtest's ridge correction (P2 v4) passed every ADR-008 criterion on a
held-out 2021 test at Cape Naturaliste (RMSE -8.1 %). It was trained on GFS-Wave station
bulletins; at the surf spots the pipeline would feed it gridded GFS-Wave values.
**Decision.** Ship it as a registered candidate (`backend/ml_models/`), not active by
default. A live transfer check through `ml/hook.py` gave 18 % of corrected hours factors
below the range seen on the test set (5th percentile 0.79 vs 0.98), so in production it
would run partly outside the validated input distribution. Activation stays an explicit
operator step after a shadow evaluation on gridded inputs.
**Consequences.** User-facing forecasts are uncorrected. The criteria are unchanged; the
gate added here is "validated on the inputs it will receive", which the next training run
(gridded GFS-Wave archived at the buoys) is designed to satisfy.
**Update (2026-10-12).** For the US spots superseded by ADR-020: an NDBC-trained version
passed the same transfer check and ships for activation there. This version (v4) stays an
inactive candidate for the Western Australian spots.

## ADR-014 Research confidence and forecast confidence are separate assessments

**Context.** Two different questions get called "confidence": how well a break's ideal
conditions are documented (research confidence, a property of the spot that changes when
evidence is added) and how trustworthy an upcoming prediction is (a property of each forecast
hour that changes with every model run). A single number would hide which one is weak: a
famous, well-documented break can have an untrustworthy 9-day forecast, and an excellent
2-day forecast says little about a break nobody has documented.
**Decision.** Three separate classifications, stored separately and shown separately, none of
them a calibrated probability: research confidence (`spot_research_assessments`, methodology
research-v1, research-v1.1 since ADR-016, research-v1.2 since ADR-018,
docs/research-confidence.md); forecast confidence of the offshore forecast and local
confidence of the breaking-wave estimate (`surf_quality_predictions`, methodology
forecast-v1, forecast-v1.1 since ADR-018, docs/forecast-confidence.md). The only link is documented: local confidence =
min(forecast confidence, research basis) with research basis = min(swell direction, swell
size, max(bathymetry, local behaviour)); without an assessment it is Low. Forecast confidence
never reads research, research never reads forecasts. Every prediction records the
assessment it used (`research_assessment_id`) and the archive records the classes as issued
(`forecast_confidence_class`, `local_confidence_class`, `research_class`, `confidence_inputs`).
The 0-100 index (`confidence`) is kept unchanged as the starting point of forecast confidence.
**Alternatives.** One blended score (hides the weak part, cannot be evaluated); weighting
research into the index (would make a well-documented break look like a better forecast).
**Consequences.** The UI shows up to three classes and must label each; until research
evidence is loaded every spot has Low research and therefore Low local confidence, even where
forecast confidence is High.

## ADR-015 Per-source staleness

**Context.** One `FORECAST_STALE_HOURS` (18 h) applied to every source. ECMWF open data runs
two long cycles a day, published about 8 h after the cycle and fetched by the next 3-hourly
acquisition, so the newest ECMWF run is routinely 20-23 h old just before the next one
arrives. It counted as stale for several hours every day, and the blend silently fell back to
NOAA only (measured on the 2026-10-11 live data: at 08:30 UTC ECMWF was 20.5 h old and no
blend was built; over 2026-10-10 20:00 to 2026-10-11 14:00 the old rule kept ECMWF for 11 of
19 hours, the new one for 18).
**Decision.** A run is stale once it is older than cycle interval + publication lag
(`*_AVAILABILITY_LAG_HOURS`) + acquisition interval (`FORECAST_REFRESH_MINUTES`) + a 2 h
margin of its source: NOAA GFS 16 h, GEFS-Wave 18.5 h, ECMWF 25 h with the default settings
(`queries.stale_after_hours`). Sources without a known cycle (demo, Open-Meteo, Windy) keep
`FORECAST_STALE_HOURS`. A blend is stale when any member is stale by its own rule. Every
caller uses `queries.is_stale`: blend member selection, the authoritative-run choice, the
index's stale factor, forecast confidence (at scoring and, for runs that went stale later, at
read time), the API's `stale` flag, the staleness check task and alerts (twice the
stale-after age).
**Alternatives.** Raising the global limit to 25 h (would hide a missed GFS cycle for a day);
per-source settings (more configuration for values that follow from the cycle and lag).
**Consequences.** ECMWF stays in the blend through its whole cycle; a GFS run now counts as
stale after 16 h instead of 18 h, i.e. as soon as one cycle has been missed.

## ADR-016 Evidence seen only through search summaries is at most Moderate

**Context.** The first real research evidence (2026-10-11: 259 sources, 805 claims for the
50 spots) was gathered with domain-restricted web searches while direct page access was
blocked. Every source was re-checked by an independent verifier, but every excerpt is a
search tool's text about a page, not the page itself. Under research-v1 that evidence would
have made three spots (Jaws, Sunset Beach, The Box) High overall. A second pass on
2026-10-12, with direct page access, re-read every source that could be read: 449 sources,
384 read directly with verbatim excerpts confirmed on the page, 1,711 claims; the 65 sources
behind bot protection stayed `web_search` (research-v1.1 result: 34 Moderate, 16 Low).
**Decision.** Record how each source was read (`spot_research_sources.retrieval`: `fetched`
by default, `web_search`; migration 0005, a check constraint, part of the evidence
fingerprint). Methodology research-v1.1 caps a variable at Moderate when all of its
supporting sources are search-only (rule `cap:search_only`, applied after the research-v1
rules) and ends its summary with "(based on search summaries; source pages not read in
full)". The format also accepts one-bound values (one bound of a direction, size or period
range left out), which the file uses for 16 claims; the evidence file itself is committed
unchanged.
**Alternatives.** Rejecting search-only evidence (would discard verified, attributed
statements and leave every spot without evidence); treating it like read pages (overstates
it: a summary can compress or misattribute what the page says); a separate source tier
(mixes how a source was read with what kind of source it is).
**Consequences.** No spot is High until some evidence is read directly (first pass: 18
Moderate, 32 Low). The methodology change adds one assessment row per spot and keeps the
research-v1 rows. With the second pass every variable that has support has some directly
read support, so the cap no longer fires for any variable; no spot is High for other reasons
(ADR-018).

## ADR-017 NDBC: one record per wave report, APD defined per station

**Context.** Checked against the live service on 2026-10-12 (docs/observations.md), the
NDBC adapter stored each 20-minute wave report of NDBC's own buoys two or three times,
because realtime `.txt` prints it on the 10-minute meteorological rows around the wave
time (3,793 wave rows for 2,139 reports at 46026). It also stored every average period as
the undefined `apd`, although NDBC documents APD as Tm02 for its own systems and Tm01 for
CDIP buoys (confirmed against CDIP `waveTa` within 0.005 s at six buoys).
**Decision.** Copies of one report (same height within one 0.1 m rounding step, same APD
and MWD, within 15 min) become one record at the `.spec` time, which is also the time of
the QC'd archive; each value comes from the closest copy that has it, and
`parse.duplicates_merged` counts the folded rows. The catalog states `apd_kind` per
station (`tm02` NDBC, `tm01` CDIP) and `tm_kind` is stored from it; a station without it
keeps `apd`. Yearly and monthly archive records (`probably_good`) supersede realtime ones
(`not_qc`) of the same report.
**Alternatives.** Keeping every row (inflates pair counts and weights NDBC buoys two to
three times in pooled scores); keeping only the row at the `.spec` time (often loses DPD,
which NDBC prints on one copy only); storing APD as `apd` everywhere (mixes Tm01 and Tm02
in one column).
**Consequences.** One record per report, matching the archive (on the August 2026
overlap, 191 of 192 merged records at 46026 fall on an archived time, the last one minute
off, and all 192 at 51201; heights within 0.05 m). Copies stored before this change would
stay, since re-ingesting does not delete rows at other times; none were stored outside
scratch databases, because NDBC was unreachable before 2026-10-12.

## ADR-018 Research-v1.2 and the profile-decision policy

**Context.** The second research pass contradicts parts of the spot profiles, which drive
scoring (wind is 0.30 of the score plus a gate) and the nearshore model. An assessment of
it found sixteen spots whose wind evidence contradicts the profile's offshore direction
(the largest prediction impact at Chicama, 150° against ~67.5°), swell-size comparisons
that ignored the maximum (overlap alone let "holds 30 ft" agree with a 12 ft maximum), no
measured physical parameter anywhere, 19 spots whose offshore wind is more than 60° from the
estimated shore normal + 180°, and no warning to users when the offshore direction itself is
disputed (local confidence does not use the wind).
**Decision.**
* **research-v1.2**: each stated size bound is compared separately (a stated maximum more
  than 25 % above the profile's, or a minimum more than 25 % below, contradicts); record
  waves are not working ranges; the tier rules are written down (official = government
  agencies with a coastal or marine mandate, national surfing reserves, government
  geological or hydrographic surveys; tourism boards and World Surf League editorial pages
  are magazine). Seven tourism sources were reclassified and five values removed, each
  noted on its source.
* **Profile decisions** (`data/surf_spots/profile_decisions.json`,
  `research.decisions.propose`): a profile value is changed only when at least two
  independent publishers whose pages were read directly agree on a stated new value, within
  the methodology's agreement tolerances, and every directly read source supporting the old
  value is of a strictly lower tier than both of them; otherwise the record says kept or
  disputed with the reason (a publisher that both supports and contradicts the value
  does not outweigh its own contradiction). Every contradicted field of the current assessment has a
  record with its sources, rationale and the before/after effect on the real 2026-10-11 run
  (`scripts/profile_sensitivity.py`, read-only). For wind the record reports the model wind
  at the forecast point, which never decides.
* **forecast-v1.1**: a wind conflict in the assessment or a disputed record adds "Sources
  disagree about which wind is offshore here." to every hour, stores the wind relation under
  the main contradicting direction and names both in the explanation; no class or score
  changes.
* **Measured bathymetry** (NOAA BlueTopo, public domain) for the four continental US spots
  where it exists, under documented acceptance rules (`scripts/bathymetry_profiles.py`);
  measured values that the straight-contour model cannot use, and depths from BlueTopo's
  NBS Generalization surface (no survey), are kept in the provenance, not applied.
* **`research lint`** lists unexplained offshore/shore-normal gaps over 60°, contradicted
  fields without a record and records the rule no longer gives.
**Alternatives.** Changing a profile when the best contradicting source outranks the best
supporting one (the methodology's "conflict:stronger"): it would have changed the offshore
wind at Chicama and Skeleton Bay, each on one forecast-site guide plus community pages
against community pages of the same tier as those (and five tide or size values the same
way), and at Chicama the sources that say the wind is "consistently offshore" and the
model's prevailing SSE wind both point the other way. Deciding by model wind (it shows which
winds blow 15-20 km offshore, not which are offshore at the lineup). Averaging the sources'
directions (would invent values nobody states).
**Consequences.** No wind profile met the rule (11 disputed, 9 kept); four values changed
(three tides to `all`, Cloudbreak's range to 2-20 ft). Research: 35 Moderate, 15 Low, none
High; the research basis of local confidence falls (High 8 → 3, Low 12 → 24 spots), almost
entirely through the size rule. 15 spots carry the disputed-wind note on every hour of the
real run. Restaurants stays listed by the lint (no source describes its orientation).

## ADR-018b A calibrated offshore range only where the spot's own buoy agrees

Numbered ADR-018 until 2026-10-26, when a second record with that number (Research-v1.2,
above) was found.

**Context.** Against five NDBC buoys (docs/ml-evaluation.md 12.2) the app's offshore 80 %
range for one deterministic source covered 53 % at 24 h and 85-89 % beyond 5 days. A
lead-dependent two-tail scale fitted on the older year covered 75.1-80.2 % at every lead
bucket on the held-out year, pooled, which meets the pre-registered criterion; by buoy it
covered 69 % (46053) to 91 % (51201). The ensemble range passed pooled but was 88-90 % at
the only two GEFS buoys that are offshore references of spots.
**Decision.** Implement the fitted scale as the calibrated OFFSHORE component
`ndbc-offshore-v1` (`surf_quality/uncertainty.py`), used only for raw single-family
GFS-Wave hours whose spread comes from the lead table, at leads 3-240 h, and only at spots
whose paired offshore buoy's own held-out coverage has a CI overlapping 75-85 % over all
leads and at every lead bucket with at least 200 pairs (jaws-peahi, trestles). The
breaking range keeps `uncertainty_kind` heuristic and records `offshore_kind: calibrated`
beside it, because its translation share is not calibrated. The ensemble range stays
`ensemble_heuristic`. The per-buoy rule was added after the results and only narrows the
claim. **Review (2026-10-13):** the builder's rule checked all leads together only and
also labelled mavericks and ocean-beach-sf; their buoy 46026 covers 79.4 % over all leads
but 69.1 % [64.3, 74.1] at 240 h (77.5 % at 120 h, 75.0 % at 168 h), so the rule now
checks every lead bucket and those two spots are not labelled. "Calibrated" refers to the
pre-registered measure, the coverage of the interval: at both remaining buoys one bound is
off on its own (46224: 18.6 % [16.5, 20.9] above p90, 4.7 % below p10; 51205: 2.3 %
[1.8, 2.7] below p10, 12.2 % above p90), and no paired buoy has both tails near 10 %.
Before the prediction job passes the context (a follow-up), a pre-registered tail check
at the spots' buoys should decide whether the bounds may be shown as p10 / p90.
**Alternatives.** Label every paired spot (pooled pass): contradicted at two buoys by
their own data. Apply the scale everywhere: in Western Australia it put 18 % below p10 and
3 % above p90. A new `uncertainty_kind` value: needs a migration and frontend labels for a
breaking range that is still mostly heuristic.
**Consequences.** The prediction job must pass an `OffshoreContext` (spot, families,
whether ML-corrected) for the label to appear; until then predictions are unchanged. With
the correction of ADR-020 active the central value is no longer raw, so the label does not
apply at the corrected spots.

## ADR-019 forecast-v1 stays: the TRAIN-found single-source cap was not adopted

**Context.** At five NDBC buoys forecast-v1's single-source High (leads up to 52 h) and
Moderate classes had the same median offshore error on the training year and an RMSE
difference whose CI included zero, so the pre-registered test failed there; no High /
Moderate lead boundary between 24 and 72 h separated them (docs/ml-evaluation.md 12.3).
The only candidate that passed on the training year, in a two-class form, was C2: no High
from a single model family without an ensemble. With a GEFS-Wave spread all three classes
were distinct on both years.
**Decision.** Keep forecast-v1. The plan adopts a change only if the changed classes pass
the same (three-class) test on the held-out year; C2 has no High class, so it cannot, and
the two-class form it passed (Low − Moderate RMSE 0.146 m [0.123, 0.169]) was written
after forecast-v1's held-out scores were known and tests the Low boundary C2 does not
change. The held-out year also does not repeat the training-year finding: High's RMSE was
0.024 m [0.009, 0.037] below Moderate's, and with verification rows forecast-v1 passed the
full test (difference 0.044 m [0.029, 0.059]). C2 stays available for analysis (`verify
confidence-classes --method candidate-c2`).
**Review (2026-10-13).** The builder had adopted C2 as method `forecast-v1.1` on the
two-class form; the review reverted it, because changing the adoption test after the
held-out scores were seen is changing a pre-registered criterion. No stored prediction
carried `forecast-v1.1` outside scratch databases, and on the real 2026-10-11 runs the cap
would have fired on no hour (every hour had a GEFS-Wave row).
**Alternatives.** Adopt C2 as a conservative cap (it can only lower classes): not
supported by the held-out year, where High was better than Moderate, and not allowed by
the plan; move the lead boundary (no boundary passed on the training year); let
verification rows decide (they separated the classes on the held-out year only).
**Consequences.** A single-family hour without an ensemble can still be High inside 52 h;
its held-out RMSE is lower than Moderate's but its median error is the same, which the
docs state (forecast-confidence.md §8). The question is open for a new pre-registered
test, ideally on gridded forecasts at the buoys rather than station bulletins.

## ADR-020 The NDBC-trained correction is approved for activation at seven US spots

**Context.** `ml train` on spectral Hm0 at five NDBC buoys and three AODN delayed-QC buoys
(docs/ml-evaluation.md 12.4) gave three candidates that passed criteria (a)-(f) on a
held-out year (2025-10 to 2026-10, 139,257 pairs). The lowest validation RMSE, the random
forest, cut test RMSE from 0.427 to 0.320 m; its factors on the real gridded run at the
spots all lay inside the central 90 % of its test factors (ADR-013's transfer check). At
46053 (Rincon) it brought no measurable benefit and moved the bias from -0.12 to
+0.16 m; the WA buoys' data were all in the training part.
**Decision.** Ship `offshore_hs_residual` v6 (random forest, compressed to 3.7 MB) in
`backend/ml_models/` for activation at jaws-peahi, mavericks, ocean-beach-sf, pipeline,
sunset-beach, trestles and waimea-bay, leads 3-240 h, `ncep_gfs` forecasts. The registry
excludes spots whose stations have no held-out test pairs (`no_held_out_test_pairs`), and
rincon was excluded with `ml restrict` after the per-buoy results (a narrowing, not a
criterion change). Activation remains an operator step (`ml import-manifest`, `ml
activate`).
**Alternatives.** Keep everything raw (rejects a correction that passed every criterion
and the transfer gate); include rincon as the plan allowed (no evidence of benefit there);
the ridge (fails the transfer check at 9 of 11 spots).
**Consequences.** Offshore Hs at the seven spots rises by a factor 1.10-1.61 on the live
run, and breaking heights with it (about +23 % at a factor 1.3), which is not validated
because breaking heights are not observed. The held-out bias after correction is +0.09 m;
review traced it to the metre scale (1.1, at its clamp) chosen with the WA-only train
part and kept after the refit that includes the NDBC year (docs/ml-evaluation.md 12.4),
a pipeline follow-up, not a reason to retune the shipped model on its test year. The swell
features are the bulletin's first listed partition (the wind sea in 16 % of the test
rows) but the spots' gridded primary swell; with the swell partition substituted, the
held-out RMSE is 0.342 m instead of 0.320 m (raw 0.427), so the benefit at the spots is
probably nearer -20 % than -25 % (docs/ml-evaluation.md 12.4).
With the correction active, the heuristic offshore range around the corrected value
covered 80-94 % by lead at the buoys and stays labelled heuristic. Deactivating
(`ml deactivate offshore_hs_residual`) returns every spot to raw forecasts.


# Upgrade 4 (2026-10): a website on Vercel, data synced to model runs, the wave look

The owner asked to put the app on a website (Vercel, testable locally first), to pull all
weather data when the forecast updates and only for the surf spots, to keep the worldwide
map layers in step with the spot forecasts, and to make the wave layer look like a
reference screenshot; it is free now, with a paywall later. The design proposed decisions
D1-D16; they are recorded here as built, with the final numbers where they differ from the
proposal. Measurements are those of the packages, the two integration rounds and the
verifier of 2026-10-23 (docs/UPGRADE_REPORT.md, "Upgrade 4"); quotes from Vercel, Neon,
Upstash and GitHub are in docs/deploy-vercel.md, section 11.

## ADR-021 (D1) One Vercel project in Services mode, built from the repository root

**Context.** Login cookies, the CSRF double-submit check and the CSP `connect-src 'self'`
need the site and the API on one origin; Vercel Daemons need a project whose Root
Directory is not a subfolder.
**Decision.** `vercel.json` declares service `web` (`frontend/`, `framework: nextjs`) and
service `api` (`backend/`, `framework: fastapi`, entrypoint `app.main:app`, one Python
function, maxDuration 60 s), rewrites `/api/(.*)` to `api` and the rest to `web`, a daemon
and a daily cron. Next.js adds no `/api` rewrite on Vercel (`API_ROUTING=platform`, the
default when `VERCEL` is set).
**As built.** `framework: nextjs` on the web service is required: without it the offline
build stopped with "[MULTIPLE_FRAMEWORKS_SERVICE] Multiple frameworks detected"
(frontend/ also holds the classic stack's Dockerfile). The offline `vercel build` (CLI
64.5.0) of the final tree: `RESULT: ok`, the api function 3,428 files and 127.3 MiB (limit
500 MiB, builder threshold 225 MiB), web functions 4.6, 3.8 and 1.7 MiB on nodejs24.x.
**Alternatives.** Two projects with an external rewrite (120 s timeout, default caching,
Deployment Protection bypass; the documented fallback, not tried).
**Consequences.** Services is a Beta; a real deployment was not made (no Vercel account was
used), so routing, cookies and the client IP on Vercel itself are first seen on the first
deploy.

## ADR-022 (D2) The background pipeline is one long-lived process, a Vercel Daemon

**Status (Swell 2.0).** Superseded for the spot forecasts by ADR-045: the refresh runs in the
API function, called by a scheduler, and the daemon is optional (map layers only). Vercel
cut the Hobby daemon quota to 5 Active CPU-hours a month (https://vercel.com/docs/daemons/limits,
read 2026-10-27), so the 4.4-7.9 CPU-hours below would be 88-158 % of it.
**Context.** Hobby cron runs once a day; Functions share 4 Active CPU-hours with the API and
cannot hold the data libraries; GitHub's terms rule out a scheduled production worker.
**Decision.** `python -m app.cli sync daemon` from the repo-root `Containerfile`, one
instance in `iad1`, 2 GiB / 1 vCPU. The same CLI runs everywhere: the daemon, the local
stack's `sync` container, Celery's `sync.tick`, and the manual `data-sync.yml`.
**As built.** The shutdown grace (`terminationGracePeriodSeconds`) is **600 s**, not the
proposed 120 s: on SIGTERM the daemon finishes its current stage, and a full map stage is
estimated at 165-287 CPU s on one vCPU (Vercel's default is 30 s, its maximum 3600 s). Under
the daemon the sync lease is 15 minutes when not set (45 under Celery), and a keeper thread
extends it while a stage runs, so a successor takes over a killed sync within about 15
minutes. Monthly CPU at the full cadence: **4.4-7.9 CPU-hours** (44-79 % of the 10-hour
Hobby quota; extrapolated from this sandbox, docs/sync.md), against the proposal's 4-7.
**Consequences.** The daemon is first truly tested on the first production deploy
(daemons do not run in previews or `vercel dev`); the local stack builds and runs the same
`Containerfile`.

## ADR-023 (D3) Syncs are event-driven by provider metadata and published together

**Status (Swell 2.0).** Amended by ADR-043 and ADR-044: the wave and the atmosphere are
refreshed separately (no 90-minute coalescing across them, `SYNC_COALESCE_MINUTES` 0), each
refresh publishes a forecast version, and the free-database profile now triggers on every
new spot run (`SYNC_TRIGGER_SOURCES=spots`).
**Context.** "All of the weather data should be pulled when the forecast updates."
**Decision.** Every `SYNC_CHECK_MINUTES` (10) the runner reads only metadata (Open-Meteo
`meta.json`, NODD `.idx` HEADs); a newer run than the stored one starts one coordinated,
idempotent sync of 12 stages under a Postgres lease, published in one commit
(`sync_runs.published_at`, `map_run_id`, `blend_run_id`). The GFS spot run and the map of
the same cycle coalesce for up to `SYNC_COALESCE_MINUTES` (90); failed components retry
every 15 minutes, up to 6 attempts per run.
**As built.** Beyond the proposal: a component Open-Meteo cannot serve unmixed (ECMWF
between its 00Z/12Z runs) is `waiting` ("not served yet") and never makes a sync due; an
acquisition the provider refuses for a moment (its servers disagreeing for minutes) is
`deferred`, not a failed attempt; a lagging server offering the stored run with a domain
still on the previous cycle is not new; an Open-Meteo daily limit makes that source wait
until 00:00 UTC. The **free-database profile** syncs only for a new map cycle
(`SYNC_TRIGGER_SOURCES=map`, `MAP_FIELD_CYCLES=06,18`): two syncs a day, each taking the
newest spot runs of both families, instead of about six; with `map` triggers and no map
component, spot runs trigger instead. Measured: after a sync, a check made 0 SQL statements
and 8 metadata requests (3,588 B); a forced re-sync over stored data wrote no data row and
left the swell events identical.
**Consequences.** On Neon Free spot forecasts change twice a day (a GFS run can read stale
for up to about 1.3 h before each sync, estimated); the full-retention profile restores a
sync per new run.

## ADR-024 (D4) Point forecasts only for the surf spots; global grids only for the map

**Decision.** The spot stage requests the active seeded spots only (`surf_spots.is_active`),
through Open-Meteo, one request per model for up to 50 spots; the direct GRIB spot
ingesters stay off; global grids are downloaded only for the world map layers.
**As built.** As designed. Measured live on 2026-10-23: Open-Meteo GFS, 10,264 records for
the 50 spots; NOAA GFS 06Z map layers, 135 fields for 9 map hours, 73.3 MB downloaded in 63
requests, 10.6 MB stored.

## ADR-025 (D5) A lean, Celery-free API; dependency extras; JOB_RUNNER

**Decision.** `[project].dependencies` is the lean API set; extras `worker` (Celery),
`server` (uvicorn[standard]), `sync` (pandas, ecCodes and `eccodeslib`, pyarrow,
scikit-learn, joblib), `all`, `dev`; `backend/uv.lock` pins every version (uv 0.11.32,
`exclude-newer`). `JOB_RUNNER=daemon` sends account emails and SMS inline and turns
"match my search" and "refresh flights" into Redis kick flags.
**As built.** The lean set measures **163.2 MB** (`scripts/check_api_bundle.py`, limit 200
MB); the function in the Vercel build 127.3 MiB. `eccodeslib` had to be named in the `sync`
extra (uv left it out of the lock otherwise, and the map stage failed with "Cannot find the
ecCodes library"). Inline sending: 3 attempts, 0.5 s and 1.5 s apart; the worst case is
about 41 s when a provider hangs (2 attempts × the 20 s HTTP timeout), not the proposed
"about 10 s", still under the 60 s function limit. Registration with Redis blackholed took
2.12 s under the hosted timeouts. Cold import of `app.main` in the lean venv 2.18-2.75 s;
the login's dummy hash is now computed on first use (import self time 97-105 ms → 0.2-0.3
ms).
**Consequences.** The API cannot run data jobs; `tests/unit/test_lean_imports.py` keeps it
importable without the extras.

## ADR-026 (D6) Neon Postgres, the pooled URL for the API, and a free-database profile

**Context.** On Neon Free the binding limits are 5 GB of egress and 100 CU-hours a month,
not only 1 GB of storage. The first measurement read 186.8 MB back per demo pass (about
27-33 GB a month).
**Decision.** The API uses Neon's pooled URL (pool 2+3, pre-ping, recycle 240 s, no
prepared statements); the daemon and migrations the direct URL; the free-database profile
keeps about 1 day of forecasts and 7 days of archive and job logs, pruned after every sync,
and wakes the database at most hourly between syncs.
**As built.** Two integration rounds cut the reads (column-selective queries, binary
`bytea` reads, a public snapshot cache in Redis computed once per publication and UTC hour,
`/api/system/status` and the map-field index served from it) and changed the free cadence
(ADR-023). One both-family sync pass on replayed real data: **169 MB → 57 MB** (package) →
**31.41 MB** (round 1) → **21.24 MB** (round 2), 21.28 MB re-measured by the verifier; the
demo pass 186.8 → 11.18 MB. Monthly background egress on the default free profile: **2.01
GB (40 % of Neon Free)**, about 20 CU-hours (arithmetic: 0.25 CU, 5 minutes after the last
statement); a quiet site adds about 0.40 GB; steady traffic (1,000 views a day) would need
5.37 GB in all and does not fit. Migration 0008 records one publication of the data an
upgraded database already holds, so the first check after an upgrade does not re-sync
everything.
**Consequences.** Signed-in visitors' session reads and uncached visitor URLs were not
measured and could keep the compute awake (720 h × 0.25 CU = 180 CU-hours); Neon Launch is
the upgrade path, with the full-retention profile.

## ADR-027 (D7) Upstash Redis through KV_URL; no Celery broker when hosted

**Decision.** Redis holds rate limits, Open-Meteo budgets, job locks, the daemon's
heartbeat, known runs and kick flags (and, since round 1, the public snapshot cache);
`KV_URL` is accepted as an alias of `REDIS_URL`; hosted timeouts 1.5 s connect, 2 s socket;
no provider response cache when hosted.
**As built.** Measured per idle check 3 commands, per kick poll one EVAL, per job run 4 (the
job lock and the CPU counter; 7 for data health): about 79,000 commands a month for the
daemon (proposal: about 66,000; first counted as about 68,000 without the CPU counter,
corrected at the final review); about 386,000 (77 % of 500K) with steady visitors. On
2026-10-22 the Marketplace page showed only the REST variables, so deploy-vercel.md says how
to set `REDIS_URL` by hand. Under the daemon a job runs without its Redis lock when Redis
does not answer (Celery still fails).

## ADR-028 (D8) Map fields stay in Postgres, behind the CDN

**Decision.** Two retained map runs (about 71 MB each with partitions); a field URL that
names its run and valid time is immutable at the edge; binary cursor reads.
**As built.** Field with run: `public, max-age=3600, immutable` and CDN `s-maxage=31536000,
immutable` (as proposed); without run: 300 s. The index and every other public GET:
`public, max-age=60` and CDN `s-maxage=300, stale-while-revalidate=3600` (proposed 60 and
600), so the edge switches the index, the conditions and the spot list together after a
publication. The index is served from the public snapshot cache, so a CDN refresh every 5
minutes no longer wakes the database. Binary reads halve a field's egress (hex text was
2.06× the stored size).

## ADR-029 (D9) Migrations at build time, and init-db in the daemon

**Decision.** The api's build command is `python -m app.cli migrate --on-build`
(`MIGRATE_ON_BUILD=production`: production builds only, through the direct URL); the daemon
runs `init-db` at every start.
**As built.** Verified in the Python builder (2026-10-22): the build command runs after the
dependencies are installed, migrated an empty database ("status": "migrated") and fails the
build on a non-zero exit. The schema now has 8 migrations; 0008 is data only (ADR-026).
**Consequences.** Without Vercel's system variables exposed to the build it prints
`skipped`, and the daemon's `init-db` migrates.

## ADR-030 (D10) Monitoring without logs

**Decision.** `sync_runs` (per-stage status, duration, `cpu_s`), job logs, the Redis
heartbeat, a sync block in `/api/health/data` and `/status`, a daily Vercel Cron watchdog
(`23 7 * * *`) and `SYNC_CPU_BUDGET_HOURS`.
**As built.** New problem keys `sync_heartbeat_stale` (failing), `sync_failed`,
`sync_cpu_budget` (above 80 %) and `licence` (failing). The month's CPU is the larger of the
job logs and a Redis counter (`jobs:cpu_s:YYYY-MM`), plus the daemon's own. Added:
`OPS_ALERT_EMAILS`, emailed by the watchdog at most once a day when the database itself is
unreachable; the plain health report is shared by the CDN for 60 s. Verified in the local
stack: cron 200 with the token, 401 wrong or missing, 404 without `CRON_SECRET`.

## ADR-031 (D11) The wave layer: a metre-anchored colour field and crest dashes

**Context.** The owner's screenshot: a smooth blue → violet → magenta/pink field with short,
soft white dashes drifting with the waves.
**Decision.** Wave Height gets its own ramp anchored in metres; the traced crest sets are
replaced by dashes snapped to the crest lattice `p·W = n·s + v·t`, moving at the phase speed
on the stated time scale; the combined sea by default, partitions selectable; settings key
v2.
**As built (differs from the proposal).** The ramp was re-anchored at integration (the first
one read electric blue for typical 2-3 m seas): 0 m #151d42 to 10 m #f3c4dc, L* 12.2 → 83.8,
neighbouring anchors ΔE ≥ 7.6, chroma ≤ 62. Each dash is one soft blob, a light tint of the
field under it, with its leading edge on the crest and a 0.55 s smear behind it, drawn as
one gradient quad (not white strokes with a halo); secondary swell split amber bars, wind
sea short cyan blobs. Cells 12 px (combined, primary), 13 (secondary), 18 (wind sea), not
26/34/30; length along the crest clamp(4 + 0.7 Hs, 4.5, 7.2) px, not up to 22. At the final
review the blobs were turned to the reference's proportions (about 5 px along the crest by 7
along the travel; until then 1.5 times longer along the crest than deep, clamp(6 + 0.8 Hs,
6.5, 10) px long), the tint became an even lift of every channel (`TINT_LIFT` 95, headroom
0.02; 150 and 0.1 greyed the blobs), and the opacity became clamp(0.40 + 0.14 Hs, 0.40,
0.95) instead of D11's clamp(0.40 + 0.09 Hs, 0.40, 0.90): the smear (speed x time) no longer
grows with the height, and the steeper opacity keeps D11's rule of more than twice the ink
at 4 m as at 1 m (2.14-2.22× per dash in e2e). The vertex budget became a device-adaptive
quad budget (1,500-7,000 a frame, median target 2.5 ms; at most 8,000 dashes, 3,000 on
phones). Land on the dark basemap is the reference's mid grey #808080 with a dark coastline
(proposed #3a3a3c with a light one; #737376 until the final review). Kept: the lattice,
spacing clamp(0.12 px/m × L, 8, 64), τ = 4, a life of 3 crest passages, the combined-sea
default, `swell.map.settings.v2`. Measured (headless Chromium, software rendering): e2e draw
p95 2.8 ms (round 1), 3.1 and 3.4 ms (2026-10-23), within D11's 5 ms; ink per dash at 4 m
2.25× that at 1 m (before the final review); density 2.73-3.73 dashes per 1,000 CSS px² on a
Pixel 7 profile and 1.95-2.58 on a 1440×900 desktop held by the budget, against 4.05 in the
reference.
**Consequences.** Not measured on a GPU-backed browser or a real phone; Firefox and WebKit
not run.

## ADR-032 (D12) Local testing in two levels

**Decision.** Level 1, no account: `docker-compose.vercel.yml` driven by
`scripts/vercel-local.sh` (Caddy edge on 3100 with Vercel's client-IP headers, the lean API,
Next.js with `API_ROUTING=platform`, PgBouncer in transaction mode, Redis, the daemon from
the same `Containerfile`, Mailpit, a cron helper). Level 2: the Vercel CLI (`vercel build`
offline, `vercel dev -L`).
**As built.** `smoke` = 19 shell checks and 6 Playwright tests, passed on 2026-10-23 with demo
and with live data. CLI 64.5.0 refuses daemons, so `scripts/vercel-build-check.sh` removes
them from a temporary copy and builds again; `vercel dev -L` serves both services only
while the checkout is unlinked.

## ADR-033 (D13) The paywall is a configuration change of the hosting

**Decision.** Before any payment is requested: Vercel Pro; `COMMERCIAL_USE=true` with
`OPEN_METEO_API_KEY` (the Open-Meteo providers refuse the free hosts otherwise, before any
request, and health reports `licence`); Neon Launch with the full-retention profile;
Upstash off Free.
**As built.** As designed; profiles in `backend/app/core/profiles.py` (`hosted`,
`free-database`, `full-retention`, the last equal to the Settings defaults). The paywall's
own product (a payment provider, plans, access control) is not built.

## ADR-034 (D14) The classic stack keeps Celery, synced too

**Decision.** Beat's acquisition, map, ensemble, prediction safety-net and detection entries
are replaced by one `sync-tick` entry (`sync.tick`, bulk queue) running the same
orchestrator; beat start queues it.
**As built.** `sync.tick` has its own time limits (3600 s hard, 3300 s soft) and the broker
visibility timeout is 5400 s, so a running sync is never handed to a second worker. The
messaging tasks ignore their results and use a 2 s broker timeout: a `.delay()` with the
broker refusing waited 0.71-0.73 s instead of 19 s. Verified 2026-10-23: beat start ran
`sync.tick`, sync 1 succeeded in 31.1 s on demo data. An optional compose profile `sync`
runs the daemon instead of worker and beat.

## ADR-035 (D15) GitHub Actions for CI and a manual-only data-sync workflow

**Decision.** `.github/workflows/data-sync.yml` runs `sync run --trigger github` by hand
(or by `repository_dispatch`); no schedule (GitHub's terms on serverless use).
**As built.** It installs the `sync` extra from `uv.lock`; credentials only from secrets;
`backend/tests/unit/test_vercel_deploy.py` asserts that no schedule is committed. CI gained
a `deploy-config` job (actionlint, compose files, Docker builds of the `Containerfile` and
`backend/Dockerfile`) and a `vercel-build` job. The steps were first run locally; all five
jobs have run on GitHub since 2026-10-23.

## ADR-036 (D16) The register of behaviour and test changes

**As built.** The changes listed in the design landed with their tests (beat schedule,
heartbeat source, publish gate, CDN headers, inline dispatch, client IP header, URL
normalisation and `KV_URL`, publish in `run_pipeline`, per-pass prune, the extras, the wave
renderer, settings v2, `API_ROUTING`, the status page). No assertion was weakened; where a
measured value moved, the commit says why. Test counts at the final tree: unit 2,040
passed (4 skipped without a lean venv; 27 of 27 with one), integration 503 (3 skipped
without PgBouncer; 3 of 3 with it), Vitest 486, e2e 127 (Chromium and Pixel 7), against
1,689, 318, 409 and 127 at the end of Upgrade 3.

## ADR-037 Security patches inside the two-week dependency rule

**Context.** `exclude-newer` keeps releases younger than its date out of `uv.lock`, and the
frontend follows the same rule by hand. On 2026-10-23 `next` 16.4.0 had two critical
advisories (GHSA-2464-fhr4-w9xw, GHSA-673q-mjm6-r5xr) and a high one (GHSA-p3m5-59mp-jv5r),
and certifi 2026.7.22 a critical one (GHSA-6q48-6c7w-9gw2).
**Decision.** Take the oldest patched release even when it is younger than two weeks:
Next.js and eslint-config-next **16.4.1** (published 2026-10-14, 9 days old), certifi
**2026.10.19** through a per-package `exclude-newer-package` entry that names the advisory.
**Consequences.** `npm audit --omit=dev` 0 vulnerabilities, pip-audit none known. Drop the
certifi entry once `exclude-newer` passes 2026-10-21; consider Next.js 16.4.2 once it is two
weeks old (2026-10-29). Dev-only advisories in vitest 3.2.7 remain (the fix is vitest 5, a
major upgrade).
**Later (final review, 2026-10-24).** GitHub reviewed GHSA-mrhx-c7xc-m3w5 (high: zod below
4.6.6 can exhaust memory while validating oversized constrained collections) on 2026-10-23
at 18:26 UTC, after the audit above. zod runs only in client components (the forms'
validation), so only a visitor's own browser was exposed. By the same rule, zod **4.6.6**
(published 2026-10-11, 12 days old) replaced 4.6.5. `npm audit --omit=dev` then reports no
high or critical advisory and one moderate one, fast-uri below 3.1.9 (GHSA-w74x-v6f9-j3xg,
GHSA-xhq7-xmqj-769h, GHSA-wjr9-j795-jxcr, GHSA-333x-w4r6-6crp; 3.1.9 was published
2026-10-19), a dependency of ajv under @hookform/resolvers; not fixed in this review.

# Swell Travel Agent 2.0 (2026-10): forecasts that follow the models, versions, targeted flights

The owner's master prompt of 2026-10-26 (spec sections 1-37). The design proposed decisions
D1-D16 (docs/architecture_audit.md, section 4, with the alternatives and costs considered);
they are recorded here as built, after integration and four review rounds (2026-10-29).
Status, context, decision and consequences for each; what was measured is in
docs/UPGRADE_REPORT.md, sections 49-50, and docs/architecture_audit.md, section 8.

## ADR-038 (D1) Keep FastAPI, SQLAlchemy 2, Alembic, Neon, Upstash and the existing auth

**Status.** Accepted, built.
**Context.** The spec prefers Next.js server code but says not to migrate unnecessarily.
The backend is about 54k lines of Python, much of it numeric, with 41k lines of tests;
Vercel deploys FastAPI as one Python function in Services mode (Python generally available
on Vercel since 2026-10-15).
**Decision.** No migration. The spec's TypeScript layout (`lib/weather/providers/...`) maps
to `backend/app/services/forecasts/*` (docs/architecture_audit.md, 4.11). Anything the API
or the refresh path imports stays lean (no pandas, scikit-learn, scipy, pyarrow, joblib or
ecCodes at module level).
**Consequences.** The API function measured 128.3 MiB of Vercel's 500 MB Python limit in the
offline build, with `maxDuration` 300 s; a cold `import app.main` took 1.47-1.80 s in a lean
install; `tests/unit/test_lean_imports.py` (32 tests with `LEAN_PYTHON`) runs a demo
spot-only refresh in the lean install. Vercel Services is still a Beta.

## ADR-039 (D2) One weather provider, chosen by WEATHER_PROVIDER

**Status.** Accepted, built.
**Context.** Spec section 5: Windy preferred once usable, Open-Meteo now and as fallback,
both behind one interface, configured by environment only.
**Decision.** `WEATHER_PROVIDER` = `auto` (default) / `open_meteo` / `windy` / `demo`.
`auto` selects Windy only when `WINDY_API_KEY` is set **and** `WINDY_STORAGE_AUTHORIZED=true`,
else Open-Meteo; production refuses `windy` while it is not eligible. `FORECAST_PROVIDERS`
stays as an advanced explicit override. `.env.example`, Docker Compose and the tests set
`demo`; the hosted profile sets `auto`. `python -m app.cli forecast provider-check` prints
the selection, its reasons and a live health check, and stores nothing.
**Consequences.** Without a Windy licence `auto` stays on Open-Meteo, by design. Measured:
the provider check with Windy unconfigured reported `not_configured` with 0 requests; with
Open-Meteo it read one `meta.json`.

## ADR-040 (D3) Windy only with a written storage licence; testing data never

**Status.** Accepted, built; not exercised with a real key.
**Context.** Windy's standard terms forbid storing the data, derived databases and
redistribution; the Testing tier "Returns randomly shuffled and slightly modified data"; the
Point Forecast response has no model-run field and Windy documents no webhook or metadata
endpoint (https://api.windy.com/point-forecast/docs, read 2026-10-27).
**Decision.** `WINDY_STORAGE_AUTHORIZED` gates every store, score and publication of Windy
data. Every response is checked for the testing warning; testing data fail the refresh with
`windy_testing_key` and are never stored. One `gfs` and one `gfsWave` request per spot (100
for a full refresh of 50 spots, 50 for one component). New runs are found by a per-model
sentinel fingerprint every `WINDY_CHECK_MINUTES` inside each cycle's expected window; run
times are labelled `estimated`. A daily counter (`WINDY_DAILY_REQUEST_LIMIT`, reset 00:00
UTC). Attribution "Contains data from the Windy database"; the logo is an owner step.
**Consequences.** Activation is configuration only (docs/WINDY_API_SETUP.md). Open: Windy's
real delay after a cycle, whether paid responses are deterministic (fingerprints need it),
the body of a quota error, the price of a storage licence.

## ADR-041 (D4) A provider manager with health checks and a runtime fallback

**Status.** Accepted, built.
**Context.** Spec section 35.4: Open-Meteo is the fallback.
**Decision.** `provider_manager.py`: `active_provider()`, `health_check()`, `fallback_for()`.
When Windy is primary and a refresh fails with a credential, testing-key, quota or
availability error, the same refresh runs on Open-Meteo at once; the version records
`provider` `open_meteo` and `fallback_from` `windy`; the fallback holds 60 minutes (a quota
until 00:00 UTC); 3 transient failures in a row count as unavailable. One primary provider at
a time, no cross-referencing.
**Consequences.** Consecutive versions can come from different providers with different
steps (Windy 3-hourly, GFS hourly to 120 h); each version names its provider and the admins
get `provider_fallback`. Tested with recorded and synthetic responses
(`tests/integration/test_refresh_fallback.py`).

## ADR-042 (D5) One model family by default; the rest kept but off; 10 days

**Status.** Accepted, built.
**Context.** Spec section 2: "Do NOT ... repeatedly cross-reference numerous weather
providers"; non-negotiable 8: a rolling 10-day horizon.
**Decision.** Every profile uses one family: Open-Meteo NOAA GFS + GFS-Wave. Kept and
switchable, off by default: the ECMWF family and the blend, GEFS-Wave, the direct NOAA and
ECMWF GRIB providers, ML corrections (`ML_CORRECTIONS_ENABLED=false`) and the prediction
archive (`PREDICTION_ARCHIVE_ENABLED=false`). `FORECAST_DAYS` 10 (at most 16). No code was
deleted.
**Consequences.** Measured live: 125 weighted calls for a refresh of both components and 75
for a wave-only refresh (Upgrade 3's two-family schedule used about 820 a day; 2.0 about
500-650, arithmetic). Forecast confidence loses the independent-family disagreement input.
With the blend switched on, the family-level syncs of Upgrade 4 apply instead of ADR-043.

## ADR-043 (D6) Wave and atmosphere refresh independently; the other is carried

**Status.** Accepted, built; revised in review rounds 1-3.
**Context.** Spec section 10: a new wind run must update the wind and keep the waves with
their original model time. GFS-Wave and the two GFS atmosphere domains were measured
46-107 minutes apart for one cycle.
**Decision.** Each spot source has two components with their own run keys, availability and
detection state; a component is due when one of its domains advanced (monotonically: a
lagging server never takes the site back) and is servable. A refresh fetches only the due
component and carries the other from the spot's served run in the database
(`carry.carry_component`), with its own run time; an hour the carried run lacks is deleted,
never published half filled; every spot is validated (`FORECAST_MIN_VALID_FRACTION` 0.8,
physical ranges). Scores are recomputed for the whole new run.
**Consequences.** When a spot's served run ends more than 3 h before the provider's horizon
the carried component is fetched again **from the same run**, so the 10-day horizon keeps
rolling: a deliberate trade-off against spec section 8 ("no queries for unchanged runs"),
on about one or two refreshes a day (up to about 150 weighted calls a day). That refetch is
best effort: a failure never holds back the due component (review r3). Measured live: a
natural wave-only refresh of the 00Z GFS-Wave run kept the wind with its 18Z time. An
atmosphere-only refresh was never observed live (Open-Meteo's servers disagreed about the
GFS 0.13 run for over 36 minutes; three refreshes skipped it safely).

## ADR-044 (D7) Versioned publication: stage, then publish in one transaction

**Status.** Accepted, built.
**Context.** Spec section 11: a unique id per published forecast, never in-progress data in
the published snapshot. Before 2.0, spot pages could serve runs that were never published.
**Decision.** Table `forecast_versions` (migration 0009): `staging`, `published`,
`superseded`, `failed`; per-spot `spot_runs`, per-component `components` with each model run,
counts and the detection-to-publication times. A refresh stages a version after
acquisition, scoring and validation and publishes it in one transaction under an advisory
lock (the previous one superseded); a unique partial index allows one published version; a
version with zero updated spots is refused. Public reads serve only the published version
(`versions.serving_runs`); before the first version the Upgrade 4 selection is used. Prune
keeps every run the published and the previous published version reference.
**Consequences.** On the local Vercel-style stack 17 versions were published with always
exactly one published. A run that dies between publication and the Redis marker rewrites
the marker at the next sync (review r1).

## ADR-045 (D8) The refresh runs in the API function, called by a scheduler; the daemon is optional

**Status.** Accepted, built; the scheduler itself not deployed (no account).
**Context.** Vercel Hobby cron runs once a day (a sub-daily expression fails the
deployment); the Hobby daemon quota fell to 5 Active CPU-hours a month, below the Upgrade 4
estimate of 4.4-7.9; Daemons are a production-only Public Beta; spec section 13 prefers
scheduler → secure endpoint (https://vercel.com/docs/cron-jobs/usage-and-pricing,
https://vercel.com/docs/daemons/limits, read 2026-10-27).
**Decision.** `GET|POST /api/internal/refresh` (Bearer `CRON_SECRET`, constant-time compare,
404 without the secret): the kill switch `FORECAST_REFRESH_ENABLED`, the Postgres lease
(`busy` answers 200), a free metadata plan and, when due, a spot-only sync inside the
function's 300 s. Called every 5 minutes by Upstash QStash on Hobby (288 of the free 1,000
messages a day) or a Vercel Cron entry on Pro; the daily cron runs one more check as a
backstop. The daemon (`SYNC_DAEMON_KINDS=map`) refreshes only the optional map layers.
Without it, `app/services/sync/standin.py` runs its other work: the refresh endpoint takes
the API's kicks, due email retries and the hourly data-health check; the daily cron runs the
overdue periodic jobs; set `MAP_FIELDS_ENABLED=false`. The api build command migrates and
seeds the reference data.
**Consequences.** Measured outside Vercel: 401 without or with a wrong token, two concurrent
calls gave one `busy` (71 ms) and one success; a live refresh of both components 38.7 s wall
(12.1 s of refresh); a check with nothing new 1.2-1.7 s. Not verified: the QStash schedule,
cron-job.org (it closes the connection after 30 s), whether Vercel's api build can read
`../data` (a build that cannot fails on an empty database), and a silent scheduler is
noticed without the daemon only by the daily cron (up to about 25 hours).

## ADR-046 (D9) Partial failures: publish the good spots, keep and retry the others

**Status.** Accepted, built.
**Context.** Spec section 14: 47 of 50 spots succeed, 3 fail.
**Decision.** A spot whose data failed keeps its previous run in the new version, `stale`
with its redacted error; the sync is `partial`; only those spots are retried, 5, 15 and 45
minutes later, at most `SYNC_MAX_ATTEMPTS`; a newer run supersedes the retry. A whole
component that fails publishes nothing.
**Consequences.** Open-Meteo answers all 50 spots in one request, so a failed request fails
every spot and becomes a component retry. A failed request's responses and weighted calls
are still counted in the run's stats (review r4).

## ADR-047 (D10) Staleness: the model's interval plus a configurable grace

**Status.** Accepted, built.
**Context.** Spec sections 5 and 14: a configurable threshold, "Forecast last updated 7
hours ago", never stale shown as current.
**Decision.** A spot is stale when a component is older than its model's update interval
(Open-Meteo's `update_interval_seconds`, 6 h for GFS; Windy 6 h) plus
`FORECAST_STALE_THRESHOLD_MINUTES` (360; per model `FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL`),
or when its latest refresh failed while other spots updated. The per-source run staleness of
ADR-015 stays for the confidence cap and the alert pause.
**Consequences.** Two separate rules, shown separately on the pages; an open tab re-reads the
public data once an hour so staleness by age reaches it without a new version (review r1).

## ADR-048 (D11) Live updates by polling a small, CDN-shared version endpoint

**Status.** Accepted, built; the CDN not verified.
**Context.** Spec section 12: open pages update by themselves within about 60 s of a
publication; a version-check endpoint is acceptable. WebSockets on Vercel close at the
function's maximum duration (300 s on Hobby).
**Decision.** `GET /api/forecasts/version` answers from Redis or a 5-second memo (one
database read re-seeds a missing marker), CDN `s-maxage` 15. A visible tab polls it every 20
s (`NEXT_PUBLIC_FORECAST_VERSION_POLL_MS`) and at once when it becomes visible; a new id
re-reads the public queries under `?v=<version_id>` URLs, keeps the selected spot and time,
updates "Forecast updated hh:mm" and announces it. A request naming another version answers
`no-store`. Latency is measured by a beacon (`POST /api/forecasts/seen`) and shown to admins.
**Consequences.** Measured: from publication to new data on screen p50 13.8 s on the local
stack at the 20 s poll (16 samples, no CDN); 3.4-3.9 s in the e2e suite with a 2 s poll; at
most one Redis GET per poll per process every 5 s. Upstash Free can be exceeded with steady
traffic and a tab open in a CDN region all month (pay-as-you-go about $1 a month,
arithmetic).

## ADR-049 (D12) Admin freshness monitoring and admin-only alerts

**Status.** Accepted, built.
**Context.** Spec section 16; users must never get admin failure emails.
**Decision.** `GET /api/admin/freshness` and `/admin/freshness` (admins only, never cached):
provider and fallback, each component's model run, detection, acquisition, scoring and
publication times, stale spots, failed requests, API usage (Open-Meteo, Windy, flights,
emails), processing times, upstream-to-publication and publication-to-browser latency, the
last refresh jobs. New admin problems: `model_run_late`, `forecast_stale_spots`,
`refresh_failures`, `provider_credentials` (also refused flight and email keys),
`provider_unavailable`, `provider_fallback`, `quota_near`, `refresh_scheduler_silent`,
emailed to verified admins only; an email key the provider refuses is reported by SMS to
admins who opted in, else on the dashboard. The public `/status` and `/api/health/data`
stay.
**Consequences.** Neon, Upstash and Vercel usage are still read in their own consoles.

## ADR-050 (D13) Scoring 2.2.0: structured reasons, two separate confidences

**Status.** Accepted, built.
**Context.** Spec sections 18-20: deterministic, research-driven scoring; research and
forecast confidence never combined.
**Decision.** Each hour gains `favourable` / `unfavourable` reasons, `offshore_class`,
`breaking_height` where defensible, `research_confidence`, `forecast_confidence` (a labelled
heuristic) and `model_version`; weights configurable (`SCORING_WEIGHTS_JSON`); light onshore
below about 8.6 km/h is capped at "workable"; the combined local confidence is removed from
the API, the UI and alerts.
**Consequences.** Opposing-coastline tests prove the same wind scores opposite at
ocean-beach-sf and cloud-9. The weights remain reasoned defaults, not fitted (no nearshore
observations).

## ADR-051 (D14) Spot research documents generated from quoted claims only

**Status.** Accepted, built.
**Context.** Spec section 17: researched conditions per spot, no fabricated values.
**Decision.** `data/surf_spots/research_conditions.json` holds the section 17 fields, each
value citing claim ids of the existing evidence; `scripts/generate_spot_research_docs.py`
writes `docs/surf_spot_research/<slug>.md` for all 50 spots; a field without a quote reads
"not documented"; a unit test fails when a document is out of date.
**Consequences.** Many fields read "not documented"; 15 spots are Low overall, none High
(research methodology v1.2).

## ADR-052 (D15) Targeted, deduplicated flight searches; per-search lead times

**Status.** Accepted, built; real providers untested.
**Context.** Spec sections 26-28: flights only when necessary, never every route for every
user; short-notice markets first.
**Decision.** Searches only for a new match, a material change, an explicit "Re-check
flights" and right before an alert; no flight timer. One shared cache per trip until the
earlier of `FLIGHT_CACHE_TTL_HOURS` and the first offer's expiry; `FLIGHT_USER_DAILY_QUOTA`
(20) with round-robin across users; every origin's first-choice date within the per-match
budget; up to 3 layovers honoured; per-search alert lead times (2-10 days by default) and a
3-7 day short-notice boost. The labelled demo provider is the default; Duffel's agreement
forbids metasearch use, so production needs its written confirmation; Amadeus is Enterprise
only.
**Consequences.** An unchanged forecast never searches again (tested). With Duffel and no
orders, about $37.50 a month at the 300-a-day quota (arithmetic on its 1500:1 search fee).
Small regional airports fall back to the spot's international gateway when they return no
offers.

## ADR-053 (D16) Cost: one provider, free checks, one-component refreshes

**Status.** Accepted, built.
**Context.** Spec section 30: minimal recurring cost, no production LLM calls.
**Decision.** One provider, metadata-only detection, one-component refreshes, a 10-day
horizon, spot-only refreshes in the function, version polling only while a tab is visible,
deterministic scoring.
**Consequences.** Measured unit costs: 125 / 75 weighted calls per refresh (about 5-6.5 % of
Open-Meteo's free daily limit a day); 6.3-7.5 MB of database egress per demo refresh and 0
bytes for a check with nothing new; 288 QStash messages a day. The monthly budgets in
docs/API_SETUP_REQUIRED.md 1.4 are arithmetic on those, not measured on Vercel, Neon or
Upstash. Hobby and the free Open-Meteo API are non-commercial: a paywall needs Vercel Pro and
an Open-Meteo plan first.
