# Verification, backtesting and the ML post-processing pipeline

This document describes how Swell verifies offshore wave forecasts against buoys and how a
statistical correction of the offshore significant wave height is trained, judged and
deployed. Code: `backend/app/services/verification/` and `backend/app/services/ml/`.

**Scope.** Everything here concerns the *offshore* significant wave height Hm0 (and Tp
and direction for verification) at buoy positions. It is not breaking (surf) height.
Breaking height is still derived by the nearshore model (`surf_quality/`) from the
offshore values. A correction changes the offshore input to that model, not its output.

## Data flow

```
point_forecasts (model at a buoy, per cycle and lead)   buoy_observations (measured)
            \                                              /
             +---- verification.pairing -----------------+
                           |
               prediction_validation (one row per pair and variable)
                           |
      verification.jobs ---+--- forecast_accuracy_metrics (30/90-day windows)
                           |
      ml.dataset -> ml.models (fit / select / test) -> ml.registry (decision, artifact)
                                                              |
                                        ml.apply.load_active_correction -> prediction job
```

The pipeline only reads the tables `point_forecasts`, `buoy_stations` and
`buoy_observations`, which the observation and point-forecast collectors fill. It
writes `prediction_validation`, `forecast_accuracy_metrics`, `ml_experiments` and
`ml_model_versions`.

## Pairing (`verification/pairing.py`)

* Each point forecast is matched with the observation of the **same station** nearest to
  its valid time, within **30 minutes**. On an exact tie the earlier observation wins.
* Observations with `qc_flag = 'bad'` are never used. The nearest observation is chosen
  separately for each variable, among observations that report it.
* **Like with like.** Model Hs is a spectral Hm0. Every Hs pair stores the observed
  height definition in `observed_kind`:
  * `spectral_hm0`;
  * `h_third` (time-domain H1/3);
  * `unknown`.

  The metrics keep these apart. Variables `hs`, `hs_h13` and `hs_unknown` are never
  pooled.
* **Tp** is paired only when both sides are peak periods. `buoy_observations.tp_s` is a
  peak period by schema. The forecast's period kind comes from
  `point_forecasts.extra.period_kind`, or else from `SOURCE_CONVENTIONS` (GFS-Wave and
  GEFS-Wave PERPW is Tp; ECMWF must declare it). Mean periods (`tm01`, `tm02`, `tm10`)
  are skipped and counted.
* **Direction** compares like kinds: peak with peak, mean with mean
  (`extra.direction_kind`). The error is the signed circular difference wrapped to
  (-180, 180]. Everything is in nautical degrees FROM, and a "going to" source must be
  converted (+180) before it is stored.
* **Incremental and idempotent.**
  * By default only forecasts in the 14-day look-back that have no raw `hs` pair yet are
    processed. Observations that arrive late are picked up on a later run.
  * `verify pair --full` reprocesses everything.
  * The pairs of each processed forecast are deleted and rewritten in one transaction,
    with one commit per station. A re-run therefore yields the same rows, and an
    observation later flagged `bad` drops out.
* Forecasts whose valid time is in the future are never paired.

## Lead buckets (`verification/metrics.py`)

| bucket L (h) | 24 | 48 | 72 | 120 | 168 | 240 |
|---|---|---|---|---|---|---|
| member leads (h) | 18–30 | 42–54 | 66–78 | 108–132 | 156–180 | 228–252 |

* A lead `h` is in bucket `L` when `|h − L| ≤ 6 h` for `L ≤ 72`, and `|h − L| ≤ 12 h`
  beyond. The buckets do not overlap.
* The pooled bucket (`lead_bucket_h = −1`, shown as "all") holds every lead ≥ 3 h.
* Leads below 3 h are analysis-time rows. The observation at the cycle time is already
  known when they are issued, so they are paired and stored but never pooled with
  forecasts or used as training targets.
* The same definition is used by the metrics job, the backtest, the training scores and
  deployment criterion (c).
* The buckets leave gaps. For the archived bulletin leads (hourly to 120 h, then 3-hourly
  to 384 h), 132 of the 206 leads lie in no bucket; they appear only in "all", and
  criterion (c) checks them as one extra group.

## Metrics (`verification/metrics.py`)

Notation: `m` is the model, `o` the observation, and sums run over the N valid pairs.
Pairs with any non-finite value are ignored.

| score | formula |
|---|---|
| bias | mean(m − o) |
| MAE | mean(\|m − o\|) |
| RMSE | sqrt(mean((m − o)²)) |
| NRMSE | sqrt(Σ(m − o)² / Σo²) |
| SI (Mentaschi) | sqrt(Σ((m − m̄) − (o − ō))² / Σo²) |
| HH (Hanna–Heinold) | sqrt(Σ(m − o)² / Σ(m·o)) |
| CC | Pearson correlation |
| skill vs reference | 1 − MSE(m, o) / MSE(ref, o), on rows where the reference exists |
| circular bias | atan2(mean sin(m − o), mean cos(m − o)) |
| circular MAE / RMSE | mean / RMS of wrap(m − o) to (−180, 180] |
| CRPS, normal | σ [z(2Φ(z) − 1) + 2φ(z) − 1/√π], with z = (y − μ)/σ (Gneiting et al. 2005) |
| CRPS, log-normal | y(2Φ(z) − 1) − 2e^{μ+σ²/2}[Φ(z − σ) + Φ(σ/√2) − 1], with z = (ln y − μ)/σ (Baran & Lerch 2015) |
| interval coverage / width | fraction of o in [lower, upper]; mean(upper − lower) |
| events (value ≥ threshold) | POD = H/(H+M), FAR = F/(H+F), CSI = H/(H+M+F), frequency bias = (H+F)/(H+M), plus the base rate |

Notes on these scores:

* **RMSE, NRMSE and SI favour negatively biased models** (Mentaschi et al. 2013, Ocean
  Modelling 72:53–58). Shrinking a noisy forecast lowers its RMSE. Bias and HH are
  therefore always reported next to them.
* A skill score is reported only when at least 30 rows have the reference
  (`MIN_REFERENCE_PAIRS`). The reference sample size `n_<ref>` is always reported.
* The CRPS closed forms are checked against numerical integration in the unit tests.

**Confidence intervals.** These come from a block bootstrap that resamples whole forecast
cycles, not rows. Errors within one cycle are strongly correlated across leads and
stations, so resampling rows would understate the uncertainty. Each replicate draws as
many cycles as there are, with replacement. The interval is the percentile interval
(90 %) of the replicates. A seeded `numpy.random.Generator` makes it reproducible.

## References

* **Raw**: the unmodified model Hs. A correction must beat it.
* **Persistence**: the observation at or before the cycle time (at most 1 h old, same
  height kind), carried forward to every valid time of that cycle. It uses only
  information available when the forecast is issued.
* **Climatology**: the station's monthly mean observed Hs.
  * It is fitted on the **training rows only**: each fold's training set in a backtest,
    train or train + validation for the ML split. The scheduled job uses observations
    before the start of its 90-day window.
  * Months with fewer than 24 observations have no climatology.
  * With less than a year of history most test months have no training month, so `n_clim`
    is small. Read the skill against climatology with its sample size.

## Dataset and features (`ml/dataset.py`, `ml/features.py`)

There is one row per (station, source, cycle_time, valid_time). By default the target
is a spectral Hm0 observation. `--hs-kind h_third` is an explicit opt-in, and versions
trained that way can never be activated (see below).

**Target.** A MOS-style residual in log space:

```
y = ln(max(obs_hs, 0.05 m)) − ln(max(model_hs, 0.05 m))
```

The corrected forecast is `model_hs × factor`.

**Features.** All of them are known at the cycle time.

| feature | definition |
|---|---|
| `lead_hours` | valid − cycle time |
| `model_hs_m`, `log_model_hs` | model Hm0 (m) and its floored log |
| `model_tp_s` | model **peak** period (NaN when the source's period is not Tp) |
| `model_dir_sin/cos` | model direction (FROM) on the unit circle |
| `swell1_hs_m`, `swell1_tp_s`, `swell1_dir_sin/cos` | primary swell partition |
| `wind_speed_kmh`, `wind_dir_sin/cos` | 10 m wind, when the source has it |
| `month_sin/cos`, `hour_sin/cos` | valid month and UTC hour on the unit circle |
| `persistence_hs_m` | observed Hs at or before the cycle time (≤ 1 h old) |
| `analysis_log_error` | ln(persistence obs) − ln(model Hs at lead 0 of the same cycle) |

* There is **no station identifier**, so a correction can transfer to spots that have no
  buoy.
* Directions are never used as raw degrees.
* Nothing is normalised, decomposed or imputed over the full series. Imputation and
  scaling happen inside the fitted pipelines, on training rows only.
* A feature with no value in any training row (e.g. wind for station bulletins) is left
  out of that model and recorded in `unused_features`.

**Mapping from `wave_forecasts`.** `features_from_wave_forecast` (re-exported as
`ml.apply.build_features`) builds the same features for the prediction job:

| `wave_forecasts` column | feature |
|---|---|
| `valid_time − cycle` | `lead_hours`, where cycle = `forecast_runs.wave_model_run_at`, else `issued_at` |
| `sig_wave_height_m` | `model_hs_m`, `log_model_hs` |
| `wave_period_s` | `model_tp_s`, only when `wave_period_kind = 'tp'` |
| `wave_direction_deg` | `model_dir_*` |
| `primary_swell_*` | `swell1_*` |
| `wind_*` | `wind_*` |

`persistence_hs_m` and `analysis_log_error` come from the paired station's observation
at or before the cycle time, supplied by the caller.

## Leakage guards and splits

* **Gap.** Consecutive sets are separated by at least **max lead + 3 days**. Errors are
  autocorrelated and one weather event appears in many cycles. Rows inside a gap are
  purged, not moved.
* **`assert_no_leakage(train, test, gap)`** raises `LeakageError` if any training row's
  valid time (its observation time) is later than `first test cycle − gap`. It runs:
  * on the train/validation boundary;
  * on (train + validation)/test;
  * before every refit;
  * in every rolling-origin fold;
  * in every leave-one-station-out run.

  Every guard that ran is recorded in `ml_experiments.dataset.leakage_guards`.
* **`assert_causal_features`** raises if a persistence or analysis observation was taken
  after the cycle time, or if a target observation was taken at or before it.
* **Chronological split.** The defaults are 60 / 20 / 20 % of the distinct cycles:
  * train = rows with valid time ≤ val_start − gap;
  * validation = cycles ≥ val_start with valid time ≤ test_start − gap;
  * test = cycles ≥ test_start.
* **Rolling origin** (expanding window): the cycles are cut into K + 1 blocks. Fold i
  tests on block i and trains on everything with valid time ≤ block start − gap.
* **Leave-one-station-out** (`--loso`): train on the other stations' development rows and
  test on the held-out station's test rows. Chronology and the gap are kept.

## Candidates and selection (`ml/models.py`)

| candidate | model |
|---|---|
| `raw` | identity (factor 1) |
| `linear` | ridge regression on the features plus lead², lead × ln Hs. Median imputation with indicators and standardisation, fitted on training rows only. Alpha from {0.1, 1, 10, 100} |
| `rf` | `RandomForestRegressor`, 80 trees, depth 12, leaf ∈ {10, 40} |
| `gbt` | `HistGradientBoostingRegressor`, 200 iterations, learning rate ∈ {0.05, 0.1}, leaves ∈ {15, 31} |

`persistence` and `climatology` are scored as references alongside the candidates.

**Procedure.**

1. For each candidate, every grid setting is fitted on **train** and scored on
   **validation**. The score is Hs RMSE in metres after the back-transform.
2. The best setting is refitted on train + validation.
3. The refitted model is evaluated **once** on **test**.

Rolling-origin and leave-one-station-out runs are reported for robustness only. They
never feed back into selection. Seeds are fixed (`SEED = 20261010`).

**Back-transform.**

```
corrected = model_hs × clamp(exp(ŷ) × s, 0.5, 2.0)
```

* `s = Σ(o·p)/Σp²` (with `p = model × exp(ŷ)`) is a single least-squares scale in metres.
  It is fitted on validation and clamped to [0.9, 1.1]. It is needed because exp of a
  log-space prediction estimates a median ratio.
* Rows outside the trained lead range, or missing a required feature, keep the raw value.
* A required feature is one present in ≥ 99 % of training rows, plus `lead_hours` and
  `model_hs_m`. The model never learned to do without these.

## Deployment criteria (`ml/registry.py`)

The criteria are evaluated on the held-out TEST period only. Each one is recorded with
its threshold, its value and pass/fail in `ml_model_versions.decision`.

| id | criterion | constant | why |
|---|---|---|---|
| a | Hs RMSE (m, after the back-transform) at least 5 % below raw over all test pairs | `MIN_RMSE_REDUCTION = 0.05` | a smaller gain is within run-to-run noise and not worth the risk |
| b | the 90 % cycle-block-bootstrap CI of RMSE(candidate) − RMSE(raw) lies entirely below 0 | `BOOTSTRAP_CONFIDENCE = 0.90` | the gain must hold across forecast cycles, not come from one lucky storm |
| c | no lead bucket with n ≥ 50 worse than raw by more than 3 %; the leads in no bucket (3–17, 31–41, 55–65, 79–107, 133–155, 181–227 and > 252 h, where the correction is still applied) are checked as one more group, `other_leads` | `MAX_BUCKET_DEGRADATION`, `BUCKET_MIN_N`, `OTHER_LEADS_GROUP` | users read forecasts at every lead; a better average must not hide a worse day 7 |
| d | beats or ties (within 1 %) the ridge baseline, unless it is the ridge baseline | `LINEAR_TIE_TOLERANCE` | complexity must pay for itself |
| e | at least 300 test pairs | `MIN_TEST_PAIRS` | fewer pairs give no meaningful verdict |
| f | HH not above raw | — | an extra guard, stricter than (a)–(e): RMSE rewards damped forecasts, HH does not |

**Statuses.**

* **ACTIVE** only when every criterion passed **and** activation was requested
  (`ml activate ID`, or `ml train --auto-activate`, which picks the passing candidate
  with the lowest *validation* RMSE).
* **REJECTED** when any criterion failed.
* **CANDIDATE** when every criterion passed but the version was not activated.

Activating a version retires the previous active version of the same name. A partial
unique index allows one active version per name.

**Which versions can be activated.** Only versions trained against spectral Hm0
observations. The correction multiplies model Hm0, so a model fitted to H1/3 would
silently change the height definition. Such versions are kept for evaluation.

**Applicability** (`ml_model_versions.applicability`):

* the training station codes;
* the spot slugs paired with them (`buoy_stations.metadata.paired_spots`) through an
  offshore relation (`offshore_reference` or `historical_reference`, see
  docs/observations.md). Spots paired as `upstream_regional` (a buoy hundreds of km
  away, timing and size context only) or `nearshore_reference` (a shallow buoy that
  already sees shoaled, refracted waves) are listed in `excluded_spots` and never
  corrected: model error learned there does not describe the spot's offshore point;
* the trained lead range;
* the sources, families and target height kind.

`Correction.applies_to(spot, lead)` is false outside it.

## Registry and artifacts

* **`ml_experiments`**: one row per training run. It records:
  * the dataset summary (stations, sources, period, row counts, features, feature
    coverage, the leakage guards that ran, a fingerprint);
  * the split;
  * the per-candidate, per-bucket scores on validation and test;
  * the rolling-origin and leave-one-station-out results;
  * every decision;
  * a conclusion written from the numbers.
* **`ml_model_versions`**: one row per candidate. The version number increments per name.
* **Artifacts.** Each is saved with joblib to `ML_ARTIFACT_DIR/<name>/v<version>.joblib`
  (written atomically) and its sha256 is recorded. Loading re-hashes the bytes and
  refuses a mismatch. Artifacts are only read from inside `ML_ARTIFACT_DIR`: a registry
  row whose `artifact_path` points elsewhere is refused even when the checksum matches.
  Model names must be plain identifiers (letters, digits, `_`, `.`, `-`; they become a
  directory name), and an imported manifest must name its artifact by a bare file name
  next to the manifest. Activation refuses an artifact that fails verification. If the
  active artifact cannot be loaded at all (checksum, location, or an unpickling error
  after a library upgrade), `load_active_correction` logs it and returns None, so
  forecasts stay raw and the jobs keep running. joblib files are pickles: only load
  artifacts from an environment you trust.
* **Moving a model between environments.**
  * `ml export-manifest ID DIR` writes the verified artifact and a JSON manifest (the
    registry record, the experiment and the package versions).
  * `ml export-manifest ID DIR --compress` re-serialises the same model with joblib
    zlib compression (level 3) to make it smaller to ship. The re-serialised model is
    loaded back and must give the original's factors on a seeded probe matrix within
    1e-9 (tree ensembles sum their trees in parallel, so the order varies at ~1e-16);
    the manifest records the new sha256 and the registry's original one
    (`source_sha256`), and importing it where the original exists is a no-op.
  * `ml import-manifest PATH` verifies the sha256 and registers the model as a new
    version. It is CANDIDATE if its recorded decision passed, otherwise REJECTED, and is
    never imported as ACTIVE. Importing the same artifact twice is a no-op. The version
    number is the next free one in the importing database, so a label can differ from
    the exporting database's.
  * A warning is logged when the scikit-learn version differs.
* **Where a version applies** (`applicability.spot_slugs`): the spots paired, as an
  offshore reference, with a training station **that also has held-out test pairs**. A
  spot reached only through stations without test pairs (e.g. buoys whose data all fall
  before the test period) is recorded in `applicability.excluded_spots` as
  `no_held_out_test_pairs`, because the correction was never tested there.
  `ml restrict ID --exclude-spot SLUG --reason TEXT` removes spots from a version after
  registration (never adds any), with the reason and time stored in
  `applicability.restrictions`; it is refused for an ACTIVE version (running processes
  cache the active applicability): deactivate, restrict, activate.

## Applying a correction (`ml/apply.py`)

```python
corr = load_active_correction(db)          # None when nothing is active / disabled
if corr and corr.applies_to(spot.slug, lead_hours):
    feats = build_features(wave_forecast, cycle_time=run.wave_model_run_at,
                           persistence_hs_m=obs_hs, analysis_log_error=err)
    hs, info = corr.correct_hs(feats)      # info: model label, raw, factor, applied, reason
```

* The result is cached per process. Each call costs one small query, and the model is
  reloaded only when the active version id or sha256 changes.
* The factor is clamped to [0.5, 2.0] and never extrapolated beyond the trained lead
  range.
* With a missing required feature the raw value is returned unchanged, and the reason is
  given in `info`.
* `ML_CORRECTIONS_ENABLED=false` disables every correction.

## Scheduled verification (`verification/jobs.py`)

The Celery task `verification.run` calls `run_verification(db)`. It returns
`{"skipped": ...}` unless `VERIFICATION_ENABLED=true`.

1. It pairs new forecasts.
2. It upserts `forecast_accuracy_metrics` for the 30- and 90-day windows ending at UTC
   midnight. The rows cover:
   * every station, region (`metadata.region`, else `all`) and the global scope;
   * every source;
   * model version `raw`, plus the active model's corrected forecasts. Those are
     recomputed from features, stored in `prediction_validation`, and scored only on
     cycles after the model's training data;
   * every variable (`hs`, `hs_h13`, `hs_unknown`, `tp`, `dir`);
   * every lead bucket.

`extra` holds:

* the cycle-bootstrap CIs of RMSE and bias;
* NRMSE;
* POD/FAR/CSI/frequency bias for Hs ≥ 2 m and ≥ 3 m;
* the reference sample sizes;
* for sources with an ensemble spread, the normal CRPS and the coverage and width of
  the mean ± 1.645 spread interval.

## CLI

```
python -m app.cli verify pair [--full] [--source S] [--lookback-days N]
python -m app.cli verify metrics [--days N]
python -m app.cli verify backtest --source S [--start ISO --end ISO] [--folds K]
                                  [--station CODE] [--hs-kind spectral_hm0|h_third]
                                  [--boot N] [--no-write] [--json]
python -m app.cli ml build-dataset [--source S] [--station CODE] [--hs-kind K] [--out PATH.parquet]
python -m app.cli ml train [--name N] [--candidates linear,rf,gbt] [--folds K] [--auto-activate]
                           [--source S | --dataset PATH.parquet] [--hs-kind K] [--loso]
                           [--no-obs-features] [--boot N]
python -m app.cli ml evaluate VERSION_ID     # recorded decision + scores on newer cycles
python -m app.cli ml list [--name N]
python -m app.cli ml activate VERSION_ID     # exit 3 when the criteria failed / not Hm0
python -m app.cli ml deactivate NAME
python -m app.cli ml export-manifest VERSION_ID DIR [--compress]
python -m app.cli ml import-manifest PATH
python -m app.cli ml restrict VERSION_ID --exclude-spot SLUG [--exclude-spot SLUG ...] --reason TEXT
python -m app.cli verify ranges --station CODE ... --test-start ISO [--ensemble-source E]
                                [--check-station CODE ...] [--boot N] [--json PATH]
python -m app.cli verify confidence-classes --station CODE ... --test-start ISO
                                [--ensemble-source E] [--method forecast-v1.1|forecast-v1]
                                [--scan-boundaries] [--window-days N] [--boot N] [--json PATH]
```

`verify ranges` scores the coverage of the app's offshore 80 % range at buoys and fits
the lead-dependent scale on the TRAIN period; `verify confidence-classes` scores the
realised error of the forecast-confidence classes (docs/ml-evaluation.md section 12).

`verify backtest` prints a per-lead table with these columns:

* n, bias, RMSE with its CI, SI, HH and CC;
* MSE skill against persistence and against climatology, with CIs and reference sample
  sizes.

`--json` adds MAE, NRMSE and the CI of the bias. It writes the rows with
`scope='global'`, `scope_key='backtest'` (or `backtest:<hash>` for a station subset).

## What has been verified

* **Unit tests** cover every metric:
  * hand-computed examples;
  * the circular statistics;
  * the CRPS closed forms against numerical integration;
  * bootstrap block structure and seeding;
  * bucket membership.

  They also cover the leakage guard with deliberately leaking data, the split gaps,
  rolling folds, LOSO, feature construction, factor clamping and lead-range limits, and
  every deployment criterion in both its passing and failing case.
* **Database tests** cover:
  * the pairing rules (tolerance, QC, H1/3 separation, peak-only Tp, circular
    direction, incremental and idempotent runs);
  * the rolling metrics;
  * the backtest;
  * the registry: versioning, activation and retirement, refusal on a checksum
    mismatch, refusal for non-Hm0 targets, and the manifest round trip;
  * the CLI.
* **End-to-end tests on synthetic data**, clearly labelled. The synthetic data inject a
  known multiplicative, lead-dependent over-forecast of 1.15 + 0.20·lead/240. Linear and
  GBT corrections must pass every criterion. In a pure-noise case every candidate must
  be rejected.
* **Live check on 2026-10-11.** This was a one-off load of real data into a scratch
  database, outside the repository.
  * **Inputs.**
    * GFS-Wave 00Z station bulletins (`noaa-gfs-bdp-pds`) at 55020 and 56006, 224
      cycles from 2026-03-01 to 2026-10-10, 3-hourly leads to 240 h.
    * The AODN real-time buoys Eden and Cape Naturaliste. Their 2026 feeds carry H1/3
      only, so the pairs are `h_third`.
    * The Met Office K5 buoy (spectral Hm0, the 8 days the bucket keeps) with bulletin
      64045.
  * **Direction convention.** The bulletin directions were "going to". The lead-0
    circular offset from the buoy peak direction was −178° and +175°, so the directions
    were converted (+180).
  * **Pairing** produced:
    * 21,176 H1/3 Hs pairs;
    * 667 Hm0 pairs;
    * 667 mean-direction pairs at K5;
    * no direction pairs at AODN, which reports no mean direction.

    The one-off load put the bulletin's partition-1 direction into `direction_deg`. The
    repository's collector (`verification/point_forecasts.py`) stores it in
    `swell1_direction_deg` and leaves `tp_s` and `direction_deg` NULL, because the
    bulletin has no total-sea peak period or direction. With the repository code these
    direction pairs are therefore not produced (see the review re-check below).
  * **Backtest** of raw GFS-Wave against H1/3 (3 folds):

    | lead | RMSE | bias | HH | CC |
    |---|---|---|---|---|
    | 24 h | 0.41 m | −0.21 m | 0.19 | 0.93 |
    | 120 h | 0.54 m | −0.18 m | 0.25 | 0.85 |
    | 240 h | 0.82 m | −0.17 m | 0.40 | 0.59 |

  * **Training** against H1/3 (test 2026-08-27 to 2026-10-10, 45 cycles, 3,033 pairs).
    Test RMSE was 0.392 m for GBT, 0.394 m for RF and 0.472 m for ridge, against 0.532 m
    raw. All three passed (a)–(f). They cannot be activated because the target is H1/3,
    and these numbers come from two buoys over seven months, so they are not a general
    accuracy claim.
  * **Hm0 training** at K5 was refused (8 days of data cannot hold a split with a
    13-day gap).
* **Review re-check on 2026-10-11** (own scratch database, the two review branches
  merged: the repository's AODN, Met Office and bulletin collectors filled the tables).
  * 2026 inputs: 00Z bulletins 2026-03-01 to 2026-10-10 at 56006 and 64045 (442 files,
    0 missing, 0 malformed), AODN Cape Naturaliste realtime (650 H1/3 records since
    2026-02-20) and Met Office K5 (199 hourly Hm0 records).
  * `verify pair --full`: 11,801 Hs pairs (9,369 H1/3, 2,432 Hm0) and no `tp` or `dir`
    pairs, as explained above.
  * `verify backtest --hs-kind h_third --folds 3` at Cape Naturaliste alone: 6,480 pairs,
    RMSE 0.377 m [0.342, 0.414] at 24 h and 1.115 m at 240 h, bias about -0.2 m.
  * `ml train --hs-kind h_third`: test 2026-08-29 onward, 1,230 pairs, raw 0.796 m; RF
    0.648 m passed (a)-(f), ridge failed (c), GBT failed (d); nothing was activated.
  * Spectral Hm0, which the builder did not have: AODN delayed-mode QC'd Cape Naturaliste
    02 (12,716 records, 2021-03-22 to 2021-12-31) against 284 00Z bulletins of 2021.
    The backtest gave 40,296 pairs with RMSE 0.441 m [0.407, 0.471] at 24 h, 0.622 m at
    120 h and 1.170 m at 240 h, and bias -0.27 m at 24 h. `ml train` (test period
    2021-11-05 to 2021-12-31, 9,162 pairs, raw RMSE 0.467 m): ridge 0.441 m (-5.4 %) passed
    every criterion and could be activated; RF and GBT (0.511 and 0.513 m) were rejected.
    This is one buoy, one test season and a margin just above the 5 % threshold, so it
    is evidence that the pipeline works end to end on real QC'd data, not a claim that
    the correction is generally useful.
* **Spectral Hm0 backtest and training on 2026-10-11** (three DoT WA buoys with
  co-located bulletin points, 567 cycles of 2021, 783,212 Hm0 pairs including a proxy
  diagnostic): full results, the decision per criterion and the caveats are in
  [docs/ml-evaluation.md](ml-evaluation.md). It supersedes the single-buoy Hm0 re-check
  above (which used 00Z cycles only). The one version that passed every criterion and
  can get its features in real time (ridge trained with `--no-obs-features`) is exported
  in `backend/ml_models/offshore_hs_residual/` for `ml import-manifest`.
* **Mock only.**
  * The point-forecast and observation collectors were not exercised in the builder's
    live check (they were in the review re-check above).
  * NDBC and CDIP observations were not seen live in these runs; those hosts were
    blocked in the build environment until 2026-10-12. Since then NDBC is verified live
    (docs/observations.md) and the raw GFS-Wave bulletins are backtested against ten
    NDBC buoys off Hawaii and California (docs/ml-evaluation.md, section 11); no
    correction was trained on those pairs yet.

## Limitations

* **Offshore Hs only.** The correction targets offshore Hm0 at buoy positions. Tp and
  direction are paired only for sources that store a total-sea peak period (`tp_s` with
  period kind `tp`) and a direction with a declared kind. The GFS-Wave bulletins, the
  only point-forecast source collected so far, have neither (partition 1 is a height
  rank, not the total sea), so today only Hs is verified.
* **Buoys only.** Corrections apply only at spots paired with a training buoy
  (`metadata.paired_spots`) and need the observation features at the cycle time, unless
  a model was trained with `--no-obs-features`. Transfer to unpaired spots is
  untested.
* **Breaking height is not corrected or validated.** That needs nearshore observations
  (surf reports, lifeguard logs, camera-derived heights) in `nearshore_observations`,
  which do not exist yet. Offshore skill does not carry over to surf height.
* **Short histories weaken the climatology reference** (few test months covered) and the
  seasonal coverage of training data. A model trained on one season may not transfer
  to another; rolling-origin results show how stable it is.
* **One correction per name** is applied to every trained source family. Training on a
  source and applying it to another is prevented only when the caller passes `family`
  to `applies_to`.
* **Swell partitions are not corrected.** ML gains are usually larger for wind sea than
  for long-period swell (research notes, ML section), so an Hs correction may move surf
  scores less than its RMSE gain suggests.
