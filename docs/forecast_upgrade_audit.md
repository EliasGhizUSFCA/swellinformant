# Forecast upgrade audit (Upgrade 3)

This is the audit the owner asked for in the 2026-10-13 request (map, weather data, wind and wave
visualization, scoring, forecast charts, confidence, text). For each area it gives the root
causes, the affected files, the planned changes and the tests required. Nothing in the code has
been changed yet. This file is the only one written by the audit.

Contents:
1. Scope and method
2. Stack inventory
3. What works and must be preserved
4. Findings by area
5. Planned changes (spec phases 2-12) and target data architecture
6. Test requirements (spec section 27)
7. Risks, owner inputs and open questions

---

## 1. Scope and method

| Item | Value |
|---|---|
| Audit date | 2026-10-13 (owner request). Live requests and measurements were taken 2026-10-13/14 UTC. |
| Base commit | `2bd2b50` (origin/main, "Merge pull request #5") |
| Working tree | Unchanged apart from this file. Every audit ran `git status --porcelain` and found it clean. |
| Data used | `swell_live2`, a copy of one live acquisition of cycle 2026-10-11 18Z, read with read-only transactions: run 1 GEFS-Wave, run 2 NOAA GFS direct, run 3 ECMWF IFS open data, run 4 blend. Writable scratch clones (`swell_audit_*`) were used where a test had to write. `swell_ndbc_live` was used for observation volumes. |
| Areas | 10 separate audits: map navigation, wind visualization, wave visualization, data acquisition, external forecast APIs, observations and tides, offshore wind and scoring, charts and spot API, confidence and text, platform and tests. |
| Scratch evidence | Scripts and outputs are in the session scratchpad under `upgrade3/audit/<area>/`. They are not part of the repository. |

**What was run and what was only read.**

| Area | Run or measured | Read only |
|---|---|---|
| Map navigation | Playwright on production builds (desktop 1440x900 and 1920x1080, phone 390x844 with CDP touch drags and pinches), a prototype in a scratch copy, 71 map vitest tests | Leaflet internals |
| Wind visualization | Independent ecCodes decode of the GFS GRIB vs the stored fields, comparison with NDBC buoys, real frontend modules bundled with esbuild, projection and speed harnesses | MDN browser-compat data (fetched, not tested in Firefox or Safari) |
| Wave visualization | Partition decode and encode with the app's own encoder, `.idx` record sizes, land-leak rasterisation, box-mean mixing statistics | Proposed renderer budgets (targets, not measured) |
| Data acquisition | `forecast_runs` bookkeeping, `.idx`/`.index` sizes, strace of the GRIB decode, Redis INFO, Open-Meteo live requests compared with direct GRIB sampling | Celery retry paths (DA-9) |
| External APIs | 25 provider pages fetched; 87 quotes checked word for word by a script. Live Open-Meteo marine, forecast and metadata requests, NOMADS listing and two grib-filter subsets. | Prices hidden behind JavaScript; Windy terms (read, no key) |
| Observations and tides | CO-OPS station list and predictions, Open-Meteo sea level vs CO-OPS, `tide_stages()` on real predictions, NDBC file sizes, METAR station list | National agency licences (BOM, SHOM, IH Portugal) from search summaries only |
| Offshore wind and scoring | Speed and direction sweeps of `score_wind`/`evaluate`, rescoring of the real blend run in memory, a prototype wind function | Practitioner sources (low tier) |
| Charts and spot API | Recharts rendered in jsdom with the real J-Bay payload, payload sizes, service timings, palette contrast | Mobile behaviour (no mobile project exists) |
| Confidence and text | API probes and server-side rendering of the real components with real data, staleness windows, word counts | Coverage of the 80 % range (taken from the project's own docs) |
| Platform and tests | Unit tests, ruff, mypy, vitest, tsc, eslint, an Alembic round trip on a scratch DB, a retry probe, API latencies | Integration (233) and Playwright (62) suites were collected and listed, not run |

**Reference screenshot.** The owner refers to a reference screenshot of a forecast page. It was
not attached to the request. The chart plan in this document follows the written description in
spec sections 11-20 only, and does not copy any third-party design.

**Lead verification.** Before relying on the audits I re-checked the most consequential "high"
findings myself.

| Finding | What I did | Result |
|---|---|---|
| MAPNAV-1, -2, -3 | Read `frontend/components/map/forecast-map.tsx:477-499` | Confirmed: `center [15,10]`, `zoom 2`, `minZoom 2`, `worldCopyJump`, no `maxBounds`; markers use `[s.latitude, s.longitude]` |
| SW-1, SW-2, SW-4 | Ran `scoring.score_wind` and `wind_relation` with offshore 90 deg | Confirmed. Offshore: Q = 1.0 at 0-35 km/h, 0.875 at 40, 0.4 at 60, 80 and 150 km/h. Cross-shore: 0.5 at 20-35 km/h. Onshore: 0.0 from 20 km/h. 5.0 km/h is `glassy` and 5.1 km/h is `onshore`. |
| SW-3 (= CH X-1) | Read `backend/app/services/surf_quality/scoring.py:192-199` | Confirmed: direction and period come from `primary_swell_*` |
| CH-1 | Read `forecast-charts.tsx:95-98`; ran the same filter over the 50 spot zones on a 3-hourly grid from 2026-10-14T00Z | Confirmed: 42 of 50 spots get zero day ticks. The 8 with ticks are in America/Mexico_City, America/Santiago, Pacific/Fiji (2), America/El_Salvador and America/Costa_Rica (3). |
| DA-1, PLAT-1, PLAT-10 | Read-only SELECT on `swell_live2.forecast_runs` | Confirmed. NOAA run: "downloaded 1913.8 MB in 811 requests for 81 forecast hours". GEFS: 201,588,201 B. NOAA and ECMWF share `started_at` 03:41:01.651303. ECMWF stores no byte count. |
| OBS-TIDE-1 | `count(sea_level_m)` per source | Confirmed: 0 of 4,050 / 3,250 / 4,050 rows |
| CT-1 | Read `forecast_confidence.py:601-613` and `confidence.tsx:110-129` | Confirmed: one combined reasons list, all rendered under "Forecast confidence" |
| PLAT-2, DA-6 | Read `ingestion.py:219-262`, `tasks.py:95-111`, `core/redis.py:81` | Confirmed: `ProviderError` is caught and returned; `QuotaExceeded(Exception)` is not a `ProviderError` |
| WAVEVIZ-1 | Read `forecast-map.tsx:57-62` | Confirmed: every non-wind layer goes through `waveFlowGrids` into the one particle canvas |
| WINDVIZ-1 | Read `forecast-map.tsx:131-138` | Confirmed: `colorType: "float16"` requested with no 8-bit path. The browser-support data was not re-checked. |
| DA-5, API-04, API-05 | grep `open_meteo.py` | Confirmed: no `cell_selection`, no tertiary swell; values copied as given (`:245-256`) |
| CT-13 | grep | Confirmed: "16-day" (`frontend/app/page.tsx:101`), "every hour" (`:18`), "Komar–Gaughan" (`opportunity_views.py:172`) |

I did not re-run the Playwright, browser, live-provider or quote measurements. Those numbers are
cited from the audits and their scratch evidence.

**Corrections made while consolidating.**

1. Two audits both used the IDs `WV-1`..`WV-13` for different findings. Here wind-visualization
   findings are `WINDVIZ-n` and wave-visualization findings are `WAVEVIZ-n`.
2. ECMWF bytes per run are not stored anywhere (DA-10). The figure of about 0.85-0.9 GB per run
   is computed from `.index` offsets (about 13.7 MB per step x 65 steps); `docs/FORECAST_SOURCES.md`
   says 847 MB. It is not a measurement of the run itself.
3. The data-acquisition audit lists `mwp` both as unused and inside the trimmed range it keeps
   (`swh..mwp`, 3.69 MB per step). Keeping `mwp`, a trimmed ECMWF run is about
   (3.69 + 3.67) MB x 65 = 0.48 GB (computed). The audit said 0.42 GB.
4. The daily download total (about 10.2 GB/day) is computed from measured runs x beat cadence.
   It was not observed over a day. The docs say "about 10 GB" and "about 9.5 GB".
5. `GET /api/spots/{slug}` latency was measured at 412 ms, 414 ms and 501 ms median by three
   audits on different runs. Section 4 cites the range.
6. The wave audit says swell partitions cost 0 MB of extra download. That holds only while NOAA
   direct ingestion runs for a spot north of 52.5N. Under the API-first plan the partition
   records cost 5.13-5.28 MB per map hour (measured record sizes).
7. Redis `used_memory` was 917 KB in one audit and 1.04 MB in another, at different times.
8. Open-Meteo bytes for a full 50-spot run are extrapolated from 2- and 32-location responses.
   They were not measured.
9. Two audits disagree on the blend's wind-direction average: weighted unit vectors (fine, says
   the scoring audit) vs not speed-weighted (a calm member pulls as hard as a strong one, says the
   tides audit). This is left as an open question (section 7).
10. The data-acquisition audit proposes a Redis response cache. The platform audit warns against
    large caches in the broker Redis, which runs with `noeviction`. Resolution in section 5: a
    small, TTL-bound cache with an application-side size cap.

---

## 2. Stack inventory

| Component | Version | Main files |
|---|---|---|
| Python | 3.13.16 (CI 3.13, `python:3.13-slim`) | `backend/Dockerfile` |
| FastAPI / Starlette / uvicorn | 0.143.0 / 1.7.0 / 0.54.0 | `backend/app/main.py`, `backend/app/api/*` |
| Pydantic / pydantic-settings | 2.14.0 / 2.15.0 | `backend/app/schemas/*`, `backend/app/core/config.py` |
| SQLAlchemy / Alembic / psycopg | 2.1.4 / 1.20.0 / 3.3.6; head `0005_research_retrieval` (5 linear revisions) | `backend/app/models/*`, `backend/alembic/versions/` |
| PostgreSQL | 16 (local 16.15) | `docker-compose.yml` |
| Celery / kombu / redis-py | 5.6.3 / 5.6.2 / 8.1.0 (beat + worker, one queue, concurrency 2) | `backend/app/workers/*` |
| Redis | 7 (compose `redis:7-alpine`, no persistence, no maxmemory, `noeviction`) | `docker-compose.yml`, `backend/app/core/redis.py` |
| httpx | 0.28.1 | `backend/app/services/http.py`, `forecasts/grib.py` |
| ecCodes / numpy / pandas / pyarrow | 2.49.0 / 2.5.3 / 3.0.6 / 26.0.0 | `forecasts/grib.py`, `forecasts/fields.py`, `surf_quality/predict.py`, `observations/aodn.py` |
| Next.js / React / TypeScript | 16.4.0 / 19.3.0 / 5.9.3 | `frontend/app/*` |
| TanStack Query | 5.104.1 (staleTime 30 s, no refetch on focus) | `frontend/components/providers.tsx`, `frontend/hooks/queries.ts` |
| Mapping | Leaflet 1.9.4 + react-leaflet 5.0.0, EPSG:3857, SVG markers | `frontend/components/map/forecast-map.tsx`, `surf-map.tsx` |
| Map layers | In-repo Canvas 2D layers (colour field, particles, vector basemap); Natural Earth 1:50m (world-atlas 2.0.2, topojson-client 3.1.0); optional OSM/CARTO tiles | `components/map/canvas-layer.ts`, `particles.ts`, `basemap-draw.ts`, `lib/map-render.ts` |
| Charts | Recharts 3.10.1 (no `syncId`) | `frontend/components/charts/*` |
| Styling | Tailwind CSS 4.3.3 | `frontend/app/globals.css` |
| Tests | pytest 9.1.1, ruff 0.16.10, mypy 2.4.0, vitest 3.2.7, Playwright 1.64.0 (Desktop Chrome only), ESLint 9.39.5 | `backend/tests`, `frontend/tests`, `frontend/e2e` |
| CI | GitHub Actions (SHA-pinned); jobs backend, frontend, e2e (e2e not on pull requests) | `.github/workflows/ci.yml` |

**Data providers in the code today.**

| Code | Transport | Enabled by default | Used in `swell_live2` |
|---|---|---|---|
| `demo` | Synthetic (hourly, includes an M2+S2 tide) | Yes (`FORECAST_PROVIDERS=demo`) | No |
| `noaa_gfs` → `noaa_gfs_direct` | NODD S3 byte ranges of GFS + GFS-Wave GRIB2, decoded in memory | No (README recommends it) | Yes |
| `ecmwf_ifs` → `ecmwf_ifs_open` | ECMWF open data S3 byte ranges, 00/12Z | No (README recommends it) | Yes |
| `noaa_gefs_wave` | NODD GEFS-Wave mean/spread, separate job (`GEFS_ENABLED`) | No | Yes |
| `open_meteo` → `open_meteo_noaa` | Open-Meteo Marine + Forecast JSON | No | No |
| `windy` → `windy_gfswave` | Windy Point Forecast v2 (paid key) | No | No |
| `noaa_gfs_fields` | Map fields only (standalone mode) | No | No |
| Observations | AODN, Met Office, NDBC (`OBSERVATIONS_ENABLED`) | No | No (0 rows in `swell_live2`) |

---

## 3. What works and must be preserved

These parts were checked and work. The plan keeps them.

**Platform and product (spec section 1).**
- Authentication, sessions and CSRF, email verification and reset (with inline sending when the
  broker is down), saved searches, flight discovery with the Postgres flight cache and quotas,
  matching, and notifications claimed with `FOR UPDATE SKIP LOCKED`. These are covered by the
  integration suites and the `full-flow`/`scenario` e2e journeys.
- The `run_job` pattern (`workers/tasks.py:35-81`): a job log per run, a Redis lock released
  only by its token, stages chained on success, and the beat schedule as a safety net.
- Idempotent ingestion keyed by `(source, run_key)`, with detection of stuck runs
  (`ingestion.py:224-238`).
- Settings validation: production refuses dev secrets and capture-only email, and demo cannot be
  mixed with live providers (`core/config.py:247-272`).
- Linear Alembic history; `alembic upgrade head`, `check`, `downgrade base` and re-upgrade all
  exit 0 on a scratch DB.
- Baselines are green: 644 backend unit tests (also with Postgres and Redis unreachable), ruff
  check and format (235 files), mypy (159 files), 160 vitest tests, `tsc --noEmit`, eslint.
  233 integration tests and 62 Playwright tests are collected and listed.

**Data path.**
- Byte-range GRIB access by `.idx`/`.index`, records matched by `VAR:LEVEL`, decoded in memory.
  strace showed 906 `openat` calls and none for writing. This stays the right tool for gridded
  map layers.
- The GRIB-to-map-field pipeline is exact. An independent ecCodes decode agrees within the
  0.25 m/s quantisation half-step at 36,771 nodes. Directions agree with 14 NDBC buoys: median
  absolute difference 9.5 deg over 182 pairs, none above 90 deg.
- Map fields: 0.5 deg area-weighted means, unit-vector direction means, zlib uint8 at about
  55-100 kB per field, run-keyed URLs, `Cache-Control: public, max-age=3600`, and 2 runs retained
  with pruning after every store.
- Land-aware cell selection in NOAA direct: waves from the nearest valid wave cell, wind from the
  nearest sea cell (`noaa.py:50-55`). Native WW3 grid per latitude (`noaa.py:151-163`).
- Model families and the blend's one-member-per-family rule (`families.py`).
- Bandwidth notes in `run.details` (NOAA, GEFS). They made this audit measurable.

**Physics and scoring.**
- One FROM convention everywhere. `wind_from_uv` matches `u = -V sin(theta)`, `v = -V cos(theta)`
  exactly. Researched `offshore_wind_direction` values use wave-face semantics for points and
  wrapping reefs.
- The direction term `(1 + cos psi)/2` is already the normalised offshore component `V.n/|V|`.
- Scoring is deterministic: all 4,050 blend scores recompute with 0 mismatches.
  `ALGORITHM_VERSION` is stored per row, and live runs are rescored after an upgrade.
- The breaking model combines partitions by energy flux and stores them per prediction. Gates
  stop flat or blown-out surf from scoring well.
- Screen projection for particles: `(u, -v)` in Web Mercator is exact (conformal; 0.000 deg error
  measured). Do not add a `cos(lat)` factor to one component.

**Frontend.**
- Leaflet with the custom `CanvasLayer` (redraws coalesced through `requestAnimationFrame`,
  CSS-scaled frames during zoom). There is no reason to change the map library.
- Antimeridian handling in the basemap, fields and particles (`map-geo.ts:57-126`,
  `map-render.ts:39-51`).
- Field download and caching: an LRU of 24 fields with a 64 MB budget, keyed by run, with
  in-flight requests shared and neighbour prefetch only around the settled step (39 downloads for
  load + 10 steps, no duplicates). A 250 ms timeline debounce.
- Spot page: the whole horizon in one request, so selecting an hour never refetches. All labels
  use the spot's IANA zone through `Intl`.
- Raw offshore data, our breaking prediction and the quality rating are kept and labelled apart.
- Forecast, local and research confidence are separate columns and API fields. "Not assessed
  yet" is used instead of a default, and no confidence is ever shown as a percentage.
- The rating headline already matches the spec format ("Excellent Surf — 9.2/10" plus a one-line
  reason built only from API factors).
- Reduced-motion and page-visibility handling; the accessible `Disclosure` component.

**Observations.**
- The AODN, Met Office and NDBC adapters with verified conventions, QC precedence and the
  station catalog (26 stations, 18 spots paired, no invented pairings).
- The tide guards: no tide is ever invented, the stage is None below a 0.15 m range, and missing
  components are renormalised away.

---

## 4. Findings by area

Severity: high = breaks a spec objective or shows wrong information to users; medium = a real
defect with limited reach; low = hygiene. "Verified" says how the audit established the finding:
**measured**, **ran** (executed code or a request), or **read** (code reading only). "Lead" marks
findings I re-checked (section 1).

### 4.1 Map navigation, bounds, date line, performance

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| MAPNAV-1 | high | No latitude bounds. `MapContainer` sets neither `maxBounds` nor `maxBoundsViscosity`. EPSG:3857 stops at ±85.05 deg and the wave data at 84N/78.5S, so a drag can leave the screen blank. At 1440x900 one 600 px drag north left 805 of 807 rows blank, and three drags left all 807. Phone touch drags: 81.5 % blank going north, 69.7 % going south. | `forecast-map.tsx:477-485` | measured; lead |
| MAPNAV-2 | high | A fixed `minZoom` of 2 makes the world 1,024 px wide whatever the screen. The view spans 1.41 world copies at 1440 px and 1.88 at 1920 px, with 25 blank rows at 1920x1080 before any interaction. The coasts of 20 spots appear twice but carry markers once. | `forecast-map.tsx:480` | measured; lead |
| MAPNAV-3 | high | Markers sit at their literal longitude, and Leaflet draws vector layers in world copy 0 only. `worldCopyJump` does not wrap the inertial pan. Centred on 179E at zoom 3, 20 of 38 spots in view had a marker. After one inertial pan, 32 of 50. | `forecast-map.tsx:492-523` (`:499`); `leaflet-src.js:13866, 13880-13916` | measured; lead |
| MAPNAV-4 | med | `flyTo` targets the literal longitude, so Cloudbreak to Pipeline flies 335.2 deg instead of 24.8 deg. | `forecast-map.tsx:218-232`; `surf-map.tsx:31` | measured |
| MAPNAV-5 | high | The basemap redraw calls `ctx.closePath()` after every ring: 2,558 calls take 214 ms of a 234 ms redraw at zoom 2, causing a 270 ms long task on every pan. Without them: 3.8 ms, one 64 ms task, and 0 of 1,963,728 pixels differ. | `basemap-draw.ts:71` | measured; lead read line 71 |
| MAPNAV-6 | med | All 50 markers get new props on every page render. Five timeline steps cause 1,000 calls each of `setLatLng`, `setStyle`, `on` and `off`; one zoom click causes 50 of each. | `forecast-map.tsx:492-523`; `app/map/page.tsx:361-384` | measured |
| MAPNAV-7 | med | On a phone the selected spot is centred under the bottom sheet (marker at y=477, sheet top at y=321). | `forecast-map.tsx:218-224`; `page.tsx:345-356` | ran |
| MAPNAV-8 | med | The map flies back to the selected spot when a filter hides and re-shows it, because the FlyTo effect depends on the filtered spot object. | `forecast-map.tsx:218-224, 428`; `page.tsx:142, 363` | ran |
| MAPNAV-9 | med | Constraint for the fix: keeping `worldCopyJump` with nearest-copy markers puts markers 7,483 px off for the whole drag. `worldCopyJump` must go. | `leaflet-src.js:13866` | measured |
| MAPNAV-10 | low | The spot-page mini map has the same unbounded options. | `surf-map.tsx:31, 79-86` | read; lead |
| MAPNAV-11 | low | Markers pop in late during long inertial pans (SVG renderer padding 0.1). | Leaflet renderer | measured |
| MAPNAV-12 | low | `land-50m` duplicates geometry already in `countries-50m`: 545,689 B raw, 174,015 B gzip. Total JS on `/map` is 2,861,666 B. | `basemap-data.ts:19` | measured |
| MAPNAV-13 | low | At a one-world minimum zoom, the desktop "Best spots" list covers the Cloud 9 marker at 1440x900. No centre longitude avoids every overlay. | `page.tsx:308-314` | measured |
| MAPNAV-14 | low | The e2e helper `screenPoint` assumes a 1,024 px world (zoom 2). Any `minZoom` change breaks field-position tests. | `e2e/map.spec.ts:82-88` | read |

Also noted: the map timeline offers valid times from 2.5 days before now without marking them as
past (see WINDVIZ-3 and CT-4).

### 4.2 Data acquisition and storage

**Measured download volumes per run and per source** (configuration recommended by the README:
`noaa_gfs,ecmwf_ifs` + `GEFS_ENABLED`).

| Source | Per run | Requests | Runs per day | Per day | Basis |
|---|---|---|---|---|---|
| NOAA GFS + GFS-Wave direct (81 steps, f000-f240) | 1,913.8 MB (23.6 MB per step) | 811 | 4 | about 7.66 GB | `run.details` of run 2 (lead re-read) |
| per step: GFS `pgrb2.0p25` UGRD/VGRD 10 m + GUST + PRMSL | 3.56 MB (LAND 32 kB once) | | | | `.idx` offsets, 2026-10-13 12Z |
| per step: `global.0p16`, 15 wave records (32 spots) | 8.05 MB | | | | same |
| per step: `gsouth.0p25` (16 spots) | 3.56 MB | | | | same |
| per step: `global.0p25` (2 spots north of 52.5N) | 8.47 MB, of which the 3 map records are 1.82 MB | | | | same |
| ECMWF IFS + ecWAM open data (65 steps) | about 0.85-0.9 GB (wave block 10.06 MB + atmosphere 3.67 MB per step) | 438 (137 of 575 attempts got 503 SlowDown) | 2 | about 1.78 GB | `.index` offsets; not stored in `run.details` |
| GEFS-Wave mean/spread (41 steps) | 201.6 MB (4.9 MB per step) | 409-410 | 4 | about 0.81 GB | `run.details` of run 1 (lead re-read) |
| Map fields, `with_noaa` mode | 0 MB extra (read from the spot blobs); 39 MB stored | | | | run 2 |
| Map fields, standalone mode | about 4.75 MB per map hour, about 380 MB per cycle | | | | README (documented, not re-measured) |
| NDBC observations (if enabled) | about 5.6 MB per hourly run (11 stations) | | 24 | about 134 MB (estimate) | `docs/observations.md` |
| **Total** | | | | **about 10.2 GB/day (computed)** | |

Nothing is written to disk: GRIB is decoded in memory. The problem the owner describes is
bandwidth and run time, not stored GRIB files.

**Database and Redis sizes** (`swell_live2`, one run of each source).

| Object | Size | Rows | Notes |
|---|---|---|---|
| Whole database | 95 MB | | |
| `map_fields` | 39 MB | 486 | about 80 MB with the 2 retained runs |
| `surf_quality_predictions` | 27 MB | 11,350 | about 2.15 kB per row (components 813 B, confidence_inputs 959 B). Stored for every member run as well as the blend: about 119 MB/day, about 1.2 GB at the 10-day retention. |
| `wave_forecasts` | 7.2 MB | 11,350 | 336 B/row NOAA, 1,034 B/row ECMWF (548 B of it the unused `extra` JSON), 357 B/row blend |
| `prediction_archive` | 7.0 MB | 3,950 | 1,506 B/row, of which 1,222 B (81 %) is `confidence_inputs`. Grows about 41.5 MB/day to about 30 GB at the 730-day retention. |
| `forecast_comparisons` | 3.1 MB | 4,050 | |
| `forecast_ensemble_stats` | 0.75 MB | 2,050 | |
| `buoy_observations` (in `swell_ndbc_live`) | 240 MB | 1,016,198 | 11 stations, 2019-2026 |
| `point_forecasts` (in `swell_ndbc_live`) | 511 MB | 589,260 | |
| Documented steady state | about 1.8 GB plus the archive | | `docs/operations.md:368-385` |
| Redis | `used_memory` 917 KB-1.04 MB; `maxmemory` 0; `noeviction` | | Holds locks, quota counters, rate-limit counters and the Celery broker and results only; no forecast data |

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| DA-1 | high | About 2.9 GB per cycle set (about 10.2 GB/day) is downloaded to sample 50 points. GRIB2 records are whole-globe, so a byte range picks a record but not a region. | run 2 and run 1 `details`; `noaa.py:515-530`; `config.py:97-98` | measured; lead |
| DA-2 | med | Two northern spots (Thurso, Mullaghmore) force the full 15-record `global.0p25` block: 8.47 MB per step, 686 MB per run, 36 % of NOAA bytes. `atlocn.0p16` covers neither spot. | `noaa.py:151-163, 743-753` | measured |
| DA-3 | med | ECMWF downloads period-band heights `h1012..h2530` (4.93 MB per step), `mp2` and the static `wmb` (re-downloaded every step), which no code reads. They go only into `wave_forecasts.extra`. See correction 3 for `mwp`. | `ecmwf.py:18-20, 127-128, 594`; grep of consumers | measured |
| DA-4 | med | GEFS reads WIND and SWELL1/SWPER1 mean/spread (about 3.1 MB per step, about 127 MB per run) that nothing consumes. Only HS and TP feed predictions. | `ensemble.py:161`; `predict.py:133-134` | measured |
| DA-5 | high | The Open-Meteo adapter is not accuracy-equivalent to direct ingestion. (a) Wind uses the default land cell: Pipeline mean wind 18.4 km/h on the land cell vs 28.0 on the sea cell, different cells at 5 of 7 breaks. (b) One interpolated 0.25 deg wave model for all spots: up to 0.22 m Hs difference vs native 0.16 deg. (c) No tertiary swell. (d) 16 days of hourly rows (19,200 per run) with Open-Meteo's interpolated hours unmarked. (e) `sea_level_height_msl` is stored as a tide. Details in 4.3. | `open_meteo.py:52-56, 63, 108, 187-195, 234-242` | ran; lead (grep) |
| DA-6 | med | `QuotaExceeded` is a plain `Exception`. It escapes the provider error handling, leaves the run RUNNING for an hour and aborts the remaining providers. The counter also counts HTTP requests, while Open-Meteo bills weighted calls per location. | `core/redis.py:81-100`; `ingestion.py:251, 254-262` | ran; lead |
| DA-7 | med | `prediction_archive` grows to about 30 GB at 730 days, and member-run predictions add about 119 MB/day. There is no size cap: `DATA_HEALTH_DATABASE_MAX_GB` is unset. | `config.py:221, 240`; `maintenance.py:168-175` | measured |
| DA-8 | med | Map layers depend on the GRIB spot provider: mode is `off` unless `noaa_gfs` is configured. Moving spots to an API would silently switch off the map layers. Standalone mode reads the 0.25 deg atmosphere (2.93 MB per hour for U+V+PRMSL); `pgrb2.0p50` has the same records for 0.86 MB. | `map_fields.py:42-49`; `.idx` sizes | measured |
| DA-9 | low | Celery `autoretry_for` never fires for acquisition (same as PLAT-2), and a killed run is re-downloaded in full. There is no per-step resume. | `tasks.py:95-111, 191-230` | read |
| DA-10 | low | Run timing is misleading (same as PLAT-1). ECMWF bytes are only logged, never stored. The docs claim NOAA and ECMWF ran in parallel; they run one after the other. | `ingestion.py:245, 325-337`; `docs/operations.md:332-333` | measured; lead |
| DA-11 | low | Redis has no memory cap or monitoring, and beat entries carry no `expires` (same as PLAT-8). | `docker-compose.yml:138`; `schedule.py:76-108` | ran |
| DA-12 | low | `GribFetcher` accepts a 200 response to a Range request and slices the whole body. A full `pgrb2.0p25` file is 548 MB. | `grib.py:206-210` | read |
| DA-13 | low | `ForecastProviderName` is a stale, unused literal. | `config.py:16` | read; lead |

### 4.3 External forecast APIs

The api-forecast audit fetched 25 provider pages and checked 87 quotes word for word with a
script; the tides audit read the tide and observation pages. I did not re-fetch them. Prices
behind JavaScript widgets could not be read.

**Forecast providers.**

| Provider | Role in the plan | Licence and commercial use | Limits | Cost | Key and env vars | Live test (audit) |
|---|---|---|---|---|---|---|
| Open-Meteo Forecast API (`/v1/gfs`, `/v1/ecmwf`) | Spot wind, gust and MSLP for the GFS and ECMWF families | Data CC BY 4.0 with a link next to the data. Free tier non-commercial only; ads or subscriptions count as commercial. | Free: 600/min, 5,000/h, 10,000/day, 300,000/month. Weighted calls: more than 10 variables or more than 14 days per location count as several. | Standard 1M calls/month, Professional 5M, Enterprise 50M+. Price not readable; a 2023 announcement says $29/month for Standard (unconfirmed for 2026). | None on the free tier; `OPEN_METEO_API_KEY` on `customer-api.open-meteo.com`. Proposed: `OPEN_METEO_ATMOS_MODELS`, `OPEN_METEO_CELL_SELECTION` | 3 points x 2 models x 4 variables x 2 days: 2,619 B on the wire |
| Open-Meteo Marine API (`/v1/marine`) | Spot waves with partitions (GFS-Wave 0.16/0.25 deg, ECMWF WAM, MFWAM) | Same | Same. The docs contradict themselves on horizon (0-8 vs 16 days). `ncep_gfswave016` returns HTTP 400 outside about 52.5N-15S. | In all paid plans | `OPEN_METEO_API_KEY`; proposed `OPEN_METEO_WAVE_MODELS` | 3 points x 4 models x 17 variables x 2 days: 7,832 B. GFS-Wave values equal the NOMADS GRIB f006 values at Pipeline. 29 of 32 spots on the same cell as direct sampling; mean absolute Hs difference 0.009 m. |
| Open-Meteo Ensemble API | Optional wind spread | Professional plan or higher for commercial use | Heavily weighted calls | Professional | `OPEN_METEO_API_KEY` | Not called |
| NOAA NODD on AWS (GFS, GFS-Wave, GEFS-Wave) | Map fields by byte range; GEFS spread; key-free fallback spot source | "Can be used as desired"; attribution requested; modified data must not be presented as original | None published | Free | None. `NOAA_GFS_BASE_URL`, `NOAA_GFS_WAVE_PATH_TEMPLATE`, `NOAA_GFS_ATMOS_PATH_TEMPLATE`, `NOAA_GEFS_BASE_URL`, `MAP_FIELD_*` | Record sizes from `.idx` and HEAD |
| NOAA NOMADS grib filter | Small regional subsets only; not for bulk | NOAA public data | 10 s between scripted fetches; may block | Free | None | 572 B and 3,292 B for 2x2 deg boxes; needs HTTP/1.1 |
| ECMWF open data | Key-free ECMWF fallback (direct) | CC BY 4.0 plus ECMWF terms; commercial use allowed with attribution | 500 simultaneous connections; only the 12 most recent runs | Free | `ECMWF_BASE_URL`, `ECMWF_MAX_LEAD_HOURS`, `ECMWF_AVAILABILITY_LAG_HOURS` | `.index` files fetched |
| Windy Point Forecast v2 | Remove from recommended setups and gate behind an acknowledgement | Terms forbid storing the data, derived databases and redistribution; trial keys not for production; data may not sit only behind a paywall | Professional 10,000 sessions/day | 990 EUR/year | `WINDY_API_KEY`; proposed `WINDY_TERMS_ACKNOWLEDGED` | Not called (no key) |
| Copernicus Marine (MFWAM) | Optional later second gridded wave family with partitions | Derivative works "for any purpose" with a visible credit; free until 30 June 2028 | Not reviewed | Free | Free account; proposed `COPERNICUSMARINE_SERVICE_USERNAME` / `_PASSWORD` | Not called |
| Stormglass | Not recommended | Free tier non-commercial; Medium and up commercial | Small 500/day, Medium 5,000/day, Large 25,000/day | 19 / 49 / 129 EUR per month | API key | Not called |
| Meteomatics | Not recommended | Contract | Not published | Quote only | Account | Not called |
| NWS `api.weather.gov` | Optional US wind cross-check | Open data, any purpose | Unpublished; User-Agent required | Free | Proposed `NWS_USER_AGENT` | Not called |

Quotes (from the audits' verified evidence):

- Open-Meteo terms: "You may only use the free API services for non-commercial purposes." and
  "Less than 10'000 API calls per day, 5'000 per hour and 600 per minute." and "Operating websites
  or apps that have subscriptions or display advertisements." ([terms](https://open-meteo.com/en/terms))
- Open-Meteo pricing: "Requests for data covering more than 10 weather variables or extending
  over a period of more than 2 weeks for a single location are considered multiple API calls."
  and "Standard: 1M calls/month. Professional: 5M calls/month. Enterprise: 50M+ calls/month." and
  "Historical, climate, ensemble, and satellite radiation APIs require the Professional API Plan
  or higher." ([pricing](https://open-meteo.com/en/pricing))
- Open-Meteo 2023 price: "is priced at $29 per month and provides 1 million API calls per month."
  ([announcement](https://openmeteo.substack.com/p/api-subscriptions-for-commercial))
- Open-Meteo licence: "You must include a link next to any location Open-Meteo data are
  displayed" ([licence](https://open-meteo.com/en/licence))
- Cell selection: "The default land finds a suitable grid-cell on land with similar elevation to
  the requested coordinates using a 90-meter digital elevation model. sea prefers grid-cells on
  sea." ([docs](https://open-meteo.com/en/docs))
- Interpolation: "For GFS, values are interpolated from 3-hourly to 1-hourly after 120 hours."
  ([GFS API](https://open-meteo.com/en/docs/gfs-api)); "The open-data IFS 0.25 updates every 6
  hours, but only offers 0.25° resolution and 3- and 6-hourly data."
  ([ECMWF API](https://open-meteo.com/en/docs/ecmwf-api))
- Run availability: "To ensure all API calls use the most recent data, please wait 10 minutes
  after the availability time." and "Note that API calls to the metadata API are not counted
  toward daily or monthly request limits." ([model updates](https://open-meteo.com/en/docs/model-updates))
- Marine: "Wave directions are always reported as the direction the waves come from." and
  "| forecast_days | Integer (0-8) | No | 5 | Per default, 7 days are returned. Up to 8 days of
  forecast are possible. |" and "The reference (datum) height is the global mean sea level, not
  the lowest astronomical tide. ... This data is not suitable for coastal navigation."
  ([marine](https://open-meteo.com/en/docs/marine-weather-api))
- NODD: "NOAA data disseminated through NODD are open to the public and can be used as desired."
  and "If you modify NOAA data, you may not state or imply that it is original, unaltered NOAA
  data." ([registry](https://registry.opendata.aws/noaa-gfs-bdp-pds/))
- NOMADS: "If your script contains loops, then be sure to include a 10 second wait between
  fetches ..." and "If a selection of parameters(s) and/or level(s) are needed, instead of the
  entire file, you are encouraged to use the partial-http transfer method ..."
  ([grib filter](https://nomads.ncep.noaa.gov/info.php?page=gribfilter)); "As NOMADS deprecates
  OpenDAP (DODS) access for major models, users are encouraged to transition to the Grib Filter
  service ..." ([migration](https://nomads.ncep.noaa.gov/info.php?page=opendap_grib_migration))
- ECMWF: "Their use is governed by the Creative Commons CC-BY-4.0 licence and the ECMWF Terms of
  Use. This means that the data may be redistributed and used commercially, subject to
  appropriate attribution." and "access to the Open-Data Portal is currently limited to 500
  simultaneous connections." and "Data are retained for the most recent 12 forecast runs"
  ([open data](https://www.ecmwf.int/en/forecasts/datasets/open-data))
- Windy terms: "store, extract, modify, distribute, use the weather data or other content of the
  Services, create any weather works or databases derived therefrom" and "Redistribution of any
  part of the API or the included data to any third party is forbidden." and "The Trial Version
  is intended for development purposes only and the User is not allowed to use it for
  production" ([terms](https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use));
  "990 € / year" ([pricing](https://api.windy.com/point-forecast/pricing)); "| windWaves |
  wwaves_height-surface wwaves_period-surface wwaves_direction-surface |"
  ([docs](https://api.windy.com/point-forecast/docs))
- Copernicus Marine: "(b) modify, adapt, develop, create and distribute Value Added Products or
  Derivative Work from Copernicus Marine Service Products for any purpose;" and the required
  credit "Generated using E.U. Copernicus Marine Service Information; insert DOIs links here"
  ([licence](https://marine.copernicus.eu/user-corner/service-commitments-and-licence))
- Stormglass: "Not for commercial use" (free tier) and "Full support and for commercial use"
  ([pricing](https://stormglass.io/pricing/))
- NWS: "All of the information presented via the API is intended to be open data, free to use
  for any purpose." and "A User Agent is required to identify your application."
  ([docs](https://www.weather.gov/documentation/services-web-api))

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| API-01 | high | The Windy adapter stores Windy data, blends it and archives predictions. Windy's terms forbid storage and derived databases, yet `docs/FORECAST_SOURCES.md:3` says every source is used within its terms. | `windy.py:111-167`; `README.md:345` | read |
| API-02 | med | The Windy adapter reads `windWaves_*` keys; the documented response keys are `wwaves_*`, so wind sea is always null. The test fixture has no wind-wave keys. | `windy.py:45-47`; `test_forecast_providers.py:247-258` | read; lead |
| API-03 | med | Open-Meteo returns 0/0/0 for an absent partition; the adapter stores 0 m, 0 s, 0 deg (north). Replay of live data: Thurso secondary swell 0.0 in 27 of 48 records; J-Bay wind sea 0.0 in 36 of 48. These reach the spot API and blend partitions. | `open_meteo.py:249-256`; `spot_views.py:339-342`; `blend.py:101-102` | ran |
| API-04 | med | Tertiary swell and peak periods are never requested. For ECMWF WAM `wave_period` is the mean period and `wave_peak_period` is Tp (14.65 vs 11.3 s at Pipeline), so switching the model would silently store the mean period. | `open_meteo.py:41-56` | ran; lead |
| API-05 | med | Wind is requested at the break with the default land cell (see DA-5). The J-Bay break was served from a cell about 8 km west. | `open_meteo.py:179-195, 276-284` | ran; lead |
| API-06 | med | The run time comes only from `last_run_initialisation_time`, ignoring the documented 10-minute wait after availability. `gfs_seamless` serves HRRR for the first 18-48 h over CONUS, so US spots get a different run than the recorded init time. | `open_meteo.py:66-72, 143-176` | ran |
| API-07 | med | Hours that Open-Meteo interpolated from 3- or 6-hourly data are stored as native hourly values. Spec 12 forbids implying exact hourly values. | `open_meteo.py:246-256` | ran |
| API-08 | med | Same model data, different transport: the GRIB path costs about 2.9 GB per cycle set; Open-Meteo returns the same numbers in kilobytes (DA-1). The GRIB path works and is licence-clean, so this is a design trade-off, not a crash. | live comparison at Pipeline | measured |
| API-09 | low | Attribution is one fixed string per provider, shown as plain text inside a collapsed disclosure. It always names NOAA, and it lacks the Open-Meteo link and the Copernicus and ECMWF notices. | `open_meteo.py:113-127`; `app/spots/[slug]/page.tsx:262` | read |
| API-10 | low | The quota counts HTTP attempts per day only. An estimated 183 weighted calls per 50-spot run (about 22,000 per month at 4 runs per day; computed from the documented rule) are counted as 15. | `http.py:60-68`; `config.py:80` | read |
| API-11 | low | GFS gust is often below the 10 m mean wind (544 of 4,050 rows). ECMWF gust is a 3-hour maximum. Both are stored in one field. | psql; Open-Meteo ECMWF docs | measured |
| API-12 | low | The Marine docs contradict themselves on horizon; the adapter always asks for 16 days and checks only the location count. | `open_meteo.py:187-211` | read |
| API-13 | low | One wave model and one atmosphere model per instance, under a fixed code, so Open-Meteo cannot supply independent families today. | `config.py:81-83`; `registry.py:157-158` | ran |
| API-14 | low | `docs/API_SETUP_REQUIRED.md` (spec 25) does not exist; `FORECAST_SOURCES.md` is partly outdated. | `ls docs/` | ran |

### 4.4 Observations and tides

**Tide and observation providers.**

| Provider | Role in the plan | Licence | Limits | Cost | Key and env vars | Live test (audit) |
|---|---|---|---|---|---|---|
| NOAA CO-OPS Data API + mdapi | Tide predictions for the 8 US spots; stations 1.7-18.6 km away | Public domain; NOS requests attribution | High/low up to 10 years per request, other intervals 1 year. Subordinate stations: high/low only, `datum=MLLW`. | Free | None. Proposed `NOAA_COOPS_BASE_URL`, `NOAA_COOPS_APPLICATION` | Station list 2,042,754 B (3,502 stations). Haleiwa high/low: 8 events per 48 h. `interval=h` at a subordinate station returns HTTP 200 with an error JSON. |
| EOT20 (DGFI-TUM, SEANOE) | Global harmonic constants for the other 42 spots | CC BY, open access | One-off 2 GB download by a developer | Free | None. Proposed `TIDE_MODEL`, `TIDE_CONSTANTS_PATH` | Metadata page only |
| FES2022 (AVISO) | Alternative global model | Heights "available for all uses"; free AVISO account | 170 MB per elevation file | Free | `AVISO_USERNAME`/`AVISO_PASSWORD`, one-off script only (proposed) | Not downloaded |
| pyTMD | Constant extraction, reference synthesis | MIT | | Free | | PyPI 3.0.9 listed |
| Marine Institute Ireland ERDDAP | Optional station tides for Mullaghmore (Sligo 19.8 km, Killybegs 18.8 km) | CC BY 4.0; LAT datum | | Free | None | Info page 200 |
| LINZ | Optional station tides for Raglan | CC BY 4.0; "not official tide tables" | | Free | None | CSV gave 403 from the sandbox |
| Open-Meteo `sea_level_height_msl` | Cross-check only, never the displayed tide | Free tier non-commercial | As 4.3 | | | Correlation 0.987-0.993 with CO-OPS on the open coast, 0.925 at Golden Gate (72 h, 7 comparisons) |
| WorldTides, Stormglass, UKHO, TPXO, BOM, SHOM, IH Portugal | Not used | WorldTides: one response per user only. TPXO: commercial licence. UKHO: terms unread (portal 503). BOM, SHOM, IH: copyright or paid (search summaries only). | | Paid or unverified | Keys | Not tested |
| NOAA NDBC `realtime2`/`5day2` | Instrument observations (US) | Public domain | | Free | None | `5day2` files are 8.9 times smaller for the same 48 h |
| CDIP Waveriders | Via NDBC | "These data may be redistributed and used without restriction." | No wind | Free | | DAS 200 |
| AWC METAR | Optional observed wind, context only | US Government; foreign METAR licence unverified | 100 requests/min, 400 entries | Free | | Nearest METAR within 30 km for 27 spots |
| Goddard-Caldwell (NCEI 0001754) | Historical calibration, 3 Oahu spots | CC0 per catalogue (search summary) | Historical only (1968-2020) | Free | | Landing page |

Quotes: "Harmonic tide prediction stations can provide tide predictions on any available
interval. Subordinate tide prediction stations can only provide tide predictions on a high / low
interval." and "Note! Subordinate tide prediction stations must use “datum=MLLW”"
([CO-OPS API](https://api.tidesandcurrents.noaa.gov/api/prod/)); "The information on government
servers are in the public domain, unless specifically annotated otherwise, and may be used freely
by the public." ([NOS disclaimers](https://tidesandcurrents.noaa.gov/disclaimers.html)); "Each
API request may only be used for a single user except where the license explicitly allows
otherwise." ([WorldTides](https://www.worldtides.info/apidocs)); "Condition of access: FES2022
heights and loading tide are available for all uses, as indicated in the licence agreement."
([AVISO](https://www.aviso.altimetry.fr/en/data/products/auxiliary-products/global-tide-fes.html));
"The final EOT20 version is provided open access and should be used for all applications."
([SEANOE](https://www.seanoe.org/data/00683/79489/)); "TPXO and TPXO-atlas models are available
for academic research and other non-commercial uses and require registration."
([TPXO](https://www.tpxo.net/tpxo-products-and-registration)).

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| OBS-TIDE-1 | high | There is no tide data in live operation. The only producers of `sea_level_m` are Open-Meteo (not used live) and the synthetic demo; NOAA and ECMWF have no tide. 0 of 11,350 rows have a sea level, so tide is never scored or shown. | `noaa.py:47-48`; `ecmwf.py:48-49`; `blend.py:360-361` | ran; lead |
| OBS-TIDE-2 | high | The schema has no tide station, source, datum or high/low events: one float per forecast hour. | `models/forecast.py:146`; `schemas/spots.py:261` | read |
| OBS-TIDE-3 | med | The tide stage is a 25 h rolling min/max. On real mixed semidiurnal predictions (Pillar Point) the higher low gets stage 0.52-0.66, so a spot that likes low tide scores 0.20-0.32 at a real low. Rising/falling is ignored, and the best possible score at an extreme is 0.84. `tide_stages` has no unit test. | `predict.py:87-100`; `scoring.py:42-48, 161-168` | measured |
| OBS-TIDE-4 | med | Tide is scored with an invented rule (slope 1.6, floor 0.2) whatever the research says. The tide research class is Low for 32 of 50 spots and the tide decision is "disputed" for 21. The UI says "Not established" only when the preference is null. Only 10 spots have a documented, undisputed, non-"all" preference. | `research_evidence.json` assessment; `profile_decisions.json`; `app/spots/[slug]/page.tsx:437` | measured |
| OBS-TIDE-5 | med | Observations never reach spot pages. The "Now" card is a model forecast hour with no source or run time. | `app/spots/[slug]/page.tsx:422`; `conditions.tsx:43-46` | read; lead |
| OBS-TIDE-6 | low | `sea_level_m` has no datum, and the blend picks the first non-None member per hour, so two producers would mix datums. | `blend.py:360-361` | read |
| OBS-TIDE-7 | low | The hourly NDBC job downloads the full 45-day `realtime2` files and `activestations.xml` every run; `5day2` is 8.9 times smaller (about 134 MB/day avoidable, estimate). | `ndbc.py:118, 618-623, 728` | measured |
| OBS-TIDE-8 | low | The 730-day retention would delete the 2019-2024 observation history used for climatology. | `maintenance.py:168-175`; `verification/jobs.py:23-24, 163` | read |
| OBS-TIDE-9 | low | Rincon's buoy 46053 has reported no waves since 2026-10-06. Nothing shows per-station staleness. | `5day2` file; `swell_ndbc_live` | ran |
| OBS-TIDE-10 | low | The `nearshore_observations` table has no reader or writer (0 rows). | `models/observations.py:145-179` | read |
| OBS-TIDE-11 | low | `swell_live2` holds stale profiles (3 tide preferences differ from `spots.json`). Re-seed a copy before validating. | psql | measured |
| OBS-TIDE-12 | low | The frontend tide factor parses English explanation text. | `lib/rating.ts:149-158` | read |

### 4.5 Offshore wind physics and spot-specific scoring

Offshore is used consistently: wind FROM land TOWARD the ocean, stored as degrees FROM. The
defects are in how speed and direction combine.

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| SW-1 | high | Offshore wind scores the maximum (Q = 1.0) from 0 to 35 km/h. Above that the penalty floors at 0.4, so 80, 100 or 150 km/h offshore on a good swell still scores 73 ("very good"). Real run: 69 offshore hours at 30 km/h or more, e.g. Cloudbreak 32.6 km/h gusting 41 scored 89. | `scoring.py:146-158` | ran; lead |
| SW-2 | high | Cross-shore and onshore stop getting worse at 20 km/h. Cross-shore keeps Q = 0.5 up to 35 km/h (score 84 on good swell); any onshore wind of 20 km/h or more scores 34. Real run: 73 cross-shore or cross-onshore hours at 25 km/h or more scored Good or better. | `scoring.py:50-52, 154-157` | ran; lead |
| SW-3 | high | Swell direction and period are scored from the primary swell even when another partition makes the breaking waves. In the blend run 758 of 4,050 hours have a non-primary dominant partition. For the 387 hours driven by secondary or tertiary swell, scoring the dominant partition raises the score by 27.6 points on average and changes 257 labels. | `scoring.py:192-203`; `breaking.py:166-190` | measured; lead |
| SW-4 | med | Labels ignore strength: 5.0 km/h is "glassy" and 5.1 km/h is "onshore", so Ocean Beach shows "onshore" next to a 97 (Exceptional). Matching uses the same strings. The frontend adds its own light/strong cut-offs (12 and 35 km/h). | `scoring.py:111-125`; `matching/evaluate.py:20-21, 84-88`; `lib/rating.ts:53-54` | ran; lead |
| SW-5 | med | The swell-direction term drops to 0 at the window edge: 89 of 100 edges step, up to 17 score points per 0.5 deg (Rincon 93 to 76). | `scoring.py:73-81` | ran; lead read |
| SW-6 | med | Gusts are stored but never scored, and GFS and ECMWF define gusts differently (median gust/speed ratio 1.15 vs 1.47). | `models.py:119, 152`; `noaa.py:46`; `ecmwf.py:41-43` | measured |
| SW-7 | med | There is no per-spot wind tolerance or shelter, and one offshore direction per spot. The research evidence already holds shelter or tolerance claims for 9 spots. | `models.py:12-30`; `seed.py:71-83` | ran |
| SW-8 | med | Trap for the refactor: the nearshore shore normal `beta` is the centre of the optimal swell window. It is more than 60 deg from the researched offshore direction at 19 spots (Chicama 120 deg). The wind normal must be built from `offshore_wind_direction`, never from `beta`. | `seed.py:241-272`; `models.py:32-44` | ran |
| SW-9 | low | The relation sectors are asymmetric and undocumented: 422 hours on the onshore side of perpendicular are labelled cross-shore. | `scoring.py:116-125` | measured |
| SW-10 | low | Tide is never scored on real data (same as OBS-TIDE-1). | psql | measured |
| SW-11 | low | Per-factor contributions are not stored (`weights_used` is dropped), so "Why this forecast?" cannot show actual contributions for older rows. | `predict.py:312-338` | read |
| SW-12 | low | `docs/SURF_SCORING.md` leaves out the 0.4 floor, the 20-35 km/h plateau, the sector thresholds and the offshore definition. | `docs/SURF_SCORING.md:140-147` | read |
| SW-13 | low | Chart arrows are teal for everything that does not contain "onshore", including "unknown". The search wizard says "Offshore or glassy only" while matching also accepts cross-offshore. | `forecast-charts.tsx:229-232`; `search-wizard.tsx:300-301` | read |
| SW-14 | low | Directional spreading steps at 9 s and 13 s, so the breaking height jumps 4-8 %. | `nearshore.py:125-133` | ran |
| SW-15 | low | The consistency term ignores tertiary swell and penalises wind sea that travels away from the break under offshore wind. | `scoring.py:172-182` | read |
| SW-16 | low | `swell_live2` spot rows are stale for 4 spots; re-seed before any before/after comparison. | psql | ran |

### 4.6 Wind visualization (map)

The wind path is physically correct end to end (section 3). The defects are in rendering and
freshness.

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| WINDVIZ-1 | high | On Firefox, Safari and every iOS browser the trail fade leaves a permanent 6/255 white veil. The fix relies on a `float16` canvas, which MDN's compat data (7.3.17) lists for Chrome/Edge 137+ only, and there is no 8-bit fallback. The e2e veil test runs in Chromium only. | `particles.ts:27, 108-112`; `forecast-map.tsx:131-138`; `playwright.config.ts:21-25` | ran; lead read |
| WINDVIZ-2 | med | Particle speed and trail length depend on the refresh rate: 10 m/s moves 120 px/s at 60 Hz, 240 at 120 Hz and 288 at 144 Hz. (Same defect as WAVEVIZ-8.) | `particles.ts:87-92, 119-125` | ran |
| WINDVIZ-3 | med | The field index is fetched once and never refreshed, and no stale state is shown. The live DB served "now" from a 54 h-old cycle without warning. The cycle time appears only in the closed legend. | `hooks/map-fields.ts:19-32`; `providers.tsx:16-17`; `legend-drawer.tsx:113` | ran |
| WINDVIZ-4 | med | The client discards the served `valid_time` and `run_key`, and the server ignores `run`. After a new run a field can be shown under the wrong time (`at=19:40Z` is served 21:00Z; `run=bogus` returns 200). | `lib/map-fields.ts:72-83, 283-292`; `api/map_fields.py:37-41`; `services/map_fields.py:211-219` | ran |
| WINDVIZ-5 | med | On the default dark basemap opaque land hides the wind field and particles over land, which hides offshore flow; 49 % of particles in the default view sit over land or ice. | `forecast-map.tsx:44`; `basemap-draw.ts:1-3, 100-102` | measured |
| WINDVIZ-6 | med | No wind toggle independent of the layer tab, no mph, and no lead time or analysis label on the map. | `forecast-map.tsx:57-62`; `lib/map-layers.ts:13-16`; `settings-popover.tsx:108-116` | read |
| WINDVIZ-7 | med | Unit tests never exercise the sign of v or the animated step; flipping v would pass every unit test. | `tests/map-particles.test.ts:44-60` | read |
| WINDVIZ-8 | low | The particle scale (0.2 CSS px per frame per m/s), projection, reduced-motion and land behaviour are undocumented, and `docs/map-layers.md:414-417` describes a fixed Cache-Control limitation. (Same as WAVEVIZ-9.) | `docs/map-layers.md:414-417`; `core/middleware.py:107-112` | ran |
| WINDVIZ-9 | low | Map wind at a spot can differ from the spot's own wind (0.5 deg box mean including land vs nearest 0.25 deg sea cell): 10 of 50 spots differ by more than 1.5 m/s. By design, but unexplained. | `noaa.py:47-52`; `fields.py:33-34` | measured |
| WINDVIZ-10 | low | Colour-field speed and readout speed are computed differently (max difference 2.92 m/s). | `forecast-map.tsx:54, 462-466` | measured |
| WINDVIZ-11 | low | Every time step wipes all trails and reseeds every particle during playback. | `forecast-map.tsx:135-139, 163-167` | read |
| WINDVIZ-12 | low | Conventions are correct (arrows TO, text FROM) but not stated where users see them; the map spot panel prints "NW" without "from". | `forecast-charts.tsx:228-231`; `conditions.tsx:63` | read |
| WINDVIZ-13 | low | Three `wind_from_uv` implementations: `geo.wind_from_uv` returns 0.0 (north) for calm wind, while the NOAA and ECMWF versions return None. | `geo.py:84-88`; `noaa.py:229-239`; `ecmwf.py:190-196` | ran; lead read |

### 4.7 Wave visualization and wave fields

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| WAVEVIZ-1 | high | All wave tabs use the wind particle engine: same white streaks, same fade, streaks along the propagation direction (the opposite of crest geometry). On served fields wave and wind streaks move at almost the same screen speed (median 1.25 vs 1.33 px per frame), although their directions differ by a median 52.5 deg. Spec 10 forbids reusing wind particles. | `forecast-map.tsx:57-62, 488`; `particles.ts:27, 133-134`; `lib/map-fields.ts:217-225` | measured; lead |
| WAVEVIZ-2 | high | The animation ignores height and has no physical wavelength or speed scale (speed = 1.1 px/frame x Tp/10). A true 10 s crest spacing is 0.004 px at zoom 2, so any visible spacing is an exaggeration that must be stated. | `particles.ts:12-18, 133-135`; `legend-drawer.tsx:94-97` | read |
| WAVEVIZ-3 | high | Swell and wind-sea partitions are never stored or served (only HTSGW/PERPW/DIRPW; the DB CHECK allows 6 variables; `/api/map/fields/swell1_dir` returns 422). In `with_noaa` mode all 15 wave records are already decoded for the two northern spots. Partition data is real: swell1 covers 90.6 % of ocean cells, swell2 74.2 %, wind sea 61.6 %. Wind sea is the higher system in 65 % of mixed cells, so the peak layer often hides the groundswell. | `noaa.py:196, 776`; `forecast_engine.py:222-229` | measured; lead read `:196` |
| WAVEVIZ-4 | med | On the Streets basemap wave streaks and colour reach 8.7 M km2 of land (2.47 % of the drawable area; p99 56 km inland). Masking works only where the dark basemap paints land on top. | `particles.ts:52-55`; `basemap-draw.ts:98-105` | measured |
| WAVEVIZ-5 | med | 0.5 deg box means mix different swell systems, because WW3 numbers partitions by height per cell. Boxes with mean resultant length below 0.7: 4.4 % (swell1), 10.6 % (swell2). Energy weighting is worse. | `fields.py:252-260` | measured |
| WAVEVIZ-6 | med | Adding partition variables naively would blank the map: "complete valid time" means every variable is present, and the CHECK constraint needs a migration. Partitions must be optional variables. | `fields.py:329-334`; `services/map_fields.py:68, 124, 154-155` | read |
| WAVEVIZ-7 | med | The field cache holds 24 entries; 12 wave fields per step with neighbour prefetch need 36, so the current step is evicted. | `hooks/map-fields.ts:36`; `lib/map-fields.ts:302-314` | read; lead |
| WAVEVIZ-8 | low | Frame-rate dependent animation (same as WINDVIZ-2). | `particles.ts:87-92` | read |
| WAVEVIZ-9 | low | Stale docs about no-store and "wave particles" (same as WINDVIZ-8). | `docs/map-layers.md:3-4, 414-417` | ran |
| WAVEVIZ-10 | low | SWPER/WVPER are called peak periods in code; NCEP calls them mean periods. Label them "period" until checked. | `noaa.py:34-35` | read |

Measured partition costs (wave-viz audit). GRIB per step: the 9 partition records (swell1,
swell2, wind sea x height, period, direction) are 5.13-5.28 MB; the current 3 records are
1.82 MB. Encoded with the app's encoder: 705.3 kB per valid time at 0.5 deg, 57.1 MB per run
(+146 % over today's 39.0 MB), or 18.6 MB per run at 1.0 deg.

### 4.8 Forecast charts and spot API

Live runs are 3-hourly (ECMWF 6-hourly after 144 h; GEFS 6-hourly); the demo provider is hourly,
so the e2e suite never sees the cadence problems below. Measured on the real J-Bay payload
(78 points, Africa/Johannesburg).

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| CH-1 | high | No time-axis labels for 42 of 50 spots. `dayTicks()` keeps only samples at local midnight; on a 3-hourly UTC grid that happens only where the offset is a multiple of 3 h. The same filter is copied in the model-comparison chart. | `forecast-charts.tsx:95-98`; `model-comparison.tsx:68-70` | ran; lead |
| CH-2 | high | There is no shared selected time and no cursor. The charts never receive the page's `selected` state; each has its own tooltip, no `syncId`, no reference line. | `app/spots/[slug]/page.tsx:343, 360, 368` | ran |
| CH-3 | high | The charts do not share an x layout (plot areas start at x = 28/40/32 px with widths 824/812/860 at 900 px), and swell and wind sit side by side at `xl`. Day boundaries cannot line up. | `forecast-charts.tsx:164-247`; `page.tsx:354` | measured |
| CH-4 | high | "Three hours later" moves three array indices: 9 h with live data (11:00 to 20:00 local). The card is titled "Hour by hour" over 3-hourly steps. | `page.tsx:160-188`; `e2e/spot.spec.ts:44-47` | ran |
| CH-5 | med | Daily "Nh good" counts 3-hour steps as hours (also CT-9). The 57 is a hard-coded copy of the Good threshold. | `spot_views.py:299` | measured; lead read |
| CH-6 | med | Night shading follows the per-step flag, so band edges are 63-168 min late against `sun.py`; only one chart has shading. | `forecast-charts.tsx:81-93` | measured |
| CH-7 | high | No tide chart, no tide in the contract, and no live tide (OBS-TIDE-1). | `schemas/spots.py:261` | measured |
| CH-8 | med | The swell chart and breakdown call the primary swell the "Main component (drives the surf)", while the backend's `Conditions` uses the dominant partition. 18.7 % of blend rows differ, so the map panel and the spot page disagree. (UI side of SW-3.) | `lib/forecast.ts:16-18`; `forecast-breakdown.tsx:173-174`; `spot_views.py:121` | measured |
| CH-9 | med | The central breaking value is delivered but never plotted; there are no observation markers; the right axis prints "6ft". | `forecast-charts.tsx:170-179`; `schemas/spots.py:265` | ran |
| CH-10 | med | Wind chart: gusts unlabelled and missing from the tooltip, arrows every third index (every 9 h), binary colour, the arrow convention unstated, km/h only. | `forecast-charts.tsx:134-139, 226-235, 249-259` | ran |
| CH-11 | med | Blend provenance is incomplete: `wave_model_run_at` and `atmosphere_model_run_at` are null, and member init times exist only as untyped dicts. The API has no server "now". | `schemas/spots.py:357-381` | measured |
| CH-12 | med | Each hour selection re-renders all three charts and the daily strip (about 168 ms per change in jsdom). | `page.tsx:277, 343-371` | measured |
| CH-13 | med | The quality palette fails non-text contrast ("fair" is 1.92:1 on white) and is not ordinal (neighbouring classes differ by 1.06-1.10). Bars carry colour only. | `lib/quality.ts:35-42`; `globals.css:32-38` | measured |
| CH-14 | low | No hour ticks or now marker; 24 h tooltips; ticks render as "13 Tue". | `forecast-charts.tsx:100-103`; `lib/format.ts:52-62` | ran |
| CH-15 | low | Mixed units: breaking height follows ft/m, swell is always m, wind always km/h. | `forecast-charts.tsx:197-238` | read |
| CH-16 | med | No mobile timeline (about 10 days squeezed into the phone width) and no mobile Playwright project. | `playwright.config.ts:21-31` | read |
| CH-17 | med | `GET /api/spots/{slug}` builds summaries for all 50 spots to return one: 412-501 ms median vs 34-37 ms for `/forecast` (also PLAT-4). | `spot_views.py:225-226` | measured; lead read |
| CH-18 | low | `/forecast` is 252,690 B JSON (31,647 B gzipped; the untyped `components` dict is 38 % of point bytes) and has no Cache-Control or ETag. | `api/spots.py:69-73` | measured |
| CH-19 | low | A failed forecast request leaves an empty card. | `page.tsx:341-343` | read |
| CH-20 | low | The selected time is not linkable (`?t=`), and the map panel link drops the map time. | `spot-panel.tsx:139` | read |
| CH-21 | low | The top summary is the static best of 7 days; the selected-hour detail sits below the charts. 578 visible words on the page. | `page.tsx:331-352` | measured |
| CH-22 | low | `SURF_SCORING.md` says "Steps are hourly". | `docs/SURF_SCORING.md:236` | read |

### 4.9 Confidence, labelling and time zones

The backend keeps the three confidences apart; the problems are in presentation.

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| CT-1 | high | The research sentence is listed under "Forecast confidence". The API returns one combined `confidence_reasons` list (forecast reasons, the disputed-wind note, the research sentence), and the UI renders it all under forecast confidence. Affects 50 of 50 current hours. The e2e test locks this in (`confidence.spec.ts:144`). | `forecast_confidence.py:601-613`; `confidence.tsx:110-129` | ran; lead |
| CT-2 | high | Model output is titled "Now". The card's valid time was +2 h from now for all 50 spots, and its details say "about 18 hours ahead" (lead counted from the run issue time), or "about 3 days ahead" when stale. | `spot_views.py:195`; `page.tsx:422`; `lib/confidence.ts:58-68` | ran; lead read `:422` |
| CT-3 | high | Opportunity cards, notifications and search matches never show forecast confidence: the real `MatchSummary` and `NotificationOut` lack the field. The e2e test passes only because the mock adds it. | `schemas/opportunities.py:71-108, 131-153`; `e2e/confidence.mock.ts:414, 461` | read |
| CT-4 | med | No stale or freshness state on the map, dashboard, event cards or landing page; the landing page says "live forecast" for 2-day-old data. | `schemas/spots.py:93-101, 118-151`; `schemas/map_fields.py:51-58`; `live-swells.tsx:14, 20` | ran |
| CT-5 | med | The stale banner says "alerts are paused" about 16 h before alerts are actually withheld (factor 1 vs 2). | `page.tsx:323-329`; `alerts.py:67, 490-493` | ran |
| CT-6 | med | Blend provenance is mislabelled: "model run" shows the oldest member's init time, "About this forecast" shows raw codes and no run time, and the email labels the init time "Forecast updated". | `forecast-breakdown.tsx:162`; `templates.py:156-160` | ran |
| CT-7 | med | Local (at-the-break) confidence is hidden on event cards, although it is lower than forecast confidence for 37 of 48 events. | `event-card.tsx:80-83` | ran |
| CT-8 | med | The legacy 0-100 index is labelled "Forecast confidence" on the opportunity page; its class differs from the forecast class in 20.5 % of blend hours. | `opportunities/[id]/page.tsx:119-124`; `opportunity_views.py:188-189` | measured |
| CT-9 | med | "Nh good" counts 3-hourly points (325 of 508 day cards). Same as CH-5. | `spot_views.py:299` | ran |
| CT-10 | med | The "80 % range" is presented as a probability interval, but it is heuristic and the project's own check found 53 % offshore coverage at 24 h. | `lib/glossary.ts:22-25`; `docs/ml-evaluation.md:53` | read |
| CT-12 | med | Spot times say "local" with no zone name; the map timeline uses the browser zone, so one instant shows as "Mon, 8:00 AM PDT" on the map and "Mon, Oct 12, 23:00 local" in the panel (Uluwatu). DST fall-back hours render identically. | `lib/format.ts:52-62`; `lib/map-time.ts:97-112` | ran |
| CT-14 | low | Mixed score scales ("92/100" on map tooltips and email vs "9.2/10"); "Best spots now" stays "now" for any selected time. | `app/map/page.tsx:150`; `templates.py:151-153`; `spot-list.tsx:44, 53` | read |
| CT-15 | low | No observations are shown to surfers (only on admin pages). | `api/*.py` | ran |
| CT-16 | low | "Not yet checked against buoys near this spot." appears on every hour. | `forecast_confidence.py` | ran |
| CT-17 | low | A deprecated legacy `ConfidenceBadge` is used only by a test. | `components/quality.tsx:57-67` | ran |

### 4.10 Text verbosity and factual copy

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| CT-11 | med | The long text a surfer sees first is data-driven: the research block (75-77 visible words, summary plus every gap), the spot description (median 35, max 50 words), the hour breakdown (106-121 visible words with labels such as "est. breaking faces, typical–sets"). Data-driven words on the first screen: median 124, max 144. The headline itself is already concise. | `app/spots/[slug]/page.tsx:308`; `confidence.tsx:223-234`; `forecast-breakdown.tsx:104-187` | ran |
| CT-13 | med | Wrong copy: "16-day wave model horizon" (data is 10 days), "every break, every hour" (3-hour steps), "Komar–Gaughan" in opportunity limitations (11,350 of 11,350 predictions use `linear_wave_v2`). | `app/page.tsx:18, 101`; `opportunity_views.py:171-172` | measured; lead |
| CH-21 | low | The page has 578 visible words; the selected-time detail is buried and repeats information. | as above | measured |

### 4.11 Platform and tests

**Baseline (measured on `2bd2b50`).**

| Check | Result |
|---|---|
| `pytest tests/unit` | 644 passed in 24.75 s, exit 0 (also 644 with Postgres and Redis unreachable) |
| `ruff check` / `ruff format --check` | pass / 235 files formatted |
| `mypy app` | 0 issues in 159 files |
| `npx vitest run` | 160 tests in 16 files, exit 0 |
| `npx tsc --noEmit` / `npx eslint .` | exit 0 / exit 0 |
| Integration tests | 233 in 26 files (collected, not run) |
| Playwright | 62 in 8 specs (listed, not run): map 26, confidence 16, product-sweep 11, public 4, spot 2, accuracy 1, full-flow 1, scenario 1 |
| Alembic | head `0005_research_retrieval`; upgrade, check, downgrade base and re-upgrade all exit 0 on a scratch DB |

| ID | Sev | Finding and root cause | Evidence | Verified |
|---|---|---|---|---|
| PLAT-1 | med | `forecast_runs.started_at` is the job start for every provider. NOAA and ECMWF share 03:41:01.651303, so ECMWF's duration reads 8 min 28.6 s instead of at most 6 min 16 s. | `ingestion.py:245, 319-338` | measured; lead |
| PLAT-2 | med | Celery autoretry on the forecast, ensemble and map-field tasks can never fire: provider errors are caught and returned as "failed", so an outage waits 3-6 h, and the job is still logged SUCCESS and chains on. | `ingestion.py:219-262`; `ensemble.py:915-929`; `tasks.py:95-111` | ran; lead |
| PLAT-3 | med | One Celery queue with concurrency 2: verification and reset emails share worker slots with multi-minute GRIB downloads. | `celery_app.py:41-71`; `docker-entrypoint.sh:36` | read |
| PLAT-4 | med | `GET /api/spots/{slug}` takes about 0.5 s (see CH-17); `spot_summaries` costs 628 ms, 9 SQL statements and 17,132 JSON decodes. | `spot_views.py:225-226` | measured |
| PLAT-5 | med | Live mode has no tide source, table or job (see OBS-TIDE-1). | `schedule.py:147-212` | measured |
| PLAT-6 | med | No test imports the forecast charts; spec 27 chart items 1-15 and e2e tide, chart wind arrows and synchronised selection are uncovered. | `frontend/tests`; `e2e/spot.spec.ts:20-63` | read |
| PLAT-7 | med | Superseded runs are kept 10 days (about 1.7 GB of the 1.8 GB steady state). `docs/operations.md:419-420` says lowering this "saves little", contradicting its own numbers. | `maintenance.py:77-95` | measured |
| PLAT-8 | low | Redis has no memory cap or monitoring, and daily quota counters reset when Redis restarts. | `docker-compose.yml:136-146`; `core/redis.py:93-96` | ran |
| PLAT-9 | low | The integration conftest drops the schema of whatever `TEST_DATABASE_URL` names, with no name guard. Real-run databases live on the same server. | `tests/conftest.py:103-110` | read |
| PLAT-10 | high | About 1.9 GB per NOAA cycle to sample 50 points (same as DA-1). | run 2 | measured; lead |
| PLAT-11 | low | The README lacks the sections spec 28 requires. The commands spot-checked are accurate today. | `README.md` | ran |
| PLAT-12 | low | Celery results are stored but never read; CI installs unpinned ranges; Starlette warns that the TestClient transport is deprecated. | `celery_app.py:44, 52`; `pyproject.toml:9-33` | ran |

---

## 5. Planned changes and target data architecture

### 5.1 Changes per spec phase

The implementation is split into a foundation step and 8 parallel packages (keys in brackets).
The foundation contracts (schemas, settings, migration, shared helpers, file splits) are written
first so that no two packages edit the same file.

**Phase 2: map repair** [`map-shell`, `map-flow`]
- New `frontend/lib/map-bounds.ts`: latitude band `[-78.5, 84]` (measured wave extent),
  longitude `[-1e5, 1e5]`, `minZoomFor(w, h) = max(1, log2(w/256), log2(h/(256 x 0.8347)))`,
  `nearestCopy`, `wrapCentre`. Rationale: MAPNAV-1, -2. The prototype measured 0 blank rows.
- `forecast-map.tsx`: remove `worldCopyJump`; add `maxBounds`, `maxBoundsViscosity 1`,
  `bounceAtZoomLimits false`; recompute `minZoom` on resize; memoised `SpotMarker` in the nearest
  world copy; `flyTo` to the nearest copy, only when the selected slug changes, offset above the
  phone sheet; re-centre silently when the centre longitude exceeds 540 deg. Rationale:
  MAPNAV-3, -4, -6, -7, -8, -9, -11.
- `surf-map.tsx`: same bounds (MAPNAV-10). `e2e` helpers read the live zoom (MAPNAV-14).
- `basemap-draw.ts`: delete `closePath()` (MAPNAV-5; owned by `map-flow`, which owns the canvas
  layers). Optional: derive land from countries (MAPNAV-12).
- The best-spots overlay at one-world zoom is an owner decision (MAPNAV-13).

**Phase 3: weather API architecture** [`point-data`, `map-fields`]
- Spot point forecasts move to Open-Meteo, as two families. `open_meteo_gfs` (family `ncep_gfs`):
  wave model per latitude (`ncep_gfswave016` inside about 52.5N-15S, `ncep_gfswave025`
  elsewhere), `cell_selection=sea`, tertiary swell, peak periods, absent partitions stored as
  None, native steps only (hourly to f120, then 3-hourly) or flagged `interpolated`, run identity
  from `meta.json` with the 10-minute availability rule, per-model init times (HRRR and GFS).
  `open_meteo_ecmwf` (family `ecmwf_ifs`): `ecmwf_wam025` (Tp from `wave_peak_period`) and
  `ecmwf_ifs025` wind. Rationale: DA-1, DA-5, API-03 to API-08, API-13.
- Quotas: `QuotaExceeded` handled as a provider error; Open-Meteo calls weighted by locations,
  variables, days and models; minute/hour/day/month budgets in Redis; any exception ends the run
  FAILED and the next provider still runs (DA-6, API-10).
- NOAA and ECMWF direct ingesters stay as optional, key-free fallback and verification sources,
  trimmed: ECMWF fetches only `swh..mwp` and `wmb` once (DA-3); GEFS reads HTSGW and PERPW only
  (DA-4); ECMWF bytes recorded in `run.details` (DA-10).
- Windy: fix `wwaves_*` keys; refuse `windy` unless `WINDY_TERMS_ACKNOWLEDGED=true`; remove it
  from recommended setups (API-01, -02).
- Map fields decoupled from the spot provider: automatic standalone mode when no GRIB spot
  provider is configured; GFS atmosphere from `pgrb2.0p50` (0.86 MB per hour for U+V+PRMSL);
  tiered cadence (3-hourly to 120 h, then 6-hourly: 61 map hours); configurable cycles; a per-run
  byte budget; `run` query parameter honoured; `updated_at`/`age_hours`/`stale` in the index
  (DA-8, WINDVIZ-3, -4).
- Storage bounds: a lean prediction archive (confidence_inputs digest), predictions of superseded
  member runs pruned, a default database size cap, `OBSERVATION_RETENTION_DAYS` separate from the
  archive (DA-7, OBS-TIDE-8, PLAT-7; retention values need owner sign-off).
- Caching and workers: a bounded response cache for API providers (TTL = model update interval,
  bodies over 2 MB refused, an application-side key cap); Redis `maxmemory` with `noeviction`
  kept and a health check on memory; `expires` on beat entries; a `bulk` queue for downloads;
  real retries for transient acquisition failures; per-provider `started_at`; `GribFetcher`
  refuses 200 to a Range request (DA-9, -11, -12, PLAT-1, -2, -3, -8).
- Docs: new `docs/API_SETUP_REQUIRED.md`; updated `FORECAST_SOURCES.md` and `operations.md`
  (API-14).

**Phase 4: offshore wind physics** [`wind-scoring`]
- New `backend/app/services/surf_quality/wind.py`:
  `V_off = V.n` with `n` the seaward unit normal at bearing `offshore_wind_direction + 180`
  (never `beta`, SW-8), `V_cross = abs(V x n)`, and a continuous
  `Q_wind = Q_chop(V_on, V_cross) x Q_strong(V_off)` with spot tolerance and shelter and no floor.
  The six `wind_relation` strings stay (String(16) column, matching). Separate fields
  `wind_strength` (Beaufort-based: glassy, light, moderate, strong) and `wind_favourability`.
  Parameters labelled research, Beaufort or heuristic. Rationale: SW-1, -2, -4, -9.
- Optional per-spot `wind_profile` (tolerance, shelter sectors, extra offshore sectors), filled
  only from directly read research claims (9 spots), validated by the seed and the research lint
  (SW-7).
- Gusts scored only with source-aware definitions, or display-only (owner decision; SW-6).
- `ALGORITHM_VERSION` 2.0.0 to 2.1.0 so the live runs are rescored.

**Phase 5: wind animation** [`map-flow`]
- An 8-bit-safe trail renderer when the canvas is not `float16` (WINDVIZ-1); time-based animation
  using the frame timestamp (WINDVIZ-2, WAVEVIZ-8).
- Index refetch every 5-10 minutes and on focus; served `valid_time`/`run_key` checked against
  the request; lead time ("GFS 18Z Oct 11 · +6 h"), analysis label and a stale badge
  (WINDVIZ-3, -4, -6; the map timeline is in `map-shell`).
- Wind over land visible on the Wind layer (translucent land above the field; owner decision),
  an independent wind-particle overlay, mph, documentation, v-sign unit tests (WINDVIZ-5 to -8).
- `setFlow` without reseeding during playback; colour and readout from the same interpolation
  (WINDVIZ-10, -11).

**Phase 6: wave animation** [`map-fields`, `map-flow`]
- Backend: 9 optional partition variables (`swell1_*`, `swell2_*`, `windsea_*` for height,
  period, direction), completeness defined on the 6 core variables only, a mixing guard
  (resultant below 0.7 means nodata, plus a present-fraction rule and a joint mask),
  `MAP_FIELD_PARTITIONS=auto|on|off` (WAVEVIZ-3, -5, -6).
- Frontend: a pure `lib/map-waves.ts` (FROM to TOWARD; `L = gT^2/(2 pi)`; phase speed
  `c = gT/(2 pi)`; group speed `c/2`; spacing `S x L` with `S = 0.12 px/m` clamped to 8-64 px;
  exaggeration shown per zoom, e.g. 4,696x at zoom 2 on the equator; time scale 4x;
  height-driven line width and opacity; per-system colour and dash) and a Canvas 2D
  `WavefrontAnimator` on the existing `CanvasLayer`, with an ocean mask (data validity AND NOT
  Natural Earth land) on both basemaps, a static-crest reduced-motion mode and draw budgets.
  Wind keeps particles. `FieldStore` capacity 24 to 48 (WAVEVIZ-1, -2, -4, -7).

**Phase 7: chart redesign** [`spot-timeline`, data from `tides-obs`]
- An additive `GET /api/spots/{slug}/timeline?days=10` contract (see 5.3):
  native steps with segment metadata, DST-aware local days with sunrise/sunset from `sun.py`,
  typed partitions with `drives_surf`, wind with gust, gust kind, strength and favourability,
  a tide block that is real or explicitly unavailable, an observations block, typed provenance
  with member init times, server `generated_at`, `Cache-Control` and `ETag`.
- A custom-SVG `ForecastTimeline` with one time scale and one gutter: header (day names, local
  dates, 12 AM / 6 AM / 12 PM / 6 PM ticks generated from the axis, now marker), quality strip
  (label text, not colour alone), wave bars (central value with p10-p90 whiskers and a sets
  tick), swell partitions, wind bars with arrows (one documented convention: arrows point where
  the wind travels, matching the map), and tide. Recharts stays for model comparison and
  accuracy. An accessible ordinal palette. Rationale: CH-1, -3, -6, -7, -9, -10, -13 to -16.
- Tide backend: CO-OPS for the 8 US spots (harmonic stations hourly plus high/low; subordinate
  stations high/low with a flagged interpolated curve), EOT20 or FES2022 constants evaluated
  locally for the other 42, a `tide_stations`/`tide_predictions` schema with datum and source,
  and "Tide unavailable" where nothing is configured (OBS-TIDE-1, -2, -6).

**Phase 8: chart synchronisation** [`spot-timeline`]
- One selection store (React context plus `useSyncExternalStore`): selected and hover time,
  `?t=` in the URL, only the cursor layer and the summary re-render on hover; arrow keys move
  one API step; the map panel link passes `?t=` (CH-2, -4, -12, -20).

**Phase 9: frontend simplification** [`confidence-text`]
- Split reason lists (`forecast_confidence_reasons`, `wind_dispute_note`); "Forecast · 6 pm
  (in 2 h)" instead of "Now", from the nearest step; model-run age instead of "about N ahead";
  freshness on the map, events, opportunities and landing; a truthful stale banner; blend member
  names and init times; local confidence on cards when lower; the legacy index relabelled;
  duration-weighted good hours; "Likely range (unverified)"; a concise first screen with
  expandables; zone-named times; factual copy fixes; one-spot `spot_detail`
  (CT-1 to CT-17, CH-5, CH-17, CH-19, CH-21).

**Phase 10: forecast model integration** [`wind-scoring`, `tides-obs`, `point-data`]
- Swell direction and period scored over all partitions, weighted by breaking energy flux
  `w_i = Hb_i^2.5 / sum(Hb_j^2.5)`; a continuous taper outside the swell window
  (`exp(-2 (d/15)^2)`); tertiary swell in consistency; continuous spreading (SW-3, -5, -14, -15).
- Tide stage from prediction events (0 at the preceding low, 1 at the next high, plus rising or
  falling), scored only when the tide research class is Moderate or High, the decision is not
  disputed and the preference is not null; otherwise omitted and explained (OBS-TIDE-3, -4).
- Stored `wind_detail`, `weights_used` and `contributions` for "Why this forecast?" (SW-11).
- Blend: absent partitions stay None; gust definition recorded per member (API-03, -11).

**Phase 11: testing and debugging** [all packages, then lead]
- Section 6. Each package keeps the baselines green; the lead runs the full regression and e2e
  suites after merging, and rescoring is validated on a re-seeded copy of the real run.

**Phase 12: documentation** [lead, with package docs]
- `README.md` sections required by spec 28, with every command re-run; `docs/API_SETUP_REQUIRED.md`;
  `docs/forecast-charts.md`, `docs/map-rendering.md`, `docs/tides.md`; updates to
  `SURF_SCORING.md`, `map-layers.md`, `observations.md`, `operations.md`,
  `forecast-confidence.md`, `FORECAST_SOURCES.md`.

### 5.2 Target data architecture

```
Remote sources
  Open-Meteo Marine + Forecast (point JSON, up to 50 spots per request, per family)
  NOAA NODD byte ranges (map fields: pgrb2.0p50 U/V/PRMSL + gfswave global.0p25 HTSGW/PERPW/DIRPW
                         [+ 9 partition records when enabled])
  NOAA NODD GEFS-Wave mean/spread (HTSGW, PERPW only)
  NOAA CO-OPS (US tides) + committed harmonic constants file (other spots, no runtime download)
  NDBC 5day2 / AODN / Met Office (observations)
  [fallback, off by default] NOAA GFS / ECMWF direct GRIB
        |
Provider adapters      ForecastProvider (per family), map FieldSource, TideProvider, ObservationProvider
        |
Normalisation          UTC; directions FROM; km/h, m, s; period_kind; None for absent partitions;
                       cell provenance; interpolated / native_step_seconds; gust_kind
        |
Bounded cache          Postgres run_key dedup (no re-download of an ingested run);
                       Redis response cache (TTL = model update interval, <= 2 MB per body, key cap);
                       browser FieldStore LRU (48 fields, 64 MB) and TanStack Query
        |
Prediction             blend -> surf_quality.predict (wind.py, partition-weighted, tide stage)
                       -> forecast/local confidence
        |
Database               wave_forecasts, surf_quality_predictions (superseded members pruned),
                       map_fields (2 runs), tide_predictions (future + 2 days),
                       prediction_archive (digest), observations (QC'd history kept)
        |
API                    /api/spots/{slug}/timeline, /tides, /observations, /forecast (kept),
                       /api/map/fields (+freshness), /api/map/conditions
        |
UI                     map shell + flow layers (particles, wavefronts), spot timeline, alerts
```

**Expected bytes per run and per day, before and after.** "Measured" values come from the runs
and `.idx` files above; "computed" values are arithmetic on measured sizes; "estimate" values are
extrapolated and must be re-measured after the change.

| Stream | Today | After | Basis |
|---|---|---|---|
| NOAA GFS + GFS-Wave direct (spots + map) | 1,913.8 MB per run x 4 = about 7.66 GB/day (measured) | 0 by default (fallback only) | |
| ECMWF direct | about 0.85-0.9 GB per run x 2 = about 1.78 GB/day (computed) | 0 by default; about 0.48 GB per run if kept, trimmed (computed) | `.index` offsets |
| GEFS-Wave | 201.6 MB per run x 4 = about 0.81 GB/day (measured) | about 65 MB per run (computed: HTSGW+PERPW mean/spread 1.60 MB x 41 steps); 0.13 GB/day at 2 cycles, 0.26 at 4 | `.idx` |
| Open-Meteo GFS family (50 spots) | not used | about 0.5 MB per cycle (estimate) | 5.2 kB marine + 2.9 kB atmosphere per location, measured on 2 locations |
| Open-Meteo ECMWF family (50 spots) | not used | about 0.24 MB per cycle (estimate) | 2.7 + 2.1 kB per location, measured on 2 locations |
| Map fields, base (wind, pressure, total sea) | 0 extra in `with_noaa` mode (read from the 1.9 GB) | 0.86 + 1.82 = 2.68 MB per map hour; 61 hours = about 163 MB per cycle; about 0.33 GB/day at 2 cycles (computed) | `.idx` |
| Map fields, with primary, secondary swell and wind sea | not available | + 5.13-5.28 MB per map hour; about 7.8 MB per hour; about 0.48 GB per cycle; about 0.96 GB/day at 2 cycles (computed) | `.idx` |
| NDBC observations (if enabled) | about 5.6 MB per hourly run, about 134 MB/day (estimate) | about 15 MB/day with `5day2` (computed from the 8.9x ratio) | |
| Tides | none | CO-OPS: a few small JSON requests per day for at most 8 stations; harmonic model: 0 bytes at runtime | |
| **Total** | **about 10.2 GB/day** | **about 0.5 GB/day base (map at 2 cycles + GEFS at 2 cycles + points + NDBC); about 1.1 GB/day with partition layers** | computed |

**Storage after the change** (computed from measured row and field sizes).

| Item | Today | After |
|---|---|---|
| `map_fields` | 39 MB per run, about 80 MB for 2 runs | + 57.1 MB per run with partitions at 0.5 deg (about 192 MB for 2 runs), or + 18.6 MB per run at 1.0 deg |
| `surf_quality_predictions` per GFS-family run | 4,050 rows (81 steps), 8.5 MB | 50 x 161 rows (hourly to f120, then 3-hourly) = 8,050 rows, about 17 MB (computed); bounded by pruning superseded member runs |
| `prediction_archive` | + 41.5 MB/day, about 30 GB at 730 days | about 10 MB/day with a digest instead of full confidence inputs (estimate from row composition; measure after) |
| `tide_predictions` | none | 50 tide points x (432 hourly values + about 72 events) for 16 future + 2 past days; about 25,000 rows (computed), pruned daily |
| Redis | about 1 MB used, no cap | cap 256 MB (owner), `noeviction`, response cache of a few MB |

### 5.3 Implementation packages and shared contracts

Work is split so that every file has exactly one owner. A foundation step, done first on the main
branch, writes the shared contracts: settings and env vars, migration `0006_forecast_upgrade`,
the tide models, all additive schema fields, the provider-interface additions, shared helpers,
and three mechanical file splits (map canvas layers out of `forecast-map.tsx`, a
`useMapLayerFields` hook out of `app/map/page.tsx`, and `e2e/map.spec.ts` split into
navigation and layer specs). Stubs for the new tide, observation and timeline routes return an
explicit "unavailable" state until their package fills them.

| Package | Scope | Main findings |
|---|---|---|
| `map-shell` | Map container, bounds, zoom, markers, flyTo, map page, spot panel, map timeline and freshness | MAPNAV-1..4, 6..11, 13, 14; CT-14 (map) |
| `map-flow` | Canvas layers, wind particles, wavefront renderer, field client and cache, legend and settings, basemap drawing | WINDVIZ-1..8, 10..12; WAVEVIZ-1, 2, 4, 7, 8; MAPNAV-5, 12 |
| `map-fields` | Backend gridded fields: partitions, mixing guard, standalone mode, 0.5 deg atmosphere, cadence, byte budget, `run` parameter, index freshness | DA-2, DA-8; WAVEVIZ-3, 5, 6, 9, 10; WINDVIZ-4 (server side) |
| `point-data` | Open-Meteo family adapters, quotas, Windy gate, trimmed ECMWF/GEFS, blend, storage bounds, cache, Redis, queues, retries, data health, API docs | DA-1, 3..7, 9..13; API-01..14; PLAT-1..3, 7, 8, 12 |
| `wind-scoring` | `wind.py`, partition-weighted scoring, window taper, tide gating, wind profiles, matching wind rules, version bump | SW-1..9, 11, 12, 14, 15; OBS-TIDE-3, 4 (scoring side) |
| `tides-obs` | Tide providers (CO-OPS, harmonic model), tide stage, tide and observation endpoints, NDBC `5day2` | OBS-TIDE-1, 2, 6, 7, 9; CT-15 |
| `spot-timeline` | `/timeline` API, `ForecastTimeline` rows, selection store, spot page layout, palette, chart docs | CH-1..4, 6..16, 18..20, 22 |
| `confidence-text` | Reason split, "Now" card, freshness fields, stale banner, provenance labels, opportunity confidence, text and copy, `spot_views` fixes | CT-1..17; CH-5, 17, 21; PLAT-4 |

**Timeline contract** (`GET /api/spots/{slug}/timeline?days=10`, `SpotTimelineOut`):
`spot_slug`, `timezone`, `generated_at` (server now), `available`, `message`, `stale`;
`units {breaking: "ft", offshore: "m", wind: "km/h"}`; `steps_meta {segments: [{start, end,
step_hours}], interpolation: "none", policy: "nearest-valid-step"}`; `provenance {source,
run {run_key, init_time, completed_at, status, age_hours, stale_after_hours}, members [{code,
name, family, run_key, init_time, wave_model, atmosphere_model}], breaking_method,
algorithm_version, calibrated}`; `days [{date, start_utc, end_utc, sunrise, sunset, first_light,
last_light, polar_state, best_time, best_score, best_label, good_hours, forecast_confidence_class}]`;
`steps [{t, step_hours, lead_hours, is_daylight, interpolated, quality {...}, breaking
{central_ft, typical_ft, sets_ft, p10_ft, p90_ft, uncertainty_kind}, offshore {...},
partitions [{kind, height_m, period_s, dir_from_deg, breaking_ft, drives_surf}], wind {speed_kmh,
gust_kmh, gust_kind, dir_from_deg, relation, strength, favourability, fit,
offshore_component_kmh}, tide {height_m, rising}, why {...}}]`; `tide` (status available,
unavailable or demo, with station, source, datum, curve, events and preference);
`observations` (status and items). Only partitions that exist are listed; model values are never
interpolated. The full field types are part of the foundation contract.

---

## 6. Test requirements (spec section 27)

Existing suites stay green after every package (section 4.11 baseline). New tests are listed by
spec item; the package that writes each test is in brackets.

### 6.1 Map

| Spec item | Test | Package |
|---|---|---|
| Bounds | `tests/map-bounds.test.ts`: band inside Mercator, `BAND_HEIGHT = 0.8347`, every spot inside the band with at least 25 deg margin. `e2e/map-navigation.spec.ts` at 1440x900, 1920x1080, 1280x720: 3 drags north and 4 south leave at most 2 px outside 84N/78.5S. | map-shell |
| Zoom | `minZoomFor` at 8 sizes (one limit tight); e2e initial zoom equals `minZoomFor(container)`, view width at most one world; 6 wheel zoom-outs keep zoom at or above `minZoom` with 0 blank rows. | map-shell |
| Mobile navigation | e2e phone 390x844 and landscape 844x390 (hasTouch, CDP touch drags and pinches): same bounds; `minZoom` follows rotation; selected marker above the sheet; controls do not intersect. | map-shell |
| Markers | e2e 50 of 50 markers on screen at load and after two 900 px inertial drags; 5 timeline steps cause 0 `setLatLng` and at most changed-marker `setStyle`; 0 marker updates for a zoom click; a filter round trip does not move the map. | map-shell |
| Date line | e2e centre 179E and 170W at zoom 3: 38 of 38 markers; Cloudbreak to Pipeline flies less than 40 deg; Pipeline marker within 2 px of its nearest copy during a 25-frame drag; `worldCopyJump === false`. | map-shell |
| Performance | vitest: `drawBasemap` issues 0 `closePath`; e2e `data-draw-ms` below 30 at zoom 2 on 1440x900. | map-flow |

### 6.2 API

| Spec item | Test | Package |
|---|---|---|
| Variables | MockTransport fixtures recorded from the live captures: per-latitude wave model routing (016 vs 025, no mixed-domain batch); tertiary swell and peak periods mapped; ECMWF `period_kind = tp` from `wave_peak_period`. | point-data |
| Units | km/h wind, metres, seconds; mph conversion on the frontend (`lib/units.ts` tests). | point-data, foundation |
| Timestamps | run key from `meta.json`; availability rule (5 min ago keeps the previous run, 11 min ago takes the new one); before/after mismatch never mislabels a run; HRRR and GFS init times both recorded; per-provider `started_at`; `steps_meta.segments` derived from stored valid times. | point-data, spot-timeline |
| Cache | second identical fetch makes 0 HTTP requests; a 3 MB body is refused; map field `Cache-Control` public and `ETag`/304 on the timeline. | point-data, spot-timeline |
| Missing data | Open-Meteo 0/0/0 partition stored as None; `interpolated` flag on non-native hours; partitions listed only when stored; a 6-variable map run stays servable; `swell1_dir` 200 when stored, 404 when not. | point-data, map-fields |
| Errors | `QuotaExceeded` ends the run FAILED and the next provider still runs; a transient failure triggers `Task.retry`; HTTP 400 "No data is available for this location" falls back per spot; CO-OPS HTTP 200 with an error JSON raises; 200 to a Range request raises `GribError`. | point-data, tides-obs |
| Storage limits | map-field byte budget per run from recorded `.idx` fixtures; archive row below 400 B; superseded member predictions pruned; database size and Redis memory health checks; tide predictions pruned to future + 2 days. | point-data, map-fields, tides-obs |

### 6.3 Wind

| Spec item | Test | Package |
|---|---|---|
| Offshore/onshore/cross-shore detection | `tests/unit/test_wind.py`: west-facing beach (offshore 90): E gives `V.n = +V`, W gives `-V`, N and S give 0; signs of NE, SE, SW, NW. | wind-scoring |
| Direction conversion | explicit `u = -V sin(theta)`, `v = -V cos(theta)` for 72 directions with error below 1e-9; calm gives None for every provider (consolidated `wind_from_uv`). | foundation, wind-scoring |
| Speed scoring | Q = 1 for calm; light offshore at least 0.95; Q non-increasing in speed at every direction; strong offshore (50 km/h) at most 0.5; Q(100) < Q(60); continuity at most 0.02 per 0.5 km/h and per 1 deg; cross-shore 35 km/h on an ideal swell below Good; 60 km/h offshore below Very Good. | wind-scoring |
| Spot orientation | Kirra (E is not offshore), Snapper Rocks, Uluwatu, Teahupoo, J-Bay, Chicama (`beta + 180` is not offshore); all 50 spots: `V.n = +V` at `offshore_wind_direction`. | wind-scoring |
| Map wind | particle velocity and step for N/E/S/W/SW/WNW at latitudes 0/45/60/75 and zooms 2/6 (bearing error below 0.05 deg; the test fails if the v sign flips); 60 vs 120 Hz travel the same distance; 8-bit trail renderer leaves 0 residual alpha; Firefox and WebKit e2e veil test. | map-flow |

### 6.4 Wave visualization

| Spec item | Test | Package |
|---|---|---|
| Swell direction | `toward(225) = 45`, `propagation(225) = (0.7071, -0.7071)`, `propagation(0) = (0, 1)`, NaN gives null; backend SWDIR 225 stored and served as 225. | map-flow, map-fields |
| Wavefront orientation | crest tangent perpendicular to propagation for 0-359 deg; recorded segments vertical for FROM 270 and along (0.707, 0.707) for FROM 225; e2e structure-tensor orientation 45 deg plus or minus 8. | map-flow |
| Animation consistency | positions at t and t + T/tau coincide; 60 vs 120 Hz equal at 1 s; geographic stability over a 100 px pan; e2e motion up-right by frame-difference correlation. | map-flow |
| Period and height | `crestSpacingPx(8/10/16) = 12.0/18.7/48.0`; clamps; passage interval T/tau; e2e spacing ratio 4.0 plus or minus 0.4 for 8 vs 16 s; width and alpha non-decreasing in Hs; Hs below 0.2 m not drawn; more ink for 4 m than 1 m. | map-flow |
| Multiple systems | distinct dash and colour per family; absent partition gives 0 crests; two orientation peaks for swell1 270 + swell2 180; chooser hidden without partitions. | map-flow |
| Ocean/land masking | NaN block and corner-spread guard (unit); e2e alpha 0 at land probes on Dark and Streets, including a point 30 km inland; backend mixing guard and present-fraction rule. | map-flow, map-fields |

### 6.5 Charts (spec items 1-15)

All in `frontend/tests/forecast-timeline.test.tsx`, `timeline-lib.test.ts`,
`timeline-selection.test.tsx`, `selected-summary.test.tsx`, `quality-palette.test.ts` (vitest),
`frontend/e2e/timeline.spec.ts` (Playwright, desktop and Pixel 7), and backend
`tests/unit/test_timeline.py` and `tests/integration/test_timeline_api.py`. Fixtures: a 3-hourly
Africa/Johannesburg payload, a mixed 3 h/6 h ECMWF-shaped payload, a Europe/Lisbon DST payload, a
tide-unavailable payload. Package: spot-timeline.

| # | Spec item | Assertion |
|---|---|---|
| 1 | All rows load | `timeline-header`, `quality-strip`, `wave-bars`, `swell-partitions`, `wind-row`, `tide-row` present (vitest and e2e); `tide-row` shows "Tide data unavailable" when unavailable |
| 2 | Hourly labels correct | For 2026-10-12T00Z..14T00Z in Africa/Johannesburg, ticks at 22:00Z "12 AM", 04:00Z "6 AM", 10:00Z "12 PM", 16:00Z "6 PM"; also Pacific/Honolulu and Asia/Kathmandu; ticks independent of sample times |
| 3 | Day boundaries align | one scale for every row; e2e `boundingBox().x` of `[data-day-boundary]` equal within 1 px in all rows at 1280 px and on Pixel 7 |
| 4 | Selecting a time updates all charts | click a bar: summary time, `aria-valuenow`, `data-selected` on the matching segment, bar and partition cell, tide marker x, one cursor x; ArrowRight moves exactly one API step |
| 5 | Wave height matches backend | summary shows `typical_ft`-`sets_ft`; bar `data-central` equals `central_ft`; whiskers equal p10/p90 |
| 6 | Wind speed matches backend | `data-speed`, `data-gust`; summary "N km/h DIR · relation"; null gust shows no tick |
| 7 | Wind arrows correct | `arrowRotationDeg(0) = 180`, `(90) = 270`, `(225) = 45`; legend states the convention; cross-check with the map drift direction for the same (u, v); relation from the API only |
| 8 | Tide aligned with timestamps | curve points and high/low markers at `scale(t)`; spot-local labels with datum; unavailable state renders text and no path |
| 9 | Quality uses the correct model | pytest: step score and label equal the authoritative run's `SurfQualityPrediction`; vitest: changing heights in the fixture does not change the strip |
| 10 | Tooltips accurate | hover and keyboard focus show the same local time, fixture values, gust line and step-length note |
| 11 | Mobile scrolling | Pixel 7: scroller `scrollWidth > clientWidth`, swipe reveals the next day, tap selects, no document-level horizontal scroll |
| 12 | Day/night shading | bands span exactly `[last_light(d), first_light(d+1)]` from the API; pytest sunrise/sunset equal `sun.sun_times` within 1 min, including polar fixtures |
| 13 | Local time-zone conversion | `TZ=America/New_York` still labels in the spot zone; Lisbon 2026-10-25 is a 25 h day; Pacific/Fiji date-line case |
| 14 | Missing data does not break the UI | null gust, no secondary or wind sea, tide unavailable, null wind step, empty steps with `available=false`, fetch error: gaps or "—", visible error state, no exception |
| 15 | Timestamps never fabricated | bar count equals API step count; no element at a time absent from the API; mixed segments give wider bars; `steps_meta.segments` derived from stored times |

Performance check: hover and selection re-render only the cursor layer and the summary (render
counter shows 0 row renders); the timeline payload is measured against 252,690 B for `/forecast`.

### 6.6 End-to-end (spec E2E list)

| # | Journey | Test | Package |
|---|---|---|---|
| 1 | Login | `full-flow.spec.ts`, `scenario.spec.ts` step 4 (existing) | kept |
| 2 | World map | `map.spec.ts` (50 markers) + `map-navigation.spec.ts` | map-shell |
| 3 | Select spot | `map.spec.ts:308`, scenario step 6 (existing, selectors updated) | map-shell |
| 4 | Detailed forecast | `timeline.spec.ts` rows load on `/spots/jeffreys-bay` | spot-timeline |
| 5 | Navigate days | `timeline.spec.ts`: day buttons scroll the timeline; `day-YYYY-MM-DD` keeps `aria-pressed` | spot-timeline |
| 6 | Select hour | `timeline.spec.ts`: click and keyboard select; all rows follow; `?t=` deep link | spot-timeline |
| 7 | Review waves | `timeline.spec.ts`: summary and bar values equal `/timeline` | spot-timeline |
| 8 | Wind direction | `timeline.spec.ts` arrows and relation; `map-layers.spec.ts` particle direction | spot-timeline, map-flow |
| 9 | Tide | `timeline.spec.ts`: tide row with the demo or mocked source; "Tide data unavailable" otherwise | spot-timeline |
| 10 | Forecast confidence | `confidence.spec.ts` (updated: forecast-only reasons) | confidence-text |
| 11 | Create saved search | `full-flow.spec.ts`, scenario step 8 (existing) | kept |
| 12 | Matching still operates | `full-flow.spec.ts`, scenario steps 10-12 (existing) | kept |
| 13 | Notifications in a test environment | `full-flow.spec.ts` ("captured"), scenario step 13 (dev inbox) (existing) | kept |

The e2e suite runs on demo forecasts, mock fares and synthetic map fields. A pass says nothing
about live providers, live tides or live map fields.

### 6.7 Regression

| Area | Suites that must stay green |
|---|---|
| Auth | `tests/integration/test_auth_api.py` (18) |
| Email verification | `test_auth_api.py`, `test_email_delivery.py` (9), scenario steps 2-3 |
| Saved searches | `test_account_and_searches_api.py` (7), `test_pipeline.py` pause/delete, e2e wizard |
| Flight search | `tests/unit/test_flight_providers.py` (13), `test_travel_windows_and_filtering.py` (17), `test_pipeline.py` flight cases |
| Background scheduling | `test_workers.py` (6) plus routing and retry tests, `test_data_health.py` (22), `tests/unit/test_ops_alerts.py` (28) |
| Notification delivery | `test_pipeline.py` (caps, retries, crash mid-send), `test_notification_providers.py` (29, updated rows), `test_delivery_events.py` (10) |
| Migrations | `test_maintenance_and_migrations.py::test_migrations_upgrade_downgrade_and_match_models` with 0006; CI `alembic upgrade head` and `alembic check` |
| Physics | re-run the scratch harnesses after any change to `fields.py`, `noaa.py` or `map-fields.ts`: GRIB vs served within 0.25 m/s, NDBC median direction difference about 10 deg and no 180 deg flip |
| Scoring | rescore a re-seeded copy of the real blend run; report the label-change count; acceptance: 0 offshore hours at 45 km/h or more labelled Very Good or better, 0 cross-shore hours at 30 km/h or more labelled Good or better |

External integrations (Duffel, Amadeus, Resend, SendGrid, Twilio, Open-Meteo commercial
endpoint, any keyed API) are tested with mocks only unless the owner supplies credentials. They
are not to be reported as tested.

---

## 7. Risks, owner inputs and open questions

### 7.1 Risks

- **Score and label changes.** The wind prototype changed 737 of 4,050 labels on the real run,
  and partition-weighted direction alone changes 257 (the overlap was not measured). Events, matches and alert emails will change
  after rescoring. Every new wind parameter is uncalibrated (no local observations exist), so
  each must be labelled heuristic in code, API and docs.
- **Single vendor for point data.** Open-Meteo's free tier is non-commercial, its terms can
  change, and the Marine docs contradict themselves on horizon. The direct GRIB providers stay as
  tested fallbacks. Open-Meteo picks its own grid cell (3 of 32 spots differed); returned cells
  are logged and compared with the NOAA cells.
- **Gridded layers stay on GRIB.** About 0.33 GB/day base, about 0.96 GB/day with partition
  layers (computed). GFS v17 may rename files or drop the 0.5 deg and 1 deg atmosphere files, so
  the template is configurable with a fallback to the 0.25 deg records.
- **Partitions are per cell.** WW3 numbers swell partitions by height in each cell, so families
  can jump between neighbours and steps; the guard reduces this but cannot remove it, and masks
  about 10.6 % of swell2 nodes.
- **Exaggerated wave scale.** Crest spacing is up to 4,696 times the true wavelength at zoom 2,
  and the time scale is 4x; the legend must state both.
- **Browser coverage.** Firefox, Safari and low-end phones were not measured for the canvas
  renderers. Adding Firefox and WebKit Playwright projects needs browsers in CI.
- **Tide accuracy.** Global harmonic models degrade in estuaries and behind reefs (Mundaka, Fiji
  passes, Bocas del Toro). Results must be labelled "model tide, relative to model mean sea
  level", never a chart datum. Datums differ by source (MLLW is 0.91 m above MSL at Pillar Point),
  so sources are never mixed in one curve.
- **Contract changes.** Many e2e tests pin spot-page test IDs and texts; they are updated in the
  same change, never deleted. New API fields are additive and `confidence_reasons` stays.
- **Workers.** A queue nobody consumes stalls silently; the default `WORKER_QUEUES` covers every
  queue, and a routing test checks it. A full Redis with `noeviction` makes broker writes fail,
  so the cache stays small and memory is alerted at 80 %.
- **Retention changes delete data permanently** and need owner sign-off and a fresh backup.
- **Map navigation.** Without `worldCopyJump` the centre longitude grows on endless panning; the
  silent re-centre above 540 deg is not prototyped. A fractional minimum zoom (for example 2.49)
  slightly softens raster tiles at that level.
- **Integration tests drop the schema** of `TEST_DATABASE_URL`; the name guard comes first, in
  the foundation.

### 7.2 Owner inputs needed

**Accounts and credentials.**

| Input | Why | Env vars |
|---|---|---|
| Is the app commercial (ads, subscriptions, paid tiers, a company product)? If yes, an Open-Meteo paid plan (Standard covers Forecast and Marine; Professional adds Ensemble). Confirm the price at checkout. | Open-Meteo free tier is non-commercial only | `OPEN_METEO_API_KEY` |
| Approve deactivating Windy (recommended), or legal review plus a 990 EUR/year key | Terms forbid storage and derived databases | `WINDY_API_KEY`, `WINDY_TERMS_ACKNOWLEDGED` |
| Tide model: EOT20 (CC BY, no account) or FES2022 (free AVISO account, used once by a developer) | Global tides for 42 non-US spots | `AVISO_USERNAME`, `AVISO_PASSWORD` (one-off script only) |
| CO-OPS `application` identifier (no key) | Required for NOAA's logs | `NOAA_COOPS_APPLICATION` |
| Optional: UKHO Discovery key (Thurso), Copernicus Marine account (MFWAM), NWS User-Agent contact | Optional sources | `UKHO_*` (to define), `COPERNICUSMARINE_SERVICE_USERNAME`/`_PASSWORD`, `NWS_USER_AGENT` |
| Optional: credentials for a live regression of flights, email and SMS | Otherwise mocks only | `DUFFEL_ACCESS_TOKEN` or `AMADEUS_*`, `RESEND_API_KEY`/`SENDGRID_API_KEY`/`SMTP_*`, `TWILIO_*` |
| Optional: remote storage for long ML history | Spec 5 prefers remote | bucket and credentials (to define) |

No personal email address will be used as a User-Agent or contact unless the owner provides one
for that purpose.

**Decisions.**

- Map: minimum zoom at exactly one world width (recommended) or whole band visible; initial
  centre; whether the desktop best-spots list starts collapsed below about 1,700 px.
- Map data budget: horizon and cadence (recommended 3-hourly to 120 h, then 6-hourly to 240 h,
  2 cycles/day); partition layers (primary, secondary, wind sea) at 0.5 deg (+57.1 MB per run)
  or 1.0 deg (+18.6 MB); no tertiary swell; `MAP_FIELD_PARTITIONS`.
- Whether NOAA and ECMWF direct providers remain as fallbacks (off by default) or are removed
  from the default setup; whether GEFS stays enabled and at 2 or 4 cycles.
- Retention: `RETENTION_FORECAST_DAYS` below 10; archive 730 or 365 days; export.
- Redis cap (suggested 256 MB, alert at 80 %); a second worker container for the bulk queue.
- Map freshness threshold (for example 12 h) and how stale layers look.
- Wind over land visible on the Wind layer of the dark basemap.
- Default wind unit and whether one setting applies to map and charts (a server-side preference
  needs a migration).
- Wind scoring defaults (offshore tolerance about 28 km/h, chop half-speed about 12-13 km/h,
  cross-shore weight 0.45-0.6); calm vs light offshore; whether matching's "offshore" excludes
  strong offshore and whether light onshore counts as "not onshore"; symmetric sectors; gusts
  scored or display-only; season stays out of the hourly score; who reviews per-spot wind
  profiles.
- Wavefront wording: "stylised, not individual waves", with the stated scale factors.
- Firefox and WebKit in CI.
- Time display: spot-local time with the zone name everywhere (recommended) or browser time with
  a toggle.
- Show buoy observations on spot pages; display of Met Office data (CC BY-SA share-alike).
- Matching's confidence component: relabel only, or switch to the forecast class (changes
  rankings).
- Rating precision: one decimal ("9.2/10", asserted in e2e) or integer ("9/10").
- Spot descriptions shortened to a first-sentence tagline, or curated taglines.
- SMS alerts with forecast confidence.
- The accessible quality palette (it also changes map markers).
- Default chart window on desktop and mobile; night steps dimmed or collapsed.
- Collecting in-app human surf reports (the table exists): moderation, privacy, labelling.
- Whether `/timeline` replaces `/forecast` for the main charts once stable.

### 7.3 Open questions

1. Blend wind direction: weighted unit vectors or speed-weighted vectors? A calm member currently
   pulls the direction as strongly as a strong one (`blend.py:351-358`).
2. Are SWPER/WVPER peak or mean periods? Label them "period" until the WW3 GRIB source is checked.
3. How Open-Meteo weights multi-location requests; budget conservatively until confirmed.
4. Will Open-Meteo enforce the documented 8-day marine limit? Response lengths are validated per
   model.
5. Storing hourly GFS-family steps to f120 doubles prediction rows per run compared with today's
   3-hourly runs (computed); is the finer timeline worth it, or should storage stay 3-hourly?
6. Accuracy of the harmonic tide model at estuary and reef spots, until compared with a gauge.
7. The "80 % range" covered 53 % offshore at 24 h in the project's own check; recalibration is
   out of scope here and stays labelled "unverified".
8. GFS v17 timing and file layout (not re-verified).
