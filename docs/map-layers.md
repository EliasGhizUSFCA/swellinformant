# Map ocean layers (global model fields)

The map can draw global layers under the surf spots: significant wave height, peak wave
period and direction, the primary swell, secondary swell and wind-sea partitions (height,
period, direction; drawn as wavefronts), 10 m wind (speed and particles) and mean
sea-level pressure (isobars). The backend reads them from NOAA open data, shrinks them to
one byte per cell on a 0.5° grid and serves them as small compressed grids. The browser
decodes and draws them (rendering: [map-rendering.md](map-rendering.md)).

* Encoding (pure numpy): `backend/app/services/forecasts/fields.py`
* Production: `backend/app/services/forecasts/noaa.py` (`GfsPointReader` with a
  `FieldSink`, `NoaaGfsProvider`, `NoaaGfsFieldsProvider`) and
  `backend/app/services/forecasts/ingestion.py` (`store_map_fields`,
  `acquire_map_fields`)
* Modes, hours and the read side: `backend/app/services/map_fields.py`, served by
  `backend/app/api/map_fields.py` under the contract in
  `backend/app/schemas/map_fields.py`
* Storage: table `map_fields` (`models.forecast_engine.MapField`), one row per run, valid
  time and variable
* Tests (synthetic GRIB2 files and arrays only; the download costs use the real `.idx`
  files in `backend/tests/fixtures/idx`): `backend/tests/unit/test_map_field_encoding.py`,
  `test_map_field_partitions.py`, `test_map_field_modes.py`, `test_noaa_provider.py`
  (map-field and partition sections) and `backend/tests/integration/test_map_fields.py`

These are **model output, not observations**. They are **not for navigation** or safety
decisions. Wave heights are **offshore significant wave heights** (of the combined sea
state, or of one partition). They are not the breaking (surf) height at any beach.

## Sources, licence and attribution

| Layer | NOAA product | Record | Unit served |
|-------|--------------|--------|-------------|
| `hs` | GFS-Wave (WAVEWATCH III) `global.0p25` | `HTSGW:surface` | m (offshore Hs) |
| `tp` | GFS-Wave `global.0p25` | `PERPW:surface` | s (peak period) |
| `wave_dir` | GFS-Wave `global.0p25` | `DIRPW:surface` | degrees FROM |
| `swell1_hs`, `swell1_tp`, `swell1_dir` | GFS-Wave `global.0p25` | `SWELL`/`SWPER`/`SWDIR:1 in sequence` | m, s (period), degrees FROM |
| `swell2_hs`, `swell2_tp`, `swell2_dir` | GFS-Wave `global.0p25` | `SWELL`/`SWPER`/`SWDIR:2 in sequence` | m, s (period), degrees FROM |
| `windsea_hs`, `windsea_tp`, `windsea_dir` | GFS-Wave `global.0p25` | `WVHGT`/`WVPER`/`WVDIR:surface` | m, s (period), degrees FROM |
| `wind_u`, `wind_v` | GFS `pgrb2.0p50` (fallback `pgrb2.0p25`) | `UGRD`/`VGRD:10 m above ground` | m/s, eastward and northward |
| `pressure` | GFS `pgrb2.0p50` (fallback `pgrb2.0p25`) | `PRMSL:mean sea level` | hPa (Pa / 100) |

The first six are the **core** variables (`CORE_MAP_FIELD_VARIABLES`): every servable
valid time has all of them. The nine **partition** variables
(`PARTITION_MAP_FIELD_VARIABLES`) are optional: they are read only with
`MAP_FIELD_PARTITIONS=on`, and a time without them is still complete.

* Files come from the NOAA Open Data Dissemination (NODD) bucket on AWS
  (`NOAA_GFS_BASE_URL`; the wave and spot-atmosphere path templates are shared with the
  spot ingester, see [noaa-direct-ingestion.md](noaa-direct-ingestion.md)). Only the
  needed records are downloaded, by HTTP `Range` from the offsets in each file's `.idx`.
  No GRIB file is written to disk: each record is decoded in memory, regridded at once,
  and only the compressed 0.5° bytes are kept.
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
  1,038,240 cells missing, land and sea ice; DIRPW: 470,163). Its records 5 to 19 are
  HTSGW, PERPW, DIRPW, WVHGT, SWELL 1-3, WVPER, SWPER 1-3, WVDIR, SWDIR 1-3. The core
  records 5-7 are one range (1.82 MB at f006). The partitions add three ranges: 8-10
  (WVHGT, SWELL 1, SWELL 2, adjacent to the core range, so merged with it), 12-14 and
  16-18. The third swell partition (records 11, 15, 19) is never downloaded. NOAA
  **interpolates** `global.0p25` from the native `gnh_10m` (1/6°, 52.5N to 15S) and
  `gsh_15m` (1/4°, 10.5S to 79.5S) grids, so it is a derived product. Spot forecasts use
  their own sources.
* **Why `pgrb2.0p50`.** It has the same three atmosphere records (PRMSL record 1,
  UGRD/VGRD 10 m records 588 and 589) for 0.86 MB per hour instead of 2.93 MB in
  `pgrb2.0p25` (f006, from the recorded `.idx` files), on a 720 × 361 grid that is
  exactly the served grid (each node takes the cell on it). `MAP_FIELD_ATMOS_GRID` and
  `MAP_FIELD_ATMOS_PATH_TEMPLATE` choose the file. When the file of one hour is not
  published (HTTP 403/404), that hour is read from `pgrb2.0p25`
  (`NOAA_GFS_ATMOS_PATH_TEMPLATE`) with a warning, so a GFS change that drops the 0.5°
  files (planned for GFS v17) costs bytes, not layers. Seen live on 2026-10-15 00Z f003,
  whose `pgrb2.0p50` file was not up yet.
* **Direction convention.** DIRPW, SWDIR and WVDIR in the GRIB2 files are degrees FROM
  (WW3 `ww3_grib` writes `MOD(630 − θ, 360)`). They are stored as they are, without +180.
  Live at 5 spots, the served `wave_dir` agreed with the spot provider's own DIRPW sample
  within 0.9° to 3.8° (table below). That sample was checked against NDBC station
  bulletins in noaa-direct-ingestion.md. Partition directions were checked against an
  independent ecCodes decode (below). In the Southern Ocean westerlies the circular means
  of `wave_dir` and of the 10 m wind direction are about 265° and 283°; "going to" would
  put the waves near 85°. The arrows and wavefronts on the map point where the waves
  travel TO; text says where they come FROM ([map-rendering.md](map-rendering.md)).
* **Periods.** ecCodes calls PERPW "Primary wave mean period", but WW3 writes the peak
  period there (research notes), so `tp` is labelled as the peak period. NCEP's inventory
  calls SWPER and WVPER "Mean Period of Swell Waves" and "Mean Period of Wind Waves", and
  their ww3_grib source was not checked, so the partition periods are labelled
  **"period"**, never "peak period".
* **Partition order (WW3 caveat).** WW3 numbers the swell partitions **by height in
  every cell and hour**: "swell 1" is the largest swell there and then, not a swell
  system tracked across the map or through time. Next to a place where two swells have
  similar heights, "primary" and "secondary" can swap from one cell or step to the next.
  The wind sea is the partition forced by the local wind. This is why the mixing guard
  below exists, and why a partition layer can look patchy where systems swap.

## Modes and settings

`app.services.map_fields.map_fields_mode(settings)` decides whether and how fields are
produced:

| `MAP_FIELDS_ENABLED` | `FORECAST_PROVIDERS` | Mode | What happens |
|----------------------|----------------------|------|--------------|
| unset (default) | contains `noaa_gfs` | `with_noaa` | Every NOAA spot acquisition (`forecasts.acquire`) of a map cycle also stores the fields on its own run |
| `true` | contains `noaa_gfs` | `with_noaa` | Same |
| unset (default) | live providers without `noaa_gfs`, e.g. `open_meteo,open_meteo_ecmwf` (the recommended setup) | `standalone` | The `forecasts.map_fields` job reads only the fields, under the source `noaa_gfs_fields` |
| `true` | anything without `noaa_gfs` | `standalone` | Same (e.g. demo spot forecasts with real ocean layers) |
| unset | `demo` only | `off` | Nothing is fetched (synthetic spot data never trigger live downloads); the index says what to set |
| `false` | any | `off` | Nothing is fetched |

So moving the spot forecasts to an API (Open-Meteo) keeps the map's wind, pressure and
wave layers: the map no longer depends on a GRIB spot provider.

| Setting | Default | Meaning |
|---------|---------|---------|
| `MAP_FIELD_STEPS` | `0-120:3,120-240:6` | Map hours per cycle as `start-end:step` forecast-hour ranges. The default is f000 to f120 every 3 h and f126 to f240 every 6 h: **61 hours**. An hour shared by two ranges counts once; only published hours are used (hourly to f120, then 3-hourly) |
| `MAP_FIELD_CYCLES` | `00,12` | GFS cycles (UTC) that carry map fields. Other cycles are skipped by the standalone job and store no fields in `with_noaa` mode |
| `MAP_FIELD_PARTITIONS` | `on` | `on`: also the nine partition variables (primary swell, secondary swell, wind sea). `off`: the six core variables only |
| `MAP_FIELD_ATMOS_GRID` | `0p50` | GFS atmosphere grid for wind and pressure read for the fields alone (`0p25`, `0p50` or `1p00`) |
| `MAP_FIELD_ATMOS_PATH_TEMPLATE` | `gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.{grid}.f{fhour:03d}` | Its file layout (`{grid}` = `MAP_FIELD_ATMOS_GRID`); `pgrb2.0p25` is the per-hour fallback |
| `MAP_FIELD_MAX_BYTES_PER_RUN` | 600000000 | Download budget for the fields of one run (see "Byte budget") |
| `MAP_FIELD_STALE_AFTER_HOURS` | 12 | The index flags the layers `stale` once the served model cycle is older than this |
| `MAP_FIELD_RESOLUTION_DEG` | 0.5 | Output grid spacing. Must divide 180 (0.25, 0.5, 1, …). Otherwise (e.g. 0.7) a NOAA spot run stores no fields and says why in `details.map_fields.warnings`, and the standalone job fails with that message; spot forecasts are unaffected |
| `MAP_FIELD_MAX_LEAD_HOURS` | 240 | Caps `MAP_FIELD_STEPS` (the CLI's `--max-lead-hours` overrides it for one run) |
| `MAP_FIELD_STEP_HOURS` | 3 | No longer decides the map hours (`MAP_FIELD_STEPS` does). Kept for compatibility |

The read side serves whatever is stored, whatever the mode. Retention removes old fields.

## HTTP contract

Contract: `backend/app/schemas/map_fields.py`.

* `GET /api/map/fields` returns the index (`FieldIndexOut`): `available`, a plain-language
  `message` when nothing is available, the `source` (code, name, attribution, `is_demo`),
  `run_key`, `cycle_time`, the `valid_times` at which **all six core** variables exist,
  and the `variables` with label, unit, display range, `group` (`core` or `partition`)
  and, for a partition, `system` (`primary`, `secondary` or `windsea`). A partition's
  three variables are listed only when the served run stores all three of them at one
  or more of its valid times. Freshness: `updated_at` (when the served run completed),
  `age_hours` (now minus `cycle_time`, the model cycle), `stale_after_hours`
  (`MAP_FIELD_STALE_AFTER_HOURS`) and `stale` (`age_hours` beyond it). Stale layers are
  still served; the map shows a badge.
  The run is the newest one (by model cycle) with at least one complete valid time. When
  several runs hold that cycle (for example the NOAA spot run and a fields-only run after
  a change of mode), the one with the most complete valid times is served, and on a tie
  the newer run (`map_fields.servable_runs`, the same order retention uses). Incomplete
  times are logged and left out. `Cache-Control: public, max-age=300`.
* `GET /api/map/fields/{variable}?at=ISO&run=RUN_KEY` returns one field as
  `application/octet-stream`. The decoding parameters are in the `X-Field-Meta` header
  (JSON with the keys of `FieldMeta`, including `group`, `valid_time` and `run_key`;
  `Access-Control-Expose-Headers: X-Field-Meta`).
  * `run` (the index's `run_key`) serves that model run while it is retained (the two
    best servable runs are). An unknown, pruned or no longer servable run is **404 with
    the error code `run_not_retained`** ("Map field run not retained"), never another
    run's field. Without `run` the current run is served.
  * `Cache-Control: public, max-age=3600` with `run` (a run's field never changes) and
    `public, max-age=300` without it (the next cycle replaces the current run). Error
    responses are `no-store`.
  * `at` defaults to now. A time without a zone is taken as UTC.
  * The served time is the complete valid time nearest to `at`; on a tie the earlier
    time wins. Values are never interpolated in time. The served `valid_time` is in
    `X-Field-Meta`, and the map checks it against the time it asked for.
  * The response is 404 when `at` lies before the first or after the last valid time by
    more than one configured map step there (3 h near the start, 6 h after f120 with the
    default `MAP_FIELD_STEPS`), when the run does not store the variable at that time
    (for example a partition), or when nothing is stored.
  * An unknown variable (e.g. `swell3_hs`) is 422.
  * The index and a field request read only metadata columns, plus the bytes of the one
    row that is served.

**Bytes.** zlib (RFC 1950, `DecompressionStream("deflate")` in the browser) compressed
uint8, `nx × ny` cells, row-major. Row 0 is the northernmost latitude `lat0` = 90, and row
j is `lat0 − j·dlat`. Column i is `lon0 + i·dlon`, with `lon0` = −180, wrapping east. A
value is `offset + scale × byte`. Byte 255 means no data (land, ice, missing, or a
partition that does not exist there, never 0). At 0.5°, `nx` = 720 and `ny` = 361, and
the last row is 90S.

| Variable | Unit | Offset | Scale | Bytes 0..254 cover | Display range |
|----------|------|--------|-------|--------------------|---------------|
| `hs`, `swell1_hs`, `swell2_hs`, `windsea_hs` | m | 0 | 0.1 | 0 to 25.4 m | 0 to 10 m |
| `tp`, `swell1_tp`, `swell2_tp`, `windsea_tp` | s | 0 | 0.1 | 0 to 25.4 s | 0 to 20 s |
| `wave_dir`, `swell1_dir`, `swell2_dir`, `windsea_dir` | deg FROM | 0 | 1.5 | 0 to 358.5° (bytes 0..239; 360° = byte 0) | 0 to 360 |
| `wind_u`, `wind_v` | m/s | −63.5 | 0.5 | −63.5 to 63.5 m/s | −30 to 30 m/s |
| `pressure` | hPa | 860 | 1.0 | 860 to 1114 hPa | 960 to 1045 hPa |

Values beyond a variable's byte range are clamped to 0 or 254. Directions wrap instead.
The decoded value is within half a quantisation step of the regridded value (tested, and
checked live against an independent decode below).

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
  and the same holds end to end through GRIB2. From `pgrb2.0p50` to the 0.5° grid each
  node takes the one cell on it (tested), so the wind and pressure are not smoothed.
* **Nodata.** Masked or NaN cells are left out. A core node is nodata when no valid cell
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

### Mixing guard (partitions)

A box mean across a partition swap (see the WW3 caveat above) gives a direction and a
period that no wave has. Energy (Hs²) weighting was measured to be worse, so it is not
used. Instead a **partition** node is nodata when

1. its direction cells' unit vectors average to a resultant of **0.8 or less** of the box
   weight (`PARTITION_MIN_RESULTANT`; 1 means all cells agree). Two equal halves of a box
   83° apart (116° and 199°) give 0.75 and are masked; a single system with 20° of spread
   gives 0.985 and passes, or
2. the partition exists in **less than half** (`PARTITION_PRESENT_FRACTION`) of the box's
   sea area, counted over the cells where the total wave height (HTSGW) is valid, so a
   partition never spreads half a box beyond where the model has it and coasts are not
   eroded, and
3. **one joint mask** applies to the three variables of a partition: at every node its
   height, period and direction are all valid or all nodata.

Both numbers are **heuristic** (chosen from the box statistics of the wave-viz audit, not
calibrated against observations). The audit suggested 0.7; 0.8 is used because 0.7 keeps
the equal two-system split above. The core `wave_dir` keeps the plain rule (nodata only
where the vectors cancel), so the combined-sea layers are unchanged. Live statistics are
below ("Mixing guard on a real hour").

## Forecast hours and cycles

Map hours come from `MAP_FIELD_STEPS` (default 61 hours, f000 to f240), capped at
`MAP_FIELD_MAX_LEAD_HOURS`, of the cycles in `MAP_FIELD_CYCLES` (default 00Z and 12Z).
The timeline therefore steps 3-hourly to five days and 6-hourly after that, and every
map hour is a real model output hour: nothing is interpolated.

* **`standalone`.** `NoaaGfsFieldsProvider.current_run` uses the same availability rules
  as the spot provider (nominal cycle with `NOAA_AVAILABILITY_LAG_HOURS`, then up to 4
  older cycles), skips cycles outside `MAP_FIELD_CYCLES` without a request, and needs
  only the `.idx` of the last map hour for the map's atmosphere file (or its
  `pgrb2.0p25` fallback) and `global.0p25`. Per map hour it downloads the atmosphere
  `.idx`, PRMSL and UGRD/VGRD 10 m (2 ranges), the `global.0p25` `.idx` and the wave
  records (1 range for the core, 3 with partitions): 5 requests per hour, 7 with
  partitions. Run key: `noaa-gfs-fields:YYYYMMDDHH`. `--cycle` takes any cycle.
* **`with_noaa`.** Only map cycles carry fields. Spot hours that are also map hours hand
  the U10, V10 and PRMSL fields the reader decodes anyway (from `pgrb2.0p25`) to the
  sink. When no spot uses `global.0p25`, the reader also fetches its wave records for the
  map (core, plus partitions when on). When a spot does use that grid (the default spots
  north of 52.5N: Thurso, Mullaghmore), its 15-record range already contains them, so the
  map's partitions cost nothing extra. Map hours beyond the spots' hours are read for the
  fields alone, from the map's atmosphere file. `current_run` then also waits for the
  last map hour of a map cycle.
* **Memory.** Each decoded core field (8 MB as float64) is regridded, quantised and
  compressed immediately, and only the compressed bytes are kept. A partition field is
  regridded at once and held as float32 (1 MB at 0.5°) until its partition's three
  variables are in, then encoded with the joint mask. Peak resident memory of a 4-hour
  live run with partitions: 588 MB (513 MB without partitions).
* **Failures.** A field that cannot be read or encoded is a warning in the run's
  `details.map_fields.warnings`, with one "map fields incomplete" line per hour that
  lacks a core variable and (with partitions on) one "map partitions incomplete" line per
  hour that lacks a partition. An index without one of a partition's three records
  ("SWELL:2 in sequence") stores nothing of that partition at that hour and downloads
  none of its records. A file that fails half-way stores nothing of that file. A map
  field is never a spot error and never a failed spot step. If the database refuses the
  fields of a NOAA spot run, they are rolled back to a SAVEPOINT and listed there as "map
  fields not stored"; the spot records are stored as usual.

### Byte budget

`MAP_FIELD_MAX_BYTES_PER_RUN` bounds what one run downloads **for the fields alone**
(standalone: everything; `with_noaa`: the extra wave records and field-only hours, not
what the spots read anyway). Before each byte range the reader reserves its size (known
from the `.idx`; index files count once read). When the partition ranges no longer fit,
the partitions stop for the rest of the run (one warning) and the core goes on; when the
core ranges no longer fit, the run's map fields stop, later hours are not requested at
all, and the hours read so far are kept (run `PARTIAL`). A budget too small for a single
hour fails the run as a permanent error (no retry). At the defaults a full cycle needs
about 484 MB of the 600 MB (below), so the budget only guards against a configuration
change or a file layout change.

## Storage, jobs and retention

* `store_map_fields` upserts on `uq_map_fields_key` (run, valid time, variable).
  Re-ingesting replaces rows in place and never duplicates them. The run's details get
  `{"map_fields": {"count", "bytes", "valid_times", "partition_times", "warnings"}}`:
  `valid_times` are the complete (core) times, `partition_times` the times with all three
  partitions. The fields-only provider also returns its download as `FetchResult.stats`
  (bytes and requests) for `run.details["stats"]`.
* **Standalone runs** belong to the `ForecastSource` `noaa_gfs_fields` ("NOAA GFS-Wave +
  GFS global fields", kind `fields`, priority 1000, atmosphere model `GFS pgrb2.0p50`).
  They have no spot records, `details.spot_ids = []`, `details.fields_only = true` and
  `predictions_computed_at` set (nothing to score). Spot forecasts, blending,
  comparisons, verification, the status page and the accuracy dashboard only read the
  configured providers' sources or ensemble sources, so these runs are never selected.
  An integration test checks that the spot API pages, run selection, status,
  predictions, archive and events are identical with and without a fields-only run.
* **Job.** The Celery task `forecasts.map_fields` is a no-op unless the mode is
  `standalone`. Beat runs it on the `forecast-acquisition` interval
  (`FORECAST_REFRESH_MINUTES`, every 3 h by default), and once when beat starts. Each run
  looks for the newest complete map cycle (`MAP_FIELD_CYCLES`), so with the default 00Z
  and 12Z cycles it downloads twice a day. It skips a cycle whose fields already exist: a
  fields run of that cycle that is SUCCESS or PARTIAL and read every requested map hour
  (`details.map_hours`), or rows of that cycle from any run (for example the NOAA spot
  run) covering every map hour and core variable. A shorter run of the same cycle (for
  example a check with `--max-lead-hours 9`) does not count for the hours it did not
  read, so the scheduled job still reads the whole cycle and replaces its rows in place.
* **CLI.** `python -m app.cli engine map-fields [--cycle YYYYMMDDHH] [--max-lead-hours N]
  [--force]` works in every mode except `off`. It prints JSON with counts, stored and
  downloaded bytes, requests, rows pruned, the duration and per-hour figures. `--cycle`
  takes any cycle, including one outside `MAP_FIELD_CYCLES`, without the availability
  check (hours not yet published become gaps).
* **Retention.** `maintenance.prune_map_fields` keeps the fields of the two best
  servable runs, in the read side's order (`map_fields.servable_runs`: newest cycle,
  then most complete valid times, then the newer run), so the served run is never
  deleted, and the previous run stays available to clients that still name it with
  `run`. Every other `map_fields` row goes, including rows of runs without a single
  complete valid time (they are never served). It runs right after every acquisition that
  stores fields (standalone job and NOAA spot run) and in `maintenance.cleanup` (reported
  as `map_fields`), so storage stays at about two runs between the daily cleanups. One
  DELETE statement picks the runs to keep and deletes the rest, so fields that another
  acquisition commits meanwhile are not deleted. A forecast run removed by its own
  retention takes its fields with it (FK cascade).

## Bandwidth and storage, measured live (2026-10-15)

Measured on 2026-10-15 between 03:39 and 03:46 UTC against the NODD bucket, on scratch
databases, with the default settings (`FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf`,
so the mode was `standalone` with `MAP_FIELDS_ENABLED` unset; partitions on; 6 download
workers) and a short horizon. No whole cycle was downloaded; the per-cycle and per-day
figures are **extrapolated** from these hours.

**Newest complete map cycle, 2026-10-14 12Z, f000 to f009** (`engine map-fields
--max-lead-hours 9`, which found the cycle itself: the nominal 18Z cycle is not a map
cycle and was skipped). Status `success`, 60 fields (4 hours × 15 variables), 4 of 4
hours complete, 28 requests, 31.6 MB downloaded, 4.64 MB stored, 6.4 s for the job
(8.1 s for the process), peak resident memory 588 MB.

| Hour | Downloaded, partitions on | Requests | Downloaded, partitions off (5 requests) |
|------|---------------------------|----------|------------------------------------------|
| f000 | 7.92 MB | 7 | 2.71 MB |
| f003 | 7.93 MB | 7 | 2.72 MB |
| f006 | 7.89 MB | 7 | 2.69 MB |
| f009 | 7.89 MB | 7 | 2.69 MB |
| f120 | 7.85 MB | 7 | |
| f240 | 8.10 MB | 7 | |

(f120 and f240 with `MAP_FIELD_STEPS=120-240:120`, 15.9 MB; the base run with
`MAP_FIELD_PARTITIONS=off`, 10.8 MB, 513 MB peak.) The recorded `.idx` files of the
2026-10-13 12Z cycle give the same per-hour costs (`test_map_field_modes.py`): base 2.72
to 2.74 MB (pgrb2.0p50 records 0.86 MB + index 0.04 MB, wave records 1.82 to 1.83 MB),
partitions 5.13 to 5.28 MB more. Reading the same wind and pressure from `pgrb2.0p25`
costs 2.90 to 2.93 MB per hour instead of 0.86.

**Stored per field** (compressed, mean of 4 hours of the 12Z cycle):

| Variable | kB | Variable | kB | Variable | kB |
|----------|----|----------|----|----------|----|
| `hs` | 56.7 | `swell1_hs` | 54.7 | `swell2_hs` | 41.9 |
| `tp` | 78.1 | `swell1_tp` | 76.5 | `swell2_tp` | 79.9 |
| `wave_dir` | 99.2 | `swell1_dir` | 91.0 | `swell2_dir` | 87.4 |
| `wind_u` | 113.6 | `windsea_hs` | 55.5 | `pressure` | 55.9 |
| `wind_v` | 112.7 | `windsea_tp` | 71.2 | `windsea_dir` | 84.7 |

One valid time: 516 kB for the six core variables, 643 kB for the nine partitions,
1.16 MB in all (1.14 MB at f120, 1.18 MB at f240). The wind fields are about 13 kB larger
than with the 0.25° box mean before, because the 0.5° cells are not smoothed.

**Default configuration, extrapolated** (61 map hours per cycle, cycles 00Z and 12Z):

| | Partitions on (default) | Partitions off |
|-|-------------------------|----------------|
| Download per cycle | about 484 MB (61 × 7.85 to 8.10 MB), 427 requests | about 165 MB (61 × 2.69 to 2.74 MB), 305 requests |
| Download per day | about 0.97 GB | about 0.33 GB |
| Stored per run | about 71 MB (61 × 1.16 MB) | about 31.5 MB |
| Stored, 2 retained runs | about 142 MB | about 63 MB |
| Job time per cycle | about 1 to 2.5 min (4.3 to 12.9 s per hour measured, 6 in parallel; depends on the network) | |
| Within `MAP_FIELD_MAX_BYTES_PER_RUN` (600 MB) | yes | yes |

Before this change the standalone job read every cycle (4 a day) at 81 hours with the
`pgrb2.0p25` atmosphere: 379.5 MB per cycle, about 1.5 GB a day (measured, below), for
the six core layers only.

**The latest cycle, 2026-10-15 00Z, while it was still being uploaded** (`--cycle
2026101500 --max-lead-hours 9` at 03:39 UTC): status `partial`, 48 fields, 3 of 4 hours
complete, 26 requests, 26.8 MB. Two real gaps showed the fallbacks: the f003
`pgrb2.0p50` file was not published yet, so f003 read `pgrb2.0p25` (10.0 MB for that
hour instead of 7.9, warning "f003: ...pgrb2.0p50.f003 not published; read
...pgrb2.0p25.f003 instead"); and the f006 `global.0p25` index was not up although f009
was, so f006 has wind and pressure but no wave layers ("f006 map fields incomplete:
missing hs, tp, wave_dir" and "f006 map partitions incomplete"). The index served this
run (newest cycle) with valid times 00Z, 03Z and 09Z: the gap stays a gap, and
`at=06Z` is answered with the 03Z field, labelled 03Z in `X-Field-Meta`. The scheduled
job avoids such runs because `current_run` waits for the cycle's last map hour.

**The same cycle once complete** (re-checked at 13:40 UTC on a fresh scratch database,
`engine map-fields --max-lead-hours 9` without `--cycle`: the nominal 06Z cycle is not a
map cycle and was skipped, so it picked 2026-10-15 00Z itself): status `success`, 60
fields, 4 of 4 hours complete, no warnings, 28 requests, 31.8 MB downloaded (7.90, 7.96,
7.96 and 7.96 MB for f000 to f009, 7 requests each), 4.65 MB stored (1.16 MB per valid
time, 516 kB of it core; per field within 2 kB of the table above), peak resident memory
574 MB. The job took 13.9 s, about 12 s per hour against 4 to 6 s at 03:39: the
sandbox's proxy was slower then, so the job time above is a range, not a constant. The
run's `details.warnings` record the download ("downloaded 31.8 MB in 28 requests for 4
map hours f000-f009 (7.9 MB per hour)").

### Served over HTTP

uvicorn on port 28731 on the scratch database (both runs stored):

* `GET /api/map/fields`: `run_key` `noaa-gfs-fields:2026101500`, `age_hours` 3.67,
  `stale_after_hours` 12, `stale` false, `updated_at` set, 15 variables (6 `core`, 9
  `partition` with `system` primary, secondary and windsea), labels "Primary swell
  period" etc. `Cache-Control: public, max-age=300`.
* `GET /api/map/fields/swell1_dir?at=2026-10-14T18:00Z&run=noaa-gfs-fields:2026101412`
  (the older retained run): 200, `X-Field-Meta` `group` partition, unit deg, scale 1.5,
  `Cache-Control: public, max-age=3600`, 90.8 kB. Without `run`: the current run,
  `max-age=300`.
* `run=noaa-gfs-fields:2026101300` (not retained): 404, error code `run_not_retained`,
  `no-store`. `swell3_hs`: 422.
* Again at 13:40 UTC on the database holding only the complete 00Z run (uvicorn on port
  28732): the index served it with `age_hours` 13.67 and **`stale` true** (the 12Z cycle
  was not published yet; see "Freshness with two cycles a day" under Limitations), 15
  variables. `windsea_tp?at=07:40Z` was answered with the 09Z field, labelled 09Z in
  `X-Field-Meta`; `hs?at=20:00Z` (beyond 09Z plus one 3 h step) was 404; the 12Z run of
  the day before, never stored there, was 404 `run_not_retained`.

### Independent decode check (3 points and more)

The f006 records of 2026-10-14 12Z were downloaded again with plain HTTP range requests
(7.8 MB) and decoded with ecCodes directly, without app code. The 0.25° → 0.5° box means
(weights 1, 1/2, 1/4), the 0.5° atmosphere (the cell on the node) and the partition guard
were re-implemented in that script and compared with the stored rows and with the bytes
served over HTTP with `run=` (byte-identical to the stored rows for all 15 variables).
Independent value / stored value:

| Variable | 50S 120E | 22.5N 158.5W | 40N 30W | 0N 140W | 34S 25E |
|----------|----------|--------------|---------|---------|---------|
| `hs` m | 6.455 / 6.5 | 1.744 / 1.7 | 3.900 / 3.9 | 2.030 / 2.0 | 1.912 / 1.9 |
| `wave_dir` ° | 329.9 / 330.0 | 317.2 / 316.5 | 294.1 / 294.0 | 194.9 / 195.0 | 224.9 / 225.0 |
| `swell1_hs` m | 3.561 / 3.6 | 1.334 / 1.3 | 3.353 / 3.4 | 0.896 / 0.9 | 1.522 / 1.5 |
| `swell1_dir` ° | 295.5 / 295.5 | 321.6 / 321.0 | 275.8 / 276.0 | 193.8 / 193.5 | 226.6 / 226.5 |
| `swell2_hs` m | nodata / nodata | 1.023 / 1.0 | 1.391 / 1.4 | 0.517 / 0.5 | 0.868 / 0.9 |
| `swell2_dir` ° | nodata / nodata | 76.9 / 76.5 | 335.0 / 334.5 | 325.6 / 325.5 | 217.6 / 217.5 |
| `windsea_hs` m | 5.385 / 5.4 | nodata / nodata | 1.680 / 1.7 | 1.603 / 1.6 | 0.770 / 0.8 |
| `windsea_dir` ° | 4.1 / 4.5 | nodata / nodata | 208.1 / 208.5 | 117.1 / 117.0 | 248.6 / 249.0 |
| `wind_u` m/s | −2.73 / −2.5 | −4.96 / −5.0 | 6.86 / 7.0 | −5.99 / −6.0 | 3.33 / 3.5 |
| `pressure` hPa | 994.7 / 995 | 1016.2 / 1016 | 1017.1 / 1017 | 1011.7 / 1012 | 1013.4 / 1013 |

All 15 variables (`tp`, the partition periods and `wind_v` too) agree at all five points
within half a quantisation step (largest differences: 0.047 m, 0.046 s, 0.72°, 0.23 m/s,
0.43 hPa), including the nodata of `swell2` at 50S 120E and of the wind sea at
22.5N 158.5W, which the independent guard also masks. Partition directions are FROM, like
the core direction, with no +180.

The same check on f006 of the complete 2026-10-15 00Z cycle (records downloaded again
independently, 7.9 MB, then deleted), at the same five points: all 15 variables within
half a quantisation step again (largest differences 0.048 m, 0.049 s, 0.72°, 0.23 m/s,
0.37 hPa), the served bytes identical to the stored rows, and the nodata agreeing
(secondary swell at 50S 120E and 22.5N 158.5W, wind sea at 40N 30W, 0N 140W and
34S 25E). For example at 50S 120E: `hs` 7.714 / 7.7 m, `swell1_dir` 249.3 / 249.0°,
`windsea_dir` 334.6 / 334.5°, `wind_u` 14.44 / 14.5 m/s, `pressure` 987.1 / 987 hPa.

### Mixing guard on a real hour

Same hour (2026-10-14 12Z f006), computed independently over every node, and compared
with the stored masks:

| Partition | Nodes with data | Resultant ≤ 0.7 | Resultant ≤ 0.8 | Present in < half the sea area | Served |
|-----------|-----------------|-----------------|-----------------|--------------------------------|--------|
| swell1 | 135,114 | 4.4 % | 6.7 % | 3.5 % | 90.0 % |
| swell2 | 116,805 | 9.9 % | 14.5 % | 8.0 % | 78.0 % |
| wind sea | 109,908 | 0.25 % | 0.5 % | 13.0 % | 86.7 % |

The stored valid masks equal the independent ones at every node for all three
partitions (0 differing nodes). On f006 of 2026-10-15 00Z the figures were nearly the
same (resultant ≤ 0.8: 7.2 %, 14.7 % and 0.45 %; served 89.5 %, 78.0 % and 87.8 %), again
with 0 differing nodes. The secondary swell is the patchiest layer, as expected
from WW3's per-cell ordering; the wind sea loses mostly thin edges.

### Against NDBC buoys (direction sanity, no 180° flip)

Both stored runs against the NDBC 5-day files of 14 moored buoys (0.9 MB), bilinear
sampling as in the map: 10 m wind direction for observed speeds of 4 m/s or more (43
pairs, 13 stations, 6 valid times) median |difference| 8.1°, p90 26.3°, none above 90°;
the same pairs flipped by 180° would give 171.9°. Peak wave direction against NDBC MWD
(74 pairs): median 13.0°, p90 44.7° (flipped: 167.0°).

### Re-check by the review (2026-10-15 12Z)

Repeated at 21:26 UTC on fresh scratch databases, same settings (standalone mode with
Open-Meteo spot providers and `MAP_FIELDS_ENABLED` unset):

* `engine map-fields --max-lead-hours 9` picked **2026-10-15 12Z** itself: `success`, 60
  fields, 28 requests, 31.96 MB downloaded (7.99 MB per hour, 7 requests each), 4.69 MB
  stored, 7.7 s for the job, peak 584 MB. The 15 records of f006 downloaded again with
  curl: 0.865 MB for PRMSL and UGRD/VGRD 10 m from `pgrb2.0p50` and 7.087 MB for the 12
  wave records; with the two `.idx` files 7,993,042 bytes, exactly what the job counted
  for f006.
* **Whole-grid check.** A separate script decoded those records with ecCodes and
  recomputed every node with its own 3 × 3 convolution (weights 1, 1/2, 1/4), the guard
  and the joint mask, then compared all 258,480 nodes outside the two pole rows for all
  15 variables with the stored rows: the nodata masks are identical for every variable,
  and every value agrees within half a quantisation step, apart from the partition
  periods above 25.4 s that are clamped (Limitations). In the North Atlantic trades
  (10N to 20N, 50W to 30W) the stored u and v give a mean wind FROM 76°; in 45S to 55S
  the circular mean of `swell1_dir` is 277° FROM (westerly swell, no 180° flip).
* Over HTTP (uvicorn, stopped afterwards): index `age_hours` 9.49, `stale` false, 15
  variables; all 60 fields served with `run` were byte-identical to the stored rows, with
  `group` set and `public, max-age=3600`; without `run`, `max-age=300`; `hs` at 23:59Z was
  served the 21Z field and at 00:01Z the next day was 404; an unstored run was 404
  `run_not_retained` (`no-store`); `swell3_hs` and a 121-character `run` were 422.
* **Byte budget, live.** With `MAP_FIELD_MAX_BYTES_PER_RUN=20000000` and one download
  worker: f000 and f003 with partitions (7.99 MB each); at f006 "partition layers stopped
  for the rest of the run ... 18.7 MB used, 5.26 MB more needed" and the core read
  (2.73 MB); at f009 "map fields stopped ... 19.6 MB used, 1.83 MB more needed", after
  its atmosphere (0.90 MB). Run `partial`, 19.62 MB downloaded, 3 complete valid times,
  partitions at 2. The wind and pressure of f009 are stored but never served (no wave
  layers at that hour).

### What was verified live, and what with mocks only

* **Live (2026-10-15):** standalone mode chosen automatically with Open-Meteo spot
  providers; the newest complete map cycle found with the 18Z cycle skipped; all 15
  variables downloaded, regridded, stored and served for 4 hours (and f120, f240);
  per-hour bytes with and without partitions; the `pgrb2.0p50` → `pgrb2.0p25` fallback and
  a missing wave hour on a cycle still being uploaded, then the same cycle complete; the
  index freshness fields (`stale` false at 3.67 h and 9.49 h, true at 13.67 h); the `run`
  parameter (retained run 200, unknown run 404); the independent decode at five points on
  two cycles and over the whole grid on a third; the guard masks over every node on three
  cycles; the NDBC direction check; the byte budget stopping the partitions, then the run.
* **Synthetic data only (tests):** a budget too small for one hour, a partition record
  missing from the index, a record failing half-way through a file, `with_noaa` mode with
  partitions (free for a spot on `global.0p25`) and with a cycle outside
  `MAP_FIELD_CYCLES`, the 0p50 fallback inside `current_run`, pruning of a no longer
  retained run, and the stored 6-variable runs of before this change staying servable.
* **Not verified:** a full 61-hour cycle (its bytes and time are extrapolated above), the
  Celery job against NOAA, a `with_noaa` acquisition with partitions against NOAA, and
  behaviour across a GFS version change.

## Earlier measurements (before the forecast upgrade)

The figures in this section were measured on 2026-10-11 and 2026-10-12 with the earlier
configuration: wind and pressure from `pgrb2.0p25`, map hours every 3 h to f240 (81
hours), every cycle, six core variables, no partitions. They still describe the core
fields' regridding and the `with_noaa` mode.

The first set was measured on 2026-10-11 between 20:42 and 20:50 UTC against the NODD
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

### Field statistics then (served by the API, valid 2026-10-11 18Z = f006 of the 12Z cycle)

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

### What was verified live then, and what with mocks only

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
  199° gave 9.9 s from 135°). The partition layers show the systems separately where the
  mixing guard allows; a break's own forecast keeps its own source's values.
* **Partitions are numbered per cell and hour** (WW3). "Primary" and "secondary" can
  swap between neighbouring cells and steps, so a partition layer is not one swell
  tracked across the ocean. The mixing guard hides mixed boxes rather than averaging
  them: on the live hour above it served 90 % of the nodes with primary-swell data, 78 %
  for the secondary swell and 87 % for the wind sea, so these layers can look patchy.
  The guard values (0.8, half) are heuristic. There is no tertiary swell layer.
* Wind and pressure come from the 0.5° GFS cells (no smoothing), the waves from 0.5°
  box means of 0.25° cells. Map wind at a spot can differ from the spot forecast's wind
  (another source, model cell and height convention).
* All layers are model output (GFS / GFS-Wave), not observations, and they carry the
  model's errors. Wave heights are offshore significant wave heights, not surf height.
  They are not for navigation.
* Quantisation steps are 0.1 m, 0.1 s, 1.5°, 0.5 m/s and 1 hPa. Values beyond the byte
  ranges are clamped, for example Hs above 25.4 m or wind components beyond ±63.5 m/s.
  Partition periods above 25.4 s do occur: on 2026-10-15 12Z f006, 63 primary-swell and
  90 secondary-swell nodes had box means above 25.4 s (up to 27.6 and 28.5 s) and are
  served as 25.4 s. They are small swells (0.0 to 0.5 m): the primary-swell ones all
  between 72N and 77N near the sea ice, the secondary-swell ones there and in the
  Southern Ocean. Byte 254 of a period therefore means "25.4 s or more".
* Pressure over high terrain (Antarctica, Greenland, Tibet) is reduced to sea level and
  is not a real surface pressure.
* **Freshness with two cycles a day.** `age_hours` counts from the model cycle. The job
  takes a cycle once `NOAA_AVAILABILITY_LAG_HOURS` (5 h) have passed and at its next run
  after that (every `FORECAST_REFRESH_MINUTES`, 3 h by default), so under normal
  operation the served cycle is 12 h + 5 h + up to 3 h, i.e. 17 to 20 h old just before
  the next map cycle replaces it. With `MAP_FIELD_STALE_AFTER_HOURS=12` the index calls
  the layers stale for 5 to 8 h of every 12 (they are still drawn, with a badge); seen
  live at 13:40 UTC on 2026-10-15, when the newest map cycle (00Z) was 13.67 h old. To
  flag only a missed cycle, the threshold must exceed that normal maximum: about 21 h
  with `MAP_FIELD_CYCLES=00,12`. Adding `06,18` (about 0.97 GB a day more) is not enough
  on its own: the normal maximum is then 6 + 5 + 3 = 14 h, so 12 h would still flag up to
  2 h of every 6 (about 15 h would not).
* GFS v17 (proposed) is expected to rename files, drop the 0.5° and 1° atmosphere files
  and move WW3 to an unstructured grid. The path templates are configuration and the
  atmosphere falls back to `pgrb2.0p25` per hour, but the `global.0p25` wave grid may
  change or disappear. A geometry change triggers a warning.
