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
  for GFS-Wave on 2026-10-14 (the other 3 a neighbouring valid cell). The cells used are
  stored with each run.
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

Live checks on scratch databases with the 50 seeded spots, 16 days (15 for ECMWF). The
values are what `forecast_runs.details.stats` and the parity script measured; nothing
here is extrapolated unless it says so.

| Check | Result |
|---|---|
| Open-Meteo Marine, GFS family, all 50 spots, 16 days (06Z and 12Z cycles) | 2 data requests (`ncep_gfswave016` for 32 spots, `ncep_gfswave025` for 18), **85.7 weighted calls**, 236-237 kB on the wire including the metadata reads, 0 errors |
| Same-cycle parity with a direct NOAA NODD sample (12Z, f006; `gfswave` `global.0p16`/`global.0p25` HTSGW and PERPW read by byte range, decoded in memory, sampled at the cell Open-Meteo reports) | **Hs: 50 of 50 spots within 0.05 m**; mean absolute difference 0.004 m, maximum 0.01 m (Open-Meteo rounds to 0.01 m). Tp: mean 0.009 s, maximum 0.02 s. The 06Z cycle gave the same (Hs mean 0.005 m, maximum 0.01 m). The NOAA sample of that single step cost 1.73 MB |
| Grid cell | Open-Meteo's cell was our nearest valid sea cell at 47 of 50 spots; at Desert Point, G-Land and Mundaka it chose a neighbouring valid cell |
| GFS family with wind (`FORECAST_PROVIDERS=open_meteo`) | Not completed: the Forecast API (`api.open-meteo.com`) answered "Daily API request limit exceeded. Please try again tomorrow." for this sandbox's shared egress address (the app had used 143 weighted calls), then stopped completing TLS handshakes. The run ended FAILED, not retried, with its cost recorded: 11 HTTP responses (10 x 200, 1 x 429), 237,152 B, 142.9 weighted calls |
| Files written by the acquisition (`strace -f -e openat,creat,rename,unlink,mkdir`) | None (the only file opened for writing was the test driver's own report) |

Examples (12Z, f006; Hs m / Tp s, Open-Meteo vs NOAA direct at the same cell):

| Spot | Wave model | Open-Meteo | NOAA direct |
|---|---|---|---|
| Pipeline | `ncep_gfswave016` | 1.62 / 12.65 | 1.61 / 12.64 |
| Mavericks | `ncep_gfswave016` | 1.26 / 13.70 | 1.25 / 13.70 |
| Nazaré | `ncep_gfswave016` | 1.72 / 14.10 | 1.72 / 14.09 |
| Jeffreys Bay | `ncep_gfswave025` | 2.20 / 10.20 | 2.20 / 10.20 |
| Thurso East | `ncep_gfswave025` | 2.84 / 12.75 | 2.84 / 12.74 |
| Cloudbreak | `ncep_gfswave025` | 1.12 / 15.85 | 1.12 / 15.85 |

**Bytes per cycle, before and after.** Direct GRIB (measured 2026-10-11): NOAA 1.91 GB
per GFS cycle, ECMWF 847 MB per run, GEFS-Wave 201.6 MB. API-first: the GFS family's waves
for 50 spots cost 0.24 MB (measured above); with the wind request and the ECMWF family the
audit's estimate is about 0.5 MB (GFS) and 0.24 MB (ECMWF) per cycle. GEFS-Wave trimmed
to HTSGW/PERPW: about 65 MB per run (computed from record sizes). Map layers, standalone:
about 0.16 GB per cycle, 0.48 GB with the partition layers (computed in the audit).

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
