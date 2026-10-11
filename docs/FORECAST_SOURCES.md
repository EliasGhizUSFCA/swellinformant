# Forecast and travel data sources

Every source below is an official, documented API, used within its terms. Nothing in this
project scrapes websites, bypasses anti-bot systems or calls private endpoints.

## Forecasts

| Source (`FORECAST_PROVIDERS` value → source code, family) | Status in this repo | Data | Access |
|--------|---------------------|------|--------|
| **NOAA GFS-Wave + GFS, direct** (`noaa_gfs` → `noaa_gfs_direct`, `ncep_gfs`) | Implemented (`forecasts/noaa.py`), synthetic-GRIB tests. **Verified live** on 2026-10-11: run `noaa-gfs:2026101100`, all 50 spots, f000–f240 every 3 h, 4,050 records, 0 errors, 811 requests, 1.91 GB, about 4 min | Hs, Tp, peak direction, 3 swell partitions + wind sea (height/period/direction), 10 m wind, gust, MSL pressure. Native WW3 grids `global.0p16` / `gsouth.0p25`, `global.0p25` only north of 52.5°N | NOAA Open Data Dissemination bucket on AWS, GRIB2 by HTTP Range from the `.idx` offsets. No key. Details: [noaa-direct-ingestion.md](noaa-direct-ingestion.md) |
| **ECMWF IFS open data** (`ecmwf_ifs` → `ecmwf_ifs_open`, `ecmwf_ifs`) | Implemented (`forecasts/ecmwf.py`), synthetic-GRIB tests. **Verified live** on 2026-10-11: run `ecmwf-ifs:2026101012`, 65 steps to 240 h, 3,250 records, 0 errors, 438 requests, 847 MB, 112 s (two HTTP 503 SlowDown responses, retried) | ecWAM Hs (`swh`), Tp (`pp1d`), mean direction, Tm-1,0, Tm02, period-band heights, IFS 10 m wind, gust, MSL pressure. **No swell partitions** in open data | ECMWF open-data bucket (00Z/12Z cycles, 0.25°), JSON `.index` offsets, HTTP Range. CC BY 4.0: attribution "Copyright ECMWF" required, shown on the spot page |
| **NOAA GEFS-Wave ensemble statistics** (job `forecasts.acquire_ensemble`, `GEFS_ENABLED=true` → `noaa_gefs_wave`, `ncep_gefs`) | Implemented (`forecasts/ensemble.py`), synthetic-GRIB tests. **Verified live** on 2026-10-11: run `gefs-wave:2026101018`, 50 spots, 41 steps (6-hourly to 240 h), 2,050 rows, 0 errors, 201 MB, 29 s; partition order check passed on 503,541 ocean cells | Ensemble mean and spread (31 members) of Hs, Tp, wind and swell partition 1. Stored in `forecast_ensemble_stats`, used only for uncertainty ranges, never as a forecast | Same NOAA bucket, no key |
| **Open-Meteo**, serving NOAA GFS + GFS-Wave (`open_meteo` → `open_meteo_noaa`, `ncep_gfs`) | Implemented (`forecasts/open_meteo.py`), mock-tested. Verified live on 2026-10-09 (19,200 records, 0 errors) | Hourly, up to 16 days: combined sea, primary/secondary swell, wind sea, wind, gusts, pressure, sea level | Free for non-commercial use; `OPEN_METEO_API_KEY` for the commercial hosts |
| **Windy Point Forecast API v2** (`windy` → `windy_gfswave`, `ncep_gfs`) | Implemented (`forecasts/windy.py`), mock-tested only | `gfsWave` waves/swells/wind waves, `gfs` wind | Paid key, `WINDY_API_KEY`. Trial keys return randomised data and must never drive real alerts |
| **Demo provider** (`demo`) | Implemented, default | Synthetic but physically plausible swells, winds and tides | None. Labelled DEMO in the UI, API and every alert |
| Copernicus Marine (MFWAM) | Not implemented (family `mfwam` reserved) | 10-day wave forecast, a third independent family | Free account; the `ForecastProvider` interface is ready for it |
| Surfline, Windy website | **Not integrated** | — | No authorised access; scraping would violate their terms |

### Families, the blend and what "our prediction" is built from

* Every source has a **dependency family** (`forecasts/families.py`). NOAA direct,
  Open-Meteo's GFS-Wave and Windy's `gfsWave` are the same model (`ncep_gfs`), so their
  agreement is not evidence; GEFS (`ncep_gefs`) is related to it; ECMWF (`ecmwf_ifs`) is
  independent.
* With two or more independent families, job A2 (`forecasts.blend`) builds a **blend**
  run (`forecasts/blend.py`): one member per family, weighted by the best linear unbiased
  combination for correlated errors (prior error correlation 0.6 related / 0.3
  independent; per-member error from verification when it exists, otherwise a lead-time
  prior). Hs is combined geometrically, Tp only over peak-period members, directions as
  unit vectors, swell partitions come from the member that has them (NOAA) scaled to the
  blended Hs. Nothing is averaged equally.
* Each hour stores a `forecast_comparisons` row: Hs log ratio, Tp, direction and wind
  differences, and flags (`hs_disagree` above a ratio of 1.4, `period_disagree` above 3 s,
  `direction_disagree` above 30°, `wind_disagree` above 15 km/h). Flagged hours widen
  the forecast range; the spot page shows every model's raw values next to our
  prediction.
* **First live comparison (2026-10-11, GFS 00Z vs ECMWF 12Z the day before):** 1,334 of
  4,050 spot-hours flagged (705 Hs, 604 period, 564 direction, 181 wind). In the first
  48 h, Hs disagreement was concentrated at sheltered Southern California spots: at
  Rincon ECMWF had 2.1–2.4 m of 7–8 s wind sea under 30–39 km/h westerlies where GFS had
  0.5–0.9 m and weaker wind; Trestles and Mundaka were similar. These are real model
  differences at different grid cells, not ingestion errors; they are the reason the blend
  widens the range rather than picking one model.
* Predictions are computed for every run; the blend (or the single primary source when no
  blend exists) is authoritative for detection, matching and alerts. Only authoritative
  runs are archived in `prediction_archive`.

### How ingestion works (job A)

* **One centralised fetch** per upstream model run for all active spots, independent of
  how many users exist. The run key (`noaa-gfs:2026101100`, `ecmwf-ifs:2026101012`,
  `ncep_gfswave025:…|gfs_seamless:…`) makes ingestion idempotent.
* Each spot is forecast at `forecast_latitude/longitude`, a point just offshore. The GRIB
  ingesters sample the **nearest valid sea cell** (land-masked cells are never used, and
  nothing is interpolated across land) within `GRIB_MAX_CELL_DISTANCE_KM`, falling back to
  the break location; wind comes from the nearest sea cell to the break. The chosen
  cells are stored in `wave_forecasts.extra` of each spot's first record.
* **Run discovery.** NOAA: the newest cycle whose last needed step is published for every
  file (`NOAA_AVAILABILITY_LAG_HOURS`, default 5 h). ECMWF: the newest 00Z/12Z cycle with
  its last needed step published (`ECMWF_AVAILABILITY_LAG_HOURS`, 8 h). GEFS: same as NOAA
  with `GEFS_AVAILABILITY_LAG_HOURS` (7.5 h).
* **Failure isolation.** Transient errors (timeouts, 429, 5xx, ECMWF 503 SlowDown) retry
  with exponential backoff; ECMWF requests are paced and capped at 4 in flight. A missing
  step is a gap with a warning and marks the spots it affects `incomplete`, a spot without
  a valid cell is an error for that spot (either makes the run `PARTIAL`), and a run where
  nothing could be read fails without replacing the last good data. A `FAILED` run is
  fetched again on the next schedule (the acquisition jobs record provider failures as
  outcomes instead of raising them, so Celery's `autoretry_for` does not fire for them).
* **Staleness.** A spot's latest run older than `FORECAST_STALE_HOURS` (18) is flagged:
  the UI warns, confidence is multiplied by 0.8, and alerts are not generated from it.
* **Retention.** Forecast runs older than `RETENTION_FORECAST_DAYS` are deleted nightly
  (the two newest successful runs per source are kept). The prediction archive keeps the
  authoritative predictions for `RETENTION_ARCHIVE_DAYS` (730).

### Bandwidth and timing (measured live, 2026-10-11, this sandbox's proxy)

| Job | Per run | Time |
|-----|---------|------|
| NOAA GFS-Wave + GFS, 81 steps | 1.91 GB, 811 requests (~24 MB per step with all three wave grids) | ~4 min with 6 workers |
| ECMWF IFS, 65 steps | 847 MB, 438 requests (~13 MB per step) | 112 s |
| GEFS-Wave statistics, 41 steps | 201 MB, 410 requests (~5 MB per step) | 29 s |
| Blend + predictions for 3 runs (11,350 predictions) + archive | — | ~12 s |

At four GFS cycles and two ECMWF cycles a day this is about 9.5 GB per day of egress from
the NOAA and ECMWF buckets (free to the user). Reduce it with `NOAA_STEP_HOURS=6` or a
shorter `NOAA_MAX_LEAD_HOURS`/`ECMWF_MAX_LEAD_HOURS`.

### Switching to live forecasts

```env
FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs  # first = primary; two independent families enable the blend
GEFS_ENABLED=true                      # ensemble spread for the uncertainty ranges
FORECAST_BLEND_ENABLED=true
# optional: OPEN_METEO_API_KEY / WINDY_API_KEY if "open_meteo" / "windy" are listed
```

`demo` cannot be combined with a live provider (the settings validator rejects it), so
synthetic and real data never mix. To try it once from the command line:

```bash
cd backend
GEFS_ENABLED=true python -m app.cli engine ensemble   # GEFS-Wave mean/spread
FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs python -m app.cli pipeline
```

Open-Meteo's free API is for **non-commercial** use with fair-use limits; a deployment
that charges users needs a commercial key.

### Limitations

* All sources here are **offshore** models (WW3 at 1/6°–1/4°, ecWAM at 0.25°). Breaking
  heights come from our nearshore model (`docs/SURF_SCORING.md`), not from the providers.
* ECMWF open data has no swell partitions, so an ECMWF-only run uses the bulk sea state
  for the breaking estimate; in the blend the partitions come from GFS-Wave.
* GFS 0.25° winds under-resolve sea breezes and terrain effects near the coast.
* GFS v17 (expected late 2026) will rename NOAA files: the bucket and path templates are
  settings (`NOAA_GFS_BASE_URL`, `NOAA_GFS_WAVE_PATH_TEMPLATE`,
  `NOAA_GFS_ATMOS_PATH_TEMPLATE`), and verification statistics should restart after the
  switch.
* No tide model is ingested from NOAA or ECMWF (`sea_level_m` stays empty for them).

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
