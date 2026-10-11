# Offshore wave forecast backtest and ML correction against spectral Hm0 buoy data

This document reports a live backtest of the raw GFS-Wave forecast at buoys and of the
offshore Hs correction (`docs/ml-pipeline.md`) trained on those pairs. Every number below
was produced on 2026-10-11 from live downloads (NOAA NODD and AODN S3 buckets) into a
scratch PostgreSQL database, using the repository's own commands. Nothing here is
synthetic.

**What this measures.** Offshore significant wave height **Hm0** (the spectral estimate,
`4·sqrt(m0)`) at buoy positions, forecast by GFS-Wave and observed by moored buoys whose
delayed-mode data passed the data owner's quality control. It does **not** measure
breaking (surf) height, which no dataset used here observes, and it says nothing about
regions other than south-west Western Australia.

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
* **Australian sites only, one region.** South-west Western Australia (Rottnest,
  Cape Naturaliste, Esperance). The results do not transfer automatically to other
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
* **Not available here:** NDBC, CDIP and CO-OPS (blocked in this sandbox) were not used.

## 10. Deployment in the application

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
