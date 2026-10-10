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
**Consequences.** ~10–15 MB per forecast step for all spots, an ecCodes dependency (binary
wheel), and file-layout risk at the GFSv17 upgrade: paths come from settings.

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
