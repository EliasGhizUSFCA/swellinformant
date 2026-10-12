# Map ocean layers (global model fields)

The map can draw global layers under the surf spots: significant wave height, peak wave
period, peak wave direction (particles), 10 m wind (speed and particles) and mean
sea-level pressure (isobars). The backend reads them from NOAA open data, shrinks them to
one byte per cell on a 0.5° grid and serves them as small compressed grids. The browser
decodes and draws them.

* Encoding (pure numpy): `backend/app/services/forecasts/fields.py`
* Production: `backend/app/services/forecasts/noaa.py` (`GfsPointReader` with a
  `FieldSink`, `NoaaGfsProvider`, `NoaaGfsFieldsProvider`) and
  `backend/app/services/forecasts/ingestion.py` (`store_map_fields`,
  `acquire_map_fields`)
* Read side: `backend/app/services/map_fields.py`, served by
  `backend/app/api/map_fields.py` under the contract in
  `backend/app/schemas/map_fields.py`
* Storage: table `map_fields` (`models.forecast_engine.MapField`), one row per run, valid
  time and variable
* Tests (synthetic GRIB2 files and arrays only): `backend/tests/unit/test_map_field_encoding.py`,
  `backend/tests/unit/test_noaa_provider.py` (map-field section),
  `backend/tests/integration/test_map_fields.py`

These are **model output, not observations**. They are **not for navigation** or safety
decisions. Wave height is the **offshore significant wave height** of the combined sea
state. It is not the breaking (surf) height at any beach.

## Sources, licence and attribution

| Layer | NOAA product | Record | Unit served |
|-------|--------------|--------|-------------|
| `hs` | GFS-Wave (WAVEWATCH III) `global.0p25` | `HTSGW:surface` | m (offshore Hs) |
| `tp` | GFS-Wave `global.0p25` | `PERPW:surface` | s (peak period) |
| `wave_dir` | GFS-Wave `global.0p25` | `DIRPW:surface` | degrees FROM |
| `wind_u`, `wind_v` | GFS `pgrb2.0p25` | `UGRD`/`VGRD:10 m above ground` | m/s, eastward and northward |
| `pressure` | GFS `pgrb2.0p25` | `PRMSL:mean sea level` | hPa (Pa / 100) |

* Files come from the NOAA Open Data Dissemination (NODD) bucket on AWS
  (`NOAA_GFS_BASE_URL`, the same layout and path templates as the spot ingester; see
  [noaa-direct-ingestion.md](noaa-direct-ingestion.md)). Only the needed records are
  downloaded, by HTTP `Range` from the offsets in each file's `.idx`.
* Attribution shown with the layers (the `source.attribution` of the index): "NOAA/NCEP
  Global Forecast System (GFS) and GFS-Wave (WAVEWATCH III), via the NOAA Open Data
  Dissemination (NODD) program."
* Licence: US government work, not subject to copyright in the US. NODD terms: data are
  open to the public and can be used as desired. NOAA requests attribution and that no
  endorsement by or affiliation with NOAA be stated or implied. The served grids are
  regridded, quantised and converted, so they must not be presented as original,
  unaltered NOAA data.
* **Why `global.0p25`.** It is the only GFS-Wave grid that covers the whole globe. Its
  geometry was decoded live (2026-10-11 12Z, f006): 1440 × 721 cells, 0.25°, rows from
  90N to 90S, columns from 0E to 359.75E, with a bitmap (HTSGW and PERPW: 470,147 of
  1,038,240 cells missing, land and sea ice; DIRPW: 470,163). HTSGW, PERPW and DIRPW are
  records 5 to 7 of 19, so they are one contiguous range (1.82 MB at f006). The three
  `pgrb2.0p25` records (PRMSL is record 1, UGRD/VGRD 10 m records 588 and 589 of 743) are
  on the same 1440 × 721 grid, in Pa and m/s. NOAA **interpolates** `global.0p25` from
  the native `gnh_10m` (1/6°, 52.5N to 15S) and `gsh_15m` (1/4°, 10.5S to 79.5S) grids,
  so it is a derived product. Spot forecasts keep using the native grids.
* **Direction convention.** DIRPW in the GRIB2 files is degrees FROM (WW3 `ww3_grib`
  writes `MOD(630 − θ, 360)`). It is stored as it is, without +180. Live at 5 spots, the
  served direction agreed with the spot provider's own DIRPW sample within 0.9° to 3.8°
  (table below). That sample was checked against NDBC station bulletins in
  noaa-direct-ingestion.md. An independent physical check on the served f006 fields: in
  the Southern Ocean westerlies (45S to 55S, all longitudes) the circular mean of
  `wave_dir` is 265° and that of the 10 m wind direction (from the served u, v) 283°;
  where the wind exceeds 7 m/s the median difference is 26°. Directions "going to" would
  put the waves near 85°. ecCodes calls PERPW "Primary wave mean period", but WW3 writes
  the peak period there (research notes), so it is labelled as the peak period.

## Modes and settings

`app.services.map_fields.map_fields_mode(settings)` decides whether fields are produced:

| `MAP_FIELDS_ENABLED` | `noaa_gfs` in `FORECAST_PROVIDERS` | Mode | What happens |
|----------------------|------------------------------------|------|--------------|
| unset (default) | yes | `with_noaa` | Every NOAA spot acquisition (`forecasts.acquire`) also stores the fields on its own run |
| `true` | yes | `with_noaa` | Same |
| unset | no | `off` | Nothing is fetched. The index says what to set |
| `true` | no | `standalone` | The `forecasts.map_fields` job reads only the fields, under the source `noaa_gfs_fields` (e.g. demo spot forecasts with real ocean layers) |
| `false` | any | `off` | Nothing is fetched |

| Setting | Default | Meaning |
|---------|---------|---------|
| `MAP_FIELD_RESOLUTION_DEG` | 0.5 | Output grid spacing. Must divide 180 (0.25, 0.5, 1, …). Otherwise (e.g. 0.7) a NOAA spot run stores no fields and says why in `details.map_fields.warnings`, and the standalone job fails with that message; spot forecasts are unaffected |
| `MAP_FIELD_MAX_LEAD_HOURS` | 240 | Last forecast hour with fields |
| `MAP_FIELD_STEP_HOURS` | 3 | Fields at the published forecast hours that are multiples of this |

The read side serves whatever is stored, whatever the mode. Retention removes old fields.

## HTTP contract

Fixed contract: `backend/app/schemas/map_fields.py`. Nothing was added to it.

* `GET /api/map/fields` returns the index (`FieldIndexOut`): `available`, a plain-language
  `message` when nothing is available, the `source` (code, name, attribution, `is_demo`),
  `run_key`, `cycle_time`, the `valid_times` at which **all six** variables exist, and the
  six `variables` with label, unit and display range. The run is the newest one (by model
  cycle) with at least one complete valid time. When several runs hold that cycle (for
  example the NOAA spot run and a fields-only run after a change of mode), the one with
  the most complete valid times is served, and on a tie the newer run
  (`map_fields.servable_runs`, the same order retention uses). Incomplete times are
  logged and left out.
  `Cache-Control: public, max-age=300`.
* `GET /api/map/fields/{variable}?at=ISO` returns one field as
  `application/octet-stream`. The decoding parameters are in the `X-Field-Meta` header
  (JSON with the keys of `FieldMeta`; `Access-Control-Expose-Headers: X-Field-Meta`),
  with `Cache-Control: public, max-age=3600`.
  * `at` defaults to now. A time without a zone is taken as UTC.
  * The served time is the complete valid time nearest to `at`. On a tie the earlier
    time wins.
  * The response is 404 when `at` is more than one field step (`MAP_FIELD_STEP_HOURS`)
    before the first or after the last valid time, or when nothing is stored.
  * An unknown variable is 422.
  * The index and a field request read only metadata columns, plus the bytes of the one
    row that is served.

**Bytes.** zlib (RFC 1950, `DecompressionStream("deflate")` in the browser) compressed
uint8, `nx × ny` cells, row-major. Row 0 is the northernmost latitude `lat0` = 90, and row
j is `lat0 − j·dlat`. Column i is `lon0 + i·dlon`, with `lon0` = −180, wrapping east. A
value is `offset + scale × byte`. Byte 255 means no data (land, ice or missing). At 0.5°,
`nx` = 720 and `ny` = 361, and the last row is 90S.

| Variable | Unit | Offset | Scale | Bytes 0..254 cover | Display range |
|----------|------|--------|-------|--------------------|---------------|
| `hs` | m | 0 | 0.1 | 0 to 25.4 m | 0 to 10 m |
| `tp` | s | 0 | 0.1 | 0 to 25.4 s | 0 to 20 s |
| `wave_dir` | deg FROM | 0 | 1.5 | 0 to 358.5° (bytes 0..239; 360° = byte 0) | 0 to 360 |
| `wind_u`, `wind_v` | m/s | −63.5 | 0.5 | −63.5 to 63.5 m/s | −30 to 30 m/s |
| `pressure` | hPa | 860 | 1.0 | 860 to 1114 hPa | 960 to 1045 hPa |

Values beyond a variable's byte range are clamped to 0 or 254. Directions wrap instead.
The decoded value is within half a quantisation step of the regridded value (tested).

## Grid and regridding

* **Nodes.** `lat = 90 − j·d` and `lon = −180 + i·d` for `i < 360/d`. The metadata's
  `lat0`, `lon0`, `dlat` and `dlon` give exactly the point where each value is centred.
* **Value at a node.** The area-weighted mean of the valid source cells in the node's
  `d × d` box. Each source cell counts as the `s × s` area around its grid point, and a
  cell that straddles the box edge counts with the fraction of it inside the box. From
  the 0.25° grids to 0.5°, the cell on the node has weight 1, its four edge neighbours
  1/2 each and the four corners 1/4 each. The mean is therefore centred exactly on the
  node. A plain "cells with lat in `[c − d/2, c + d/2)`" box would put it 0.125° off.
  Tests prove this: a single hot cell lands on its node, linear ramps come out unchanged,
  and the same holds end to end through GRIB2.
* **Nodata.** Masked or NaN cells are left out. A node is nodata when no valid cell
  overlaps its box. Coastal nodes average only their sea cells, so a coastal node can sit
  up to one box from the nearest valid cell.
* **Longitudes.** The GFS grids start at 0E. The 180W node's box takes cells on both
  sides of 180°.
* **Poles.** Boxes and cells are cut at ±90°. A pole row averages the source rows within
  d/2 of the pole, so its mean sits d/4 from the pole in latitude.
* **Directions.** Averaged as unit vectors, never as plain numbers (350° and 10° average
  to 0°). Where the vectors cancel, the node is nodata.
* **Wind** u and v are averaged as components. The client computes the speed from the
  averaged components, which is lower than the mean speed where the direction varies
  inside a box.
* **Weights** are plain lat × lon areas. cos(lat) varies by about `d × tan(lat)` inside a
  box (1.5 % at 60° for 0.5°), which is ignored.

## Forecast hours

Map hours are the published hours (hourly to f120, then 3-hourly to f384) up to
`MAP_FIELD_MAX_LEAD_HOURS` that are multiples of `MAP_FIELD_STEP_HOURS`. The default is
f000 to f240 every 3 h: 81 hours.

* **`with_noaa`.** Spot hours that are also map hours hand the U10, V10 and PRMSL fields
  the reader decodes anyway to the sink. When no spot uses `global.0p25`, the reader also
  fetches its HTSGW, PERPW and DIRPW, which are contiguous, so this is one index and one
  range request. When a spot does use that grid (the default spots north of 52.5N:
  Thurso, Mullaghmore), its 15-record range already contains the three, so nothing extra
  is downloaded. Map hours beyond the spots' hours (for example with `FORECAST_DAYS` < 10)
  are read for the fields alone: atmosphere index, PRMSL, UGRD and VGRD (never GUST or
  LAND), and the three wave records. `current_run` then also waits for the last map hour.
* **`standalone`.** `NoaaGfsFieldsProvider.current_run` uses the same availability rules
  as the spot provider (nominal cycle with `NOAA_AVAILABILITY_LAG_HOURS`, then up to 4
  older cycles). It only needs the `.idx` of the last map hour for the atmosphere file
  and `global.0p25`. Run key: `noaa-gfs-fields:YYYYMMDDHH`.
* **Memory.** Each decoded field (8 MB as float64) is regridded, quantised and compressed
  immediately, and only the compressed bytes are kept: about 0.48 MB per valid time, so
  about 39 MB for an 81-hour run until ingestion stores them. Encoding one field needs
  at most about 35 MB of temporary arrays (measured with tracemalloc on a synthetic
  1440 × 721 field: 24 MB for a scalar, 35 MB for a direction), freed at once; each
  download worker encodes one field at a time.
* **Failures.** A field that cannot be read or encoded is a warning in the run's
  `details.map_fields.warnings` (with one "incomplete" line per hour that lacks a
  variable). It is never a spot error and never a failed spot step. If the database
  refuses the fields of a NOAA spot run, they are rolled back to a SAVEPOINT and listed
  there as "map fields not stored"; the spot records are stored as usual.

## Storage, jobs and retention

* `store_map_fields` upserts on `uq_map_fields_key` (run, valid time, variable).
  Re-ingesting replaces rows in place and never duplicates them. The run's details get
  `{"map_fields": {"count", "bytes", "valid_times", "warnings"}}`.
* **Standalone runs** belong to the `ForecastSource` `noaa_gfs_fields` ("NOAA GFS-Wave +
  GFS global fields", kind `fields`, priority 1000). They have no spot records,
  `details.spot_ids = []`, `details.fields_only = true` and `predictions_computed_at`
  set (nothing to score). Spot forecasts, blending, comparisons, verification, the status
  page and the accuracy dashboard only read the configured providers' sources or ensemble
  sources, so these runs are never selected. An integration test checks that the spot
  API pages, run selection, status, predictions, archive and events are identical with
  and without a fields-only run.
* **Job.** The Celery task `forecasts.map_fields` is a no-op unless the mode is
  `standalone`. Beat runs it on the `forecast-acquisition` interval
  (`FORECAST_REFRESH_MINUTES`, every 3 h by default), and once when beat starts. It skips
  a cycle whose fields already exist: a fields run of that cycle that is SUCCESS or
  PARTIAL and read every requested map hour (`details.map_hours`), or rows of that cycle
  from any run (for example the NOAA spot run) covering every map hour and variable. A
  shorter run of the same cycle (for example a check with `--max-lead-hours 12`) does not
  count for the hours it did not read, so the scheduled job still reads the whole cycle
  and replaces its rows in place.
* **CLI.** `python -m app.cli engine map-fields [--cycle YYYYMMDDHH] [--max-lead-hours N]
  [--force]` works in every mode except `off`. It prints JSON with counts, stored and
  downloaded bytes, requests, rows pruned, the duration and per-hour figures.
* **Retention.** `maintenance.prune_map_fields` keeps the fields of the two best
  servable runs, in the read side's order (`map_fields.servable_runs`: newest cycle,
  then most complete valid times, then the newer run), so the served run is never
  deleted. Every other `map_fields` row goes, including rows of runs without a single
  complete valid time (they are never served). It runs right after every acquisition that
  stores fields (standalone job and NOAA spot run) and in `maintenance.cleanup` (reported
  as `map_fields`), so storage stays at about two runs between the daily cleanups. One
  DELETE statement picks the runs to keep and deletes the rest, so fields that another
  acquisition commits meanwhile are not deleted. A forecast run removed by its own
  retention takes its fields with it (FK cascade).

## Bandwidth and storage (measured live)

All figures were measured on 2026-10-11 between 20:42 and 20:50 UTC against the NODD
bucket, on a scratch database, for the newest complete cycle **2026-10-11 12Z**. Wall
times depend on the network (6 download workers, `GRIB_DOWNLOAD_WORKERS`).

**Standalone, 5 hours.** `python -m app.cli engine map-fields --max-lead-hours 12`
(f000 to f012 every 3 h): status `success`, 30 fields, 5 of 5 hours complete, 25 requests,
23.9 MB downloaded, 2.42 MB stored, 7.0 s for the job (8.8 s for the whole process). A
second call returned `skipped` ("already stored") without downloading anything.

| Hour | Downloaded | Requests | Wall time |
|------|------------|----------|-----------|
| f000 | 4.81 MB | 5 | 6.3 s |
| f003 | 4.79 MB | 5 | 5.2 s |
| f006 | 4.78 MB | 5 | 6.0 s |
| f009 | 4.77 MB | 5 | 5.4 s |
| f012 | 4.77 MB | 5 | 6.2 s |

Per hour: the atmosphere `.idx`, one range with PRMSL, one with UGRD/VGRD 10 m (2.93 MB
at f006), the `global.0p25` `.idx` and one range with the three wave records (1.82 MB).

**Standalone, the full 81 hours** (`--max-lead-hours 240 --force`, so measured rather than
extrapolated): status `success`, 486 fields, 81 of 81 hours complete, 405 requests,
**379.5 MB** downloaded (3.86 to 4.83 MB per hour, mean 4.69 MB), each hour 3.1 to 7.8 s
(median 4.5 s, 6 in parallel), **66.0 s** for the job (68.1 s for the process), peak
resident memory 662 MB (103 MB after the imports alone). Stored: **39.0 MB** for the run.

| Variable | Compressed bytes per field (mean of 81; min to max) |
|----------|-----------------------------------------------------|
| `hs` | 55.6 kB (53.7 to 56.7) |
| `tp` | 76.5 kB (75.4 to 78.1) |
| `wave_dir` | 99.3 kB (98.1 to 100.6) |
| `wind_u` | 100.8 kB (98.9 to 102.5) |
| `wind_v` | 99.5 kB (97.4 to 101.2) |
| `pressure` | 50.2 kB (47.7 to 52.2) |
| One valid time (all six) | 482 kB |

PostgreSQL reported 43 MB for the `map_fields` table holding these 486 rows and the 18
rows below (the bytes are already compressed). Retention keeps two runs: about 80 MB,
plus deleted row versions until autovacuum reclaims them.

**Re-measured by the review** on the next cycle, **2026-10-11 18Z** (2026-10-12 01:54 to
02:00 UTC, another scratch database):

* 5-hour CLI run: 30 fields, 25 requests, 23.95 MB downloaded, 2.42 MB stored, 3.9 s
  for the job (7.0 s for the process), peak 564 MB. A second call was skipped.
* Full run of the same cycle, started without `--force` after the 5-hour run: it read
  all 81 hours. 486 fields, 405 requests, 380.7 MB (3.97 to 4.84 MB per hour, 1.8 to
  5.2 s each), 39.04 MB stored, 45.2 s for the job (48.1 s for the process), peak 659 MB.
  Mean compressed bytes per field: hs 55.5 kB, tp 76.6, wave_dir 99.9, wind_u 100.6,
  wind_v 99.3, pressure 50.1 (482 kB per valid time). The table held 42 MB.
* Served f006 (valid 2026-10-12 00Z): hs nodata 42.6 % of nodes, 31.1 % of the area; hs
  node mean 2.22 m, p99 6.80 m, max 9.5 m; tp 1.1 to 24.9 s; 10 m wind max 31.8 m/s;
  pressure 955 to 1060 hPa. In the Southern Ocean (45S to 55S) the circular mean of
  `wave_dir` is 264° and that of the wind direction (FROM) 284°.
* Orientation, checked in the raw served bytes (row `(90 - lat) / 0.5`, column
  `(lon + 180) / 0.5`): hs is nodata at 0N 20E, 45N 100E, 23N 10E, 40N 100W and 85S 0E,
  and valid at 0N 160W, 50S 90E, 45S 100E, 0N 160E and 30N 40W, so neither a north-south
  nor an east-west flip is possible.
* `with_noaa`, the same 2 spots, f000 to f006: 46.0 MB and 25 requests without fields,
  51.4 MB and 31 with. The spot records were identical, and the NOAA run's 18 fields were
  byte-identical to the fields-only run's fields of the same hours. The index then served
  the fields-only run of the cycle (81 complete valid times against 3).
* The same cross-check as below, at 18Z f006 (Hs m point / field (raw box mean); Tp s;
  direction point / field (raw box mean); wind m/s): Pipeline 1.25 / 1.3 (1.294), 14.4 /
  14.5, 311 / 307.5 (307.8), 5.58 / 5.52. Jeffreys Bay 1.82 / 1.6 (1.612), 12.0 / 12.1,
  220 / 219.0 (218.5), 2.00 / 2.69. Thurso East 1.24 / 1.5 (1.514), 9.1 / 9.1, 314 /
  322.5 (322.9), 8.69 / 9.18. Nazaré 1.24 / 1.2 (1.243), 11.0 / 11.0, 316 / 316.5
  (317.1), 3.82 / 3.54. Teahupoo 2.40 / 2.4 (2.426), **7.0 / 9.9**, **117 / 135.0**
  (134.9), 9.82 / 10.00. At Teahupoo's node the raw `global.0p25` box holds two wave
  systems: five cells peak at 7.0 s from about 116° (trade-wind sea), the southern row at
  18.0 s from 199° (a long-period south swell). The box mean, 9.9 s from 135°, matches
  no cell (see Limitations).

**`with_noaa`, 2 spots** (Pipeline on `global.0p16`, Jeffreys Bay on `gsouth.0p25`, so
no spot uses `global.0p25`), f000 to f006 every 3 h, same cycle, run twice:

| | Downloaded | Requests | Wall time | Peak memory | Stored |
|-|------------|----------|-----------|-------------|--------|
| Map fields off | 46.2 MB | 25 | 16.2 s | 301 MB | nothing |
| Map fields on | 51.7 MB | 31 | 13.2 s | 337 MB | 18 fields, 1.45 MB |

The fields added 5.5 MB (1.83 MB per hour, the three wave records and their `.idx`) and
36 MB of peak memory, and the spot records were the same 6 rows (status `success` both
times). For 81 hours that is about 150 MB on top of the spot download (extrapolated).
With the default 50 spots, Thurso East and Mullaghmore Head use `global.0p25`, so the
spots' own range already holds the three records and nothing extra is downloaded at the
spot hours (the default spot hours equal the map hours, f000 to f240 every 3 h).

### Field statistics (served by the API, valid 2026-10-11 18Z = f006 of the 12Z cycle)

The index (81 valid times after the full run) and all six variables were fetched over
HTTP from uvicorn on port 28700, then decoded with the served `X-Field-Meta`.

* **hs nodata:** 42.6 % of nodes, **31.1 % of the area** (land and sea ice; polar rows are
  over-represented in a node count).
* **hs over valid ocean:** node mean 2.22 m, area-weighted mean 2.18 m, p99 6.60 m, max
  9.2 m at 48.5S 131.0W (the raw 0.25° maximum is 9.83 m; a 0.5° box mean is lower).
* **tp:** 1.1 to 24.9 s, median 11.0 s.
* **10 m wind speed** (from the served u and v): max 28.5 m/s at 27.5N 147.5E. No wind
  nodata.
* **Pressure:** min 951 hPa at 66.0S 162.0E (a Southern Ocean low), max 1064 hPa at
  78.0S 89.0E. That is PRMSL over the Antarctic ice sheet, a reduction to sea level
  rather than a real surface pressure, above the 1045 hPa display maximum (raw PRMSL
  range at this step: 949.9 to 1065.2 hPa).
* Requests: `at` = f240 and f240 + 2 h were served f240; f240 + 3.5 h and the cycle
  time − 4 h were 404; `/api/map/fields/sst` was 422.

### Cross-check against the spot provider's point samples (same cycle and step)

`sample_gfs_points` for 5 spots at 2026-10-11 12Z f006 (24.0 MB, 11 requests). It uses
the native wave grid for each spot and the nearest GFS sea cell to the break for wind.
"Field" is the served 0.5° value at the node nearest to the provider's wave cell (or
wind cell, for wind). "Raw box mean" recomputes, independently, the 1, 1/2, 1/4 weighted
mean of the raw `global.0p25` cells around that node (1.82 MB download).

| Spot | Provider grid, wave cell | 0.5° node | Hs m: point / raw 0.25° at the cell / field (raw box mean) | Tp s: point / field | Dir: point / field | Wind m/s: point / field |
|------|--------------------------|-----------|-------------------------------------|------------------|-------------------|------------------------|
| pipeline | global.0p16, 21.83N 158.17W | 22.0N 158.0W | 1.34 / 1.24 / 1.4 (1.389) | 15.5 / 15.6 | 311 / 307.5 | 6.01 / 6.52 |
| jeffreys-bay | gsouth.0p25, 34.25S 25.00E | 34.0S 25.0E, coastal | 2.01 / 2.01 / 1.8 (1.788) | 12.1 / 12.2 | 221 / 219.0 | 2.47 / 2.55 |
| thurso-east | global.0p25, 58.75N 3.75W | 59.0N 4.0W | 1.44 / 1.44 / 1.7 (1.692) | 9.6 / 9.6 | 301 / 304.5 | 2.60 / 3.04 |
| nazare | global.0p16, 39.67N 9.33W | 39.5N 9.5W, coastal | 1.29 / 1.26 / 1.3 (1.283) | 11.8 / 11.9 | 320 / 321.0 | 4.12 / 4.12 |
| teahupoo | gsouth.0p25, 18.00S 149.25W | 18.0S 149.0W | 2.39 / 2.39 / 2.4 (2.405) | 6.8 / 6.8 | 123 / 121.5 | 10.44 / 10.82 |

* In every case the served Hs equals the independent box mean within quantisation
  (0.05 m). This checks the whole chain: download, decode, regrid, quantise, zlib, the
  database, HTTP and decode.
* Tp agrees within 0.1 s and direction within 0.9° to 3.8°: same convention, no 180°
  flip.
* The larger differences have clear causes:
  * Jeffreys Bay (2.01 against 1.8 m): the coastal node's box holds nearshore cells with
    lower Hs.
  * Thurso (1.44 against 1.7 m): the wave cell lies exactly between nodes, so the
    nearest node is 0.25° north and 0.25° west (about 31 km), on the open north coast
    where Hs changes quickly towards the Pentland Firth.
  * Pipeline (1.34 against 1.4 m): the native 1/6° grid and the interpolated 0.25° grid
    differ by 0.10 m at the same cell.
  * Wind: the point is one 0.25° cell, the field a 0.5° box mean of u and v. The raw
    GFS speeds in Thurso's box range from 1.4 to 5.5 m/s (all sea cells); their box-mean
    vector speed is 3.08 m/s, served 3.04. Pipeline: box mean 6.49, served 6.52.
    Teahupoo: box mean 10.86, served 10.82 (2.0 MB download for this check).

### What was verified live, and what with mocks only

* **Live:** the standalone CLI on a real cycle (5 hours, then all 81), the skip of an
  already stored cycle, the served index and all six fields over HTTP (nearest time, 404
  outside the range, 422), the statistics and cross-check above, the `global.0p25` and
  `pgrb2.0p25` geometry, and a `with_noaa` acquisition for 2 spots storing fields on the
  NOAA run, compared with the same acquisition without fields. Also live: a full-cycle
  request after a 5-hour run of the same cycle reads all 81 hours, and with the 3-hour
  NOAA spot run and the 81-hour fields run of one cycle stored, the index serves the
  fields run. All of this was repeated by the review on the 18Z cycle (above), with the
  NOAA run's fields byte-identical to the fields-only run's.
* **Synthetic data only (tests):** missing or failing files, encoding failures, an
  invalid MAP_FIELD_RESOLUTION_DEG, fields the database refuses, pruning while another
  session commits newer fields, re-ingestion replacing rows, the skip rules, the
  choice between runs of one cycle, retention and pruning after each acquisition, the
  Celery task and beat entry, the CLI argument handling, the isolation of fields-only
  runs from the spot pipeline, and a reader with an idle sink making exactly the requests
  (files and byte ranges) of a reader without one. On the same synthetic files, the
  provider without map fields made the same 31 requests (same byte ranges, 8.38 MB) and
  returned the same records and warnings as the code before map fields (a one-off
  comparison against that commit, not a test).
* **Not verified:** a full 81-hour `with_noaa` acquisition with all 50 spots (its extra
  cost is extrapolated above), the Celery worker running the job against NOAA, and
  behaviour across a GFS version change.

## Limitations

* `global.0p25` is NOAA's interpolated product, not a native WW3 grid. Near coasts and
  where the native grids meet, it can differ from the native grids the spot forecasts
  use (0.10 m at Pipeline above).
* The 0.5° boxes (about 55 km) smooth coastal detail. Coastal nodes average only their
  sea cells, and small islands and narrow straits disappear. The layers show the big
  picture, not conditions at a break.
* Peak period and peak direction are box means too. Where the dominant wave system
  changes inside a box, the node's value lies between the systems and may match no
  cell (live at Teahupoo's node above: cells of 7.0 s from about 116° and of 18.0 s from
  199° gave 9.9 s from 135°). The layers show the big picture; a break's own forecast
  keeps the native cell's values.
* All layers are model output (GFS / GFS-Wave), not observations, and they carry the
  model's errors. Wave height is offshore significant wave height, not surf height. They
  are not for navigation.
* Quantisation steps are 0.1 m, 0.1 s, 1.5°, 0.5 m/s and 1 hPa. Values beyond the byte
  ranges are clamped, for example Hs above 25.4 m or wind components beyond ±63.5 m/s.
* Pressure over high terrain (Antarctica, Greenland, Tibet) is reduced to sea level and
  is not a real surface pressure.
* `SecurityHeadersMiddleware` (`app/core/middleware.py`) also appends
  `Cache-Control: no-store` to every `/api/` response (seen live). Browsers then do not
  cache the fields despite the endpoint's `public, max-age=…`. This needs a change in that
  file (not part of this work).
* GFS v17 (proposed) is expected to rename files and move WW3 to an unstructured grid.
  The path templates are configuration, but the `global.0p25` grid may change or
  disappear. A geometry change triggers a warning.
