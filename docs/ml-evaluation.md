# Offshore wave forecast backtest and ML correction against spectral Hm0 buoy data

This document reports a live backtest of the raw GFS-Wave forecast at buoys and of the
offshore Hs correction (`docs/ml-pipeline.md`) trained on those pairs. Every number below
was produced on 2026-10-11 from live downloads (NOAA NODD and AODN S3 buckets) into a
scratch PostgreSQL database, using the repository's own commands. Nothing here is
synthetic.

**What this measures.** Offshore significant wave height **Hm0** (the spectral estimate,
`4·sqrt(m0)`) at buoy positions, forecast by GFS-Wave and observed by moored buoys whose
delayed-mode data passed the data owner's quality control. It does **not** measure
breaking (surf) height, which no dataset used here observes. Sections 1-10 cover
south-west Western Australia (2021); section 11 adds a backtest of the raw forecast
against NDBC buoys off Hawaii and California (2024-2026), run on 2026-10-12; section 12
validates the app's 80 % ranges and forecast-confidence classes against those buoys and
decides a correction trained on them.

## Headline

* **Raw GFS-Wave (station bulletins), three WA buoys pooled, 2021-05-10 to 2021-12-31,
  209,658 pairs over 472 cycles.** RMSE 0.40 m at 24 h, 0.47 m at 72 h, 0.57 m at 120 h,
  0.75 m at 168 h and 1.01 m at 240 h. The model is biased low by about 0.23 m at every
  lead up to 168 h (observed Hm0 about 10 % above the model at analysis time). Against a
  long-term monthly climatology of the same buoys (2004-2021, all before the evaluated
  period) the raw forecast keeps positive skill to 168 h and none at 240 h.
* **ML correction, primary dataset (Cape Naturaliste, the only buoy paired with surf
  spots), held-out test 2021-11-05 to 2021-12-31, 14,658 pairs.** Raw
  RMSE 0.360 m; the ridge correction without observation features (P2 v4)
  0.331 m (-8.1 %), with per-lead RMSE raw -> corrected 0.248 -> 0.210 m at 24 h, 0.252
  -> 0.217 at 48 h, 0.293 -> 0.263 at 72 h, 0.351 -> 0.325 at 120 h, 0.521 -> 0.498 at
  168 h and 0.678 -> 0.609 at 240 h. Random forest and gradient boosting were worse than
  raw on the test period (0.38-0.40 m) in both runs.
* **Decision.** P2 v4 (ridge, no observation features) passed every deployment criterion
  and needs only inputs available at issue time; it was activated in the backtest
  database and exported to `backend/ml_models/offshore_hs_residual/`. P1 v1 (ridge with
  observation features) also passed but is not activated, because its observation inputs
  (cycle-time Hm0) do not exist in real time. All RF and GBT versions were rejected. The
  evidence is one buoy and one eight-week test period; see the caveats.
* **In the application: shipped as a candidate, not active by default** (section 10). A
  live transfer check on the 2026-10-11 GFS run showed that, at the surf spots' own
  gridded forecast points, 18 % of the corrected hours got factors below the range the
  model produced on its held-out test (5th percentile 0.79 vs 0.98 at the buoy), i.e. the
  model would run on inputs it was not validated on. Activation is an explicit operator
  step after a shadow check on gridded forecasts.
* **Second region: NDBC buoys off Hawaii and California** (section 11; 10 buoys,
  2025-02-11 to 2026-10-12, 409,344 pairs over 305 cycles, raw bulletins only). The same
  low bias as in Western Australia (pooled -0.23 m at every lead); RMSE 0.38 m at 24 h,
  0.51 m at 168 h and 0.64 m at 240 h. Skill over a 2019-2024 monthly climatology of the
  same buoys: 0.58 at 24 h, 0.23 at 168 h, negative at 240 h (-0.28). Off Trestles (46224)
  the raw forecast is not distinguishably better than that climatology at any lead, and
  off Pe'ahi (51205) only slightly; winter has larger errors but more skill than summer.
* **Validation against NDBC buoys** (section 12, held-out year 2025-10 to 2026-10). The
  app's offshore 80 % range covered 53 % at 24 h and 85-89 % beyond 5 days (misses above
  p90: the model is biased low); a lead-dependent two-tail scale fitted on the older year
  covers 75.1-80.2 % at every lead bucket pooled over five buoys, so the offshore
  component is implemented as calibrated (`ndbc-offshore-v1`), labelled so only at the
  two spots whose own buoy agrees over all leads and at every lead bucket (jaws-peahi,
  trestles; 69-91 % by buoy; mavericks and ocean-beach-sf were dropped in review because
  46026 covers only 69 % at 240 h) and not yet passed in by the prediction job, so no
  prediction carries the label today. The ensemble-based range
  stays unvalidated. forecast-v1's High and Moderate single-source classes had equal median
  errors on the training year; forecast-v1.1 drops High for single-source hours without
  an ensemble, and its classes (with and without a GEFS-Wave spread) are ordered by
  realised error on the held-out year (that held-out check of the new rule was not blind,
  12.3).
* **Correction approved for activation at seven US spots** (12.4): a random forest
  trained on the NDBC and AODN pairs cut held-out RMSE from 0.427 to 0.320 m (-25 %) at
  five NDBC buoys, passed
  criteria (a)-(f) and the transfer check on the live gridded run, and ships as
  `offshore_hs_residual` v6 for jaws-peahi, mavericks, ocean-beach-sf, pipeline,
  sunset-beach, trestles and waimea-bay (not rincon: no benefit at its buoy; not the WA
  spots: never tested). Activating it in a deployment is the operator step of section 12.4
  (`ml import-manifest`, `ml activate`); it was activated and checked end to end only in a
  scratch copy of the real 2026-10-11 runs.

## 1. Data inventory

### Sources

| Data | Source | Licence | Verified live |
| --- | --- | --- | --- |
| Model at buoys | GFS-Wave station bulletins `gfswave.{ID}.bull`, 00Z and 12Z cycles, `s3://noaa-gfs-bdp-pds/gfs.YYYYMMDD/HH/wave/station/bulls.tHHz/` (archive starts 2021-03-22 18Z) | NOAA, public domain | yes |
| Observations | AODN `wave_buoy_delayed_qc.parquet` (delayed mode, quality controlled), `s3://aodn-cloud-optimised` | CC BY 4.0 (IMOS / AODN; the DoT WA files add their own acknowledgement, recorded on each station) | yes |
| Bulletin point depths | first point line of `gfswave.{ID}.spec` (HTTP range request, 2026-06-01 12Z) | NOAA | yes |

Downloads for the whole study were about 0.33 GB (5,504 bulletin files of 52 KB, about
780 AODN partition files of 30-50 KB, listings, two source NetCDF files to read their
metadata, and one sample partition per AODN site for the site survey).

### Which buoys could carry an Hm0 backtest

The GFS-Wave bulletin archive starts on 2021-03-22 18Z, so only delayed-QC records after
that date can be paired. The data plan named four AODN sites; checked against the data:

| Station | Depth | Delayed-QC Hm0 after 2021-03-22 | Bulletin point and distance | Used |
| --- | --- | --- | --- | --- |
| `aodn:cape-naturaliste` (Cape Naturaliste 02, DoT WA, Datawell DWR MkIII) | 50 m | 12,716 records, 2021-03-22 to 2021-12-31, all QC flag 1 | 56006 (33.36S 114.78E), **19.5 km** north; WW3 depth 73.1 m | **yes (primary: the only buoy paired with surf spots)** |
| `aodn:rottnest-island-02` (DoT WA, Datawell Directional Waverider MK III) | 48 m | 12,816 records, 2021-03-22 to 2021-12-31, all flag 1 | 56005 (32.11S 115.40E), **1.9 km**; WW3 depth 85.9 m | yes (added for this study, verification only) |
| `aodn:esperance-04` (DoT WA, Datawell Directional Waverider MK III) | 52 m | 13,009 records, 2021-03-22 to 2021-12-31, all flag 1 | 56010 (34.00S 121.90E), **0.1 km**; WW3 depth 25.7 m | yes (added for this study, verification only) |
| `aodn:apollo-bay` (IMOS Coastal Wave Buoys, SOFAR Spotter) | 30 m | 85,645 good records, 2021-03-22 to 2026-06-07 (1,507 more rejected by QC and stored as rejection markers) | none within 30 km; nearest NKING (39.20S 143.80E) **50.0 km** south, WW3 depth 103.4 m | **excluded** from the results; diagnostic only (see "Is each pairing defensible?") |
| `aodn:tweed-heads` | 22-25 m | **none**: the 2021-2022 delayed-mode files carry only `WHTH` (time-domain H1/3); 24,836 H1/3 records stored | none within 30 km (55035, 77 km) | **excluded** (no Hm0, no bulletin) |
| `aodn:gold-coast` | 17 m | **none**: `WHTH` only; 16,175 H1/3 records stored | none within 30 km (55035, 56 km) | **excluded** (no Hm0, no bulletin) |

The data plan expected spectral Hm0 at Tweed Heads and the Gold Coast to 2022-12. The
`WSSH` column exists in those Parquet files but is empty in every 2021-2022 partition
(checked on the ingested records and on a downloaded partition of 2022-07, which has
1,277 `WHTH` values and 0 `WSSH`). Mixing those H1/3 values into an Hm0 result would
violate the like-with-like rule, so they were not used. A survey of all 172 delayed-QC
sites (one partition per site) found two further DoT WA buoys with Hm0 in 2021 and a
bulletin output point within 2 km: Rottnest Island 02 and Esperance 04. They were added
to `data/observation_stations.json` as inactive verification stations without spot
pairings (the data end in 2021-12 and their realtime feeds are not configured).

Climatology history (supplementary reference, section 3): the same three WA stations'
delayed-QC Hm0 from 2004-04-27 (Cape Naturaliste), 2004-09-15 (Rottnest) and 2006-06-17
(Esperance) to 2021-03-21: 229,078, 271,298 and 182,599 records, all flag 1.

### Forecast archive and pairing

* `obs collect-forecasts`, 00Z and 12Z, 2021-03-22 18Z to 2021-12-31 12Z: 567 cycles per
  WA station (91,287 rows each, hourly leads to 120 h, 3-hourly to 240 h). One cycle per
  station was refused and reported, not stored: the file published under 2021-03-23 12Z
  holds the 00Z cycle (`bulletin is for cycle 20210323 00Z`). 0 malformed cells.
* Apollo Bay, NKING bulletin as a proxy (source `gfswave_bulletin_proxy`, never pooled
  with `gfswave_bulletin`): 3,803 cycles fetched (2021-03-23 to 2026-06-07), 2 missing
  (HTTP 404), 1 refused for the same reason, 612,283 rows.
* `verify pair --full`: 886,144 forecasts scanned, 783,212 Hs pairs, **all against
  spectral Hm0** (`observed_kind = spectral_hm0`), 102,932 forecasts without an
  observation within 30 min. Per station: Cape Naturaliste 85,243, Rottnest 85,328,
  Esperance 86,417, Apollo Bay (proxy) 526,224. No Tp or direction pairs: the bulletin has
  no total-sea peak period or direction, and the 2021 DoT WA files carry no directions.
* QC filters: only `qc_flag = good` (AODN `WAVE_quality_control` = 1) rows exist for the
  paired WA stations in the window; rows flagged `bad` are never paired. No realtime
  (`not_qc`) data were ingested: the AODN ingest was restricted to the delayed-QC dataset
  (`obs ingest --aodn-dataset delayed_qc`, added for this study).

### Is each pairing defensible?

The analysis-time rows (lead 0-2 h, never used for training or scoring) show how well the
bulletin point represents the buoy before forecast error accumulates. Ratio = median of
observed Hm0 / bulletin Hst; sector = direction of bulletin partition 1 (FROM).

| Station | n | bias m | RMSE m | CC | median obs/model | by sector |
| --- | --- | --- | --- | --- | --- | --- |
| Cape Naturaliste / 56006 | 1,612 | -0.28 | 0.44 | 0.960 | 1.10 | S 1.07, SSW-SW 1.10, WSW 1.12, W-WNW 1.02 |
| Rottnest 02 / 56005 | 1,618 | -0.18 | 0.35 | 0.962 | 1.08 | S 1.14, SSW-SW 1.09, WSW 1.09, W-WNW 1.06 |
| Esperance 04 / 56010 | 1,633 | -0.22 | 0.37 | 0.939 | 1.13 | S 1.03, SSW-SW 1.15 |
| Apollo Bay / NKING (proxy) | 9,824 | **+0.86** | 1.08 | 0.664 | **0.66** | S 0.94, SSW-SW 0.84, WSW 0.63, W-WNW **0.39** |

* **Cape Naturaliste / 56006: defensible.** Both sit on the open shelf off Cape
  Naturaliste in comparable exposure to the SW-W Southern Ocean swell (buoy 50 m, model
  point 73 m). The output point is the 1999-2004 mooring, 19.5 km north of the current
  one. The ratio is stable across directions (1.02-1.12) and the correlation is 0.96, so
  the low bias looks like model error rather than a change in exposure between the two
  places.
* **Rottnest 02 / 56005: defensible.** 1.9 km apart; the model point is deeper (86 m vs
  48 m) but the ratio is uniform across directions.
* **Esperance 04 / 56010: defensible with a caveat.** 0.1 km apart, but the WW3 depth at
  the output point is 25.7 m against 52 m at the mooring, and the coast off Esperance is
  fringed by the islands of the western Recherche Archipelago, which a 1/4-1/6 degree
  wave grid resolves only roughly. Local sheltering and depth effects are part of its
  error (ratio 1.15 for SSW-SW swell, 1.03 for S).
* **Apollo Bay / NKING: not defensible.** NKING lies 50 km south, in open water north of
  King Island; the Apollo Bay Spotter is in 30 m on the Otway coast, with Cape Otway
  21.5 km to the WSW. The model over-forecasts the buoy by 0.86 m on average and the
  ratio falls from 0.94 for southerly to 0.39 for westerly swell: the buoy is sheltered
  from the west and the bulletin point is not. A "correction" trained on these pairs
  would learn the sheltering, not model error. Apollo Bay is therefore reported only as
  a diagnostic and excluded from the backtest results and from training.

## 2. Method

* **Pairing** (`verify pair --full`): each point forecast is matched with the same
  station's nearest observation within 30 minutes; delayed-mode records are at :11/:41,
  so every hourly lead has a match. Only spectral Hm0 targets (`--hs-kind
  spectral_hm0`, the default); no H1/3 value enters any Hm0 result.
* **Lead buckets**: 24 h = 18-30 h, 48 = 42-54, 72 = 66-78, 120 = 108-132, 168 = 156-180,
  240 = 228-252 h; "all" = every lead from 3 h to 240 h (leads below 3 h are analysis
  rows and never scored).
* **Scores** (`app/services/verification/metrics.py`): bias, MAE, RMSE, SI (Mentaschi
  form), HH (Hanna-Heinold), CC, and MSE skill 1 - MSE/MSE_ref vs persistence and
  climatology. RMSE and SI favour negatively biased forecasts, so bias and HH are
  always read alongside.
* **Confidence intervals**: 90 % percentile intervals from a block bootstrap that resamples
  whole forecast cycles (1,000 replicates, seed 20261010). Section 5 repeats the key
  interval with 72 h and 168 h blocks, because consecutive cycles share weather.
* **Persistence**: the observation at or before the cycle time (at most 1 h old) carried
  to every lead.
* **Climatology, as implemented**: monthly mean Hm0 per station from the training rows
  only (each backtest fold's training set; train or train + validation for the ML split),
  months with at least 24 observations. With nine months of data this reference is
  nearly empty or degenerate (see section 3), so a **supplementary long-term
  climatology** is also reported: the monthly mean of each station's QC'd Hm0 from
  2004-2006 to 2021-03-21, all before the first evaluated cycle, so it is out of sample
  for every row (`scripts/ml_evaluation_report.py --lt-climatology-before 2021-03-22`).
* **Backtest** (`verify backtest --folds 5 --boot 1000`): rolling-origin, expanding
  window; the 567 cycles are cut into 6 blocks and fold i tests block i with training
  data whose valid time ends at least 312 h (max lead 240 h + 3 days) before the block.
  The test blocks together cover 2021-05-10 to 2021-12-31.
* **ML** (`ml train --candidates linear,rf,gbt --folds 3 --boot 1000`): chronological
  60/20/20 % split of the cycles with 312 h purged gaps: train 2021-03-23 to 2021-08-27
  (314 cycles, 46,446 rows), validation 2021-09-09 12Z to 2021-10-22 12Z (87 cycles,
  12,547 rows), test 2021-11-05 to 2021-12-31 (114 cycles, 14,658 rows). Hyperparameters
  are chosen on validation only; the chosen setting is refitted on train + validation and
  evaluated once on test. Leakage guards (training valid time before first test cycle
  minus gap; observation features at or before the cycle time; targets after it) ran on
  every split, refit and fold, and passed. Target: ln(obs) - ln(model); features are
  listed in `docs/ml-pipeline.md` (no station identifier). The bulletin has no total Tp,
  no total direction and no wind, so those features are absent; the partition-1 height,
  period and direction, the lead, month and hour are used, and in the default run the
  observation features `persistence_hs_m` and `analysis_log_error`.
* **Deployment criteria**, unchanged (`app/services/ml/registry.py`), evaluated on the
  test period only: (a) RMSE at least 5 % below raw; (b) the 90 % cycle-bootstrap CI of
  RMSE(candidate) - RMSE(raw) entirely below 0; (c) no lead bucket (n >= 50), nor the
  group of leads outside all buckets, worse than raw by more than 3 %; (d) beats or ties
  (1 %) the ridge baseline; (e) at least 300 test pairs; (f) HH not above raw.
* **Runs.** P1: default features on the Cape Naturaliste pairs (registered first; the
  correction can only ever be applied at the spots paired with this buoy). P2: the same
  pairs, split and criteria with `--no-obs-features`, added after P1 had been evaluated
  (on request), because only a version that can obtain its features in real time
  may be activated. D1: pooled WA (three buoys) with leave-one-station-out, a diagnostic.
  A planned diagnostic on the Apollo Bay proxy pairs (D2) was not run once the exposure
  check above showed the pairing is not defensible.

## 3. Backtest of the raw forecast (`verify backtest`, spectral Hm0)

Rolling origin, 5 folds, gap 312 h, test blocks 2021-05-10 to 2021-12-31. "SS clim (folds)"
uses the implemented training-fold climatology; its sample size is in brackets.

**Pooled: Cape Naturaliste, Rottnest 02, Esperance 04**: 209,658 test pairs, 472 cycles.

| lead h | n | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | RMSE persistence | SS clim (folds) (n) |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 209658 | -0.23 [-0.24, -0.22] | 0.40 | 0.570 [0.556, 0.585] | 0.19 | 0.22 | 0.90 | 0.81 [0.79, 0.82] | 1.303 | 0.96 (12769) |
| 24 | 17467 | -0.24 [-0.25, -0.22] | 0.31 | 0.401 [0.385, 0.415] | 0.12 | 0.16 | 0.96 | 0.83 [0.81, 0.85] | 0.964 | 0.97 (1504) |
| 48 | 17389 | -0.23 [-0.25, -0.22] | 0.32 | 0.420 [0.403, 0.434] | 0.13 | 0.16 | 0.95 | 0.89 [0.88, 0.91] | 1.280 | 0.97 (1270) |
| 72 | 17311 | -0.23 [-0.26, -0.21] | 0.34 | 0.465 [0.438, 0.491] | 0.15 | 0.18 | 0.94 | 0.89 [0.87, 0.90] | 1.391 | 0.96 (1098) |
| 120 | 22461 | -0.27 [-0.29, -0.24] | 0.41 | 0.566 [0.538, 0.596] | 0.18 | 0.22 | 0.90 | 0.85 [0.83, 0.87] | 1.459 | 0.96 (1068) |
| 168 | 11790 | -0.23 [-0.27, -0.19] | 0.53 | 0.748 [0.706, 0.789] | 0.26 | 0.29 | 0.81 | 0.72 [0.68, 0.76] | 1.430 | 0.91 (450) |
| 240 | 6471 | -0.15 [-0.21, -0.09] | 0.72 | 1.012 [0.951, 1.071] | 0.36 | 0.39 | 0.63 | 0.51 [0.44, 0.57] | 1.454 | 0.96 (168) |

**Cape Naturaliste (bulletin 56006)**: 69,642 test pairs, 472 cycles.

| lead h | n | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | RMSE persistence | SS clim (folds) (n) |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 69642 | -0.24 [-0.26, -0.22] | 0.45 | 0.638 [0.621, 0.656] | 0.18 | 0.21 | 0.89 | 0.82 [0.80, 0.83] | 1.492 | 0.98 (4251) |
| 24 | 5800 | -0.25 [-0.28, -0.23] | 0.33 | 0.436 [0.416, 0.456] | 0.11 | 0.14 | 0.96 | 0.84 [0.81, 0.86] | 1.078 | 0.99 (500) |
| 48 | 5774 | -0.25 [-0.27, -0.22] | 0.35 | 0.466 [0.443, 0.485] | 0.12 | 0.15 | 0.95 | 0.90 [0.88, 0.91] | 1.448 | 0.99 (422) |
| 72 | 5748 | -0.25 [-0.27, -0.21] | 0.38 | 0.509 [0.476, 0.544] | 0.14 | 0.16 | 0.94 | 0.90 [0.88, 0.91] | 1.598 | 0.98 (366) |
| 120 | 7462 | -0.29 [-0.32, -0.25] | 0.47 | 0.624 [0.590, 0.658] | 0.17 | 0.20 | 0.90 | 0.86 [0.83, 0.88] | 1.658 | 0.98 (356) |
| 168 | 3922 | -0.22 [-0.27, -0.17] | 0.61 | 0.839 [0.786, 0.896] | 0.25 | 0.27 | 0.80 | 0.75 [0.70, 0.79] | 1.684 | 0.97 (150) |
| 240 | 2153 | -0.12 [-0.21, -0.04] | 0.85 | 1.183 [1.101, 1.262] | 0.36 | 0.38 | 0.59 | 0.48 [0.39, 0.56] | 1.672 | 0.97 (56) |

**Rottnest Island 02 (bulletin 56005)**: 69,695 test pairs, 472 cycles.

| lead h | n | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | RMSE persistence | SS clim (folds) (n) |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 69695 | -0.22 [-0.23, -0.21] | 0.39 | 0.559 [0.542, 0.576] | 0.19 | 0.22 | 0.89 | 0.82 [0.80, 0.84] | 1.315 | 0.95 (4259) |
| 24 | 5808 | -0.23 [-0.25, -0.21] | 0.30 | 0.381 [0.363, 0.399] | 0.11 | 0.15 | 0.96 | 0.84 [0.82, 0.87] | 0.944 | 0.96 (502) |
| 48 | 5782 | -0.23 [-0.25, -0.21] | 0.32 | 0.406 [0.389, 0.424] | 0.12 | 0.16 | 0.96 | 0.90 [0.88, 0.91] | 1.271 | 0.95 (424) |
| 72 | 5756 | -0.23 [-0.25, -0.20] | 0.34 | 0.464 [0.427, 0.505] | 0.15 | 0.18 | 0.93 | 0.89 [0.86, 0.91] | 1.383 | 0.95 (366) |
| 120 | 7467 | -0.26 [-0.29, -0.23] | 0.40 | 0.537 [0.508, 0.567] | 0.17 | 0.21 | 0.91 | 0.88 [0.85, 0.90] | 1.505 | 0.93 (356) |
| 168 | 3919 | -0.20 [-0.25, -0.16] | 0.51 | 0.720 [0.667, 0.774] | 0.25 | 0.28 | 0.81 | 0.75 [0.70, 0.79] | 1.456 | 0.90 (150) |
| 240 | 2151 | -0.14 [-0.22, -0.07] | 0.73 | 1.010 [0.942, 1.080] | 0.37 | 0.40 | 0.58 | 0.54 [0.46, 0.61] | 1.481 | 0.96 (56) |

**Esperance 04 (bulletin 56010)**: 70,321 test pairs, 472 cycles.

| lead h | n | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | RMSE persistence | SS clim (folds) (n) |
|---|---|---|---|---|---|---|---|---|---|---|
| all | 70321 | -0.24 [-0.25, -0.22] | 0.36 | 0.507 [0.492, 0.521] | 0.21 | 0.26 | 0.85 | 0.77 [0.75, 0.79] | 1.071 | 0.90 (4259) |
| 24 | 5859 | -0.23 [-0.25, -0.21] | 0.30 | 0.383 [0.363, 0.404] | 0.14 | 0.19 | 0.94 | 0.80 [0.78, 0.82] | 0.859 | 0.91 (502) |
| 48 | 5833 | -0.22 [-0.24, -0.20] | 0.30 | 0.383 [0.364, 0.403] | 0.15 | 0.19 | 0.94 | 0.88 [0.86, 0.89] | 1.099 | 0.91 (424) |
| 72 | 5807 | -0.23 [-0.25, -0.20] | 0.31 | 0.416 [0.386, 0.447] | 0.16 | 0.21 | 0.92 | 0.87 [0.85, 0.89] | 1.161 | 0.92 (366) |
| 120 | 7532 | -0.26 [-0.29, -0.23] | 0.37 | 0.533 [0.489, 0.577] | 0.22 | 0.27 | 0.84 | 0.79 [0.75, 0.83] | 1.176 | 0.91 (356) |
| 168 | 3949 | -0.27 [-0.32, -0.23] | 0.47 | 0.675 [0.629, 0.726] | 0.29 | 0.35 | 0.69 | 0.61 [0.54, 0.67] | 1.093 | 0.68 (150) |
| 240 | 2167 | -0.19 [-0.24, -0.13] | 0.58 | 0.807 [0.751, 0.866] | 0.36 | 0.41 | 0.52 | 0.51 [0.44, 0.57] | 1.166 | 0.91 (56) |

**About the training-fold climatology.** Each fold trains on the months before its test
block, so a test month has a climatology only when the fold boundary falls inside it:
the reference is then the mean of the first part of the same month (one weather regime),
covers only the first days of each test block (n = 56-1,504 per bucket) and has an RMSE
of 1.1-2.3 m, against about 1.0 m for the long-term climatology. The resulting skill of
0.68-0.99 is correct arithmetic on a poor reference, not evidence of skill against
climatology. The long-term climatology below is the meaningful
comparison.

**Raw forecast vs the long-term monthly climatology** (station monthly means of QC'd
Hm0, 2004-2006 to 2021-03-21; 14-17 years per month), same rows as the backtest (cycles
from 2021-05-10, 209,658 pairs, every row has a climatology value). MSE skill [90 % CI];
the climatology's own RMSE (pooled) is in the last column.

| lead h | pooled | cape-naturaliste | esperance-04 | rottnest-island-02 | RMSE climatology, pooled |
|---|---|---|---|---|---|
| all | 0.69 [0.66, 0.71] | 0.69 [0.67, 0.72] | 0.60 [0.58, 0.63] | 0.72 [0.70, 0.74] | 1.016 |
| 24 | 0.84 [0.83, 0.86] | 0.86 [0.83, 0.87] | 0.77 [0.75, 0.79] | 0.87 [0.85, 0.89] | 1.013 |
| 48 | 0.83 [0.81, 0.85] | 0.84 [0.81, 0.86] | 0.77 [0.75, 0.80] | 0.85 [0.83, 0.87] | 1.015 |
| 72 | 0.79 [0.76, 0.82] | 0.80 [0.77, 0.83] | 0.73 [0.69, 0.77] | 0.81 [0.76, 0.84] | 1.016 |
| 120 | 0.69 [0.65, 0.72] | 0.71 [0.67, 0.75] | 0.56 [0.50, 0.62] | 0.74 [0.70, 0.78] | 1.018 |
| 168 | 0.47 [0.40, 0.53] | 0.49 [0.40, 0.56] | 0.31 [0.23, 0.38] | 0.54 [0.46, 0.61] | 1.027 |
| 240 | 0.03 [-0.09, 0.15] | -0.01 [-0.18, 0.14] | 0.01 [-0.11, 0.12] | 0.10 [-0.05, 0.24] | 1.029 |

**Reading the backtest.**

* Errors grow with lead as expected: RMSE roughly doubles from 24 h to 168 h and is
  2.5 times larger at 240 h; correlation falls from 0.96 to 0.63.
* The raw model is low by 0.20-0.29 m at every station and lead to 168 h, and by
  0.12-0.19 m at 240 h; the bias CI excludes zero everywhere. With mean observed Hm0 of
  2.3-2.9 m (Esperance 1.9 m), that is about 10 %, the same ratio as at analysis time.
* The forecast beats persistence at every lead (MSE skill 0.48-0.90). Against the
  long-term monthly climatology it has clear skill to 120 h (pooled 0.84 at 24 h, 0.69 at
  120 h), smaller skill at 168 h (0.47) and none at 240 h (0.03, CI -0.09 to 0.15);
  Esperance, whose model point sits among the islands, loses skill fastest (0.31 at
  168 h).
* 2021 is one season of one model version: GFS v16 before the v16.3.0 upgrade, which
  went live on 2022-11-29 (research notes, GFS section). Numbers for the current model
  may differ.

Apollo Bay with the NKING proxy (diagnostic, not a forecast-skill result; test blocks
2022-05-30 to 2026-06-07, 431,468 pairs): bias +0.92 m and RMSE 1.15 m at 24 h, skill vs
persistence -2.6 and vs the training-fold climatology -3.0. The numbers measure the
difference in exposure between NKING and the buoy, not the model.

## 4. ML correction on the Cape Naturaliste Hm0 pairs

Dataset (`ml build-dataset --source gfswave_bulletin --station aodn:cape-naturaliste`,
fingerprint `8471d2e6806e83d5`): 83,631 rows from 91,287 point forecasts (1,701
analysis-time rows and 5,955 without an observation within 30 min dropped), 567 cycles,
leads 3-240 h. Persistence (an Hm0 observation at most 1 h before the cycle) exists for
95.6 % of the rows. Split as in section 2; the training-only climatology has 6 monthly
entries (March-August) and the train + validation one 8, so neither covers a validation
or test month: **the implemented climatology reference is empty here** (n = 0) and only
the long-term climatology (2004-04-27 to 2021-03-21) is shown.

All per-lead numbers below were recomputed by `scripts/ml_evaluation_report.py` from the
registered artifacts (test) and a seeded refit of the selected setting (validation); the
script checks that it reproduces the recorded validation and test RMSE of every version
exactly (it did, to 1e-6 m).

### 4.1 P1: default features (with the observation features)

Selected settings: ridge alpha 0.1 (metre scale 0.927), RF min_samples_leaf 40 (1.005),
GBT learning rate 0.05 / 31 leaves (1.005).

**Test (held out, evaluated once): Hs RMSE in m [90 % CI]**

| lead h | n | raw | persistence | LT climatology | linear | rf | gbt |
|---|---|---|---|---|---|---|---|
| all | 14658 | 0.360 [0.334, 0.388] | 0.784 [0.709, 0.859] | 0.669 [0.647, 0.691] | 0.330 [0.310, 0.350] | 0.384 [0.368, 0.402] | 0.390 [0.374, 0.407] |
| 24 | 1279 | 0.248 [0.224, 0.273] | 0.529 [0.474, 0.586] | 0.668 [0.599, 0.736] | 0.209 [0.195, 0.225] | 0.269 [0.249, 0.288] | 0.262 [0.242, 0.280] |
| 48 | 1253 | 0.252 [0.219, 0.285] | 0.747 [0.670, 0.826] | 0.664 [0.595, 0.733] | 0.216 [0.197, 0.235] | 0.273 [0.254, 0.293] | 0.270 [0.251, 0.291] |
| 72 | 1227 | 0.293 [0.243, 0.349] | 0.792 [0.701, 0.876] | 0.670 [0.603, 0.738] | 0.263 [0.226, 0.304] | 0.309 [0.282, 0.337] | 0.309 [0.280, 0.336] |
| 120 | 1542 | 0.351 [0.302, 0.401] | 0.867 [0.746, 0.984] | 0.676 [0.606, 0.745] | 0.326 [0.294, 0.362] | 0.392 [0.358, 0.427] | 0.406 [0.371, 0.438] |
| 168 | 778 | 0.521 [0.458, 0.589] | 0.959 [0.839, 1.075] | 0.660 [0.595, 0.726] | 0.493 [0.436, 0.554] | 0.537 [0.480, 0.598] | 0.555 [0.491, 0.625] |
| 240 | 405 | 0.678 [0.572, 0.784] | 1.041 [0.859, 1.207] | 0.666 [0.593, 0.744] | 0.602 [0.508, 0.693] | 0.696 [0.596, 0.799] | 0.710 [0.624, 0.799] |

**Test (held out, evaluated once): bias m, HH, and RMSE(candidate) - RMSE(raw)
in m [90 % CI]**

| lead h | raw bias, HH | linear bias, HH | rf bias, HH | gbt bias, HH | linear - raw | rf - raw | gbt - raw |
|---|---|---|---|---|---|---|---|
| all | -0.10, 0.182 | +0.05, 0.160 | +0.20, 0.181 | +0.20, 0.183 | -0.030 [-0.040, -0.020] | +0.024 [+0.009, +0.040] | +0.030 [+0.013, +0.047] |
| 24 | -0.11, 0.124 | -0.03, 0.102 | +0.17, 0.125 | +0.16, 0.122 | -0.039 [-0.055, -0.021] | +0.021 [-0.015, +0.054] | +0.014 [-0.019, +0.046] |
| 48 | -0.11, 0.127 | +0.00, 0.106 | +0.17, 0.129 | +0.16, 0.128 | -0.036 [-0.055, -0.016] | +0.022 [-0.015, +0.060] | +0.019 [-0.019, +0.058] |
| 72 | -0.10, 0.149 | +0.03, 0.129 | +0.17, 0.147 | +0.17, 0.147 | -0.030 [-0.052, -0.007] | +0.016 [-0.031, +0.063] | +0.015 [-0.032, +0.062] |
| 120 | -0.12, 0.180 | +0.06, 0.159 | +0.19, 0.187 | +0.21, 0.192 | -0.025 [-0.048, +0.001] | +0.042 [+0.004, +0.083] | +0.055 [+0.015, +0.099] |
| 168 | -0.07, 0.260 | +0.16, 0.233 | +0.24, 0.250 | +0.26, 0.257 | -0.028 [-0.060, +0.004] | +0.016 [-0.030, +0.064] | +0.034 [-0.013, +0.086] |
| 240 | -0.03, 0.333 | +0.21, 0.281 | +0.34, 0.316 | +0.43, 0.318 | -0.076 [-0.114, -0.039] | +0.018 [-0.026, +0.065] | +0.032 [-0.033, +0.098] |

**Validation: Hs RMSE in m [90 % CI]**

| lead h | n | raw | persistence | LT climatology | linear | rf | gbt |
|---|---|---|---|---|---|---|---|
| all | 12547 | 0.628 [0.592, 0.662] | 1.219 [1.121, 1.321] | 1.017 [0.989, 1.045] | 0.489 [0.463, 0.514] | 0.495 [0.469, 0.520] | 0.510 [0.482, 0.536] |
| 24 | 1105 | 0.474 [0.431, 0.515] | 1.020 [0.875, 1.160] | 1.001 [0.894, 1.103] | 0.340 [0.305, 0.378] | 0.316 [0.288, 0.346] | 0.323 [0.296, 0.351] |
| 48 | 1079 | 0.484 [0.438, 0.526] | 1.232 [1.078, 1.390] | 1.013 [0.892, 1.123] | 0.347 [0.307, 0.388] | 0.325 [0.293, 0.355] | 0.347 [0.314, 0.382] |
| 72 | 1053 | 0.477 [0.433, 0.521] | 1.334 [1.181, 1.508] | 1.023 [0.902, 1.136] | 0.355 [0.317, 0.390] | 0.366 [0.327, 0.404] | 0.378 [0.340, 0.415] |
| 120 | 1315 | 0.643 [0.562, 0.720] | 1.427 [1.251, 1.598] | 1.031 [0.926, 1.131] | 0.488 [0.411, 0.566] | 0.509 [0.435, 0.579] | 0.526 [0.455, 0.597] |
| 168 | 661 | 0.895 [0.691, 1.093] | 1.222 [1.062, 1.377] | 1.040 [0.932, 1.144] | 0.666 [0.534, 0.811] | 0.740 [0.580, 0.897] | 0.777 [0.600, 0.949] |
| 240 | 340 | 1.125 [0.943, 1.296] | 1.246 [1.071, 1.425] | 1.029 [0.908, 1.142] | 0.941 [0.805, 1.076] | 0.982 [0.844, 1.116] | 0.965 [0.855, 1.079] |

**Rolling-origin folds** (development period, robustness only, never used for
selection), RMSE in m and change vs raw:

| fold, test cycles | raw | linear | rf | gbt |
| --- | --- | --- | --- | --- |
| 1: 2021-05-13 to 07-01 (9,709 training rows) | 0.644 | 0.614 (-4.8 %) | 0.727 (+12.8 %) | 0.737 (+14.4 %) |
| 2: 2021-07-02 to 08-20 (25,459) | 0.798 | **0.931 (+16.6 %)**, bias -0.56 m vs raw -0.24 m | 0.760 (-4.8 %) | 0.787 (-1.4 %) |
| 3: 2021-08-21 to 10-22 (40,343) | 0.624 | 0.493 (-20.9 %) | 0.501 (-19.7 %) | 0.515 (-17.3 %) |

### 4.2 P2: without the observation features (`--no-obs-features`)

Same dataset, split, grids, seed and criteria; the features `persistence_hs_m` and
`analysis_log_error` are left out, so every input is known from the forecast itself
(lead, model Hs, partition-1 height, period and direction, valid month and hour).
Selected settings: ridge alpha 0.1 (metre scale 0.926), RF min_samples_leaf 40 (1.009),
GBT learning rate 0.05 / 15 leaves (1.003).

**Test (held out, evaluated once): Hs RMSE in m [90 % CI]**

| lead h | n | raw | persistence | LT climatology | linear | rf | gbt |
|---|---|---|---|---|---|---|---|
| all | 14658 | 0.360 [0.334, 0.388] | 0.784 [0.709, 0.859] | 0.669 [0.647, 0.691] | 0.331 [0.312, 0.351] | 0.396 [0.381, 0.412] | 0.383 [0.367, 0.401] |
| 24 | 1279 | 0.248 [0.224, 0.273] | 0.529 [0.474, 0.586] | 0.668 [0.599, 0.736] | 0.210 [0.195, 0.226] | 0.295 [0.274, 0.315] | 0.268 [0.248, 0.287] |
| 48 | 1253 | 0.252 [0.219, 0.285] | 0.747 [0.670, 0.826] | 0.664 [0.595, 0.733] | 0.217 [0.199, 0.234] | 0.293 [0.275, 0.312] | 0.267 [0.250, 0.286] |
| 72 | 1227 | 0.293 [0.243, 0.349] | 0.792 [0.701, 0.876] | 0.670 [0.603, 0.738] | 0.263 [0.228, 0.302] | 0.323 [0.297, 0.350] | 0.297 [0.269, 0.326] |
| 120 | 1542 | 0.351 [0.302, 0.401] | 0.867 [0.746, 0.984] | 0.676 [0.606, 0.745] | 0.325 [0.295, 0.357] | 0.395 [0.364, 0.424] | 0.370 [0.341, 0.400] |
| 168 | 778 | 0.521 [0.458, 0.589] | 0.959 [0.839, 1.075] | 0.660 [0.595, 0.726] | 0.498 [0.439, 0.560] | 0.537 [0.482, 0.599] | 0.540 [0.477, 0.608] |
| 240 | 405 | 0.678 [0.572, 0.784] | 1.041 [0.859, 1.207] | 0.666 [0.593, 0.744] | 0.609 [0.512, 0.703] | 0.689 [0.596, 0.782] | 0.725 [0.646, 0.802] |

**Test (held out, evaluated once): bias m, HH, and RMSE(candidate) - RMSE(raw)
in m [90 % CI]**

| lead h | raw bias, HH | linear bias, HH | rf bias, HH | gbt bias, HH | linear - raw | rf - raw | gbt - raw |
|---|---|---|---|---|---|---|---|
| all | -0.10, 0.182 | +0.06, 0.160 | +0.23, 0.185 | +0.20, 0.180 | -0.029 [-0.040, -0.018] | +0.036 [+0.018, +0.054] | +0.023 [+0.006, +0.040] |
| 24 | -0.11, 0.124 | -0.01, 0.102 | +0.21, 0.137 | +0.16, 0.125 | -0.038 [-0.055, -0.020] | +0.047 [+0.011, +0.083] | +0.020 [-0.014, +0.053] |
| 48 | -0.11, 0.127 | +0.02, 0.106 | +0.20, 0.137 | +0.16, 0.127 | -0.035 [-0.057, -0.013] | +0.041 [+0.002, +0.082] | +0.015 [-0.021, +0.055] |
| 72 | -0.10, 0.149 | +0.05, 0.129 | +0.21, 0.153 | +0.16, 0.142 | -0.030 [-0.057, -0.003] | +0.030 [-0.021, +0.080] | +0.004 [-0.040, +0.049] |
| 120 | -0.12, 0.180 | +0.08, 0.158 | +0.23, 0.186 | +0.18, 0.176 | -0.026 [-0.052, +0.004] | +0.044 [-0.000, +0.092] | +0.020 [-0.020, +0.063] |
| 168 | -0.07, 0.260 | +0.18, 0.235 | +0.27, 0.248 | +0.25, 0.250 | -0.023 [-0.058, +0.013] | +0.016 [-0.034, +0.068] | +0.019 [-0.030, +0.070] |
| 240 | -0.03, 0.333 | +0.23, 0.283 | +0.37, 0.311 | +0.47, 0.323 | -0.069 [-0.107, -0.033] | +0.011 [-0.040, +0.063] | +0.047 [-0.021, +0.117] |

**Test (held out, evaluated once): MSE skill vs persistence / vs LT climatology [90 % CI]**

| lead h | raw | linear | rf | gbt |
|---|---|---|---|---|
| all | 0.79 [0.74, 0.83] / 0.71 [0.67, 0.75] | 0.82 [0.78, 0.86] / 0.76 [0.73, 0.78] | 0.75 [0.69, 0.79] / 0.65 [0.62, 0.68] | 0.76 [0.71, 0.81] / 0.67 [0.64, 0.70] |
| 24 | 0.79 [0.73, 0.85] / 0.86 [0.83, 0.89] | 0.85 [0.80, 0.88] / 0.90 [0.87, 0.92] | 0.68 [0.59, 0.76] / 0.81 [0.75, 0.84] | 0.74 [0.66, 0.80] / 0.84 [0.79, 0.87] |
| 48 | 0.89 [0.86, 0.92] / 0.86 [0.82, 0.89] | 0.92 [0.89, 0.93] / 0.89 [0.87, 0.91] | 0.84 [0.80, 0.88] / 0.81 [0.75, 0.85] | 0.87 [0.83, 0.90] / 0.84 [0.79, 0.87] |
| 72 | 0.86 [0.79, 0.91] / 0.81 [0.75, 0.86] | 0.89 [0.84, 0.92] / 0.85 [0.81, 0.88] | 0.83 [0.78, 0.87] / 0.77 [0.71, 0.81] | 0.85 [0.81, 0.89] / 0.80 [0.75, 0.84] |
| 120 | 0.83 [0.76, 0.88] / 0.73 [0.67, 0.79] | 0.86 [0.80, 0.90] / 0.77 [0.73, 0.81] | 0.79 [0.71, 0.85] / 0.66 [0.58, 0.72] | 0.82 [0.74, 0.87] / 0.70 [0.63, 0.76] |
| 168 | 0.69 [0.58, 0.79] / 0.38 [0.19, 0.52] | 0.72 [0.62, 0.80] / 0.43 [0.25, 0.57] | 0.68 [0.56, 0.77] / 0.34 [0.12, 0.50] | 0.68 [0.56, 0.77] / 0.33 [0.10, 0.50] |
| 240 | 0.54 [0.24, 0.72] / -0.04 [-0.50, 0.30] | 0.63 [0.42, 0.77] / 0.16 [-0.19, 0.43] | 0.54 [0.28, 0.70] / -0.07 [-0.49, 0.25] | 0.49 [0.26, 0.64] / -0.19 [-0.56, 0.11] |

n with persistence: 13438; n with the training-only climatology: 0

**Validation: Hs RMSE in m [90 % CI]**

| lead h | n | raw | persistence | LT climatology | linear | rf | gbt |
|---|---|---|---|---|---|---|---|
| all | 12547 | 0.628 [0.592, 0.662] | 1.219 [1.121, 1.321] | 1.017 [0.989, 1.045] | 0.485 [0.458, 0.511] | 0.495 [0.470, 0.521] | 0.507 [0.481, 0.531] |
| 24 | 1105 | 0.474 [0.431, 0.515] | 1.020 [0.875, 1.160] | 1.001 [0.894, 1.103] | 0.334 [0.303, 0.371] | 0.312 [0.285, 0.339] | 0.334 [0.308, 0.362] |
| 48 | 1079 | 0.484 [0.438, 0.526] | 1.232 [1.078, 1.390] | 1.013 [0.892, 1.123] | 0.344 [0.304, 0.382] | 0.325 [0.291, 0.355] | 0.350 [0.318, 0.380] |
| 72 | 1053 | 0.477 [0.433, 0.521] | 1.334 [1.181, 1.508] | 1.023 [0.902, 1.136] | 0.344 [0.310, 0.376] | 0.374 [0.327, 0.421] | 0.393 [0.349, 0.440] |
| 120 | 1315 | 0.643 [0.562, 0.720] | 1.427 [1.251, 1.598] | 1.031 [0.926, 1.131] | 0.491 [0.410, 0.572] | 0.514 [0.439, 0.584] | 0.527 [0.455, 0.600] |
| 168 | 661 | 0.895 [0.691, 1.093] | 1.222 [1.062, 1.377] | 1.040 [0.932, 1.144] | 0.664 [0.529, 0.817] | 0.748 [0.587, 0.905] | 0.750 [0.587, 0.906] |
| 240 | 340 | 1.125 [0.943, 1.296] | 1.246 [1.071, 1.425] | 1.029 [0.908, 1.142] | 0.928 [0.798, 1.058] | 0.965 [0.829, 1.100] | 0.966 [0.828, 1.101] |

**Validation: bias m, HH, and RMSE(candidate) - RMSE(raw) in m [90 % CI]**

| lead h | raw bias, HH | linear bias, HH | rf bias, HH | gbt bias, HH | linear - raw | rf - raw | gbt - raw |
|---|---|---|---|---|---|---|---|
| all | -0.29, 0.219 | -0.03, 0.162 | -0.03, 0.166 | -0.04, 0.170 | -0.143 [-0.161, -0.127] | -0.133 [-0.148, -0.117] | -0.121 [-0.136, -0.105] |
| 24 | -0.33, 0.166 | -0.08, 0.111 | -0.06, 0.104 | -0.09, 0.112 | -0.140 [-0.173, -0.104] | -0.162 [-0.189, -0.132] | -0.140 [-0.176, -0.103] |
| 48 | -0.33, 0.169 | -0.06, 0.114 | -0.06, 0.108 | -0.09, 0.117 | -0.140 [-0.177, -0.105] | -0.158 [-0.187, -0.128] | -0.134 [-0.170, -0.095] |
| 72 | -0.31, 0.167 | -0.04, 0.115 | -0.04, 0.125 | -0.06, 0.131 | -0.133 [-0.166, -0.098] | -0.104 [-0.146, -0.054] | -0.084 [-0.131, -0.032] |
| 120 | -0.31, 0.224 | -0.04, 0.164 | -0.04, 0.171 | -0.05, 0.175 | -0.152 [-0.184, -0.118] | -0.129 [-0.162, -0.092] | -0.116 [-0.157, -0.072] |
| 168 | -0.19, 0.306 | +0.04, 0.220 | +0.02, 0.248 | +0.01, 0.249 | -0.231 [-0.316, -0.139] | -0.147 [-0.206, -0.083] | -0.146 [-0.202, -0.084] |
| 240 | -0.04, 0.397 | +0.14, 0.319 | +0.15, 0.331 | +0.22, 0.328 | -0.197 [-0.259, -0.129] | -0.160 [-0.223, -0.086] | -0.159 [-0.257, -0.058] |

**Validation: MSE skill vs persistence / vs LT climatology [90 % CI]**

| lead h | raw | linear | rf | gbt |
|---|---|---|---|---|
| all | 0.73 [0.68, 0.78] / 0.62 [0.57, 0.67] | 0.84 [0.81, 0.87] / 0.77 [0.74, 0.80] | 0.83 [0.80, 0.87] / 0.76 [0.73, 0.79] | 0.83 [0.79, 0.86] / 0.75 [0.72, 0.78] |
| 24 | 0.78 [0.71, 0.84] / 0.78 [0.72, 0.82] | 0.89 [0.85, 0.92] / 0.89 [0.85, 0.91] | 0.91 [0.87, 0.93] / 0.90 [0.88, 0.92] | 0.89 [0.86, 0.92] / 0.89 [0.86, 0.91] |
| 48 | 0.85 [0.79, 0.88] / 0.77 [0.71, 0.82] | 0.92 [0.89, 0.94] / 0.88 [0.85, 0.91] | 0.93 [0.91, 0.95] / 0.90 [0.87, 0.92] | 0.92 [0.89, 0.94] / 0.88 [0.85, 0.90] |
| 72 | 0.87 [0.83, 0.90] / 0.78 [0.71, 0.83] | 0.93 [0.91, 0.95] / 0.89 [0.85, 0.91] | 0.92 [0.90, 0.94] / 0.87 [0.83, 0.89] | 0.91 [0.89, 0.93] / 0.85 [0.81, 0.88] |
| 120 | 0.80 [0.72, 0.85] / 0.61 [0.50, 0.70] | 0.88 [0.83, 0.92] / 0.77 [0.68, 0.84] | 0.87 [0.82, 0.91] / 0.75 [0.68, 0.81] | 0.86 [0.81, 0.90] / 0.74 [0.66, 0.80] |
| 168 | 0.46 [0.20, 0.68] / 0.26 [-0.12, 0.56] | 0.70 [0.56, 0.81] / 0.59 [0.37, 0.74] | 0.63 [0.43, 0.77] / 0.48 [0.24, 0.68] | 0.62 [0.44, 0.77] / 0.48 [0.24, 0.67] |
| 240 | 0.18 [-0.25, 0.47] / -0.20 [-0.60, 0.14] | 0.44 [0.16, 0.63] / 0.19 [-0.03, 0.38] | 0.40 [0.09, 0.59] / 0.12 [-0.13, 0.33] | 0.40 [0.09, 0.60] / 0.12 [-0.16, 0.34] |

n with persistence: 12547; n with the training-only climatology: 0

**Rolling-origin folds** (robustness only):

| fold, test cycles | raw | linear | rf | gbt |
| --- | --- | --- | --- | --- |
| 1: 2021-05-13 to 07-01 | 0.644 | 0.613 (-4.9 %) | 0.728 (+13.0 %) | 0.725 (+12.6 %) |
| 2: 2021-07-02 to 08-20 | 0.798 | **0.997 (+25.0 %)**, bias -0.66 m vs raw -0.24 m | 0.777 (-2.7 %) | 0.804 (+0.8 %) |
| 3: 2021-08-21 to 10-22 | 0.624 | 0.497 (-20.2 %) | 0.501 (-19.6 %) | 0.511 (-18.1 %) |

In fold 2 the ridge model was trained on March to mid-June only and tested on the
winter swells of July and August; it pushed large forecasts down and doubled the low
bias. The final model's training data include July and August 2021, but its held-out
test period does not, so this failure mode is neither excluded nor confirmed for the
final model.

### 4.3 D1: pooled WA buoys with leave-one-station-out (diagnostic)

Dataset `wa3.parquet` (fingerprint `d68b32d3482c0bc7`): 252,125 rows from the three WA
stations, same cycle split (test 2021-11-05 to 2021-12-31, 45,581 pairs), default
features (with the observation features). Registered under the separate name
`diag_wa3_pooled` and never activated: it needs cycle-time Hm0 observations, and two of
its three stations have no paired spot.

**Test (held out, evaluated once): Hs RMSE in m [90 % CI]**

| lead h | n | raw | persistence | LT climatology | linear | rf | gbt |
|---|---|---|---|---|---|---|---|
| all | 45581 | 0.366 [0.345, 0.388] | 0.724 [0.677, 0.775] | 0.614 [0.596, 0.632] | 0.318 [0.301, 0.335] | 0.331 [0.317, 0.345] | 0.313 [0.299, 0.329] |
| 24 | 3939 | 0.280 [0.259, 0.302] | 0.496 [0.452, 0.538] | 0.610 [0.566, 0.659] | 0.223 [0.210, 0.237] | 0.243 [0.231, 0.256] | 0.222 [0.211, 0.234] |
| 48 | 3899 | 0.283 [0.260, 0.310] | 0.657 [0.608, 0.705] | 0.607 [0.562, 0.658] | 0.228 [0.215, 0.244] | 0.246 [0.234, 0.259] | 0.226 [0.214, 0.239] |
| 72 | 3821 | 0.305 [0.273, 0.340] | 0.707 [0.649, 0.765] | 0.611 [0.562, 0.658] | 0.261 [0.236, 0.288] | 0.275 [0.256, 0.295] | 0.259 [0.238, 0.283] |
| 120 | 4809 | 0.363 [0.325, 0.405] | 0.815 [0.745, 0.885] | 0.618 [0.573, 0.664] | 0.319 [0.289, 0.350] | 0.325 [0.304, 0.347] | 0.312 [0.288, 0.338] |
| 168 | 2431 | 0.504 [0.451, 0.554] | 0.907 [0.810, 0.995] | 0.616 [0.568, 0.663] | 0.458 [0.412, 0.500] | 0.476 [0.427, 0.525] | 0.451 [0.409, 0.492] |
| 240 | 1269 | 0.632 [0.562, 0.705] | 0.951 [0.823, 1.068] | 0.617 [0.563, 0.671] | 0.554 [0.490, 0.619] | 0.573 [0.502, 0.643] | 0.544 [0.480, 0.610] |

**Test period by station: RMSE m (bias m)**

| station | n | raw | linear | rf | gbt |
|---|---|---|---|---|---|
| aodn:cape-naturaliste | 14658 | 0.360 (-0.10) | 0.333 (+0.03) | 0.371 (+0.17) | 0.337 (+0.07) |
| aodn:esperance-04 | 14623 | 0.343 (-0.12) | 0.297 (-0.01) | 0.302 (+0.08) | 0.283 (+0.01) |
| aodn:rottnest-island-02 | 16300 | 0.391 (-0.21) | 0.321 (-0.06) | 0.317 (+0.06) | 0.318 (-0.03) |

Decisions under the unchanged criteria: linear (test 0.318 m, -13.2 % vs raw 0.366 m) and
GBT (0.313 m, -14.4 %) pass (a)-(f); RF (0.331 m, -9.7 %) fails (d) only.

Rolling folds (change vs raw): linear +8.0 %, -1.1 %, -16.3 %; RF +2.1 %, -6.9 %,
-16.4 %; GBT +0.3 %, -4.3 %, -17.1 %.

Leave-one-station-out (train on the other two stations' development rows, test on the
held-out station's test rows), change in test RMSE vs raw:

| held-out station | raw RMSE | linear | rf | gbt |
| --- | --- | --- | --- | --- |
| Cape Naturaliste | 0.360 | -3.4 % | -4.0 % | -9.0 % |
| Esperance 04 | 0.343 | -7.0 % | -4.3 % | -12.1 % |
| Rottnest 02 | 0.391 | -18.5 % | -17.1 % | -16.9 % |

With three times the data GBT beats raw at every station and RF at two of three (RF
is still worse than raw at Cape Naturaliste, 0.371 vs 0.360 m), and every model
improves a buoy it was not trained on. The gain at the held-out Cape
Naturaliste buoy (3-9 %) is smaller than at Rottnest (17-19 %), whose raw bias is
largest (-0.21 m).

## 5. Decision per criterion

Both runs use the same 14,658 test pairs (Cape Naturaliste, 2021-11-05 to
2021-12-31, 114 cycles); raw RMSE 0.360 m, raw HH 0.182, raw bias -0.102 m.

| version | (a) RMSE >= 5 % below raw | (b) 90 % CI of RMSE - raw below 0 | (c) no bucket > 3 % worse | (d) vs ridge | (e) n >= 300 | (f) HH <= raw | result |
| --- | --- | --- | --- | --- | --- | --- | --- |
| P1 linear (`offshore_hs_residual@v1`) | pass: 0.330 m, -8.4 % | pass: -0.030 [-0.040, -0.020] m | pass: worst -5.4 % (168 h) | pass (is ridge) | pass: 14,658 | pass: 0.160 | **passes all; not activated** |
| P1 rf (`@v2`) | fail: 0.384 m, +6.7 % | fail: +0.024 [+0.009, +0.040] | fail: +11.9 % (120 h) | fail: +16.5 % | pass | pass: 0.181 | rejected |
| P1 gbt (`@v3`) | fail: 0.390 m, +8.3 % | fail: +0.030 [+0.013, +0.047] | fail: +15.6 % (120 h) | fail: +18.3 % | pass | fail: 0.183 | rejected |
| P2 linear (`@v4`) | pass: 0.331 m, -8.1 % | pass: -0.029 [-0.040, -0.018] m | pass: worst -4.4 % (168 h) | pass (is ridge) | pass: 14,658 | pass: 0.160 | **passes all; activated and exported** |
| P2 rf (`@v5`) | fail: 0.396 m, +10.0 % | fail: +0.036 [+0.018, +0.054] | fail: +18.9 % (24 h) | fail: +19.6 % | pass | fail: 0.185 | rejected |
| P2 gbt (`@v6`) | fail: 0.383 m, +6.3 % | fail: +0.023 [+0.006, +0.040] | fail: +8.0 % (24 h) | fail: +15.6 % | pass | pass: 0.180 | rejected |

Per-bucket RMSE change of the two passing versions (criterion (c)), P1 / P2: 24 h -15.7 %
/ -15.2 %, 48 h -14.2 / -13.9, 72 h -10.4 / -10.4, 120 h -7.2 / -7.5, 168 h -5.4 / -4.4,
240 h -11.2 / -10.2, leads outside the buckets -7.7 / -7.3 %. The 90 % CIs of these
per-bucket gains exclude zero at 24-72 h and at 240 h; at 120 h and 168 h they include
zero (tables in 4.1 and 4.2).

**Why RF and GBT fail.** On validation (September-October) both beat raw clearly
(0.495 and 0.51 m vs 0.628 m), but on the test period they over-correct: they raise the
forecast by about 0.2 m on average (test bias +0.20 to +0.23 m against raw -0.10 m), so
they are worse than raw at every lead. The ridge model raises it less (+0.05 to +0.06 m).
With one buoy and nine months, the trees fit the conditions of the training months too
closely; pooled over three buoys (D1) GBT passes every criterion and RF fails only (d).

**Which version is usable in real time.** A correction gets its features at the cycle
time. P1 v1 uses the observation features (an Hm0 observation at most 1 h before the
cycle and the analysis error derived from it). The AODN realtime feed of Cape
Naturaliste reports only H1/3 (`WHTH`), and its delayed-mode Hm0 arrives months to
years later, so in real time those features are always missing. Because they were
present in 96.7 % of its training rows they are not formally "required", so v1 would
still run on median-imputed values, but that is not the configuration the criteria
judged (and the apply API takes a bare number with no height kind, so an H1/3 value
could be passed by mistake). **P1 v1 is therefore not activated.** P2 v4 needs only
lead, model Hs, partition 1 and the valid time, all available from every forecast at
issue time; it passes every criterion, so **P2 v4 was activated** in the backtest
database (as the auto-activation rule would also choose: lowest validation RMSE among
passing versions) and exported with `ml export-manifest` to
`backend/ml_models/offshore_hs_residual/` (artifact 2,780 bytes, manifest 127 KB,
committed). `ml import-manifest` registers it as CANDIDATE; it becomes active in an
environment only after `ml activate`.

**Recommendation that does not change the decision.** The criteria passed, but the
evidence is thin: one buoy, one eight-week test period in late spring, a 2021 model
version, a rolling fold in which the same model class was 25 % worse than raw, and a
weekly-block interval that touches zero. Before relying on the correction for user
alerts, run it in shadow (corrected and raw side by side) and re-evaluate when
delayed-mode Hm0 from 2022 onward is published, or on a longer archive (section 7).

### Sensitivity checks (diagnostics, not criteria)

| check | P1 v1 | P2 v4 |
| --- | --- | --- |
| (b) with cycles grouped in 72 h blocks (19 blocks) | -0.030 [-0.048, -0.011] m | -0.029 [-0.048, -0.009] m |
| (b) with 168 h blocks (9 blocks) | -0.030 [-0.056, -0.002] m | -0.029 [-0.057, **+0.002**] m |
| test RMSE with the observation features removed | 0.334 m (raw 0.360) | not applicable (none used) |
| test RMSE by valid month, raw -> corrected | Nov 0.294 -> 0.266, Dec 0.409 -> 0.376 | Nov 0.294 -> 0.268, Dec 0.409 -> 0.377 |
| correction factor on test, 5 / 50 / 95 % | 0.98 / 1.08 / 1.24 | 0.99 / 1.09 / 1.25 |
| factors at the [0.5, 2.0] clamp | 0 % | 0 % |

The interval in (b) widens as blocks get longer, because consecutive cycles share
weather. The criterion as implemented uses one block per cycle, and that is what
decided; the longer blocks are reported so the reader can judge the margin.

## 6. Secondary result: H1/3 targets (earlier runs, not repeated here)

These were produced before this study by the ML builder and the ML review, against
**time-domain H1/3** observations (AODN 2026 realtime feeds, `WHTH`, not QC'd). H1/3 and
Hm0 differ by several per cent, so these numbers must not be compared one to one with
the Hm0 results above, and no version trained on them can be activated (the registry
refuses non-Hm0 targets). They were not re-run for this document.

* ML builder: GFS-Wave 00Z bulletins 2026-03-01 to 2026-10-10 at AODN Eden and Cape
  Naturaliste (H1/3) plus 8 days of Met Office K5 (Hm0). Backtest on 15,341 H1/3 pairs:
  RMSE 0.409 m at 24 h and 0.823 m at 240 h, bias about -0.2 m. `ml train` with test
  period 2026-08-27 to 2026-10-10 (3,033 pairs): raw 0.532 m, GBT 0.392 m (-26 %), RF
  0.394 m, ridge 0.472 m; all passed (a)-(f) on that test period, none is activatable
  (target H1/3), and ridge was worse than raw in 2 of 3 rolling folds.
* ML review re-check (Cape Naturaliste alone, 2026 realtime H1/3): backtest 6,480 pairs,
  RMSE 0.377 m at 24 h and 1.115 m at 240 h; `ml train --hs-kind h_third` with test from
  2026-08-29 (1,230 pairs): raw 0.796 m, RF 0.648 m passed (a)-(f), ridge failed (c),
  GBT failed (d).

The two H1/3 studies and this Hm0 study disagree on which candidate class wins (tree
models on the 2026 H1/3 data and on the pooled 2021 Hm0 data, ridge on the single-buoy
2021 Hm0 data), which is one more sign that a single short test period does not settle
the choice.

## 7. Caveats

* **Offshore Hs at buoys only.** Everything here is offshore Hm0 at three buoy positions.
  Breaking (surf) height is not validated at all: no nearshore or surf-height
  observations exist in the project. Offshore skill does not carry over to surf height.
* **One region for the ML correction.** The correction was trained and tested in
  south-west Western Australia only (Rottnest, Cape Naturaliste, Esperance). Section 11
  backtests the raw forecast at NDBC buoys off Hawaii and California, but no correction
  was trained or tested there. The results do not transfer automatically to other
  regions, other buoys or other forecast sources.
* **Applicability of the exported correction.** It is applied only at the spots paired
  with its training station through an offshore relation (`margaret-river-main-break`,
  `the-box`, `north-point-gracetown`), only at leads 3-240 h, and only to the source
  family it was trained on (`ncep_gfs`, when the caller passes the family). At the spots
  its inputs would come from `wave_forecasts` (GFS-Wave at each spot's own forecast
  point, 52-54 km from the buoy, with `primary_swell_*` from the GRIB SWELL1 partition),
  whereas it was trained on the station bulletin (partition 1 as listed, which can be the
  wind sea). That transfer is not validated; the live check in section 10 shows the
  inputs at the spots fall partly outside the validated range. The forecast pipeline
  applies an ACTIVE correction through `ml/hook.py` (ADR-012); the exported version is
  not active by default.
* **One season, one model version, one short test period.** Nine months of 2021 (GFS
  v16 before the v16.3.0 upgrade of 2022-11-29). The held-out test covers 2021-11-05 to
  2021-12-31 only (late spring / early summer, mean observed Hm0 1.85-2.08 m); the
  winter performance of the final model is not tested on held-out data, and the rolling
  fold that tested on winter (fold 2) made the ridge correction 17-25 % worse than raw.
* **Uncertainty of criterion (b).** The cycle bootstrap treats 12-hourly cycles as
  independent blocks. With weekly blocks the 90 % interval of the activated version's
  RMSE gain touches zero ([-0.057, +0.002] m).
* **No real-time verification at the training buoy.** Cape Naturaliste's realtime feed
  reports H1/3 only (sparse, not QC'd), and its delayed-mode Hm0 currently ends in
  2021-12. The scheduled verification job will therefore find no Hm0 pairs to score the
  correction against until newer delayed-mode files are published.
* **Climatology reference.** The implemented training-only climatology is empty or
  degenerate with under a year of training data; the long-term climatology used instead
  is computed from the same buoys' 2004-2021 delayed-mode records, before the evaluated
  period.
* **No accuracy claim beyond these numbers.** The rows above are held-out results on the
  stated periods and stations. They are not a general statement about GFS-Wave or the
  correction.

## 8. Reproduction

Own database (`DATABASE_URL=...swell_bt_live`), `ML_ARTIFACT_DIR` set to a scratch
directory, run from `backend/`:

```
python -m app.cli init-db
python -m app.cli obs seed-stations
python -m app.cli obs ingest --provider aodn --aodn-dataset delayed_qc \
    --station aodn:cape-naturaliste --station aodn:rottnest-island-02 \
    --station aodn:esperance-04 --since 2021-03-22T00:00:00Z --until 2022-01-15T00:00:00Z
python -m app.cli obs ingest --provider aodn --aodn-dataset delayed_qc \
    --station aodn:tweed-heads --station aodn:gold-coast \
    --since 2021-03-22T00:00:00Z --until 2023-01-15T00:00:00Z        # H1/3 only, not used
python -m app.cli obs ingest --provider aodn --aodn-dataset delayed_qc \
    --station aodn:apollo-bay --since 2021-03-22T00:00:00Z --until 2026-07-15T00:00:00Z
python -m app.cli obs ingest --provider aodn --aodn-dataset delayed_qc \
    --station aodn:cape-naturaliste --station aodn:rottnest-island-02 \
    --station aodn:esperance-04 --since 2004-01-01T00:00:00Z --until 2021-03-22T00:00:00Z
FORECAST_DAYS=10 python -m app.cli obs collect-forecasts --start 2021-03-22T18:00:00Z \
    --end 2021-12-31T12:00:00Z --cycles 0,12 --station aodn:cape-naturaliste \
    --station aodn:rottnest-island-02 --station aodn:esperance-04
FORECAST_DAYS=10 python -m app.cli obs collect-forecasts --start 2021-03-22T18:00:00Z \
    --end 2026-06-07T12:00:00Z --station aodn:apollo-bay --bulletin-id NKING   # proxy
python -m app.cli verify pair --full
python -m app.cli verify backtest --source gfswave_bulletin --hs-kind spectral_hm0 \
    --folds 5 --boot 1000 --json --station aodn:cape-naturaliste  # each station, pooled
python -m app.cli ml build-dataset --source gfswave_bulletin \
    --station aodn:cape-naturaliste --out capenat.parquet
python -m app.cli ml train --dataset capenat.parquet --candidates linear,rf,gbt \
    --folds 3 --boot 1000                                   # P1
python -m app.cli ml train --dataset capenat.parquet --candidates linear,rf,gbt \
    --folds 3 --boot 1000 --no-obs-features                 # P2
python -m app.cli ml train --name diag_wa3_pooled --dataset wa3.parquet \
    --candidates linear,rf,gbt --folds 3 --boot 1000 --loso  # D1
python ../scripts/ml_evaluation_report.py EXPERIMENT_ID capenat.parquet --boot 1000 \
    --lt-climatology-before 2021-03-22T00:00:00Z --json eval.json
python -m app.cli ml activate VERSION_ID
python -m app.cli ml export-manifest VERSION_ID ml_models/offshore_hs_residual
```

`FORECAST_DAYS=10` keeps leads to 240 h (the largest lead bucket). The analysis-time
exposure check (section 1) used the repository's loaders and `match_observations` on
leads 0-2 h; it is a one-off query, not a command.

## 9. Verified live vs mock

* **Live:** every number in this document (NOAA NODD bulletins and `.spec` headers, AODN
  delayed-QC Parquet, AODN source NetCDF metadata for the two added stations). The
  exported manifest was imported into a fresh database with `ml import-manifest`
  (status `candidate`), activated with `ml activate`, and applied through
  `load_active_correction(...).correct_hs(...)` with synthetic feature values: factor
  1.07 at 48 h, raw value returned outside 3-240 h and when a required feature is
  missing.
* **Mock only:** the new `--aodn-dataset` filter and `--bulletin-id` proxy option are
  covered by integration tests with mocked HTTP; they were also used live for this study.
* **Not available here (2026-10-11):** NDBC, CDIP and CO-OPS were blocked in the sandbox
  when sections 1-10 were produced. NDBC and CDIP became reachable on 2026-10-12 and are
  used in section 11 (its own live/mock list is in 11.8).

## 10. Deployment in the application

*Update 2026-10-12: for the US spots this is superseded by section 12.4 (an NDBC-trained
random forest, v6, that passed the transfer check). P2 v4 below is unchanged and stays an
inactive candidate for the Western Australian spots.*

**Decision: the passing correction (P2 v4) is shipped as a registered candidate in
`backend/ml_models/offshore_hs_residual/`, not active by default.** Forecasts shown to users
stay uncorrected until an operator activates it. This does not change the deployment
criteria (it passed them); it is about where the model would be applied versus where it
was validated.

*Live transfer check (2026-10-11).* The manifest was imported into the scratch database
holding the live run `noaa-gfs:2026101100` (gridded GFS-Wave at every spot's forecast
point), activated, and the run's predictions recomputed through the pipeline hook
(`ml/hook.py`):

| | Held-out test at the buoy (bulletin inputs) | Spots, gridded inputs (live) |
|---|---|---|
| rows corrected | 14,658 | 201 (67 of 81 hours at each of the 3 paired spots; none elsewhere) |
| factor 5 / 50 / 95 % | 0.98 / 1.08 / 1.24 | 0.79 / 1.06 / 1.09 |
| factors below 0.98 | about 5 % | 36 of 201 (18 %), minimum 0.60 |

The median agrees with the buoy result (the raw model is biased low), but a fifth of the
corrected hours would cut offshore Hs by 2-40 %, outside the behaviour that was tested.
The cause is the input shift between the bulletin (partition 1 as listed, at the buoy)
and the gridded fields at the spots 52-54 km away.

*Why not activate anyway.* The improvement is 8 % on one buoy over eight weeks of 2021,
on a GFS-Wave version that has since been upgraded, with a weekly-block bootstrap interval
that touches zero and one rolling fold 17-25 % worse than raw. Applying it to inputs
outside the validated range could make the surf estimate worse at the only three spots
it covers. The conservative choice is the raw forecast.

*What would justify activation.* Train and test on the same inputs the spots use: gridded
GFS-Wave sampled at the buoy (`forecasts.noaa.sample_gfs_points`) archived as point
forecasts, verified against delayed-mode Hm0 once 2022+ files are published, on the
current model version, with a winter season in the test period. Then:

```bash
python -m app.cli ml import-manifest ml_models/offshore_hs_residual/offshore_hs_residual-v4.manifest.json
python -m app.cli ml activate <id printed by the import>    # explicit operator step
python -m app.cli ml deactivate offshore_hs_residual       # back to raw at any time
```

## 11. NDBC buoys off Hawaii and California (2024-10 to 2026-10)

A second backtest of the **raw** GFS-Wave station bulletins, against NOAA NDBC buoys near
the eight US spots, run on 2026-10-12 after `www.ndbc.noaa.gov` became reachable. Every
number comes from live downloads (NDBC files, NOAA NODD bulletins) into a scratch
database (`swell_ndbc_live`) and the repository's own commands; nothing is synthetic.
What the NDBC adapter was checked against, and the fixes, are in
`docs/observations.md` ("NOAA NDBC (US), live-verified 2026-10-12").

**What this measures.** Offshore spectral Hm0 at the buoys (NDBC `WVHT` is
4·sqrt(m0) from the non-directional spectrum, NDBC wavecalc.shtml; it equals CDIP
`waveHs` exactly at the six CDIP buoys checked) against the bulletin's total-sea `Hst`
at the bulletin output point next to each buoy. It does **not** measure breaking (surf)
height. No ML correction was trained or applied here (the shipped correction covers the
Western Australian spots only).

### 11.1 Data

| Data | Window | Volume | Downloaded |
| --- | --- | --- | --- |
| NDBC observations, 11 stations (yearly `.txt.gz` 2024 and 2025, monthly files January-August 2026, realtime `.txt` + `.spec`) | 2024-10-12 00:00 to 2026-10-12 12:00 UTC | 337,400 records (spectral Hm0), 0 errors | 135 requests, 16,983,432 bytes |
| NDBC observations for the long-term climatology, 10 stations (yearly files 2019-2024) | 2019-10-12 to 2024-10-12 | 680,809 records | 61 requests, 24,265,456 bytes |
| GFS-Wave station bulletins, 10 stations, 00Z cycle on every second day (`obs collect-forecasts`) | 2024-10-12 00Z to 2026-10-12 00Z | 366 cycles x 161 leads (hourly to 120 h, 3-hourly to 240 h) = 58,926 rows per station, 589,260 in all; 0 missing, 0 errors, 0 malformed cells | 3,996 bulletin files of about 52 KB (about 208 MB) |

* **Bulletin archive.** The NODD bucket holds bulletins from 2021-03-22 18Z, so the
  24-month window was not limited by the archive. Bulletins exist (bulls.t00z listing of
  2026-10-11) for 51201, 51205, 51101, 51000, 51001, 46026, 46237, 46214, 46012, 46224,
  46086, 46053, 46054 and 46025; there is none for 46277.
* **Sampling.** Only the 00Z cycle of every second day was archived (25 % of the 1,462
  00Z/12Z cycles in the window). A stored forecast row takes about 870 bytes in
  PostgreSQL; all cycles for ten stations would have needed about 2 GB more on a build
  machine shared with other work. The first month (2024-10-12 to 2024-11-11) was first
  collected daily for all 16 bulletin stations of the catalog (496 files); the rows of
  the odd days and of the six non-NDBC stations were then deleted from the scratch
  database so that every station and month has the same sampling. The days were fixed by
  the calendar (every second day from the first), not by the weather. Consecutive tested
  cycles are 48 h apart, so the cycle-block bootstrap resamples less correlated blocks
  than with 12-hourly cycles.
* **Gaps** (months with fewer than half the expected reports): 46026 February to May
  2025 (no reports in March and April; new hull 2025-05-20); 51205 January to March and
  June to August 2025; 46214 August, November and December 2025 and January 2026
  (redeployed 2026-01-26); 51101 February to September 2026 (redeployed 2026-09-20);
  51201 March and April 2026 (the monthly files answer 404 under both names); 46237
  November and December 2024, August and September 2026; 46053 October and December
  2024, March 2025, and no reports after 2026-10-06 05:40 UTC; 46054 November 2025;
  46086 September 2025. 46224 has none. Forecasts without an observation within 30 min
  are dropped and counted, never filled.
* **Pairing** (`verify pair --full`): 585,970 forecasts with a past valid time scanned,
  504,351 Hs pairs, **all against spectral Hm0** (`observed_kind = spectral_hm0`), 81,619
  forecasts without an observation within 30 min (the gaps above). Per station: 46026
  50,090, 46053 50,441, 46054 55,899, 46086 53,961, 46214 47,209, 46224 58,258, 46237
  50,851, 51101 40,536, 51201 52,569, 51205 44,537 (all leads, analysis rows included).
  Mean observed Hm0 over these pairs is 0.93 m (46224) to 2.43 m (46214), mean bulletin
  Hst 0.71 to 2.36 m. No Tp or direction pairs: the bulletin has no total-sea peak period
  or direction.
* **QC.** Observations up to 2026-08-31 come from NDBC's quality-controlled yearly and
  monthly archive (`probably_good`). Those from 2026-09-01 (46237 from 2026-10-02, 51101
  from 2026-09-21) come from the realtime files (`not_qc`, automated checks only): the
  adapter reads a month's QC'd file only when the realtime file does not cover the whole
  month, and on 2026-10-12 it covered September. That is 2-9 % of each station's
  test-period observations; a sensitivity run without them is in 11.4.

### 11.2 Pairings (spot to buoy)

Positions from NDBC `activestations.xml` (2026-10-12), depths from the NDBC station
pages (CDIP's own value where it differs), bulletin output points and WW3 depths from
the 2026-10-11 00Z bulletins (`.bull` header, `.spec` point line). Bearing = direction
from the break to the buoy. Analysis-time check: bulletin `Hst` at leads 0-2 h (never
scored) against the buoy over the whole window, as in section 1; ratio = median of
observed / model, by the sector of bulletin partition 1 (FROM; sectors with 30 or more
rows).

| Spot(s) | Buoy (operator, platform, depth) | Relation | km to break, bearing | Bulletin point km / WW3 depth | Analysis time: n, bias m, RMSE m, CC, obs/model (by sector) | Representative? |
| --- | --- | --- | --- | --- | --- | --- |
| pipeline, sunset-beach, waimea-bay | 51201 Waimea Bay (PacIOOS / CDIP 106, Waverider DWR-M3, 200 m) | offshore_reference | 6.7 / 7.8 / 6.0, W to WNW (263-300 deg) | 0.2 / 472.7 m | 987, -0.29, 0.40, 0.944, 1.20 (N 1.22, NE 1.20, NW 1.18) | **yes**: directly offshore of the North Shore in the same NW-N exposure; ratio uniform across directions |
| pipeline, sunset-beach, waimea-bay | 51101 NW Hawaii Two (NDBC, 4,847 m) | upstream_regional | 508, NW (306 deg) | 2.6 / 4,715.7 m | 758, -0.16, 0.33, 0.948, 1.06 (uniform 1.04-1.07) | context only: open ocean on the NW swell path, about 9-10 h upstream at 15 s |
| jaws-peahi | 51205 Pauwela (PacIOOS / CDIP 187, DWR-M3, 200 m) | offshore_reference | 15.3, WNW (303 deg) | 0.2 / 242.7 m | 836, -0.41, 0.55, 0.838, 1.24 (E 1.17, NE 1.25, N 1.39, NW 1.57) | **yes, with a caveat**: same open north coast of Maui, but the buoy exceeds the model far more for N-NW swell (ratio 1.39-1.57) than for E-NE seas (1.17-1.25), so model error depends on direction |
| ocean-beach-sf, mavericks | 46026 San Francisco (NDBC 3-m discus, 53 m) | offshore_reference | 28.6 / 41.3, W / NW (268 / 314 deg) | 1.6 / 53.0 m | 939, -0.22, 0.33, 0.946, 1.13 (W 1.14, NW 1.14, S 1.10, SW 1.07) | **yes**: open shelf in the same W-NW exposure, model point at the same depth |
| ocean-beach-sf | 46237 San Francisco Bar (CDIP 142, DWR-M4, 16.8 m) | nearshore_reference | 11.1, WNW (286 deg) | 0.4 / 26.7 m | 954, -0.35, 0.45, 0.911, 1.30 (W 1.32, SW 1.30) | partly: the swell is already transformed by the bar, which the grid resolves only roughly (26.7 m model depth) |
| ocean-beach-sf, mavericks | 46214 Point Reyes (CDIP 029, DWR-M3, 600 m) | upstream_regional | 86.2 / 98.6, WNW (284 / 301 deg) | 0.7 / 818.8 m | 888, -0.15, 0.36, 0.942, 1.05 (NW 1.06, W 1.03) | context only: deep water upstream for W-NW swell |
| trestles | 46224 Oceanside Offshore (CDIP 045, DWR-M3, 238 m) | offshore_reference | 25.2, SSE (154 deg) | 0.3 / 361.3 m | 1,095, -0.24, 0.28, 0.863, 1.35 (S 1.34, SW 1.31, W 1.39) | **yes, with a caveat**: same island-shadowed Bight exposure, but observed Hm0 is 31-39 % above the model at analysis time in every sector |
| trestles | 46277 Green Beach Offshore (CDIP 271, DWR-M4, 303 m) | offshore_reference | 8.3, SW (232 deg) | no bulletin | not possible | the closest offshore buoy for Trestles, but not in the bulletin backtest (observations ingested for later gridded verification) |
| trestles | 46086 San Clemente Basin (NDBC 2.1-m buoy, 1,862 m) | upstream_regional | 106, SSW (203 deg) | 2.8 / 1,513.2 m | 1,007, -0.34, 0.43, 0.887, 1.28 (S 1.20, W 1.31, NW 1.27) | context only: outside the island shadow, upstream for southern-hemisphere swell |
| rincon | 46053 East Santa Barbara (NDBC 3-m discus, 417 m) | offshore_reference | 36.3, WSW (247 deg) | 1.0 / 283.2 m | 935, -0.13, 0.24, 0.883, 1.14 (W 1.14) | **yes**: in the channel that W swell runs down to Rincon, edge of its window |
| rincon | 46054 West Santa Barbara (NDBC 3-m discus, 454 m) | upstream_regional | 91.6, W (263 deg) | 1.7 / 290.6 m | 1,049, -0.42, 0.50, 0.936, 1.25 (NW 1.26, W 1.25) | context only: western entrance of the channel, upstream for W swell |

Every pairing has its justification in the catalog (`data/observation_stations.json`,
`paired_spots[].note`). Buoy Hs is an offshore reference for each spot, never its surf
height. The analysis-time ratios differ from station to station (1.05 to 1.35): part of
what follows is the model's local error at each output point, which is why the
stations are also reported separately.

**Considered and not used.** 46012 Half Moon Bay (the traditional Mavericks buoy) has
been an uncrewed surface vehicle since 2023-08, moving within an 18.5 km watch circle,
without APD or MWD and without yearly files for 2024 or 2025. 51001 (NW Hawaii): its
bulletin output point is 118 km from the current mooring. 51000 (NE of Oahu): outside
the NW-N windows of the Hawaiian spots, output point 31 km from the buoy. 51207, 51210
(Kaneohe Bay), 51202 (Mokapu Point), 51211 (Pearl Harbor entrance) and 51212 (Barbers
Point): 37-48 km from the North Shore breaks on the windward or south shore of Oahu, in
other exposure. 46025 Santa Monica Basin: 77 km SSE of Rincon, outside its 250-300
degree window. 46217 (Anacapa Passage): a bulletin exists, but the station is not in
activestations.xml and has no yearly, monthly or realtime file for 2024-2026. 46285 and
46275 (Capistrano Beach and Red Beach Nearshore, 11-13 km from Trestles): nearshore
CDIP buoys without a bulletin. 46042 (Monterey) and 46013 (Bodega Bay), 79-109 km from
the San Francisco spots, were not added: 46026, 46237 and 46214 already cover the
offshore, bar and upstream positions there. No other spot of the 50 has any station
listed in activestations.xml (2026-10-12) within 150 km; the nearest NDBC-listed
stations of the other spots are 200 km or more away (for example the Gascogne buoy
62001, 273 km from Mundaka).

### 11.3 Results (`verify backtest`, raw GFS-Wave bulletins vs NDBC Hm0)

Rolling origin, 5 folds, gap 312 h; test blocks 2025-02-11 to 2026-10-12 (fold 1
2025-02-11 to 06-11, 2: 06-13 to 10-11, 3: 2025-10-13 to 2026-02-10, 4: 02-12 to 06-12,
5: 06-14 to 10-12), cycle-block bootstrap with 1,000 replicates, seed 20261010, 90 %
intervals. Lead buckets as in section 2. "LT clim" is the long-term monthly climatology
of each station's QC'd Hm0 from 2019-10-12 to 2024-10-11 (`--lt-climatology-before
2024-10-12`; 12 months x 10 stations, 5 years per month, all before the first tested
cycle, every tested row covered). "SS clim (folds)" is the implemented training-fold
climatology (its sample size in brackets); with two years of data it now covers
58 % of the rows and has an RMSE of 0.655 m, against 0.581 m for the long-term one.

**Pooled, all ten stations**: 409,344 test pairs, 305 cycles.

| lead h | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | RMSE persistence | SS clim (folds) (n) | SS LT clim [90 % CI] | RMSE LT clim |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 409344 | 305 | -0.23 [-0.24, -0.22] | 0.33 | 0.437 [0.426, 0.447] | 0.21 | 0.26 | 0.87 | 0.59 [0.56, 0.62] | 0.682 | 0.54 (237754) | 0.43 [0.41, 0.46] | 0.581 |
| 24 | 33791 | 304 | -0.25 [-0.26, -0.24] | 0.30 | 0.376 [0.364, 0.389] | 0.16 | 0.23 | 0.93 | 0.47 [0.42, 0.52] | 0.519 | 0.66 (19511) | 0.58 [0.53, 0.62] | 0.580 |
| 48 | 33801 | 304 | -0.24 [-0.25, -0.23] | 0.30 | 0.376 [0.364, 0.389] | 0.16 | 0.23 | 0.92 | 0.71 [0.67, 0.74] | 0.694 | 0.66 (19600) | 0.58 [0.54, 0.61] | 0.578 |
| 72 | 33687 | 303 | -0.24 [-0.25, -0.22] | 0.30 | 0.385 [0.371, 0.397] | 0.17 | 0.23 | 0.91 | 0.72 [0.70, 0.75] | 0.731 | 0.64 (19511) | 0.56 [0.52, 0.60] | 0.580 |
| 120 | 43922 | 302 | -0.22 [-0.24, -0.21] | 0.32 | 0.425 [0.407, 0.442] | 0.20 | 0.26 | 0.88 | 0.67 [0.63, 0.70] | 0.734 | 0.57 (25522) | 0.46 [0.41, 0.51] | 0.580 |
| 168 | 23191 | 301 | -0.20 [-0.22, -0.18] | 0.37 | 0.508 [0.488, 0.530] | 0.26 | 0.31 | 0.79 | 0.55 [0.49, 0.60] | 0.758 | 0.39 (13519) | 0.23 [0.16, 0.30] | 0.580 |
| 240 | 12851 | 300 | -0.21 [-0.25, -0.18] | 0.47 | 0.642 [0.610, 0.677] | 0.34 | 0.40 | 0.64 | 0.26 [0.16, 0.35] | 0.745 | -0.03 (7532) | -0.28 [-0.41, -0.16] | 0.568 |

**Pooled, the five offshore references of the paired spots** (51201, 51205, 46026,
46224, 46053; the buoy that stands for each spot's offshore swell): 210,878 test pairs,
305 cycles.

| lead h | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | RMSE persistence | SS clim (folds) (n) | SS LT clim [90 % CI] | RMSE LT clim |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| all | 210878 | 305 | -0.23 [-0.24, -0.22] | 0.30 | 0.399 [0.388, 0.410] | 0.21 | 0.28 | 0.87 | 0.53 [0.50, 0.57] | 0.583 | 0.43 (127704) | 0.38 [0.35, 0.42] | 0.508 |
| 24 | 17404 | 304 | -0.24 [-0.26, -0.23] | 0.28 | 0.355 [0.339, 0.370] | 0.17 | 0.25 | 0.92 | 0.33 [0.25, 0.40] | 0.434 | 0.56 (10486) | 0.51 [0.46, 0.56] | 0.507 |
| 48 | 17394 | 304 | -0.23 [-0.25, -0.22] | 0.28 | 0.361 [0.346, 0.376] | 0.18 | 0.25 | 0.91 | 0.63 [0.58, 0.67] | 0.593 | 0.55 (10535) | 0.49 [0.44, 0.54] | 0.505 |
| 72 | 17365 | 303 | -0.23 [-0.25, -0.22] | 0.29 | 0.367 [0.351, 0.381] | 0.18 | 0.26 | 0.90 | 0.65 [0.61, 0.69] | 0.621 | 0.52 (10486) | 0.48 [0.42, 0.53] | 0.507 |
| 120 | 22651 | 302 | -0.23 [-0.24, -0.21] | 0.30 | 0.389 [0.372, 0.405] | 0.20 | 0.28 | 0.87 | 0.62 [0.58, 0.66] | 0.633 | 0.46 (13723) | 0.41 [0.35, 0.47] | 0.508 |
| 168 | 11965 | 301 | -0.21 [-0.23, -0.19] | 0.33 | 0.447 [0.427, 0.469] | 0.25 | 0.32 | 0.80 | 0.53 [0.47, 0.58] | 0.646 | 0.29 (7259) | 0.23 [0.14, 0.30] | 0.508 |
| 240 | 6630 | 300 | -0.22 [-0.25, -0.19] | 0.40 | 0.550 [0.521, 0.582] | 0.33 | 0.40 | 0.65 | 0.29 [0.18, 0.38] | 0.650 | -0.14 (4037) | -0.22 [-0.36, -0.11] | 0.497 |

**Per station** (90 % CIs; reference RMSEs are in the pooled tables). Station order: by
spot group (Hawaii, San Francisco, Trestles, Rincon), as in the pairing table.

*All leads (3-240 h)*

| station | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 42782 | 280 | -0.28 [-0.29, -0.27] | 0.31 | 0.418 [0.400, 0.437] | 0.18 | 0.27 | 0.90 | 0.59 [0.52, 0.65] | 0.40 [0.34, 0.45] |
| ndbc:51205 | 36697 | 240 | -0.43 [-0.44, -0.41] | 0.45 | 0.540 [0.518, 0.560] | 0.16 | 0.31 | 0.83 | 0.22 [0.13, 0.30] | 0.11 [0.04, 0.17] |
| ndbc:51101 | 32357 | 233 | -0.11 [-0.13, -0.09] | 0.27 | 0.412 [0.388, 0.436] | 0.16 | 0.18 | 0.88 | 0.78 [0.74, 0.81] | 0.63 [0.59, 0.66] |
| ndbc:46026 | 40788 | 265 | -0.15 [-0.17, -0.13] | 0.30 | 0.418 [0.401, 0.435] | 0.21 | 0.24 | 0.81 | 0.70 [0.66, 0.73] | 0.61 [0.57, 0.65] |
| ndbc:46237 | 41924 | 273 | -0.30 [-0.31, -0.28] | 0.36 | 0.459 [0.442, 0.476] | 0.21 | 0.31 | 0.80 | 0.52 [0.45, 0.57] | 0.34 [0.28, 0.40] |
| ndbc:46214 | 38271 | 255 | -0.05 [-0.08, -0.03] | 0.37 | 0.540 [0.522, 0.556] | 0.21 | 0.22 | 0.80 | 0.75 [0.72, 0.77] | 0.62 [0.58, 0.65] |
| ndbc:46224 | 47533 | 305 | -0.22 [-0.23, -0.21] | 0.25 | 0.305 [0.295, 0.315] | 0.21 | 0.35 | 0.74 | 0.29 [0.16, 0.39] | 0.06 [-0.04, 0.14] |
| ndbc:46086 | 43339 | 303 | -0.27 [-0.29, -0.26] | 0.32 | 0.405 [0.388, 0.423] | 0.20 | 0.30 | 0.77 | 0.41 [0.34, 0.48] | 0.36 [0.30, 0.41] |
| ndbc:46053 | 43829 | 290 | -0.12 [-0.13, -0.10] | 0.22 | 0.298 [0.288, 0.308] | 0.24 | 0.28 | 0.76 | 0.63 [0.58, 0.67] | 0.48 [0.44, 0.53] |
| ndbc:46054 | 45236 | 301 | -0.37 [-0.38, -0.35] | 0.42 | 0.527 [0.512, 0.542] | 0.19 | 0.30 | 0.82 | 0.49 [0.43, 0.54] | 0.35 [0.30, 0.40] |

*Lead 24 h*

| station | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 3538 | 274 | -0.29 [-0.31, -0.26] | 0.31 | 0.396 [0.363, 0.427] | 0.16 | 0.26 | 0.93 | 0.39 [0.23, 0.51] | 0.46 [0.36, 0.54] |
| ndbc:51205 | 3024 | 233 | -0.41 [-0.43, -0.38] | 0.42 | 0.500 [0.466, 0.536] | 0.15 | 0.28 | 0.87 | -0.31 [-0.60, -0.10] | 0.26 [0.08, 0.39] |
| ndbc:51101 | 2681 | 222 | -0.12 [-0.14, -0.09] | 0.21 | 0.295 [0.266, 0.324] | 0.11 | 0.13 | 0.95 | 0.84 [0.79, 0.88] | 0.80 [0.75, 0.84] |
| ndbc:46026 | 3375 | 260 | -0.17 [-0.19, -0.15] | 0.24 | 0.305 [0.288, 0.321] | 0.14 | 0.18 | 0.91 | 0.62 [0.54, 0.69] | 0.79 [0.74, 0.82] |
| ndbc:46237 | 3467 | 267 | -0.31 [-0.33, -0.28] | 0.34 | 0.415 [0.390, 0.442] | 0.17 | 0.28 | 0.88 | 0.27 [0.11, 0.39] | 0.47 [0.36, 0.55] |
| ndbc:46214 | 3159 | 243 | -0.12 [-0.15, -0.09] | 0.26 | 0.355 [0.326, 0.387] | 0.13 | 0.15 | 0.93 | 0.82 [0.79, 0.85] | 0.84 [0.81, 0.86] |
| ndbc:46224 | 3926 | 302 | -0.25 [-0.26, -0.23] | 0.25 | 0.291 [0.278, 0.306] | 0.16 | 0.34 | 0.86 | -0.07 [-0.45, 0.19] | 0.14 [-0.03, 0.28] |
| ndbc:46086 | 3568 | 280 | -0.30 [-0.31, -0.28] | 0.31 | 0.371 [0.351, 0.390] | 0.15 | 0.28 | 0.87 | 0.08 [-0.13, 0.25] | 0.45 [0.34, 0.53] |
| ndbc:46053 | 3628 | 280 | -0.14 [-0.16, -0.12] | 0.20 | 0.261 [0.243, 0.280] | 0.19 | 0.23 | 0.86 | 0.62 [0.52, 0.70] | 0.65 [0.57, 0.71] |
| ndbc:46054 | 3730 | 289 | -0.39 [-0.41, -0.36] | 0.40 | 0.483 [0.458, 0.506] | 0.14 | 0.27 | 0.91 | 0.23 [0.09, 0.34] | 0.45 [0.37, 0.53] |

*Lead 48 h*

| station | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 3526 | 272 | -0.29 [-0.31, -0.27] | 0.30 | 0.391 [0.359, 0.423] | 0.15 | 0.25 | 0.93 | 0.67 [0.58, 0.74] | 0.45 [0.33, 0.55] |
| ndbc:51205 | 3025 | 234 | -0.42 [-0.45, -0.39] | 0.43 | 0.512 [0.479, 0.544] | 0.15 | 0.29 | 0.87 | 0.24 [0.09, 0.35] | 0.15 [0.00, 0.27] |
| ndbc:51101 | 2683 | 222 | -0.13 [-0.16, -0.11] | 0.22 | 0.314 [0.281, 0.347] | 0.12 | 0.13 | 0.94 | 0.87 [0.84, 0.90] | 0.79 [0.73, 0.83] |
| ndbc:46026 | 3366 | 260 | -0.16 [-0.19, -0.14] | 0.25 | 0.329 [0.306, 0.351] | 0.15 | 0.19 | 0.91 | 0.82 [0.79, 0.85] | 0.77 [0.73, 0.81] |
| ndbc:46237 | 3469 | 267 | -0.29 [-0.32, -0.27] | 0.34 | 0.409 [0.388, 0.430] | 0.17 | 0.27 | 0.88 | 0.63 [0.55, 0.69] | 0.49 [0.39, 0.57] |
| ndbc:46214 | 3169 | 245 | -0.09 [-0.12, -0.06] | 0.27 | 0.352 [0.328, 0.377] | 0.14 | 0.14 | 0.92 | 0.90 [0.88, 0.92] | 0.83 [0.80, 0.86] |
| ndbc:46224 | 3930 | 303 | -0.23 [-0.25, -0.22] | 0.25 | 0.289 [0.273, 0.305] | 0.17 | 0.34 | 0.82 | 0.34 [0.19, 0.46] | 0.11 [-0.05, 0.23] |
| ndbc:46086 | 3566 | 280 | -0.29 [-0.31, -0.27] | 0.30 | 0.370 [0.350, 0.389] | 0.16 | 0.27 | 0.86 | 0.52 [0.40, 0.59] | 0.45 [0.35, 0.53] |
| ndbc:46053 | 3616 | 280 | -0.09 [-0.11, -0.07] | 0.21 | 0.266 [0.251, 0.281] | 0.22 | 0.24 | 0.81 | 0.70 [0.64, 0.75] | 0.58 [0.51, 0.65] |
| ndbc:46054 | 3741 | 291 | -0.36 [-0.39, -0.34] | 0.38 | 0.465 [0.442, 0.486] | 0.14 | 0.26 | 0.90 | 0.60 [0.52, 0.66] | 0.48 [0.40, 0.55] |

*Lead 72 h*

| station | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 3525 | 273 | -0.30 [-0.32, -0.27] | 0.31 | 0.402 [0.373, 0.432] | 0.16 | 0.26 | 0.93 | 0.67 [0.58, 0.73] | 0.44 [0.34, 0.53] |
| ndbc:51205 | 3024 | 233 | -0.43 [-0.46, -0.40] | 0.44 | 0.522 [0.487, 0.558] | 0.15 | 0.30 | 0.87 | 0.38 [0.25, 0.49] | 0.19 [0.00, 0.33] |
| ndbc:51101 | 2668 | 221 | -0.12 [-0.15, -0.09] | 0.23 | 0.333 [0.297, 0.372] | 0.13 | 0.14 | 0.93 | 0.86 [0.82, 0.89] | 0.75 [0.69, 0.79] |
| ndbc:46026 | 3362 | 259 | -0.14 [-0.16, -0.11] | 0.25 | 0.318 [0.298, 0.338] | 0.16 | 0.19 | 0.89 | 0.83 [0.80, 0.86] | 0.76 [0.71, 0.80] |
| ndbc:46237 | 3454 | 266 | -0.29 [-0.32, -0.27] | 0.34 | 0.416 [0.387, 0.449] | 0.18 | 0.28 | 0.86 | 0.67 [0.60, 0.72] | 0.45 [0.34, 0.54] |
| ndbc:46214 | 3146 | 242 | -0.07 [-0.10, -0.03] | 0.28 | 0.381 [0.352, 0.414] | 0.15 | 0.15 | 0.91 | 0.89 [0.87, 0.91] | 0.81 [0.78, 0.84] |
| ndbc:46224 | 3913 | 301 | -0.24 [-0.25, -0.22] | 0.24 | 0.288 [0.273, 0.302] | 0.17 | 0.33 | 0.84 | 0.46 [0.31, 0.56] | 0.16 [-0.01, 0.29] |
| ndbc:46086 | 3555 | 279 | -0.27 [-0.29, -0.25] | 0.29 | 0.359 [0.336, 0.381] | 0.16 | 0.27 | 0.85 | 0.60 [0.50, 0.68] | 0.48 [0.38, 0.57] |
| ndbc:46053 | 3615 | 279 | -0.10 [-0.12, -0.08] | 0.22 | 0.281 [0.263, 0.299] | 0.22 | 0.25 | 0.81 | 0.72 [0.66, 0.77] | 0.59 [0.51, 0.65] |
| ndbc:46054 | 3717 | 288 | -0.36 [-0.38, -0.33] | 0.39 | 0.480 [0.454, 0.505] | 0.16 | 0.27 | 0.88 | 0.65 [0.58, 0.70] | 0.46 [0.37, 0.54] |

*Lead 120 h*

| station | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 4585 | 272 | -0.28 [-0.30, -0.25] | 0.30 | 0.399 [0.366, 0.432] | 0.17 | 0.26 | 0.91 | 0.69 [0.60, 0.74] | 0.47 [0.37, 0.55] |
| ndbc:51205 | 3956 | 233 | -0.44 [-0.47, -0.41] | 0.45 | 0.534 [0.500, 0.567] | 0.15 | 0.31 | 0.86 | 0.38 [0.26, 0.49] | 0.16 [0.00, 0.28] |
| ndbc:51101 | 3458 | 223 | -0.11 [-0.15, -0.07] | 0.28 | 0.427 [0.358, 0.498] | 0.17 | 0.18 | 0.87 | 0.80 [0.74, 0.86] | 0.60 [0.46, 0.71] |
| ndbc:46026 | 4375 | 258 | -0.14 [-0.18, -0.11] | 0.30 | 0.413 [0.378, 0.450] | 0.22 | 0.24 | 0.79 | 0.74 [0.69, 0.79] | 0.60 [0.50, 0.66] |
| ndbc:46237 | 4494 | 265 | -0.30 [-0.33, -0.27] | 0.37 | 0.459 [0.427, 0.492] | 0.21 | 0.31 | 0.80 | 0.57 [0.49, 0.64] | 0.31 [0.18, 0.42] |
| ndbc:46214 | 4102 | 242 | -0.03 [-0.08, 0.01] | 0.39 | 0.524 [0.483, 0.568] | 0.21 | 0.21 | 0.81 | 0.78 [0.73, 0.82] | 0.64 [0.56, 0.70] |
| ndbc:46224 | 5099 | 300 | -0.20 [-0.22, -0.19] | 0.24 | 0.302 [0.281, 0.324] | 0.22 | 0.34 | 0.74 | 0.40 [0.27, 0.50] | 0.11 [-0.07, 0.24] |
| ndbc:46086 | 4659 | 281 | -0.25 [-0.27, -0.23] | 0.30 | 0.390 [0.358, 0.419] | 0.20 | 0.29 | 0.78 | 0.54 [0.43, 0.62] | 0.42 [0.33, 0.50] |
| ndbc:46053 | 4710 | 279 | -0.10 [-0.12, -0.08] | 0.21 | 0.285 [0.269, 0.302] | 0.24 | 0.27 | 0.77 | 0.73 [0.67, 0.77] | 0.54 [0.46, 0.61] |
| ndbc:46054 | 4844 | 289 | -0.34 [-0.37, -0.31] | 0.40 | 0.505 [0.474, 0.536] | 0.19 | 0.29 | 0.83 | 0.61 [0.53, 0.67] | 0.40 [0.30, 0.49] |

*Lead 168 h*

| station | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 2429 | 272 | -0.26 [-0.30, -0.23] | 0.33 | 0.470 [0.424, 0.517] | 0.23 | 0.30 | 0.81 | 0.59 [0.48, 0.67] | 0.26 [0.10, 0.39] |
| ndbc:51205 | 2083 | 232 | -0.42 [-0.45, -0.38] | 0.45 | 0.568 [0.527, 0.608] | 0.19 | 0.32 | 0.76 | 0.32 [0.19, 0.43] | 0.05 [-0.14, 0.21] |
| ndbc:51101 | 1827 | 222 | -0.08 [-0.13, -0.02] | 0.36 | 0.521 [0.467, 0.577] | 0.21 | 0.22 | 0.80 | 0.69 [0.58, 0.77] | 0.40 [0.24, 0.53] |
| ndbc:46026 | 2309 | 257 | -0.09 [-0.14, -0.05] | 0.38 | 0.517 [0.478, 0.555] | 0.28 | 0.30 | 0.64 | 0.62 [0.53, 0.69] | 0.36 [0.23, 0.46] |
| ndbc:46237 | 2371 | 264 | -0.26 [-0.29, -0.22] | 0.38 | 0.496 [0.456, 0.533] | 0.26 | 0.34 | 0.68 | 0.54 [0.44, 0.61] | 0.19 [0.04, 0.31] |
| ndbc:46214 | 2162 | 241 | 0.04 [-0.03, 0.10] | 0.56 | 0.757 [0.699, 0.810] | 0.30 | 0.31 | 0.60 | 0.60 [0.50, 0.67] | 0.24 [0.09, 0.36] |
| ndbc:46224 | 2690 | 299 | -0.20 [-0.22, -0.18] | 0.25 | 0.331 [0.302, 0.359] | 0.27 | 0.38 | 0.63 | 0.32 [0.18, 0.44] | -0.10 [-0.31, 0.08] |
| ndbc:46086 | 2455 | 280 | -0.24 [-0.27, -0.21] | 0.34 | 0.465 [0.419, 0.513] | 0.26 | 0.34 | 0.62 | 0.35 [0.18, 0.50] | 0.17 [0.00, 0.32] |
| ndbc:46053 | 2484 | 278 | -0.10 [-0.13, -0.08] | 0.26 | 0.332 [0.314, 0.351] | 0.27 | 0.31 | 0.67 | 0.61 [0.54, 0.67] | 0.37 [0.27, 0.46] |
| ndbc:46054 | 2561 | 288 | -0.34 [-0.38, -0.31] | 0.43 | 0.561 [0.526, 0.593] | 0.22 | 0.32 | 0.73 | 0.54 [0.45, 0.61] | 0.28 [0.16, 0.38] |

*Lead 240 h*

| station | n | cycles | bias m [90 % CI] | MAE m | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 1344 | 270 | -0.29 [-0.33, -0.24] | 0.40 | 0.553 [0.498, 0.612] | 0.28 | 0.37 | 0.69 | 0.42 [0.27, 0.54] | -0.14 [-0.33, 0.02] |
| ndbc:51205 | 1150 | 230 | -0.43 [-0.48, -0.38] | 0.51 | 0.648 [0.596, 0.698] | 0.24 | 0.38 | 0.56 | 0.13 [-0.04, 0.28] | -0.36 [-0.63, -0.14] |
| ndbc:51101 | 1010 | 212 | -0.13 [-0.21, -0.06] | 0.49 | 0.725 [0.625, 0.824] | 0.29 | 0.31 | 0.60 | 0.41 [0.21, 0.57] | -0.10 [-0.34, 0.11] |
| ndbc:46026 | 1277 | 256 | -0.13 [-0.20, -0.06] | 0.51 | 0.677 [0.618, 0.737] | 0.36 | 0.40 | 0.39 | 0.37 [0.22, 0.50] | -0.07 [-0.27, 0.09] |
| ndbc:46237 | 1314 | 263 | -0.30 [-0.36, -0.24] | 0.48 | 0.645 [0.587, 0.702] | 0.34 | 0.45 | 0.37 | 0.21 [0.01, 0.36] | -0.32 [-0.57, -0.12] |
| ndbc:46214 | 1196 | 241 | -0.00 [-0.10, 0.10] | 0.74 | 0.968 [0.891, 1.041] | 0.38 | 0.40 | 0.32 | 0.24 [0.07, 0.39] | -0.37 [-0.65, -0.14] |
| ndbc:46224 | 1491 | 299 | -0.19 [-0.22, -0.16] | 0.28 | 0.377 [0.342, 0.412] | 0.33 | 0.43 | 0.39 | 0.10 [-0.13, 0.27] | -0.48 [-0.76, -0.24] |
| ndbc:46086 | 1359 | 275 | -0.24 [-0.29, -0.20] | 0.42 | 0.552 [0.508, 0.598] | 0.33 | 0.41 | 0.41 | 0.18 [-0.01, 0.33] | -0.17 [-0.39, 0.01] |
| ndbc:46053 | 1376 | 276 | -0.07 [-0.12, -0.03] | 0.34 | 0.471 [0.435, 0.507] | 0.43 | 0.47 | 0.33 | 0.17 [0.00, 0.32] | -0.34 [-0.61, -0.10] |
| ndbc:46054 | 1426 | 286 | -0.31 [-0.37, -0.25] | 0.55 | 0.726 [0.671, 0.778] | 0.34 | 0.42 | 0.35 | 0.15 [-0.03, 0.29] | -0.34 [-0.56, -0.13] |

**Seasons** (valid time; winter = October to March, summer = April to September; the
split was fixed in the code before any result was computed, `--by-season`). Every
fold except fold 3 (2025-10-13 to 2026-02-10, all winter) contains both seasons.

*Pooled, all ten stations, winter* (159,267 pairs):

| lead h | n | bias m [90 % CI] | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|
| all | 159267 | -0.30 [-0.32, -0.29] | 0.509 [0.496, 0.522] | 0.20 | 0.27 | 0.88 | 0.61 [0.57, 0.65] | 0.48 [0.44, 0.52] |
| 24 | 13229 | -0.31 [-0.33, -0.29] | 0.451 [0.431, 0.471] | 0.16 | 0.24 | 0.93 | 0.54 [0.46, 0.59] | 0.59 [0.53, 0.64] |
| 48 | 13220 | -0.31 [-0.33, -0.29] | 0.445 [0.425, 0.464] | 0.16 | 0.24 | 0.93 | 0.72 [0.67, 0.76] | 0.60 [0.55, 0.65] |
| 72 | 13125 | -0.30 [-0.33, -0.28] | 0.460 [0.439, 0.482] | 0.17 | 0.25 | 0.92 | 0.72 [0.68, 0.76] | 0.58 [0.52, 0.63] |
| 120 | 17023 | -0.28 [-0.31, -0.26] | 0.498 [0.469, 0.525] | 0.20 | 0.27 | 0.88 | 0.68 [0.63, 0.73] | 0.50 [0.43, 0.57] |
| 168 | 8944 | -0.29 [-0.32, -0.26] | 0.574 [0.537, 0.612] | 0.25 | 0.31 | 0.83 | 0.59 [0.51, 0.66] | 0.34 [0.24, 0.43] |
| 240 | 4935 | -0.30 [-0.36, -0.23] | 0.748 [0.701, 0.796] | 0.34 | 0.42 | 0.65 | 0.31 [0.16, 0.43] | -0.14 [-0.34, 0.02] |

*Pooled, all ten stations, summer* (250,077 pairs):

| lead h | n | bias m [90 % CI] | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|
| all | 250077 | -0.19 [-0.20, -0.18] | 0.384 [0.373, 0.397] | 0.20 | 0.25 | 0.85 | 0.57 [0.53, 0.60] | 0.37 [0.33, 0.41] |
| 24 | 20562 | -0.21 [-0.23, -0.20] | 0.320 [0.308, 0.332] | 0.15 | 0.21 | 0.92 | 0.36 [0.28, 0.43] | 0.56 [0.50, 0.61] |
| 48 | 20581 | -0.19 [-0.21, -0.18] | 0.325 [0.312, 0.337] | 0.16 | 0.21 | 0.91 | 0.69 [0.64, 0.72] | 0.54 [0.48, 0.60] |
| 72 | 20562 | -0.19 [-0.21, -0.18] | 0.328 [0.316, 0.339] | 0.16 | 0.22 | 0.90 | 0.73 [0.69, 0.76] | 0.54 [0.47, 0.59] |
| 120 | 26899 | -0.19 [-0.20, -0.17] | 0.372 [0.354, 0.391] | 0.20 | 0.25 | 0.86 | 0.64 [0.59, 0.69] | 0.41 [0.33, 0.48] |
| 168 | 14247 | -0.14 [-0.17, -0.12] | 0.462 [0.436, 0.485] | 0.27 | 0.30 | 0.75 | 0.51 [0.43, 0.59] | 0.08 [-0.03, 0.19] |
| 240 | 7916 | -0.16 [-0.20, -0.13] | 0.567 [0.526, 0.611] | 0.34 | 0.38 | 0.61 | 0.19 [0.06, 0.30] | -0.47 [-0.63, -0.32] |

*Pooled, five offshore references, winter* (81,237 pairs):

| lead h | n | bias m [90 % CI] | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|
| all | 81237 | -0.30 [-0.31, -0.29] | 0.481 [0.467, 0.495] | 0.21 | 0.30 | 0.88 | 0.56 [0.51, 0.61] | 0.42 [0.38, 0.46] |
| 24 | 6747 | -0.31 [-0.33, -0.29] | 0.433 [0.408, 0.459] | 0.17 | 0.27 | 0.92 | 0.39 [0.28, 0.48] | 0.52 [0.44, 0.59] |
| 48 | 6730 | -0.31 [-0.33, -0.29] | 0.437 [0.412, 0.463] | 0.17 | 0.27 | 0.93 | 0.66 [0.59, 0.71] | 0.51 [0.44, 0.58] |
| 72 | 6708 | -0.30 [-0.32, -0.28] | 0.441 [0.415, 0.467] | 0.18 | 0.28 | 0.91 | 0.68 [0.62, 0.72] | 0.50 [0.42, 0.57] |
| 120 | 8698 | -0.29 [-0.31, -0.27] | 0.463 [0.436, 0.490] | 0.20 | 0.29 | 0.89 | 0.65 [0.59, 0.70] | 0.46 [0.37, 0.53] |
| 168 | 4582 | -0.29 [-0.32, -0.26] | 0.520 [0.481, 0.558] | 0.24 | 0.33 | 0.83 | 0.57 [0.49, 0.65] | 0.32 [0.20, 0.43] |
| 240 | 2532 | -0.29 [-0.35, -0.22] | 0.684 [0.635, 0.734] | 0.35 | 0.44 | 0.64 | 0.28 [0.13, 0.42] | -0.18 [-0.37, -0.02] |

*Pooled, five offshore references, summer* (129,641 pairs):

| lead h | n | bias m [90 % CI] | RMSE m [90 % CI] | SI | HH | CC | SS vs persistence [90 % CI] | SS LT clim [90 % CI] |
|---|---|---|---|---|---|---|---|---|
| all | 129641 | -0.19 [-0.20, -0.18] | 0.337 [0.328, 0.348] | 0.20 | 0.26 | 0.84 | 0.49 [0.45, 0.53] | 0.33 [0.29, 0.37] |
| 24 | 10657 | -0.20 [-0.21, -0.19] | 0.295 [0.282, 0.308] | 0.15 | 0.23 | 0.91 | 0.23 [0.11, 0.31] | 0.50 [0.42, 0.57] |
| 48 | 10664 | -0.18 [-0.20, -0.17] | 0.304 [0.290, 0.318] | 0.17 | 0.23 | 0.88 | 0.57 [0.52, 0.62] | 0.45 [0.39, 0.51] |
| 72 | 10657 | -0.19 [-0.21, -0.18] | 0.311 [0.298, 0.324] | 0.17 | 0.24 | 0.88 | 0.61 [0.56, 0.65] | 0.44 [0.36, 0.51] |
| 120 | 13953 | -0.19 [-0.20, -0.17] | 0.335 [0.319, 0.352] | 0.20 | 0.27 | 0.84 | 0.58 [0.52, 0.63] | 0.34 [0.25, 0.41] |
| 168 | 7383 | -0.16 [-0.18, -0.14] | 0.394 [0.375, 0.416] | 0.26 | 0.31 | 0.73 | 0.47 [0.39, 0.54] | 0.09 [-0.05, 0.21] |
| 240 | 4098 | -0.17 [-0.20, -0.15] | 0.447 [0.420, 0.478] | 0.30 | 0.36 | 0.63 | 0.29 [0.18, 0.39] | -0.29 [-0.44, -0.14] |

*Per station, all leads 3-240 h and the 168 h bucket*:

| station | winter n | winter bias m | winter RMSE m | winter SS LT clim [90 % CI] | summer n | summer bias m | summer RMSE m | summer SS LT clim [90 % CI] | RMSE 168 h winter / summer | SS LT clim 168 h winter / summer |
|---|---|---|---|---|---|---|---|---|---|---|
| ndbc:51201 | 16737 | -0.37 | 0.544 | 0.39 [0.32, 0.45] | 26045 | -0.22 | 0.312 | 0.41 [0.33, 0.47] | 0.601 / 0.363 | 0.28 / 0.22 |
| ndbc:51205 | 15255 | -0.52 | 0.642 | 0.07 [-0.02, 0.14] | 21442 | -0.36 | 0.454 | 0.17 [0.06, 0.26] | 0.680 / 0.472 | -0.02 / 0.13 |
| ndbc:51101 | 17130 | -0.13 | 0.497 | 0.61 [0.55, 0.65] | 15227 | -0.09 | 0.286 | 0.70 [0.63, 0.74] | 0.634 / 0.356 | 0.37 / 0.49 |
| ndbc:46026 | 15832 | -0.24 | 0.485 | 0.68 [0.63, 0.72] | 24956 | -0.09 | 0.369 | 0.50 [0.44, 0.56] | 0.542 / 0.501 | 0.55 / 0.08 |
| ndbc:46237 | 17656 | -0.38 | 0.539 | 0.47 [0.41, 0.53] | 24268 | -0.24 | 0.390 | -0.01 [-0.12, 0.10] | 0.578 / 0.428 | 0.38 / -0.36 |
| ndbc:46214 | 12515 | -0.15 | 0.549 | 0.72 [0.68, 0.76] | 25756 | -0.01 | 0.535 | 0.53 [0.48, 0.58] | 0.754 / 0.758 | 0.48 / 0.04 |
| ndbc:46224 | 18920 | -0.22 | 0.336 | 0.19 [0.08, 0.30] | 28613 | -0.22 | 0.283 | -0.12 [-0.25, 0.01] | 0.363 / 0.309 | 0.09 / -0.35 |
| ndbc:46086 | 16989 | -0.31 | 0.467 | 0.46 [0.38, 0.53] | 26350 | -0.25 | 0.360 | 0.18 [0.08, 0.27] | 0.513 / 0.431 | 0.36 / -0.13 |
| ndbc:46053 | 15295 | -0.18 | 0.352 | 0.54 [0.47, 0.60] | 28534 | -0.08 | 0.265 | 0.42 [0.36, 0.47] | 0.374 / 0.308 | 0.51 / 0.20 |
| ndbc:46054 | 16401 | -0.44 | 0.613 | 0.40 [0.32, 0.47] | 28835 | -0.33 | 0.472 | 0.29 [0.22, 0.35] | 0.662 / 0.495 | 0.31 / 0.24 |

### 11.4 Sensitivity: QC'd observations only

The same pooled backtest (all ten stations) with the last cycle on 2026-08-21 00Z
(`--end`), so that every valid time is before 2026-09-01 and every observation comes
from NDBC's QC'd archive. The folds are recut on the shorter period: test blocks
2025-02-03 to 2026-08-21, 383,495 pairs over 283 cycles.

| lead h | n (main / QC'd only) | bias m, main | bias m, QC'd only | RMSE m, main | RMSE m, QC'd only | SS vs persistence, main / QC'd only | SS LT clim, main | SS LT clim, QC'd only |
|---|---|---|---|---|---|---|---|---|
| all | 409344 / 383495 | -0.23 [-0.24, -0.22] | -0.23 [-0.25, -0.22] | 0.437 [0.426, 0.447] | 0.441 [0.430, 0.453] | 0.59 / 0.59 | 0.43 [0.41, 0.46] | 0.43 [0.40, 0.46] |
| 24 | 33791 / 31546 | -0.25 [-0.26, -0.24] | -0.25 [-0.27, -0.24] | 0.376 [0.364, 0.389] | 0.380 [0.368, 0.394] | 0.47 / 0.48 | 0.58 [0.53, 0.62] | 0.57 [0.53, 0.61] |
| 48 | 33801 / 31529 | -0.24 [-0.25, -0.23] | -0.24 [-0.26, -0.23] | 0.376 [0.364, 0.389] | 0.381 [0.368, 0.393] | 0.71 / 0.71 | 0.58 [0.54, 0.61] | 0.57 [0.53, 0.61] |
| 72 | 33687 / 31539 | -0.24 [-0.25, -0.22] | -0.24 [-0.25, -0.22] | 0.385 [0.371, 0.397] | 0.388 [0.375, 0.403] | 0.72 / 0.72 | 0.56 [0.52, 0.60] | 0.55 [0.51, 0.60] |
| 120 | 43922 / 41280 | -0.22 [-0.24, -0.21] | -0.22 [-0.24, -0.20] | 0.425 [0.407, 0.442] | 0.431 [0.413, 0.449] | 0.67 / 0.66 | 0.46 [0.41, 0.51] | 0.45 [0.40, 0.50] |
| 168 | 23191 / 21859 | -0.20 [-0.22, -0.18] | -0.20 [-0.22, -0.18] | 0.508 [0.488, 0.530] | 0.511 [0.490, 0.535] | 0.55 / 0.55 | 0.23 [0.16, 0.30] | 0.23 [0.15, 0.30] |
| 240 | 12851 / 12150 | -0.21 [-0.25, -0.18] | -0.21 [-0.25, -0.17] | 0.642 [0.610, 0.677] | 0.643 [0.609, 0.679] | 0.26 / 0.27 | -0.28 [-0.41, -0.16] | -0.25 [-0.39, -0.11] |

Leaving out the realtime (`not_qc`) observations of September and October 2026 changes
no RMSE by more than 0.006 m and no skill score by more than 0.03, well inside the
bootstrap intervals.

### 11.5 Comparison with the Western Australian (AODN) backtest (section 3)

| | WA, AODN delayed-QC, 3 buoys pooled | NDBC, 10 buoys pooled | NDBC, 5 offshore references |
| --- | --- | --- | --- |
| test period, cycles | 2021-05-10 to 2021-12-31, 472 cycles (00Z and 12Z daily) | 2025-02-11 to 2026-10-12, 305 cycles (00Z every second day) | same |
| test pairs | 209,658 | 409,344 | 210,878 |
| mean observed Hm0 | 2.3-2.9 m (Esperance 1.9 m) | 0.93-2.43 m by station | 0.93-1.99 m |
| analysis time: median observed / model | 1.08-1.13 | 1.05-1.35 | 1.13-1.35 |
| bias, all leads [90 % CI] | -0.23 [-0.24, -0.22] m | -0.23 [-0.24, -0.22] m | -0.23 [-0.24, -0.22] m |
| RMSE 24 / 72 / 168 / 240 h | 0.401 / 0.465 / 0.748 / 1.012 m | 0.376 / 0.385 / 0.508 / 0.642 m | 0.355 / 0.367 / 0.447 / 0.550 m |
| SI 24 / 240 h | 0.12 / 0.36 | 0.16 / 0.34 | 0.17 / 0.33 |
| HH 24 / 240 h | 0.16 / 0.39 | 0.23 / 0.40 | 0.25 / 0.40 |
| CC 24 / 240 h | 0.96 / 0.63 | 0.93 / 0.64 | 0.92 / 0.65 |
| SS vs persistence 24 / 168 / 240 h | 0.83 / 0.72 / 0.51 | 0.47 / 0.55 / 0.26 | 0.33 / 0.53 / 0.29 |
| SS vs long-term climatology 24 / 168 / 240 h | 0.84 / 0.47 / 0.03 [-0.09, 0.15] | 0.58 / 0.23 / -0.28 [-0.41, -0.16] | 0.51 / 0.23 / -0.22 [-0.36, -0.11] |
| RMSE of persistence at 24 h / of the long-term climatology | 0.964 / 1.016 m | 0.519 / 0.581 m | 0.434 / 0.508 m |

### 11.6 Reading the NDBC backtest

* **The same low bias as in Western Australia.** Pooled, the raw bulletin forecast is
  0.20-0.25 m below the buoys at every lead (CI excluding zero), exactly the pooled WA
  value of -0.23 m, now in another ocean, hemisphere, set of buoy operators and model
  year. Relative to the smaller NDBC sea states it is larger: observed Hm0 is 5-35 %
  above the model at analysis time (median ratio by station), against 8-13 % in WA. The
  bias is smallest at the deep-water and open-shelf buoys (46214 -0.05 m, 51101 -0.11 m,
  46053 -0.12 m, 46026 -0.15 m) and largest at 51205 (-0.43 m), 46054 (-0.37 m), 46237
  (-0.30 m) and 51201 (-0.28 m). It is larger in winter (pooled -0.30 m) than in summer
  (-0.19 m).
* **Errors grow with lead, more slowly than in WA in metres.** Pooled RMSE 0.38 m at
  24-72 h, 0.43 m at 120 h, 0.51 m at 168 h and 0.64 m at 240 h; correlation falls from
  0.93 to 0.64. SI and HH are similar to WA's or larger at short leads (SI 0.16 against
  0.12 at 24 h): the absolute errors are smaller because the waves are smaller, not
  because the forecast is relatively better.
* **Skill against references is lower than in WA, because the references are better
  here.** Persistence (RMSE 0.52 m at 24 h) and the long-term climatology (0.58 m) are
  much closer to the observations than at the WA buoys (0.96 and 1.02 m). Against
  persistence the raw forecast keeps positive skill at every lead pooled (0.26-0.72),
  lowest at 24 h (0.47) and 240 h (0.26). Against the long-term monthly climatology it
  has skill to 120 h (0.58 at 24 h, 0.46 at 120 h), little at 168 h (0.23 [0.16, 0.30])
  and is **worse than climatology at 240 h** (-0.28 [-0.41, -0.16]); WA had about zero
  at 240 h.
* **Stations where the raw forecast barely beats the simple references.**
  * 51205 (Pe'ahi): worse than persistence at 24 h (-0.31 [-0.60, -0.10]) and only
    0.11 [0.04, 0.17] better than climatology over all leads (none at 168 h, 0.05
    [-0.14, 0.21]); bias -0.43 m, 22 % of the mean observed Hm0 (1.99 m over the
    pairs). In winter the skill over climatology is 0.07 [-0.02, 0.14].
  * 46224 (offshore reference for Trestles): no skill over climatology at any lead
    (all leads 0.06 [-0.04, 0.14]; 24 h 0.14 [-0.03, 0.28]; 240 h -0.48) and no clear
    skill over persistence at 24 h (-0.07 [-0.45, 0.19]); bias -0.22 m on a mean of
    0.93 m. In summer (April to September, when southern-hemisphere swell reaches
    southern California) it is worse than climatology over all leads (-0.12 [-0.25,
    0.01]).
  * 46086 (upstream of Trestles): skill over persistence at 24 h 0.08 [-0.13, 0.25].
  * 46237 (SF Bar, 17 m): no skill over climatology in summer (-0.01 [-0.12, 0.10]).
* **Where it works best:** the open-ocean and shelf-edge buoys 51101, 46214 and 46026
  (skill over climatology 0.61-0.63 over all leads, 0.79-0.84 at 24 h). 46214 has almost
  no bias, but the largest long-lead errors (RMSE 0.76 m at 168 h, 0.97 m at 240 h) in
  the largest sea states (mean 2.43 m).
* **Seasons.** Winter has larger errors (pooled RMSE 0.51 m over all leads against 0.38
  m) and a larger bias, but also more skill over climatology (0.48 against 0.37; 0.34
  against 0.08 at 168 h), because winter sea states vary more. In summer the raw forecast
  beats climatology only to about 120 h (168 h 0.08 [-0.03, 0.19], 240 h -0.47); at
  46224, 46237 and 46086 the summer skill at 168 h is negative.
* **The offshore references alone** (the five buoys that stand for the spots' offshore
  swell) give the same picture: bias -0.23 m, RMSE 0.36 m at 24 h and 0.55 m at 240 h,
  skill over climatology 0.51 at 24 h, 0.23 at 168 h, -0.22 at 240 h.
* **Training-fold climatology.** With two years of data the implemented reference is no
  longer degenerate (RMSE 0.655 m, 58 % of the rows), and it agrees with the long-term
  one in sign at every lead (pooled SS 0.66 at 24 h, -0.03 at 240 h). The long-term one
  is still the comparison quoted, because it covers every row and is fully out of sample.
* **Not comparable one to one with WA.** Different years (2021 vs 2025-2026), regions,
  buoy networks (Datawell Waveriders of DoT WA vs CDIP Waveriders and NDBC discus and
  2.1-m hulls) and cycle sampling. Whether NCEP changed GFS-Wave during 2024-10 to
  2026-10 was not checked; a change would be mixed into these numbers.

### 11.7 Consequences for the application

* **No change to the ML decision (ADR-013).** The shipped candidate correction covers
  only the three spots paired with Cape Naturaliste; nothing here tests it. The NDBC
  results show the same systematic low bias of the raw bulletins in a second region,
  which makes a bias correction for the Hawaiian and Californian spots worth testing on
  these pairs with the unchanged pre-registered criteria (follow-up; not done here).
* **No change to the forecast-confidence thresholds or the uncertainty ranges.** This
  backtest scores the raw station bulletins at the buoys, not the gridded forecasts at
  the spots that forecast confidence reads (`forecast_accuracy_metrics` of the gridded
  sources), and not our 80 % ranges, whose coverage was not tested. Nothing was tuned on
  these numbers.
* **What a user should take from it.** Off Pe'ahi (51205) the raw offshore model is
  only slightly better than a monthly climatology of the same buoy (skill 0.05-0.26 at
  24-168 h) and off Trestles (46224) not distinguishably better at any lead; both are
  biased low by 0.2-0.4 m. Off the North Shore (51201), San Francisco (46026) and
  Rincon (46053) it has clear skill to at least five days (0.47-0.60 at 120 h) and less
  at seven (0.26-0.37 at 168 h). At ten days (228-252 h) it has no skill over
  climatology at any of the ten buoys. These are offshore Hm0 statements; surf height
  was not validated.

### 11.8 Verified live vs not

* **Live:** every number in this section: NDBC yearly, monthly and realtime files and
  activestations.xml, CDIP THREDDS files for the checks in docs/observations.md, NOAA
  NODD bulletins and listings, the repository's `obs ingest`, `obs collect-forecasts`,
  `verify pair --full` and `verify backtest` on that data.
* **Unit and integration tests only:** the long-term climatology and seasonal options of
  `verify backtest` on synthetic data (`tests/integration/test_verification.py`,
  `tests/unit/test_backtest_seasons.py`); they were then run live as above.
* **Not done:** 12Z cycles and odd days (not archived); the September 2026 QC'd monthly
  files (the realtime files were used, see 11.4); a model-version check of GFS-Wave over
  the window; any ML correction or calibration at these buoys; gridded GFS-Wave at the
  buoys; surf height.

### 11.9 Reproduction

Own database (`DATABASE_URL=...swell_ndbc_live`), from `backend/`:

```
python -m app.cli init-db
python -m app.cli obs seed-stations
python -m app.cli obs ingest --provider ndbc \
    --since 2024-10-12T00:00:00Z --until 2026-10-12T12:00:00Z         # 11 active stations
python -m app.cli obs ingest --provider ndbc --since 2019-10-12T00:00:00Z \
    --until 2024-10-12T00:00:00Z --station ndbc:51201 --station ndbc:51205 \
    --station ndbc:51101 --station ndbc:46026 --station ndbc:46237 --station ndbc:46214 \
    --station ndbc:46224 --station ndbc:46086 --station ndbc:46053 --station ndbc:46054
# 00Z bulletins on every second day from 2024-10-12 to 2026-10-12 (one call per day):
FORECAST_DAYS=10 python -m app.cli obs collect-forecasts --start DAYT00:00:00Z \
    --end DAYT00:00:00Z --cycles 0 --station ndbc:51201 ... --station ndbc:46054
python -m app.cli verify pair --full
python -m app.cli verify backtest --source gfswave_bulletin --hs-kind spectral_hm0 \
    --folds 5 --boot 1000 --lt-climatology-before 2024-10-12T00:00:00Z --by-season --json \
    --station ndbc:51201                       # each station, the five, and all ten
python -m app.cli verify backtest ... --end 2026-08-21T00:00:00Z --no-write   # 11.4
```

The analysis-time check (11.2) used the repository's loaders and `match_observations`
on leads 0-2 h, as in section 1; it is a one-off query, not a command.

## 12. Validation of the 80 % ranges, the forecast-confidence classes and the correction against NDBC (2026-10-12)

### 12.0 Plan, fixed before any result in this section was computed

This plan was written and committed before any coverage, class or training result of
section 12 was computed. The criteria in it are not changed afterwards; where a result
fails them, the result is reported, not the criterion.

**Data.** `swell_fval_live`, an unchanged copy of the section-11 database
(`swell_ndbc_live`): raw GFS-Wave station bulletins (`gfswave_bulletin`, 00Z on every
second day, 2024-10-12 to 2026-10-12) paired with NDBC spectral Hm0 (nearest observation
within 30 min, leads >= 3 h, `ml.dataset.build_dataset`). Primary station set: the five
offshore references of the paired spots, 51201, 51205, 46026, 46224 and 46053 ("OFF5");
all ten NDBC buoys of section 11 are a sensitivity check. For the correction, the AODN
delayed-QC Hm0 pairs of section 3 (Cape Naturaliste, Rottnest Island 02, Esperance 04,
2021) are added. GEFS-Wave station bulletins (`gefs.wave.{ID}.bull` in
`s3://noaa-gefs-pds/gefs.YYYYMMDD/00/wave/station/gefs.wave.t00z.bull_tar`) print the
ensemble spread only from the 2026-02-26 00Z cycle onward (earlier files list the
averages only) and exist for 6 of the 10 buoys (51201, 51205, 51101, 46214, 46086,
46054); they are collected for the same days as the deterministic bulletins.

**Split (ranges and classes).** TEST = cycles from 2025-10-12 00Z to the end of the
archive (the newer 12 months, one northern winter and one summer). TRAIN = every row
whose valid time is at or before 2025-10-12 00Z - 312 h (max lead 240 h + 3 days, the
gap used everywhere in the project); rows in between are purged. For the ensemble range
(7.5 months of data): TEST = cycles from 2026-06-20 00Z, TRAIN = valid times at or before
2026-06-07 00Z. Confidence intervals: 90 %, cycle-block bootstrap, 1,000 replicates,
seed 20261010.

**1. Offshore 80 % range.** For each pair, the app's offshore range for a single
deterministic source without ensemble (`surf_quality/uncertainty.py`): sigma_f =
`lead_sigma(lead)`, sigma_off = sqrt(sigma_f^2 + 0.15^2), p10/p90 = Hs_model x
exp(-/+ 1.2816 sigma_off), with Hs_model the bulletin's total Hst. Coverage = share of
observed Hm0 inside [p10, p90]; the shares below p10 and above p90 are reported too; by
lead bucket (24, 48, 72, 120, 168, 240 h and all leads) and by station.

* If TEST coverage lies in 75-85 % at every lead bucket with n >= 200, the heuristic range
  has passed a held-out coverage check as it is.
* Otherwise a lead-dependent scale is fitted on TRAIN only. With e = ln(max(obs, 0.05)) -
  ln(max(model, 0.05)) and s = sigma_off(lead): per lead bucket b, k_lo,b =
  Q0.90(-e / s) / 1.2816 and k_hi,b = Q0.90(e / s) / 1.2816 over the TRAIN rows of b (so
  that each tail holds 10 % on TRAIN: the app calls the bounds p10 and p90, so each bound
  should be a 10 % quantile, not only their span an 80 % interval); k(lead) is the linear
  interpolation of k_b between the bucket centres, constant before 24 h and after 240 h;
  fitted p10 = Hs_model x exp(-1.2816 k_lo(lead) s), p90 = Hs_model x exp(+1.2816
  k_hi(lead) s). The central value is not changed. A symmetric variant (one k_b =
  Q0.80(|e| / s) / 1.2816) is reported as a diagnostic only.
* The app's offshore range may be called calibrated only if the fitted range has a TEST
  coverage in 75-85 % at every lead bucket with n >= 200 (OFF5 pooled). Even then the label
  applies only to the offshore component, only to hours whose spread comes from the lead
  table (single deterministic source, no ensemble), and only at spots paired as
  offshore reference with a buoy in the TEST set; the breaking-height range keeps its
  nearshore heuristic share and says so. The WA 2021 pairs (other ocean, earlier years)
  are scored with the TRAIN-fitted range as an out-of-region check and reported.
* Ensemble range (`ensemble_heuristic`): sigma_f = 1.05 x Hs spread / Hs mean of the
  GEFS-Wave bulletin at the valid time (mean > 0.05 m, else the lead table), the same
  floor, centred on the deterministic Hst; same coverage table, criterion and fit on the
  ensemble split, all six GEFS buoys pooled.

**2. Forecast-confidence classes.** For each pair, the forecast-v1 class from
`classify_forecast` with the inputs that exist historically: lead time; one
deterministic family (`ncep_gfs`); index = `confidence_index(lead_sigma(lead),
completeness, stale=False)` with completeness from the bulletin's partition-1 height,
period and direction plus wind counted as present (the app's NOAA source always carries
GFS 10 m wind; the bulletin has none, and counting it missing would cap every hour at
Moderate for a reason the app never has); `missing_swell` when partition 1 has no period
or direction; no disagreement, no run-to-run change (the archive's previous cycle is 48 h
older, not the app's 6 h), not stale. Two variants: V0 without verification rows (as in
the live system today), V1 with the verification row the scheduled job would have
produced for the buoy at that cycle (raw bulletin, 90-day window of valid times ending at
the cycle time, n >= 200, nearest lead bucket, same scatter index and persistence skill).
The ensemble variant adds the GEFS spread (six buoys, ensemble split). Reported on TEST
by class and by class x lead bucket: n, bias, median |error|, p90 |error|, RMSE (m) and
median |ln(obs/model)|.

* Monotonicity (pass): pooled over leads, RMSE, median |error| and p90 |error| increase
  strictly High < Moderate < Low, and the 90 % CIs of RMSE(Moderate) - RMSE(High) and
  RMSE(Low) - RMSE(Moderate) exclude zero.
* A class boundary counts as clearly misplaced only if the same test fails on TRAIN. Only
  then is a change proposed, derived from TRAIN alone, and it is adopted (as forecast-v1.1)
  only if the changed classes pass the same test on TEST. Otherwise forecast-v1 stays.

**3. ML correction.** Decision run M1: `ml train --candidates linear,rf,gbt --folds 3
--boot 1000 --no-obs-features` on the pairs of OFF5 plus the three WA buoys (spectral Hm0,
`gfswave_bulletin`), default chronological 60/20/20 split by cycles with the 312 h gaps,
deployment criteria (a)-(f) of `ml/registry.py` unchanged. Only features known at issue
time are used (as for P2 v4), because the spots receive no buoy observation at the cycle
time through the prediction job. Transfer check (ADR-013) for the version the
auto-activation rule would pick (lowest validation RMSE among versions that pass every
criterion): imported into a migrated copy of `swell_live2` (the real 2026-10-11 runs),
factors computed through `ml/hook.py` on the gridded NOAA GFS run at every spot the
correction applies to, compared with the factors on the held-out test at the buoys. A
spot passes if at most 15 % of its corrected hours have a factor outside the central 90 %
range (p5-p95) of the test factors (about 10 % are outside by construction when the
inputs look like the test inputs; ADR-013 failed with 18 % below p5 alone). Activation
only if the version passes (a)-(f) and the transfer check, only at the spots that pass it
and that are paired with a buoy that has held-out TEST pairs, and only at the trained
leads; otherwise the reasons are recorded.

**Deviations from this plan, all decided after results were seen** (each is repeated
where it applies). Three only narrow a claim or its use: the station-consistency rule for
where the calibrated range label is used (12.2); excluding rincon from the correction
(12.4); and re-running the identical training with the final registry code so that the
registered applicability is the one the code now computes (12.4). One does not: the
boundary candidates, the rule choosing among them and the two-class form of the test for
the one candidate without a High class were written after the forecast-v1 TEST scores had
been computed, and they led to forecast-v1.1 (12.3, "How blind that TEST confirmation
is"). A fifth, added in the adversarial review of 2026-10-13 and also only narrowing:
the station-consistency rule checks every lead bucket of the station, not only all leads
together, which removes mavericks and ocean-beach-sf from the calibrated label (12.2).

### 12.1 Data

`swell_fval_live` (PostgreSQL, scratch): the section-11 NDBC observations (1,016,198
records, 2019-10 to 2026-10, spectral Hm0) and raw GFS-Wave bulletins (589,260 rows, 366
cycles, 00Z every second day, 2024-10-12 to 2026-10-12); for the correction, AODN
delayed-QC Hm0 at Cape Naturaliste, Rottnest Island 02 and Esperance 04 (38,541 records,
2021-03-22 to 2021-12-31, 34 requests, 0 errors) and their bulletins (273,861 rows, 568
cycles 00Z and 12Z, 3 errors: the 2021-03-23 12Z files carry the 00Z cycle and were
refused); GEFS-Wave station statistics (`gefswave_station`, docs/observations.md) for
51201, 51205, 51101, 46214, 46086 and 46054, 115 cycles (00Z every second day,
2026-02-26 to 2026-10-12), 9,315 rows per station, 425 MB downloaded, 0 errors. NOAA does
publish GEFS-Wave station bulletins before 2026-02-26 (checked: 2025-01-15), but those
files list only three numbers per row and no spread, so the ensemble range could only be
tested on the 7.5 months since then.

Pairs (`ml.dataset.build_dataset`, nearest observation within 30 min, leads >= 3 h):
251,103 at OFF5, 252,125 at the three WA buoys, 42,378 GEFS-matched pairs at the six GEFS
buoys. Split for ranges and classes: TRAIN = valid times up to 2025-09-29 (108,356 OFF5
pairs, 176 cycles), TEST = cycles 2025-10-13 to 2026-10-12 (136,097 pairs, 183 cycles);
ensemble split TRAIN to 2026-06-07 (16,598 pairs, 51 cycles), TEST 2026-06-20 to
2026-10-12 (22,380 pairs, 58 cycles, northern summer only). Leakage guards passed for every
split.

### 12.2 The offshore 80 % range

**Lead-table range (single deterministic source, no ensemble), OFF5 pooled, TEST**
(`verify ranges`; coverage = observed Hm0 inside [p10, p90], 90 % CI over cycles; nominal
80 % inside, 10 % below, 10 % above):

| Lead | n | App range: coverage | below p10 | above p90 | Fitted range: coverage | below p10 | above p90 | p90/p10 (fitted) |
|---|---:|---|---:|---:|---|---:|---:|---:|
| 24 h | 11,274 | 53.2 % [51.1, 55.3] | 1.5 | 45.3 | **79.6 %** [78.0, 81.2] | 11.1 | 9.2 | 1.55 |
| 48 h | 11,285 | 62.3 % [60.0, 64.5] | 2.1 | 35.7 | **80.2 %** [78.8, 81.6] | 11.4 | 8.4 | 1.60 |
| 72 h | 11,209 | 73.3 % [71.4, 75.4] | 1.5 | 25.2 | **79.5 %** [77.8, 81.1] | 10.5 | 10.1 | 1.60 |
| 120 h | 14,571 | 84.9 % [83.4, 86.5] | 1.3 | 13.8 | **79.5 %** [77.9, 81.0] | 10.2 | 10.3 | 1.67 |
| 168 h | 7,670 | 88.5 % [87.0, 90.0] | 1.6 | 9.9 | **79.6 %** [78.0, 81.3] | 9.8 | 10.6 | 1.89 |
| 240 h | 4,239 | 85.4 % [83.2, 87.4] | 3.5 | 11.1 | **75.1 %** [72.8, 77.4] | 12.1 | 12.8 | 2.18 |
| other leads (3-17, 31-41 h, ...) | 75,849 | 72.5 % | 1.5 | 26.0 | 80.5 % [79.6, 81.3] | 9.9 | 9.6 | 1.62 |
| all | 136,097 | 72.8 % [71.6, 74.1] | 1.6 | 25.6 | 80.0 % [79.2, 80.7] | 10.3 | 9.8 | 1.61 |

* **The app's range fails** the pre-registered test (24, 48, 72, 168 and 240 h outside
  75-85 %; TRAIN: 53.6 / 62.5 / 74.8 / 85.9 / 89.1 / 89.2 %). It is too narrow inside 3
  days and too wide beyond 5, and its misses are almost all above p90: the raw model is
  biased low (section 11) and the lead table, measured against the model's own analysis,
  understates the short-range error against buoys. By station (TEST, 24 h): 46224 27.8 %,
  51205 44.1 %, 51201 61.0 %, 46053 60.3 %, 46026 74.4 %.
* **Fitted scale** (TRAIN only, formula in 12.0): k_lo = 0.086 / 0.148 / 0.164 / 0.214 /
  0.321 / 0.413 and k_hi = 1.938 / 1.665 / 1.335 / 1.068 / 0.973 / 0.949 at 24 / 48 / 72 /
  120 / 168 / 240 h (per bucket 3,377-11,652 TRAIN pairs). The lower bound sits close to
  the model value and the upper bound far above it, i.e. the range mostly absorbs the low
  bias. On TRAIN it covers 79.7-80.7 % per bucket; on TEST 75.1-80.2 % at every bucket,
  so **the fitted range passes** the pre-registered criterion (240 h at 75.1 %, its CI
  reaching 72.8 %, is the weakest). The symmetric diagnostic variant covers 74.5-80.7 %
  but leaves 18-20 % above p90 at every lead, so it is not used.
* **By station the fitted range is not equally good** (TEST, all leads): 46026 79.4 %
  [78.0, 80.8], 46224 76.7 % [74.6, 78.8], 51205 85.5 % [83.8, 87.2], 51201 **90.8 %**
  [89.7, 91.8], 46053 **69.3 %** [67.4, 71.1]. Pooled calibration does not make every site
  calibrated: on the North Shore buoy the range is too wide, off Rincon too narrow.
* **Out of region (WA, 2021, never fitted on):** the fitted range covers 79.3 % overall
  (79.0-81.4 % to 120 h, 77.2 % at 168 h, 71.4 % at 240 h), but with 18.0 % below p10 and
  2.7 % above p90: relative to the forecast height the WA errors are smaller and less
  one-sided than at the NDBC buoys (the app's own range covers 89.0 % there). The scale is
  specific to where it was fitted.

**Ensemble range** (`ensemble_heuristic`: 1.05 x GEFS-Wave spread / mean, centred on the
deterministic bulletin Hst), six GEFS buoys pooled, ensemble TEST:

| Lead | n | App range | above p90 | Fitted range | below p10 | above p90 |
|---|---:|---|---:|---|---:|---:|
| 24 h | 1,440 | 58.9 % [55.1, 62.4] | 40.8 | 84.2 % [81.6, 86.5] | 9.8 | 6.0 |
| 48 h | 1,444 | 61.6 % [57.2, 65.7] | 37.5 | 84.3 % [81.6, 86.7] | 11.1 | 4.6 |
| 72 h | 1,415 | 62.6 % [58.9, 66.6] | 36.3 | 84.8 % [82.2, 87.1] | 10.6 | 4.6 |
| 120 h | 2,508 | 63.1 % [59.6, 66.3] | 33.3 | 78.1 % [75.0, 81.3] | 15.4 | 6.5 |
| 168 h | 2,463 | 62.3 % [58.4, 66.1] | 31.5 | 79.7 % [76.3, 83.1] | 13.7 | 6.6 |
| 240 h | 1,344 | 59.0 % [54.1, 63.6] | 34.6 | 79.6 % [75.3, 83.5] | 7.6 | 12.8 |
| all | 22,380 | 61.2 % [59.4, 63.1] | 35.3 | 81.5 % [80.2, 82.7] | 11.5 | 6.9 |

The app's ensemble range covers only 52-63 % on TRAIN and 59-63 % on TEST: the GEFS spread
is far smaller than the error of the deterministic forecast against buoys. A scale fitted
on TRAIN (k_hi 1.67-1.88, k_lo 0.07-0.91) passes pooled (78.1-84.8 %, close to the upper
edge inside 3 days), but at the only two GEFS buoys that are offshore references of spots
it is too wide: 51201 89.5 % [87.4, 91.3] and 51205 88.4 % [86.5, 90.0] (46214 63.4 %,
too narrow; 46054 81.9 %, 46086 84.1 %, 51101 85.5 %). The TEST period is one northern
summer.

**Decision.** The pre-registered criterion is met by the fitted lead-table range and by
the fitted ensemble range, pooled. Added after seeing the per-station results, and used
only to narrow where the word "calibrated" is applied: a spot gets the calibrated label
only if its paired offshore buoy's own TEST coverage has a CI that overlaps 75-85 %, over
all leads and at every lead bucket with at least 200 pairs (`ranges.station_consistency`;
the station-level form of the pre-registered per-bucket rule). That leaves, for the
lead-table range, **jaws-peahi (51205) and trestles (46224)**; pipeline, sunset-beach and
waimea-bay (51201, too wide), rincon (46053, too narrow) and mavericks and ocean-beach-sf
(46026, too narrow at 240 h) are not labelled. For the ensemble range no spot qualifies,
so **the ensemble-based range remains `ensemble_heuristic` and is recorded as not
validated** (pooled pass, contradicted at both paired buoys).

*Fitted range by buoy and lead bucket* (TEST, coverage % [90 % CI], from the `verify
ranges` by-station tables; an independent re-derivation in review on 2026-10-13, with raw
SQL pairing, its own formulas and its own bootstrap, gave the same coverages within 0.1
point and CI bounds within 0.6 point):

| Buoy (spots) | all | 24 h | 48 h | 72 h | 120 h | 168 h | 240 h |
|---|---|---|---|---|---|---|---|
| 51201 (pipeline, sunset-beach, waimea-bay) | 90.8 [89.7, 91.8] | 90.7 [88.1, 93.0] | 92.8 [90.4, 95.2] | 91.4 [88.6, 93.9] | 91.3 [89.0, 93.2] | 88.5 [85.4, 91.5] | 85.7 [81.8, 89.1] |
| 51205 (jaws-peahi) | 85.5 [83.8, 87.2] | 86.2 [82.9, 89.2] | 85.5 [82.2, 88.7] | 85.7 [82.4, 88.9] | 84.9 [81.9, 87.9] | 84.2 [81.3, 87.5] | 82.9 [79.0, 86.6] |
| 46026 (mavericks, ocean-beach-sf) | 79.4 [78.0, 80.8] | 80.1 [76.8, 83.0] | 84.5 [81.5, 87.4] | 80.8 [77.6, 83.7] | 77.5 [74.0, 80.9] | 75.0 [71.3, 78.6] | **69.1 [64.3, 74.1]** |
| 46224 (trestles) | 76.7 [74.6, 78.8] | 74.9 [71.2, 78.8] | 77.8 [74.0, 81.2] | 74.3 [70.1, 78.2] | 76.0 [72.2, 79.5] | 78.7 [74.9, 82.7] | 78.6 [74.4, 82.5] |
| 46053 (rincon) | 69.3 [67.4, 71.1] | 68.1 [64.4, 71.9] | 62.6 [58.4, 66.6] | 67.1 [62.8, 71.3] | 69.8 [66.2, 73.3] | 73.2 [69.7, 76.8] | 60.9 [56.0, 66.2] |

(n per buoy and bucket 711-3,059.) By point estimate no buoy is inside 75-85 % at every
bucket: 51205 is slightly too wide inside 3 days (85.5-86.2 %) and 46224 slightly too
narrow at 24 and 72 h (74.9, 74.3 %), but their CIs overlap the band, so those two keep
the label. At 46026 coverage falls with lead (77.5, 75.0, 69.1 %) and the 240 h CI lies
entirely below 75 %, so labelling its spots calibrated would contradict their own
held-out data at long leads; the builder's all-leads rule (79.4 %) missed this. Both
versions of the rule were chosen after the per-buoy numbers were seen; they only remove
spots, and which spots survive is itself a TEST-informed choice, so the two remaining
labels rest on the pooled pass plus "not contradicted at the spot's own buoy", not on an
independent per-spot test. These CIs resample whole cycles, but cycles 48 h apart share
valid times and weather, so they are probably too narrow, which makes the rule exclude
more readily than correctly sized intervals would. The range was validated on station
bulletins at the buoys, not on the gridded values the spots receive.

**Implemented** (`surf_quality/uncertainty.py`, `OFFSHORE_CALIBRATION`, version
`ndbc-offshore-v1`): the TRAIN-fitted k_lo / k_hi (4 decimals; scored on TEST they give
exactly the coverages above). `breaking_uncertainty` applies them to the offshore part of
the breaking range, and `offshore_range` to the offshore range itself, only when the
caller passes an `OffshoreContext` and the hour's spread comes from the lead table, its
central value is a raw single-family GFS-Wave value (not a blend, not ML-corrected), the
lead lies in 3-240 h, and the spot is one of the two above (four in the builder's
version; mavericks and ocean-beach-sf removed in review). Then
`components.uncertainty.offshore_kind` = `calibrated` with `offshore_calibration` =
`ndbc-offshore-v1`, while the breaking range's `uncertainty_kind` stays `heuristic`: its
translation share σ_m = 0.30 (larger than the offshore share) is still an assumption, and
breaking heights have no observations. **The prediction job does not pass the context
yet** (`surf_quality/predict.py` is outside this change), so no prediction is labelled
calibrated today; the wiring is a follow-up.

**With the correction of 12.4 active**, the central value at the corrected spots is no
longer raw, so the calibrated factors (which absorb the raw low bias) do not apply. The
app's heuristic lead-table range around the corrected Hs, scored on the correction's
held-out test at the five buoys (139,257 pairs, diagnostic only): 80.3 % at 24 h, 85.0 %
at 48 h, 89.5 % at 72 h, 92.7 % at 120 h, 93.6 % at 168 h, 90.8 % at 240 h (88.6 % over
all leads; 9.8 % below p10, 1.6 % above p90). It is too wide beyond 2 days and stays
labelled heuristic.

### 12.3 Forecast-confidence classes

For each pair the class from `classify_forecast` with the historical inputs (12.0): one
family, lead table or GEFS spread, completeness from the bulletin, no disagreement, no
run-to-run change, not stale. V0 = without verification rows (the live system today), V1
= with the verification row the scheduled job would have produced from the station's own
pairs in the 90 days before the cycle (8,348 rows over the OFF5 cycles). Errors are offshore
Hs errors at the buoy (m).

**forecast-v1, single source, OFF5:**

| | Class | n | bias | median abs. error | p90 abs. error | RMSE [90 % CI] |
|---|---|---:|---:|---:|---:|---|
| TRAIN, V0 | High | 34,527 | -0.255 | 0.23 | 0.54 | 0.387 [0.357, 0.417] |
| | Moderate | 53,467 | -0.234 | 0.23 | 0.58 | 0.404 [0.376, 0.430] |
| | Low | 20,362 | -0.215 | 0.26 | 0.77 | 0.504 [0.469, 0.539] |
| TEST, V0 | High | 43,401 | -0.267 | 0.25 | 0.60 | 0.382 [0.365, 0.398] |
| | Moderate | 67,153 | -0.252 | 0.25 | 0.63 | 0.406 [0.389, 0.420] |
| | Low | 25,543 | -0.243 | 0.29 | 0.86 | 0.543 [0.518, 0.567] |
| TEST, V1 | High | 31,385 | -0.237 | 0.22 | 0.58 | 0.364 [0.346, 0.382] |
| | Moderate | 79,169 | -0.266 | 0.26 | 0.63 | 0.408 [0.393, 0.423] |
| | Low | 25,543 | -0.243 | 0.29 | 0.86 | 0.543 [0.518, 0.567] |

RMSE(Moderate) - RMSE(High): TRAIN V0 0.017 [-0.010, 0.043], TRAIN V1 0.019 [-0.005,
0.043], TEST V0 0.024 [0.009, 0.037], TEST V1 0.044 [0.029, 0.059]. RMSE(Low) - RMSE(Moderate):
0.100 [0.065, 0.138] on TRAIN, 0.137 [0.112, 0.163] on TEST. The test **fails on TRAIN**
(V0 and V1: equal medians, Moderate - High CI includes zero) and on TEST in V0 (equal
medians); V1 passes on TEST, but by the plan a change is decided on TRAIN. Single-source
High is reached only at leads up to 52 h (index 70), so the High / Moderate split is a lead
split.

**Realised error by class and lead bucket, TEST** (pairs; median |error| / p90 |error| /
RMSE, m; offshore Hs at the buoy). Cells with fewer than 50 pairs are left out: Low
single-source hours with a missing partition-1 swell at 24, 72 and 120 h (5-6 pairs each)
and, with a GEFS-Wave spread, Moderate at 24 and 48 h (2 and 13), Low at 72 h (3) and
120 h (34).

| Lead | forecast-v1, single source (OFF5, V0) | forecast-v1.1, single source (OFF5) | v1 = v1.1 with a GEFS-Wave spread (GEFS6) |
|---|---|---|---|
| 24 h | High 11,268: 0.24 / 0.58 / 0.378 | Moderate 11,268: 0.24 / 0.58 / 0.378 | High 1,438: 0.25 / 0.53 / 0.333 |
| 48 h | High 9,549: 0.25 / 0.60 / 0.380; Moderate 1,736: 0.24 / 0.61 / 0.398 | Moderate 11,285: 0.25 / 0.60 / 0.383 | High 1,431: 0.25 / 0.53 / 0.336 |
| 72 h | Moderate 11,203: 0.25 / 0.61 / 0.392 | as v1 | High 825: 0.24 / 0.54 / 0.338; Moderate 587: 0.25 / 0.58 / 0.358 |
| 120 h | Moderate 14,566: 0.25 / 0.64 / 0.415 | as v1 | Moderate 2,474: 0.28 / 0.63 / 0.403 |
| 168 h | Low 7,670: 0.27 / 0.74 / 0.476 | as v1 | Moderate 2,404: 0.31 / 0.79 / 0.502; Low 59: 0.57 / 1.00 / 0.887 |
| 240 h | Low 4,239: 0.33 / 0.97 / 0.603 | as v1 | Moderate 1,256: 0.38 / 0.96 / 0.630; Low 88: 0.41 / 1.06 / 0.675 |

Without an ensemble the class of a complete single-source hour is a function of its lead
time, so the single-source ordering Moderate < Low validates the lead caps (beyond 7 days
the error is larger), not a separate certainty signal; inside one lead bucket there is
nothing to compare. With a GEFS-Wave spread the class also separates hours at the same
lead (72 h: High 0.338 vs Moderate 0.358 m RMSE; 168 and 240 h: Low above Moderate, on few
Low pairs).

**Boundary scan (TRAIN only, `--scan-boundaries`).** Every High / Moderate boundary b1 =
24, 30, ..., 72 h fails on TRAIN (median 0.23 m in both classes for every b1; Moderate -
High RMSE between 0.008 and 0.020 m, every CI includes zero). The only passing candidate
is C2, "no High without an ensemble or a second family" (Moderate RMSE 0.397, median 0.23,
p90 0.57 vs Low 0.504 / 0.26 / 0.77; gap 0.107 [0.073, 0.145]). C2 has no High class, so
it was tested in the two-class form (Moderate < Low on all three statistics and the gap CI
above zero); the choice rule (passing b1 nearest the current boundary, else C2) picks the
candidate from TRAIN results only, before the chosen one is scored on TEST. **On TEST, C2
passes:** Moderate 110,554 pairs, RMSE 0.396, median 0.25, p90 0.62; Low 25,543, 0.543 /
0.29 / 0.86; gap 0.146 [0.123, 0.169].

*How blind that TEST confirmation is.* The forecast-v1 TEST table above was computed, as
the plan requires, before the boundary scan, its candidates and its choice rule were
written (they are not in the pre-registered plan). C2's TEST classes are a merge of
forecast-v1's TEST High and Moderate classes, with Low unchanged, so C2's TEST result could
be anticipated from that table. The candidate is derived from TRAIN statistics and the
TEST rows were never used to fit anything, but the TEST check of C2 is not blind, so it is
weaker evidence than a held-out test whose outcome was unknown when the rule was chosen.

**forecast-v1.1 = forecast-v1 + C2** (`single_source_no_ensemble`). Its single-source
classes on OFF5 are exactly C2 (TRAIN and TEST as above; V1 = V0, because the verification
cap cannot lower a Moderate hour further). With a GEFS-Wave spread the rule does not fire,
and v1.1 = v1 (identical classes, checked):

| GEFS6, ensemble TEST | n | median abs. error | p90 | RMSE [90 % CI] |
|---|---:|---:|---:|---|
| High | 6,859 | 0.25 | 0.54 | 0.340 [0.326, 0.355] |
| Moderate | 15,131 | 0.30 | 0.77 | 0.490 [0.469, 0.512] |
| Low | 390 | 0.48 | 1.06 | 0.752 [0.633, 0.852] |

Moderate - High 0.150 [0.125, 0.174], Low - Moderate 0.262 [0.144, 0.368]: the full
three-class test passes on TRAIN and TEST, with and without verification rows (V1 TEST:
High 0.322, Moderate 0.474, Low 0.752). By lead on TEST, High occurs at 24-72 h (RMSE
0.333-0.338) and Moderate from 72 h (0.358 at 72 h, 0.403 at 120 h, 0.502 at 168 h, 0.630
at 240 h).

On the real 2026-10-11 runs (rescored with v1.1 in a scratch copy of `swell_live2`) the
new rule fired on 0 of 4,050 NOAA GFS and 0 of 4,050 blend hours: every hour had a
GEFS-Wave row.

**What this validates:** the ordering of the offshore error of single-family GFS-Wave
hours by class, without (Moderate < Low) and with a GEFS-Wave spread (High < Moderate <
Low), at buoys off Hawaii and California, 2024-2026. **What it cannot:** any rule that
needs two independent families (disagreement, agreement, `single_source_over_3d` vs a real
second model), ECMWF-only hours, run-to-run change at the app's 6-hourly cycles, staleness,
and breaking (local) confidence. It also says nothing about how a class translates into a
probability: the classes stay ordinal labels, not calibrated probabilities. A side
observation, not acted on (no pre-registered test): with causal verification rows the
`verification_poor` cap lowered hours whose error was higher at 24 h (RMSE 0.425 vs 0.351)
but lower at longer leads (e.g. 120 h 0.259 vs 0.422).

### 12.4 The correction (decision run M1)

`ml train --dataset <M1> --candidates linear,rf,gbt --folds 3 --boot 1000
--no-obs-features` on 503,228 pairs (OFF5 and the three WA buoys, spectral Hm0 only,
`gfswave_bulletin`, 933 cycles), features known at issue time (lead, model Hs and its
log, partition-1 height / period / direction, month and hour). Chronological split by
cycles with 312 h gaps: train 236,813 pairs, validation 106,682, **test 139,257 (cycles
2025-10-05 to 2026-10-12, NDBC only; the WA data all fall in the development part)**.
The train part is the three WA buoys only (valid times 2021-03-23 to 2021-12-15); the
validation part is the WA tail plus the first NDBC year (to 2025-09-22), and the final
model is refit on both (checked in review). Climatology reference from the development
period only.

| Candidate | validation RMSE | test RMSE (raw 0.427) | change | 90 % CI of the difference (m) | bias (raw -0.256) | HH (raw 0.287) | criteria |
|---|---:|---:|---:|---|---:|---:|---|
| ridge (alpha 100) | 0.403 | 0.335 | -21.5 % | [-0.101, -0.083] | +0.088 | 0.201 | all pass |
| **random forest** (80 trees, depth 12) | **0.385** | **0.320** | **-25.0 %** | [-0.115, -0.098] | +0.086 | 0.192 | all pass |
| gradient boosting | 0.395 | 0.322 | -24.7 % | [-0.114, -0.097] | +0.092 | 0.192 | all pass |

Random forest per lead bucket on test (raw -> corrected RMSE, m): 24 h 0.379 -> 0.251, 48
h 0.381 -> 0.265, 72 h 0.391 -> 0.277, 120 h 0.415 -> 0.315, 168 h 0.476 -> 0.397, 240 h
0.605 -> 0.518 (criterion c: every bucket at least 14 % better). Rolling-origin folds
(RMSE change vs raw): -9.3 % (test 2021-06 to 2021-09, WA), -16.0 % (2021-09 to 2021-12,
WA), -8.3 % (2021-12 to 2025-09, NDBC); ridge was worse than raw in fold 1 (+14 %) and
gradient boosting too (+4.7 %). Criteria (a)-(f) of `ml/registry.py`, unchanged, pass for
all three; auto-activation would pick the lowest validation RMSE, **the random forest**.
The correction overshoots slightly (bias +0.09 m instead of -0.26 m).

*Why it overshoots (review, 2026-10-13; diagnostic only, nothing was changed).* `ml train`
chooses the hyperparameters and the metre scale with a model fitted on the train part and
scored on validation, then refits on train + validation and keeps that scale. In M1 the
train part is WA only, so the WA-trained model under-corrected the NDBC validation rows
and the least-squares scale hit its upper clamp (`metre_scale` 1.1, limits 0.9-1.1); the
refit model has seen the NDBC year and corrects the NDBC bias itself, and the extra x1.1
then pushes it above the observations. Scored with the shipped model: validation bias
+0.119 m with the scale against -0.023 m without it (validation is in-sample for the
refit model), test bias +0.086 m against -0.057 m and test RMSE 0.320 against 0.315 m.
The test figures are not a reason to change the shipped model (that would tune on the
test year); the pipeline's scale step is a follow-up for the next pre-registered run
(estimate the scale on out-of-sample predictions of the refit model, e.g. the rolling
folds).

**Per buoy (held-out test, random forest; diagnostic):**

| Buoy (spots) | n | RMSE raw -> corrected | difference [90 % CI] | bias raw -> corrected |
|---|---:|---|---|---|
| 51201 (pipeline, sunset-beach, waimea-bay) | 23,599 | 0.469 -> 0.344 | -0.125 [-0.144, -0.106] | -0.325 -> +0.105 |
| 51205 (jaws-peahi) | 29,174 | 0.564 -> 0.317 | -0.247 [-0.263, -0.232] | -0.452 -> -0.006 |
| 46026 (mavericks, ocean-beach-sf) | 29,129 | 0.441 -> 0.392 | -0.048 [-0.061, -0.036] | -0.182 -> +0.109 |
| 46224 (trestles) | 28,898 | 0.300 -> 0.224 | -0.076 [-0.088, -0.065] | -0.214 -> +0.074 |
| 46053 (rincon) | 28,457 | 0.306 -> 0.304 | -0.002 [-0.015, +0.011] | -0.116 -> **+0.155** |

Winter (October-March) 0.493 -> 0.364, summer 0.348 -> 0.269.

**Transfer check (ADR-013).** The exported (compressed) random forest was imported into a
migrated copy of `swell_live2` and activated; `ml/hook.py` computed the factor at every
hour of the real gridded run `noaa-gfs:2026101118`. Test factors at the buoys: p5 / p50 /
p95 = 1.087 / 1.310 / 1.615 (range 0.79-2.00). At the spots: jaws-peahi 1.28-1.39,
mavericks 1.10-1.40, ocean-beach-sf 1.17-1.39, pipeline / sunset-beach / waimea-bay
1.16-1.40, trestles 1.17-1.61, and (first run, identical model) rincon 1.27-1.65: **0 %**
of the corrected hours outside p5-p95 at every spot (rincon 2.5 %), so every spot passes
(limit 15 %). For comparison, the ridge would fail at 9 of 11 spots (up to 64 % outside)
and the gradient boosting at rincon and the WA spots.

**Decision: the random forest (`offshore_hs_residual` v6) is shipped for activation**, at
jaws-peahi, mavericks, ocean-beach-sf, pipeline, sunset-beach, trestles and waimea-bay,
leads 3-240 h, `ncep_gfs` forecasts only (`backend/ml_models/offshore_hs_residual/`,
ADR-020). Not at:

* the WA spots: their buoys contributed only training data, so the correction was never
  tested there (`no_held_out_test_pairs`, recorded automatically by the registry);
* **rincon: excluded after the per-buoy results** (`ml restrict`): at 46053 the correction
  brought no measurable benefit (RMSE difference -0.002 m, CI includes zero) and turned a
  -0.12 m bias into +0.16 m. This is a narrowing of where a passing model is used, not a
  change of any criterion.

The training was run twice with the same dataset, settings and seed (v1-v3 before the
registry recorded untested stations, v5-v7 after; v4 is the imported WA ridge of section
10); test metrics, per-buoy scores and transfer factors are identical, and v6 is the one
exported. The 9.8 MB artifact is shipped re-serialised with zlib (3.7 MB); the
re-serialised model gives the same factors on a probe matrix (max difference 4e-16) and on
the live run.

**Verified end to end on the real runs** (scratch copy, activation, then the pipeline's
own code): rescoring the NOAA GFS run gave corrected values (and `ml_model_version`) on 80
of 81 hours at each of the seven spots (lead 0 is outside 3-240 h) and changed nothing at
the other 43 spots, including rincon and the WA spots; rebuilding the GFS + ECMWF blend
corrected the GFS member at the same 560 spot-hours (`wave_forecasts.extra.ml_corrections`)
and no others.

**A feature that means something else at the spots (review, 2026-10-13; diagnostic
only).** The swell features (`swell1_*`) come from the bulletin's first partition as
listed, which is the local wind sea in 16 % of the held-out test rows (4.5 % at 46224,
11 % at 46053, 17 % at 46026, 20 % at 51201, 29 % at 51205); at the spots they come from
the gridded GFS-Wave primary swell (SWELL 1), which is never the wind sea. Re-scoring the
shipped model on the same test rows with `swell1_*` replaced by the bulletin's first
non-wind-sea partition (the closest stand-in for what the spots receive; 1,504 rows with
no swell partition are then left raw): RMSE 0.342 m instead of 0.320 m (raw 0.427), bias
+0.104 m instead of +0.086 m; on the affected rows the factor changes by x0.86 / x1.06 /
x1.24 (p5 / p50 / p95). So the held-out benefit at the spots is probably closer to
-20 % than -25 %, still well clear of raw; the transfer check cannot see this, because it
compares factor distributions, not feature meanings. The next pre-registered run should
train on the swell partition the spots receive.

**Caveats.** The model is trained and tested on station bulletins, not on the gridded
values the spots receive; the transfer check shows that the live factors lie inside the
tested range, not that the corrected gridded forecast is better at the spots (there is no
observation at the spot). One year of test data, one GFS-Wave version (not checked
against NCEP change notices). The bias after correction is +0.09 m (slight
overforecasting). Bulletins were archived for every second day only. The correction acts
on offshore Hm0; the effect on breaking height (Hb ∝ H^0.8, so a factor 1.3 raises Hb by
about 23 %) is not validated, since breaking heights are not observed.

### 12.5 Verified live vs not

| Item | Status |
|---|---|
| GEFS-Wave station statistics download, parse, archive | live (115 cycles, 0 errors) |
| Range coverage and fitted scale, class scores, boundary scan | live data, repository commands |
| Shipped calibration constants reproduce the TEST coverage | live data (identical table) |
| Re-run of `verify ranges` (OFF5, GEFS6) and `verify confidence-classes` (OFF5 v1 with the boundary scan, OFF5 v1.1, GEFS6) on 2026-10-13 with commit 5ddfe2a | live data: every table and JSON result identical to the ones reported here |
| Shipped v6 / v4 manifests import, verify (sha256), apply only at their spots and leads | integration test on the real files (`test_shipped_ml_manifests.py`) |
| Calibrated offshore label in `breaking_uncertainty` | unit tests only; not wired into the prediction job |
| Correction training, criteria, per-buoy scores | live data |
| Transfer check, activation, NOAA rescoring, blend rebuild | live data (scratch copy of the real 2026-10-11 runs) |
| `ml restrict`, `export-manifest --compress`, untested-station exclusion | live (registry) and integration tests |
| Ensemble range validation | live, but one summer; not labelled anywhere |
| Breaking heights, local confidence, multi-family rules | not validated (no observations / no history) |

### 12.6 Reproduction

```bash
export DATABASE_URL=postgresql+psycopg://swell:swell@localhost:5432/swell_fval_live
OFF5="--station ndbc:51201 --station ndbc:51205 --station ndbc:46026 --station ndbc:46224 --station ndbc:46053"
GEFS6="--station ndbc:51201 --station ndbc:51205 --station ndbc:51101 --station ndbc:46214 --station ndbc:46086 --station ndbc:46054"
# GEFS-Wave statistics on the days of the archived deterministic cycles (00Z, every second
# day), one call per cycle (a one-off loop over gefs_bulletins.collect_range), e.g.:
python -m app.cli obs collect-forecasts --source gefswave_station --start 2026-02-26T00:00:00Z \
    --end 2026-02-26T01:00:00Z --cycles 0 $GEFS6
python -m app.cli verify ranges $OFF5 --test-start 2025-10-12T00:00:00Z \
    --check-station aodn:cape-naturaliste --check-station aodn:rottnest-island-02 \
    --check-station aodn:esperance-04 --json ranges_off5.json
python -m app.cli verify ranges $GEFS6 --test-start 2026-06-20T00:00:00Z \
    --ensemble-source gefswave_station --json ranges_gefs6.json
python -m app.cli verify confidence-classes $OFF5 --test-start 2025-10-12T00:00:00Z \
    --method forecast-v1 --scan-boundaries --json classes_off5_v1.json
python -m app.cli verify confidence-classes $OFF5 --test-start 2025-10-12T00:00:00Z --json classes_off5_v11.json
python -m app.cli verify confidence-classes $GEFS6 --test-start 2026-06-20T00:00:00Z \
    --ensemble-source gefswave_station --json classes_gefs6.json
python -m app.cli ml build-dataset --source gfswave_bulletin $OFF5 --station aodn:cape-naturaliste \
    --station aodn:rottnest-island-02 --station aodn:esperance-04 --out m1.parquet
python -m app.cli ml train --dataset m1.parquet --candidates linear,rf,gbt --folds 3 --boot 1000 --no-obs-features
python -m app.cli ml restrict <rf id> --exclude-spot rincon --reason "..."
python -m app.cli ml export-manifest <rf id> ml_models/offshore_hs_residual --compress
```

The per-buoy table, the transfer check, the corrected-centre range diagnostic and the
end-to-end activation check were one-off scripts over the repository's functions
(`ml.dataset.chronological_split`, `registry.load_artifact`, `ml.hook.RunCorrector`,
`surf_quality.predict.compute_predictions_for_run`, `forecasts.blend.build_blend`), not
commands.

### 12.7 Adversarial review (2026-10-13)

Re-run on `swell_revval_live`, a copy of `swell_fval_live`, with the committed code;
one-off review scripts kept outside the repository.

**Reproduced.** An independent re-derivation of the OFF5 coverage table and the
forecast-v1 class table, with raw SQL pairing (nearest spectral Hm0 within 30 min), its
own sigma, bucket, class and bootstrap code, gave the same pair counts (251,103; TRAIN
108,356; TEST 136,097), the same app-range and fitted-range coverages, the same refitted
k_lo / k_hi as the shipped constants, and the same class counts, medians, p90s and
RMSEs. `verify confidence-classes` (forecast-v1 with the boundary scan, v1.1, GEFS6)
gave JSON identical to the builder's; `verify ranges` OFF5 gave identical results apart
from the station-consistency change below. The pooled ten-buoy backtest of section 11
(409,344 pairs) gave the same table (bias -0.234 m; RMSE 0.376 / 0.508 / 0.642 m at 24 /
168 / 240 h; skill vs the long-term climatology 0.578 / 0.233 / -0.278 [-0.414, -0.157]).
The transfer check of the shipped v6, imported into a fresh migrated copy of `swell_live2`:
test factors p5 / p50 / p95 1.0867 / 1.3095 / 1.6146, 80 of 81 hours corrected at each of
the seven spots (lead 0 is outside 3-240 h), 0 % outside p5-p95, no hour corrected at any
other spot.

**Re-checked against the live services.** `activestations.xml` (2026-10-13): every NDBC
catalog position equal; station pages: the water depths of 46277, 46224, 51205, 46053,
46086, 51101 and 46237 equal. CDIP THREDDS 106 (= 51201) against the stored records:
monthly QC'd NDBC Hm0 equal to CDIP `waveHs` exactly (4,838 records, late May to
September 2026), Tp and Ta within 0.005 s, Dp within 0.5 degree, NDBC time = CDIP sample
start + 26 min; realtime records within the 0.1 m / 1 s rounding of realtime files.
Folding of realtime copies (ADR-017) against the QC'd September 2026 files: 46026 2,500
wave rows -> 1,404 records, 1,397 on an archived time and 7 within 2 min, |dHs| <= 0.05
m; 46053 2,023 -> 1,376, all on archived times; 51201 1,440, all on archived times.
Missing files (monthly gz of March / April 2026 at 51201, `51201h2026.txt.gz`,
`view_text_file.php` for a missing year) answer 404.

**Deviations from the plan, verdicts.**

* *Per-station label rule* (12.2): defensible only as a narrowing, and the builder's
  version was too lenient: it looked at all leads together and kept 46026, whose 240 h
  coverage CI [64.3, 74.1] lies below the band. Fixed: every bucket is checked, mavericks
  and ocean-beach-sf are no longer labelled. The remaining labels (jaws-peahi, trestles)
  rest on the pooled pass plus "not contradicted at the own buoy"; by point estimate 51205
  is just above the band at 24-72 h and 46224 just below it at 24 and 72 h.
* *Two-class test for forecast-v1.1* (12.3): defensible as a conservative cap (it can only
  lower classes), not as strong evidence. The candidate set and the two-class form were
  written after forecast-v1's TEST scores, C2's TEST classes are a merge of v1's, and on
  TEST v1's High was in fact 0.024 m [0.009, 0.037] better than Moderate in RMSE (0.044
  with verification rows, where v1 passes the full test). The plan decides on TRAIN, where
  v1 failed, so v1.1 follows the plan; what TEST confirms is the lead caps (Moderate <
  Low), not that High was misplaced.
* *Rincon exclusion* (12.4): defensible. Per lead bucket at 46053 (review script, shipped
  artifact, own cycle-block bootstrap) the correction is worse
  than raw at 48, 72 and 120 h (+4.1 %, +2.9 %, +3.6 %; criterion (c), if applied to this
  buoy alone, would fail at 48 and 120 h), no bucket's improvement CI excludes zero, and
  the bias moves from -0.12 to +0.16 m. At the four buoys of the seven approved spots no
  bucket gets worse (worst -1.8 % at 46026, 168 h; improvement CIs include zero at 46026
  72 and 168 h and 46224 240 h).
* *Criteria and activation scope*: `git diff` of `ml/registry.py` from d2b7e42 touches
  no line of `evaluate_deployment` or its constants; the shipped manifest applies to the
  seven spots, leads 3-240 h, `ncep_gfs`, and imports as CANDIDATE.

**New findings, documented, not acted on** (acting on test-year numbers would be tuning):
the metre-scale overshoot and the wind-sea swell feature (12.4).

**Test flake.** `test_paused_search_does_not_generate_notifications` failed depending on
the date and the microsecond: natural demo swells follow calendar slots, the simulated
swell merges into one that starts 4-5 days ahead, and a search with no earlier match gets
no new match inside `DETECTION_LEAD_MIN_DAYS`. The test now runs without natural demo
swells and checks more (event inside the matching window, no match while paused, the
alert belongs to the simulated swell). A clock-shift sweep (freezegun, 0-45 days for this
test; 45-800 days for the unit suite; several shifts for the integration suite) found no
other date-dependent test.
