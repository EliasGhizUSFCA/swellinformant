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
**Consequences.** All 50 spots currently run on estimated profiles, shown on the spot page.

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

## ADR-014 Research confidence and forecast confidence are separate assessments

**Context.** Two different questions get called "confidence": how well a break's ideal
conditions are documented (research confidence, a property of the spot that changes when
evidence is added) and how trustworthy an upcoming prediction is (a property of each forecast
hour that changes with every model run). A single number would hide which one is weak: a
famous, well-documented break can have an untrustworthy 9-day forecast, and an excellent
2-day forecast says little about a break nobody has documented.
**Decision.** Three separate classifications, stored separately and shown separately, none of
them a calibrated probability: research confidence (`spot_research_assessments`, methodology
research-v1, research-v1.1 since ADR-016, docs/research-confidence.md); forecast confidence of the offshore forecast and
local confidence of the breaking-wave estimate (`surf_quality_predictions`, methodology
forecast-v1, docs/forecast-confidence.md). The only link is documented: local confidence =
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
have made three spots (Jaws, Sunset Beach, The Box) High overall.
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
**Consequences.** No spot is High until some evidence is read directly (live: 18 Moderate,
32 Low). The methodology change adds one assessment row per spot and keeps the research-v1
rows. Local confidence, which uses the research basis, is at most Moderate everywhere for
now.

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
