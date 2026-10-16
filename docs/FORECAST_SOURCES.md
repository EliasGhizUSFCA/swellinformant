# Forecast and travel data sources

Every source below is an official, documented API. Nothing in this project scrapes
websites, bypasses anti-bot systems or calls private endpoints. Licences, keys, free tiers
and costs of every service: [API_SETUP_REQUIRED.md](API_SETUP_REQUIRED.md). Windy is the
exception to "used within its terms" unless a licence allows storage: its token is
refused by default (below).

## Forecasts

**Recommended setup (API-first):** `FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf`. Spot
forecasts come from Open-Meteo as two independent model families, the map layers from
NOAA NODD byte ranges, the ensemble spread from GEFS-Wave (HTSGW and PERPW only). The
direct GRIB ingesters stay as tested, key-free fallbacks, off by default.

| Source (`FORECAST_PROVIDERS` token → source code, family) | Status | Data | Access |
|--------|---------------------|------|--------|
| **Open-Meteo, NOAA GFS + GFS-Wave** (`open_meteo` → `open_meteo_noaa`, `ncep_gfs`) | Recommended. `forecasts/open_meteo.py`; mock tests on recorded responses; live runs below | Waves from `ncep_gfswave016` (0.16°, about 15°S-52.5°N) or `ncep_gfswave025` elsewhere: Hs, peak period, direction, primary/secondary/tertiary swell and wind sea. Wind, gust (GFS instantaneous), MSL pressure from `gfs_global`. Hourly to 120 h, then 3-hourly (the model's own steps) | Free for non-commercial use; `OPEN_METEO_API_KEY` for the commercial hosts |
| **Open-Meteo, ECMWF IFS + ecWAM** (`open_meteo_ecmwf` → `open_meteo_ecmwf`, `ecmwf_ifs`) | Recommended. `forecasts/open_meteo_ecmwf.py`; mock tests; live runs below | `ecmwf_wam025`: Hs, peak period (`wave_peak_period`; the mean period only in `extra`), mean direction; no swell partitions. `ecmwf_ifs025`: wind, gust (3-hour maximum), MSL pressure. 3-hourly to 144 h, then 6-hourly, 00Z and 12Z runs only | same |
| **NOAA GFS-Wave + GFS, direct** (`noaa_gfs` → `noaa_gfs_direct`, `ncep_gfs`) | Optional fallback, off by default. `forecasts/noaa.py`. Verified live 2026-10-11: 4,050 records, 811 requests, 1.91 GB | as above, 3-hourly, from the native WW3 grids, sampled at our nearest valid sea cell | NOAA NODD on AWS, GRIB2 by HTTP Range. No key. [noaa-direct-ingestion.md](noaa-direct-ingestion.md) |
| **ECMWF IFS open data, direct** (`ecmwf_ifs` → `ecmwf_ifs_open`, `ecmwf_ifs`) | Optional fallback, off by default. `forecasts/ecmwf.py`. Now reads only `ECMWF_WAVE_PARAMS` (default `swh,mwd,pp1d,mwp`: 3.69 MB per step instead of 10.06 MB; `wmb` once per run) and records its bytes | Hs, Tp (`pp1d`), mean direction, Tm-1,0, wind, gust, pressure | ECMWF open-data bucket, HTTP Range. CC BY 4.0, "Copyright ECMWF" |
| **NOAA GEFS-Wave ensemble statistics** (`GEFS_ENABLED=true` → `noaa_gefs_wave`, `ncep_gefs`) | `forecasts/ensemble.py`. Now reads only `GEFS_STAT_FIELDS` (default HTSGW, PERPW: 4 records, 1.60 MB per step instead of 10 records, about 4.9 MB) for the cycles in `GEFS_CYCLES` | Mean and spread of Hs and Tp, used only for uncertainty ranges and forecast confidence | NOAA bucket, no key |
| **Windy Point Forecast API v2** (`windy` → `windy_gfswave`, `ncep_gfs`) | Refused unless `WINDY_TERMS_ACKNOWLEDGED=true`: Windy's terms forbid storing the data and derived databases. Wind-sea keys fixed to `wwaves_*`. Mock-tested only | `gfsWave` waves, `gfs` wind | Paid key, `WINDY_API_KEY` |
| **Demo provider** (`demo`) | Default | Synthetic but physically plausible swells, winds and tides | None. Labelled DEMO everywhere |
| Copernicus Marine (MFWAM) | Not implemented (family `mfwam` reserved) | | Free account |
| Surfline, Windy website | **Not integrated** | — | No authorised access; scraping would violate their terms |

### Families, the blend and what "our prediction" is built from

* Every source has a **dependency family** (`forecasts/families.py`). NOAA direct,
  Open-Meteo's GFS family and Windy's `gfsWave` are the same model (`ncep_gfs`), so their
  agreement is not evidence; GEFS (`ncep_gefs`) is related to it; ECMWF (`ecmwf_ifs`,
  direct or through Open-Meteo) is independent. With `open_meteo` and `noaa_gfs` both
  configured, the higher-priority one is the GFS member and the other only an integrity
  cross-check.
* With two or more independent families, job A2 (`forecasts.blend`) builds a **blend**
  run (`forecasts/blend.py`): one member per family, weighted by the best linear unbiased
  combination for correlated errors (prior error correlation 0.6 related / 0.3
  independent; per-member error from verification when it exists, otherwise a lead-time
  prior). Hs is combined geometrically, Tp only over peak-period members, wind direction
  as unit vectors, swell partitions come from the member that has them (GFS-Wave) scaled
  to the blended Hs; a partition the member does not have stays empty. Gusts are averaged
  only over members with the same gust definition as the highest-weight member
  (`extra.gust_kind`, with every member's in `extra.gust_kinds`): a GFS instantaneous
  gust and an ECMWF 3-hour maximum are different quantities. Nothing is averaged equally.
* Members are aligned in time by linear interpolation between their own steps (at most
  6 h apart, never extrapolated). A blended hour that uses such an interpolated member
  value is marked `extra.interpolated = true` with the members in
  `extra.interpolated_members`, so it is never shown as all-native model output (with
  the GFS family hourly to 120 h and ECMWF 3-hourly, two of three hours in that range use
  an interpolated ECMWF value).
* Each hour stores a `forecast_comparisons` row: Hs log ratio, Tp, direction and wind
  differences, and flags (`hs_disagree` above a ratio of 1.4, `period_disagree` above 3 s,
  `direction_disagree` above 30°, `wind_disagree` above 15 km/h; heuristic thresholds).
  Flagged hours widen the forecast range and cap forecast confidence at Moderate (Low when
  Hs differs by more than a factor 2).
* Predictions are computed for every run; the blend (or the single primary source when no
  blend exists) is authoritative for detection, matching and alerts. Only authoritative
  runs are archived in `prediction_archive`. Predictions of superseded member runs are
  pruned nightly (derived data; see "Storage").

### How the Open-Meteo adapters match direct sampling

The point of the API-first design is the same model data for kilobytes instead of
gigabytes, without losing accuracy. What the adapters do (`forecasts/open_meteo.py`):

* **Sea cells.** Every request sends `cell_selection=sea` (`OPEN_METEO_CELL_SELECTION`).
  Open-Meteo's default picks a land cell of similar elevation for weather, which gave
  18.4 instead of 28.0 km/h mean wind at Pipeline in the audit. Waves are requested at the
  spot's offshore forecast point, wind at the break; the cells Open-Meteo used and their
  distances are stored in each spot's first record (`extra.wave_cell`, `extra.wind_cell`).
  A wave cell with no value in any hour (masked by the wave model although Open-Meteo
  calls it sea: `ecmwf_wam025` at Keramas and Puerto Escondido on 2026-10-14) is replaced
  by the nearest of its eight neighbouring cells that has data, at most
  `GRIB_MAX_CELL_DISTANCE_KM` (60) from the forecast point, as the GRIB ingesters take the
  nearest valid cell; the replacement is listed in the run's warnings.
* **Native wave grids.** `ncep_gfswave016` where its domain (read from the model's
  metadata) contains the point, `ncep_gfswave025` elsewhere; one request never mixes
  models, and a spot refused with HTTP 400 "No data is available for this location" is
  retried with the next model.
* **Partitions.** Primary, secondary and tertiary swell and wind sea. Open-Meteo returns
  0 m / 0 s / 0° for a partition the model does not have at that time; it is stored as
  empty, never as a 0 m swell from the north.
* **Native steps only.** Open-Meteo interpolates every model to hourly. Only the model's
  own output steps are stored (GFS: hourly to 120 h, then 3-hourly; IFS/WAM 0.25:
  3-hourly to 144 h, then 6-hourly); every record carries `extra.native_step_seconds`.
  With `OPEN_METEO_NATIVE_STEPS_ONLY=false` the other hours are kept and flagged
  `extra.interpolated`.
* **Run identity.** The run is read from each model's `meta.json` and used only
  `OPEN_METEO_AVAILABILITY_WAIT_MINUTES` (10) after its availability time, as Open-Meteo
  advises. The metadata is read again after the data requests: a run published in between
  is never stored under the old key. Hours past the run's last step (which Open-Meteo fills
  from older, longer runs) are dropped. Every model run behind the data is stored in
  `forecast_runs.details.model_runs` (for `gfs_seamless`: HRRR and GFS). The ECMWF family
  reads only the 00Z and 12Z runs: the 06Z/18Z runs stop at 144 h and Open-Meteo fills the
  rest of their series from the previous run.
* **No tide from the wave API.** `sea_level_height_msl` is not requested (a modelled sea
  surface height, "not suitable for coastal navigation"); tides come from `app.services.tides`.

### Quotas, cache and retries

* **Weighted call budget** (`forecasts/quota.py`): each request is charged
  `locations x max(1, variables/10) x max(1, days/14)` weighted calls before it is sent,
  atomically against minute, hour, day and month windows in Redis
  (`OPEN_METEO_CALLS_PER_*`, free-tier defaults 600 / 5,000 / 10,000 / 300,000). A full
  minute or hour window is transient (the task retries when it resets); a full day or
  month is `QuotaExhaustedError`: that provider's run ends FAILED with the reason and the
  next provider still runs. Open-Meteo's own HTTP 429 reasons are mapped the same way.
  Metadata requests are free and not charged. `python -m app.cli engine budget` prints
  the counters.
* **Response cache** (`forecasts/provider_cache.py`): bodies of one model run are kept in
  Redis for the model's update interval (6 h), zlib-compressed, at most
  `PROVIDER_RESPONSE_CACHE_MAX_KEYS` (200) entries, bodies above
  `PROVIDER_RESPONSE_CACHE_MAX_BYTES` (2 MB) never cached. A retried run reads what it
  already received instead of paying for it again. Redis errors never fail a fetch.
* **Retries and bookkeeping** (`forecasts/ingestion.py`, `workers/tasks.py`): providers
  run one after another; each run records its own `started_at`, its cost in
  `details.stats` (bytes on the wire, HTTP responses by status, weighted calls; also for a
  FAILED run) and its model runs. Any exception ends only that provider's run, FAILED,
  never RUNNING. A transient failure retries the Celery task after 2, 4, 8 min (or the
  provider's wait, at most 1 h); an acquisition where every provider failed is logged
  FAILED and does not chain to the blend.

### Staleness (per source, ADR-015)

A run is stale once it is older than its source's cycle interval + publication lag +
acquisition interval (`FORECAST_REFRESH_MINUTES`) + 2 h: **Open-Meteo GFS family 17.2 h**
(6 + 6 + 0.17 + 3 + 2; Open-Meteo published the GFS domains 5.3-6.0 h after
initialisation on 2026-10-14, plus the 10-minute wait), **Open-Meteo ECMWF family 24.7 h**
(12 + 7.5 + 0.17 + 3 + 2; 7.0-7.2 h, 00Z/12Z only), **NOAA GFS direct 16 h**, **GEFS-Wave
18.5 h** (24.5 h with `GEFS_CYCLES=00,12`), **ECMWF direct 25 h**; demo and Windy keep
`FORECAST_STALE_HOURS` (18). A blend is stale when any member is stale by its own rule.
Publication lags vary: for the 06Z run of 2026-10-15 Open-Meteo published `ncep_gfs025`
8.2 h after initialisation (`ncep_gfs013` 6.0 h, the waves 5.3 h). A run's issue time is
its oldest model run, so in such a cycle the GFS family can read stale for a few minutes
before the next acquisition; the 2 h margin covers most of it.
Stale runs are left out of the blend, lose to fresh runs as the authoritative source, cap
forecast confidence at Moderate, are flagged in the UI and `/api/system/status`, and alerts
are not generated once a run is twice its stale-after age.

### Storage

* Forecast runs older than `RETENTION_FORECAST_DAYS` (10) are deleted nightly, keeping the
  two newest usable runs per source.
* The predictions of superseded member runs are deleted nightly: derived, recomputable
  data (about 2.5 kB per row), never the blend's, never the newest run of a source, never
  a run that is still authoritative for a spot.
* The prediction archive keeps the authoritative predictions for
  `RETENTION_ARCHIVE_DAYS` (730) with a digest of the confidence inputs
  (`ARCHIVE_CONFIDENCE_DIGEST`): measured on the real 2026-10-11 blend, 562 B per row
  instead of 1,506 B.
* Open-Meteo bodies are never written to disk; GRIB is decoded in memory. A trace of a
  live acquisition (`strace -e openat,creat,rename,unlink,mkdir`) showed no file opened
  for writing by the acquisition.

### Switching to live forecasts

```env
FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf   # first = primary; two families -> blend
GEFS_ENABLED=true                                # ensemble spread for the uncertainty ranges
FORECAST_BLEND_ENABLED=true
OPEN_METEO_API_KEY=                              # only for commercial use
```

`demo` cannot be combined with a live provider (the settings validator rejects it), so
synthetic and real data never mix. To try it once from the command line:

```bash
cd backend
FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf python -m app.cli pipeline
python -m app.cli engine budget                    # weighted calls used
GEFS_ENABLED=true python -m app.cli engine ensemble # GEFS-Wave mean/spread
```

The direct GRIB ingesters (`noaa_gfs`, `ecmwf_ifs`) remain available as key-free
fallbacks, for example if Open-Meteo is unavailable or its terms change; they download
about 1.9 GB (NOAA) and 0.48 GB (ECMWF, trimmed; computed) per run.

### Limitations

* All sources here are **offshore** models (WW3 at 1/6°-1/4°, ecWAM at 0.25°). Breaking
  heights come from our nearshore model (`docs/SURF_SCORING.md`), not from the providers.
* ECMWF open data has no swell partitions, so an ECMWF-only run uses the bulk sea state
  for the breaking estimate; in the blend the partitions come from GFS-Wave.
* Open-Meteo picks its own grid cell; it was our nearest valid sea cell at 47 of 50 spots
  for GFS-Wave on 2026-10-14 (the other 3 a neighbouring valid cell). For `ecmwf_wam025`
  it chose a masked cell at 2 spots, replaced by a neighbouring cell 14.3 km (Puerto
  Escondido) and 26 km (Keramas) from the forecast point. The cells used are stored with
  each run.
* One vendor for spot forecasts: the free tier is non-commercial, counts calls per IP
  address and its terms can change; the direct GRIB providers stay as tested fallbacks.
* GFS 0.25° and Open-Meteo `gfs_global` winds under-resolve sea breezes and terrain effects
  near the coast.
* GFS v17 (expected late 2026) will rename NOAA files: the bucket and path templates are
  settings (`NOAA_GFS_BASE_URL`, `NOAA_GFS_WAVE_PATH_TEMPLATE`,
  `NOAA_GFS_ATMOS_PATH_TEMPLATE`, `MAP_FIELD_ATMOS_PATH_TEMPLATE`), and verification
  statistics should restart after the switch.
* No tide comes from the forecast providers (`sea_level_m` stays empty); tides come from
  `app.services.tides` (NOAA CO-OPS and harmonic constants, labelled with their datum).

### API-first acquisition (measured live, 2026-10-14, this sandbox's proxy)

One acquisition with `FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf` on a fresh scratch
database (`init-db`, the 50 seeded spots), every response fetched live (empty cache), then
the blend and the predictions (`build_blend`, `compute_pending_predictions`: jobs A2 and
B). The numbers are what `forecast_runs.details.stats` and the parity scripts recorded;
nothing here is extrapolated unless it says so.

| Run (GFS and ECMWF 12Z runs of 2026-10-14) | Result |
|---|---|
| `open_meteo_noaa` (GFS family, 16 days) | SUCCESS, 50 of 50 spots, 10,200 rows (120 hourly steps to f119, then 84 3-hourly), 3 data requests (`ncep_gfswave016` for 32 spots, `ncep_gfswave025` for 18, `gfs_global`), 11 HTTP responses with the metadata, **345.6 kB**, **142.9 weighted calls**, 6.3 s. Model runs recorded: `ncep_gfswave016`, `ncep_gfswave025`, `ncep_gfs013`, `ncep_gfs025`, all 12Z |
| `open_meteo_ecmwf` (ECMWF family, 15 days) | SUCCESS, 50 of 50 spots, 4,100 rows (48 3-hourly steps, then 34 6-hourly), 3 data requests, 7 HTTP responses, **175.1 kB**, **124.3 weighted calls** (17.1 of them for the neighbouring cells of Keramas and Puerto Escondido), 4.7 s; `extra.gust_kind` `ecmwf_max_3h` on every row |
| Blend | SUCCESS, 2 independent families (`ncep_gfs`, `ecmwf_ifs`), 10,200 rows and 10,200 comparison rows, 9.9 s; 5,650 rows marked `interpolated` (an ECMWF value aligned in time); gust kind of the highest-weight member: `gfs_instantaneous` 9,650, `ecmwf_max_3h` 550 |
| Predictions | 24,500 for the 3 runs, 4,050 archived (the blend's), 19.7 s |
| Storage | database 12.2 MB after `init-db`, 101.3 MB after this cycle: `surf_quality_predictions` 63.6 MB (2.29 kB per row), `wave_forecasts` 14.6 MB (GFS family 407 B, ECMWF family 597 B, blend 535 B per row), `forecast_comparisons` 7.7 MB, `prediction_archive` 3.0 MB (555 B per row). Redis 1.98 MB before, 2.53 MB after |
| Files written (`strace -f -e openat,creat,rename,unlink,mkdir`) | None by the acquisition: the only files opened for writing were the test driver's own report and a joblib semaphore in `/dev/shm` (shared memory) |
| Open-Meteo refusals | Earlier the same day the Forecast API answered "Daily API request limit exceeded. Please try again tomorrow." for this sandbox's shared egress address (other users of the address; the app had used 143 weighted calls). Those runs ended FAILED, not retried, with their cost recorded, and the next acquisition read the wave responses it had already received from the cache and paid only for the wind request |

**Same-cycle parity** (valid time 2026-10-14 18:00 UTC = lead 6 h of both 12Z runs; the
direct samples were read by byte range and decoded in memory, at the cell Open-Meteo
reported for each spot):

| Comparison, 50 spots | Hs | Peak period | Wind speed | Wind direction |
|---|---|---|---|---|
| GFS family vs NOAA NODD `gfswave` `global.0p16`/`global.0p25` (HTSGW, PERPW) | **50 of 50 within 0.05 m**; mean 0.004 m, max 0.01 m | mean 0.009 s, max 0.02 s | | |
| GFS family vs NOAA NODD `gfs.t12z.sfluxgrbf006` (UGRD/VGRD 10 m on the same ~13 km Gaussian grid Open-Meteo's `gfs_global` wind comes from, `ncep_gfs013`) | | | mean 0.08 km/h, max 0.21 km/h | mean 0.4°, max 2.4° |
| GFS family vs NOAA NODD `pgrb2.0p25` (the 0.25° grid our direct ingester reads), nearest 0.25° cell | | | mean 2.7 km/h, max 9.7 km/h | mean 7.4°, max 32° |
| ECMWF family vs ECMWF open data `wave`/`oper` step 6 (swh, pp1d, mwd, 10u/10v) | **50 of 50 within 0.05 m**; mean 0.005 m, max 0.01 m | mean 0.013 s, max 0.025 s | mean 0.09 km/h, max 0.24 km/h | mean 0.4°, max 3.1° |

The remaining differences are of the size of Open-Meteo's output rounding (0.01 m,
0.05 s, 0.1 km/h, 1°) and the GRIB packing precision; the largest direction differences
are at light wind (3.1° at 0.8 km/h at Ocean Beach SF, 2.4° at 4.5 km/h at Punta Roca).
The wind difference against `pgrb2.0p25` is the grid, not the transport: the same GFS run
on a coarser grid. The direct samples cost 4.3 MB (waves and the `pgrb2.0p25` wind), 7.3 MB
(sflux wind) and 4.6 MB (ECMWF) for this single step. Open-Meteo's GFS-Wave cell was our
nearest valid sea cell at 47 of 50 spots; at Desert Point, G-Land and Mundaka it chose a
neighbouring valid cell. The 12Z and 06Z waves-only checks earlier the same day gave the
same Hs agreement (mean 0.004-0.005 m, max 0.01 m). An independent re-check on the 06Z
run of 2026-10-15 (50 spots, lead 6 h, a fresh live acquisition) gave Hs 50 of 50 within
0.05 m (mean 0.005 m, max 0.01 m), Tp max 0.02 s, and against the `sfluxgrbf006` wind
mean 0.09 km/h, max 0.24 km/h, direction max 4.6°; the GFS-family run cost 142.9 weighted
calls and 341.0 kB.

Examples (Hs m / Tp s / wind km/h, from degrees; Open-Meteo as served, the direct
samples to 2 decimals and 0.1°):

| Spot | GFS family | NOAA direct | ECMWF family | ECMWF direct |
|---|---|---|---|---|
| Pipeline (`ncep_gfswave016`) | 1.62 / 12.65 / 20.0 from 76° | 1.61 / 12.64 / 20.00 from 76.5° | 2.04 / 13.10 / 17.1 from 75° | 2.03 / 13.08 / 17.18 from 74.9° |
| Mavericks (`ncep_gfswave016`) | 1.26 / 13.70 / 8.4 from 263° | 1.25 / 13.70 / 8.29 from 263.8° | 1.80 / 15.10 / 4.0 from 265° | 1.81 / 15.10 / 3.97 from 263.3° |
| Nazaré (`ncep_gfswave016`) | 1.72 / 14.10 / 13.7 from 14° | 1.72 / 14.09 / 13.80 from 13.3° | 1.62 / 13.15 / 14.5 from 19° | 1.62 / 13.16 / 14.50 from 19.0° |
| Jeffreys Bay (`ncep_gfswave025`) | 2.20 / 10.20 / 15.3 from 246° | 2.20 / 10.20 / 15.45 from 246.7° | 2.04 / 10.65 / 14.6 from 250° | 2.04 / 10.65 / 14.41 from 249.6° |
| Thurso East (`ncep_gfswave025`) | 2.84 / 12.75 / 34.7 from 236° | 2.84 / 12.74 / 34.77 from 235.9° | 2.20 / 11.40 / 41.8 from 188° | 2.21 / 11.42 / 41.68 from 188.3° |

The NOAA direct wind is the sflux sample (same grid). The two families differ from each
other by far more than either differs from its source (Pipeline Hs 1.62 vs 2.04 m,
Thurso East wind from 236° vs 188°): that disagreement is what the blend and the forecast
confidence use.

**Bytes per cycle, before and after.** Direct GRIB (measured 2026-10-11): NOAA 1.91 GB
per GFS cycle, ECMWF 847 MB per run, GEFS-Wave 201.6 MB. API-first (measured above): the
GFS family 345.6 kB, the ECMWF family 175.1 kB for 50 spots. GEFS-Wave trimmed to
HTSGW/PERPW: 69.4 MB per run (measured 2026-10-16, below; about 65 MB computed from record
sizes before). Map layers, standalone
(`pgrb2.0p50` atmosphere, 61 map hours, cycles 00Z and 12Z): measured 7.85-8.10 MB per map
hour with the partition layers and 2.69-2.72 MB without (2026-10-14 12Z, map-layers.md),
so about 484 MB per cycle (0.97 GB a day) with them and 165 MB (0.33 GB a day) without
(extrapolated from the measured hours).

### Measured on 2026-10-16: the whole recommended setup on a fresh database

One pass of every acquisition job with the recommended live configuration
(`FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf`, `GEFS_ENABLED=true` with the default
`GEFS_STAT_FIELDS`, map fields standalone with the partition layers, `TIDE_PROVIDERS`
default, `OBSERVATIONS_ENABLED=true` for NDBC) on a fresh scratch database after `init-db`
(50 spots), 00Z cycles of 2026-10-16, through this sandbox's proxy. The map-field download
was capped at the first 24 h (`MAP_FIELD_MAX_LEAD_HOURS=24`: 9 of the 61 default map hours)
to keep the bytes modest. Each job ran in its own process; bytes on the wire and HTTP
responses were counted independently of the app (an httpx transport tally of
`num_bytes_downloaded`) and agree with the runs' own `details.stats`. Table growth is
`pg_total_relation_size` (heap, TOAST and indexes) before and after each job.

| Job | HTTP responses | Bytes on the wire | Wall time | Rows | Table growth |
|---|---|---|---|---|---|
| Tides: NOAA CO-OPS (6 US stations) and EOT20 (50 model points, computed locally) | 16 (CO-OPS, with the station list and datums) | 140,355 B (2,173,270 B of JSON once decoded) | 20.0 s | 53,230 (5,673 CO-OPS, 47,557 EOT20) | `tide_predictions` 12.5 MB |
| Open-Meteo GFS family, run 00Z (16 days) | 11 for the run (3 data requests), 142.9 weighted calls | 329,421 B | 4.7 s for the run; 72 s for the job (2 TLS handshake time-outs to the API, retried) | 10,400 | `wave_forecasts` 5.37 MB (517 B per row) |
| Open-Meteo ECMWF family, run 00Z (15 days) | 7 for the run, 124.3 weighted calls | 172,195 B | 4.1 s for the run | 4,200 | 3.20 MB (763 B per row) |
| GEFS-Wave mean and spread, HTSGW and PERPW, 41 steps | 165 (83 `.idx`, 82 byte ranges) | 69,357,546 B (1.69 MB per step) | 11.8 s | 2,050 | `forecast_ensemble_stats` 0.57 MB |
| Map fields, standalone, partitions on, f000-f024 (9 map hours) | 65 (63 for the fields, 2 cycle checks) | 71,266,858 B: 7.82-7.98 MB per map hour | 14.0 s | 135 fields, 9 of 9 hours complete with partitions | `map_fields` 11.0 MB (10.4 MB of field data) |
| NDBC buoys (`5day2`), 11 stations, 48 h | 23 (with `activestations.xml`) | 120,816 B (859,171 B of files once decoded) | 3.9 s | 864 observations (+26 stations seeded) | 0.30 MB |
| Blend (2 independent families) | none | 0 | 7.3 s | 10,400 + 10,400 comparisons | 6.38 MB (614 B per row) + 7.90 MB (759 B per row) |
| Predictions, algorithm 2.1.0 (3 runs) | none | 0 | 26.5 s | 25,000 predictions, 4,050 archived | 85.1 MB (3,403 B per row) + 3.08 MB (760 B per row) |

Database: 12.4 MB after `init-db`, 148.8 MB after this pass (with 9 map hours). Redis: 1.90 MB
before, 2.43 MB after (15 keys: cached responses and budget counters). Peak memory per job:
98 MB (NDBC) to 627 MB (predictions); GEFS 418 MB, map fields 568 MB. Files written: none
by any job (`strace -f -e openat,creat,rename,unlink,mkdir` over every job: only the
driver's own report and joblib's semaphores in `/dev/shm`, removed again), so no GRIB or
temporary file remains on disk. The ECMWF IFS 00Z run appeared on Open-Meteo at 08:27:30 UTC
(8.5 h after initialisation; 7.0-7.2 h on 2026-10-14): an acquisition at 08:14 correctly
skipped the family instead of mixing runs, and the next one read it.

**Per cycle and per day** ("measured" is per run above; "computed" or "extrapolated" is
arithmetic on it with the default schedule):

| Stream | Per cycle | Cycles a day | Per day | Basis |
|---|---|---|---|---|
| Open-Meteo GFS family | 0.33 MB, 142.9 weighted calls | 4 | 1.3 MB, 571 weighted calls | measured; computed |
| Open-Meteo ECMWF family | 0.17 MB, 124.3 weighted calls | 2 (00Z, 12Z) | 0.34 MB, 249 weighted calls | measured; computed |
| GEFS-Wave (HTSGW, PERPW) | 69.4 MB | 4 (`GEFS_CYCLES` default) | 0.28 GB (0.14 GB with `GEFS_CYCLES=00,12`) | measured; computed |
| Map fields, 61 map hours with the partition layers | about 477-487 MB (61 x 7.82-7.98 MB) | 2 (`MAP_FIELD_CYCLES`) | about 0.96 GB | per map hour measured; per cycle extrapolated (the hours got slightly smaller with lead time; the later hours are assumed no larger than f000) |
| Tides (CO-OPS) | 0.14 MB | 1 | 0.14 MB | measured (first run, with the station list) |
| NDBC buoys | 0.12 MB | 24 | at most 2.9 MB | measured; computed |
| **All** | | | **about 1.25 GB a day** (1.1 GB with GEFS at 2 cycles; about 0.6 GB without the partition layers) | computed |

**Before and after.** The direct GRIB setup measured in the audit downloaded, per set of
cycles, 1,913.8 MB (NOAA GFS + GFS-Wave, with the map fields) + about 0.85-0.9 GB (ECMWF)
+ 201.6 MB (GEFS-Wave), about 3.0 GB, and about 10.2 GB a day (4 NOAA, 2 ECMWF, 4 GEFS
cycles). The same set now costs about 0.55 GB (0.5 MB for both spot-forecast families,
69.4 MB GEFS-Wave, about 483 MB map layers), about 82 % less, and about 1.25 GB a day,
about 88 % less (extrapolated as above). The spot forecasts alone went from about 2.8 GB
to 0.5 MB per set of cycles. The map layers are now three quarters of the bandwidth.

**Storage, computed from the per-row sizes above** (a fresh table, no dead rows, so a lower
bound). Algorithm 2.1.0 stores more per prediction than 2.0.0 (wind detail, contributions,
direction partitions, tide detail): 3,403 B per row instead of 2,596 B (2026-10-14), +31 %.
Six blends a day (one per new member run) of 10,400 steps write about 0.30 GB a day of blend
forecasts, comparisons and predictions; with the member runs (about 28 MB a day) the 10 days
of `RETENTION_FORECAST_DAYS` hold about 3.3 GB, plus about 0.15 GB of map fields (two runs
of about 75 MB, extrapolated from 1.23 MB per map hour) and 12.5 MB of tides. The archive
adds about 18.5 MB a day (760 B per row, 6 x 4,050 rows). The database therefore passes the
default `DATA_HEALTH_DATABASE_MAX_GB` alert (5, compared in GiB) after about 100 days and
reaches about 17 GB at the 730-day archive retention (computed); see docs/operations.md,
section 6.

## Flights

| Provider | Status | Notes |
|----------|--------|-------|
| **Duffel** (`FLIGHT_PROVIDER=duffel`) | Implemented: offer requests, offer revalidation (`GET /air/offers/{id}`), booking link. Mock-tested, **not exercised live here** | Self-serve signup. `duffel_test_…` tokens hit Duffel's sandbox ("Duffel Airways" test data); live tokens need an activated account. Duffel's pricing and search-to-book limits apply; check them before going live |
| **Amadeus Enterprise** (`FLIGHT_PROVIDER=amadeus`) | Implemented: OAuth2 client credentials, Flight Offers Search, Flight Offers Price (`/v1/shopping/flight-offers/pricing`) for revalidation. Mock-tested only | Amadeus Self-Service has been discontinued (see ARCHITECTURE.md), so this needs an Enterprise agreement. `AMADEUS_BASE_URL` is configurable |
| **Demo** (`FLIGHT_PROVIDER=demo`) | Default | Deterministic mock offers. `is_mock = true`, IDs prefixed `demo_`, a "Mock fares" badge in the UI and a "MOCK FARE" note in every alert |
| Skyscanner, Google Flights, Kiwi | Not integrated as data sources | No openly available API for this project. Only ordinary public search URLs are generated as "compare" links |

Mock offers are never presented as real: they carry `is_mock`, a separate provider code and
explicit UI and alert labelling. The app never fabricates fares for a live provider. If a
live provider fails or returns nothing, the opportunity becomes "surf only" with the reason
shown.

## Map tiles

| Provider | Setting | Notes |
|----------|---------|-------|
| OpenStreetMap standard tiles | default (`NEXT_PUBLIC_MAP_TILE_URL` empty) | No key. The [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/) applies: visible attribution, a valid Referer (the app's referrer policy sends its origin), light use only. A high-traffic deployment should use a commercial tile provider or host its own tiles |
| CARTO Voyager | `NEXT_PUBLIC_CARTO_API_KEY=…`, or the URL `https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png?key=YOUR_KEY` | By October 2026 CARTO serves "API KEY REQUIRED" placeholder tiles to key-less raster requests. Free keys (about 5M tiles/month non-commercial) come from carto.com/basemaps/apikey. The key is visible in the browser by design; restrict it to your domain |
| Any other XYZ raster provider | `NEXT_PUBLIC_MAP_TILE_URL` + `NEXT_PUBLIC_MAP_ATTRIBUTION` | The tile host is added to the CSP automatically (`frontend/lib/map-tiles.ts`) |

## Notifications

| Provider | Setting | Status |
|----------|---------|--------|
| Console (writes to `OUTBOX_DIR/outbox.jsonl` + log line) | `EMAIL_PROVIDER=console`, `SMS_PROVIDER=console` | Default; fully tested |
| SMTP (e.g. local Mailpit, Postmark SMTP, SES SMTP) | `EMAIL_PROVIDER=smtp` + `SMTP_*` | Implemented; unit-tested with a fake SMTP server and verified end to end against Mailpit in Docker Compose |
| Resend | `EMAIL_PROVIDER=resend` + `RESEND_API_KEY` | Implemented; mock-tested only |
| SendGrid | `EMAIL_PROVIDER=sendgrid` + `SENDGRID_API_KEY` | Implemented; mock-tested only |
| Twilio SMS | `SMS_PROVIDER=twilio` + `TWILIO_*` | Implemented (send + signature-verified inbound STOP/START webhook); mock-tested only |
