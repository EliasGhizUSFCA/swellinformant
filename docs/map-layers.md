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
* Serving on Vercel and Neon (the publish gate, CDN caching, binary reads, database
  egress): [Serving, caching and database egress](#serving-caching-and-database-egress-upgrade-4);
  the measuring script is `scripts/measure_db_egress.py`
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
| `MAP_FIELD_STALE_AFTER_HOURS` | 21 | The index (and the data-health page) flags the layers `stale` once the served model cycle is older than this. 21 h because with cycles 00,12 a cycle is normally 17 to 20 h old before the next one replaces it, so the badge means a late or missed cycle (see Limitations) |
| `MAP_FIELD_RESOLUTION_DEG` | 0.5 | Output grid spacing. Must divide 180 (0.25, 0.5, 1, …). Otherwise (e.g. 0.7) a NOAA spot run stores no fields and says why in `details.map_fields.warnings`, and the standalone job fails with that message; spot forecasts are unaffected |
| `MAP_FIELD_MAX_LEAD_HOURS` | 240 | Caps `MAP_FIELD_STEPS` (the CLI's `--max-lead-hours` overrides it for one run) |
| `MAP_FIELD_STEP_HOURS` | 3 | **Deprecated**, not read: `MAP_FIELD_STEPS` decides the map hours. Still accepted so old `.env` files load |

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
  The run served is the map run of the newest sync publication while `SYNC_PUBLISH_GATE`
  is on (the default) and that run is still servable; then `published_at` and
  `sync_run_id` name that publication. Otherwise (no publication yet, the gate off, or the
  published run pruned or deleted) it is the newest run (by model cycle) with at least
  one complete valid time, and `published_at` and `sync_run_id` are null unless that run
  is the newest publication's. When several runs hold that cycle (for example the NOAA
  spot run and a fields-only run after a change of mode), the one with the most complete
  valid times is served, and on a tie the newer run (`map_fields.servable_runs`, the same
  order retention uses). Incomplete times are logged and left out. Caching: browsers
  60 s, the CDN 300 s plus `stale-while-revalidate=3600`, the windows of every public GET
  (see the table below).
* `GET /api/map/fields/{variable}?at=ISO&run=RUN_KEY` returns one field as
  `application/octet-stream`. The decoding parameters are in the `X-Field-Meta` header
  (JSON with the keys of `FieldMeta`, including `group`, `valid_time` and `run_key`;
  `Access-Control-Expose-Headers: X-Field-Meta`).
  * `run` (the index's `run_key`) serves that model run while it is retained (the two
    best servable runs are). An unknown, pruned or no longer servable run is **404 with
    the error code `run_not_retained`** ("Map field run not retained"), never another
    run's field. Without `run` the index's run is served (the published one under the
    gate).
  * Caching (`app.core.http_cache.cache_headers`): a URL with `run` and an `at` equal to
    the served valid time is immutable (browsers an hour, the CDN a year); any other
    field URL is cached 5 minutes by both. Error responses are `no-store` and never carry
    `CDN-Cache-Control`. See the table below.
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
    row that is served, through a binary cursor (one statement, metadata included).
* The map client (`frontend/lib/map-fields.ts`, `hooks/map-fields.ts`) asks for a
  partition only when the index lists it with `group` "partition" and a `system`
  (`primary`, `secondary`, `windsea`); a 404 for a listed partition is treated as an
  out-of-date index, which is re-read (at most every 30 s). Partitions the user has not
  chosen to draw are not downloaded. The wave systems are drawn as stylised travelling
  crests and the wind as particles (rendering, scaling and limitations:
  [map-rendering.md](map-rendering.md)).

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
  With the synced pipeline (Upgrade 4) a sync's `map` stage runs the same acquisition when
  a new map cycle is out, and its `publish` stage makes the run visible (see The publish
  gate below).
* **CLI.** `python -m app.cli engine map-fields [--cycle YYYYMMDDHH] [--max-lead-hours N]
  [--force]` works in every mode except `off`. It prints JSON with counts, stored and
  downloaded bytes, requests, rows pruned, the duration and per-hour figures. `--cycle`
  takes any cycle, including one outside `MAP_FIELD_CYCLES`, without the availability
  check (hours not yet published become gaps).
* **Retention.** `maintenance.prune_map_fields` keeps the fields of the two best
  servable runs, in the read side's order (`map_fields.servable_runs`: newest cycle,
  then most complete valid times, then the newer run), and the map run of the newest
  sync publication, so the served run is never deleted (under `SYNC_PUBLISH_GATE` it is
  the published run), and the previous run stays available to clients that still name it
  with `run`. Normally the published run is one of the two best anyway (a sync stores one
  new map run and then publishes it); a third run is kept only while newer runs wait for
  a publication. Every other `map_fields` row goes, including rows of runs without a
  single complete valid time (they are never served). It runs right after every
  acquisition that stores fields (standalone job and NOAA spot run), in every sync's prune
  stage (`maintenance.prune_pass`, with `SYNC_PRUNE_EACH_PASS`) and in
  `maintenance.cleanup` (reported as `map_fields`), so storage stays at about two runs.
  One DELETE statement picks the runs to keep and deletes the rest, so fields that another
  acquisition commits meanwhile are not deleted. A forecast run removed by its own
  retention takes its fields with it (FK cascade; the publication's `map_run_id` becomes
  null and the index falls back to the newest servable run).

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

(The cache headers below are the ones of 2026-10-15; Upgrade 4 changed them, see
[Serving, caching and database egress](#serving-caching-and-database-egress-upgrade-4).)

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

## Serving, caching and database egress (Upgrade 4)

Hosted on Vercel (Hobby) with Neon Postgres (Free), the map's costs that matter are the
bytes Postgres sends to the API and to the sync ("egress": Neon Free includes 5 GB a
month, and running out suspends the compute until the next billing period; storage is
1 GB, https://neon.com/docs/introduction/plans, read 2026-10-18) and the requests that
reach the API function at all. This section covers what serves the map, how responses are
cached in front of it, and what one sync and one page view read from the database.

### The publish gate

A sync (app.services.sync) stores a new map run in its `map` stage and makes it visible
in its `publish` stage, which commits `sync_runs.published_at` and `map_run_id` (the best
servable run then). With `SYNC_PUBLISH_GATE=true` (the default):

* `GET /api/map/fields` serves the map run of the newest publication (`published_at`, then
  id) while it is servable, even when a newer run is already stored: the map switches to
  a new GFS cycle together with the spot forecasts of the same sync, not when the map
  stage happens to finish. `published_at` and `sync_run_id` in the index name that
  publication (for the map's freshness chip).
* `GET /api/map/fields/{variable}` without `run` serves the same run. With `run` it serves
  the named retained run, as before (a client never mixes two runs).
* Fallback to the newest servable run (`published_at` and `sync_run_id` null): before the
  first publication, when the newest publication has no map run (none was servable then),
  when the published run is no longer servable (its fields pruned or incomplete) or was
  deleted (`map_run_id` is set to null by the foreign key).
* `SYNC_PUBLISH_GATE=false` always serves the newest servable run (the behaviour before
  Upgrade 4); the index still names the newest publication when it happens to be its run.
* Retention keeps the published run besides the two best servable ones (see Storage,
  jobs and retention), so the gate never points at a run that pruning removed while it
  was served.

The index reads the publication through one three-column query (id, published_at,
map_run_id; the same filter and order as `app.services.sync.published.latest_publication`),
never the sync's plan or stages, and the run's key, completion time and source without its
`details`. Tests: `backend/tests/integration/test_map_publish_gate.py`.

### Browser and CDN caching

Both endpoints are the same for every visitor (no user dependency, no cookie), so they
carry shared-cache headers (`app.core.http_cache.cache_headers`). Vercel's CDN reads
`CDN-Cache-Control` before `Cache-Control` and caches a function response only with
`s-maxage`, never one with `Set-Cookie` or for a request with an `Authorization` header,
up to 10 MB (https://vercel.com/docs/caching/cdn-cache, read 2026-10-18; a field is at
most about 115 kB).

| Response | `Cache-Control` (browsers) | `CDN-Cache-Control` | Why |
|----------|----------------------------|---------------------|-----|
| `GET /api/map/fields` (the index) | `public, max-age=60` | `public, s-maxage=300, stale-while-revalidate=3600` | It names the served run, which a publication switches about every 12 hours. After a publication the CDN can still hand out the previous index for up to 300 s, and for up to an hour more to the requests that trigger its refresh; browsers keep it 60 s, and the map re-reads it every 5 minutes. These are the windows of every other public GET (next row), so the edge switches the index, the conditions and the spot list together (60/600 s until the egress integration). Since round 2 of the egress integration the API answers it from the public snapshot cache too (its age and staleness judged at the start of the UTC hour), so a CDN refresh every 5 minutes no longer wakes the database. |
| The other user-independent public GETs: `/api/spots`, `/api/map/conditions`, a spot's detail, `/forecast`, `/timeline` (with its ETag and 304), `/events`, `/tides`, `/observations`, `/api/events` and `/api/system/status` | `public, max-age=60` | `public, s-maxage=300, stale-while-revalidate=3600` | Their data changes with a publication or the hour; the API answers all of them but `/tides` and `/observations` from the public snapshot cache (Egress integration below). `/api/system/status` was `no-store` until round 2: it has no user dependency, and its job list is now the one of the hour's first request (new after every sync). |
| Routes with a user dependency (`/api/auth/me`, a spot's `/opportunities`, accounts, searches, notifications) | `no-store` (the middleware's default); a response that sets a cookie never keeps shared-cache headers (`private, no-store`) | none | One visitor's answer must never be handed to another. |
| A field URL with `run` and an `at` equal to the served valid time (what the map requests: `fieldUrl(variable, at, run)` with times from the index) | `public, max-age=3600, immutable` | `public, s-maxage=31536000, immutable` | The URL names the model run and the valid time, and a run's stored field never changes, so the CDN can keep it for a year and the database reads each such field about once per CDN cache. |
| Any other field URL: no `run`, no `at` (now), or an `at` between valid times | `public, max-age=300` | `public, s-maxage=300` | The answer can change with the next publication or with the clock, so it is cached for 5 minutes only. The served time and run are in `X-Field-Meta`, which the map checks. |
| Errors (404 `run_not_retained`, 404 no field, 422) | `no-store` (the security middleware's default) | none | A missing field or run must not stick at the edge. |

`X-Field-Meta` stays exposed (`Access-Control-Expose-Headers`). An immutable URL of a run
that is later pruned can still be answered from the CDN with that run's field (the true
field of that run and time); the map only asks for the run its current index names.

### Reading a field from Postgres

`map_fields.data` is `bytea`. psycopg's default text result format sends bytea as hex, two
characters for every stored byte, so the database sent about twice the field's size for
every field request (2.06× measured, below). The field is now read with a binary cursor on
the session's own connection and transaction (`map_fields._field_row`: one statement for
the row's metadata and bytes, keyed by run, variable and valid time), and the bytes come
back as they are stored. Served bytes are identical (tests compare them with the stored
row); the index never reads `data`.

### Storage per run

From the stored sizes above (2026-10-14/15 cycles, 0.5° grid): one valid time is 516 kB
for the six core variables and 1.16 MB with the nine partition variables. With the
default 61 map hours a run is about 71 MB with partitions (`MAP_FIELD_PARTITIONS=on`,
the default) and about 31.5 MB without; the two retained runs are about 142 MB (or 63 MB).
A third run is kept only while newer runs wait for a publication. On Neon Free (1 GB for
everything) the map is therefore about 14 % of the storage with partitions and 6 %
without; the spot forecasts, predictions and archive share the rest (the free-database
retention profile is `app.core.profiles.FREE_DATABASE_PROFILE`).

### Database egress, measured (2026-10-22)

`scripts/measure_db_egress.py` puts a counting TCP proxy between the app and Postgres and
counts every byte of every connection, in both directions, around each pipeline stage
(and around chosen functions inside a stage) or each API request. TLS is on between the
app and the local Postgres (sslmode prefer, as Neon requires TLS), so the counts include
it. "Before" is the code at 2d3ffe7 (the Upgrade 4 foundation), run from an extracted copy
of that commit; "after" is this package. Both ran on the same machine against the same
template database, one after the other.

**One demo pass** (`measure_db_egress.py pipeline`: a fresh `init-db` scratch database
and `run_pipeline` exactly as `python -m app.cli pipeline` runs it, with
`FORECAST_PROVIDERS=demo`: 50 spots, 19,200 SYNTHETIC forecast rows and predictions, 4,050
archive rows, 150 swell events, no saved searches). DB → client, MB:

| Stage | Before | After |
|-------|-------:|------:|
| acquisition | 0.61 | 0.61 |
| predictions (scoring and archive) | 97.94 | 14.87 |
| of which the archive | 88.14 | 5.08 |
| detection | 88.45 | 3.63 |
| of which `load_steps` (50 calls) | 88.24 | 2.67 |
| matching, flights, alerts, delivery | 0.32 | 0.32 |
| **pass total** | **187.31** | **19.43** |
| client → DB (writes) | 90.07 | 90.11 |

The research measured 186.8 MB for the same pass before (D6). The rows after the pass were
identical (19,200 predictions, 4,050 archive rows, 150 events). With `--searches 3` (one
SYNTHETIC account with three saved searches for every spot) the same pass reads 239.73 MB
before and 24.02 MB after: matching alone 49.63 → 1.81 MB (`load_steps` for 51 events:
49.32 → 1.50 MB), with the same 150 matches.

**One pass on a copy of the real 2026-10-16 live run** (`swell_u3i_live_run`: Open-Meteo
GFS and ECMWF spot runs of 2026-10-16 00Z, their blend, GEFS statistics and 9 hours of map
fields; `measure_db_egress.py pipeline --template swell_u3i_live_run --providers
open_meteo,open_meteo_ecmwf --replay --no-acquire --searches 3 --now
2026-10-16T06:00:00Z` with `GEFS_ENABLED=true`). `--replay` deletes the blend run and the
predictions of both member runs before the pass, so the pass re-blends and re-scores
25,000 predictions of real forecasts, as after new model runs; `--searches 3` adds one
SYNTHETIC account with three saved searches for every spot, so matching evaluates the
events as it does for a real user. No provider was called. DB → client, MB:

| Stage | Before | After |
|-------|-------:|------:|
| blending | 7.80 | 7.80 |
| predictions | 93.75 | 43.02 |
| of which the archive | 56.70 | 5.96 |
| of which scoring the three runs (`compute_predictions_for_run`) | 37.05 (93.75 − 56.70) | 37.05 |
| detection | 57.32 | 2.69 |
| of which `load_steps` (50 calls) | 56.85 | 1.68 |
| matching | 6.80 | 0.48 |
| of which `load_steps` (19 events) | 6.52 | 0.20 |
| flights, alerts, delivery | 3.28 | 3.28 |
| **pass total** | **168.95** | **57.26** |

Rows after the pass were identical (25,000 predictions, 4,050 archive rows, 98 events, 57
matches). Inside "scoring" (not changed here): the confidence context 9.21 MB (3 calls),
tide stages 8.18 MB (150 calls: every spot's tide predictions read again for each run),
the ensemble index 1.02 MB; the rest (about 18.6 MB) was not broken down further and
includes reading every `wave_forecasts` row of the three runs as a whole entity.

**One page view, anonymous, without a CDN in front** (`measure_db_egress.py pages
--template swell_u3i_live_run --providers open_meteo,open_meteo_ecmwf --publish --now
2026-10-16T06:00:00Z`: the requests the map page and a spot page make). DB → API, kB:

| Request | Response | Before | After |
|---------|---------:|-------:|------:|
| `/api/spots` | 300.6 | 15,730.6 | 15,730.6 |
| `/api/map/fields` (index) | 3.0 | 7.9 | 5.3 |
| `/api/map/conditions` (twice: now and the field time) | 182.6 | 1,062.7 | 1,062.7 |
| 9 fields (`hs`, `tp`, `wave_dir` at the step on screen and both neighbours) | 708.9 | 1,463.0 (2.06×) | 722.4 (1.02×) |
| **map page** | 1,195.0 | **18,264.2** | **17,521.0** |
| spot page (`/api/spots/pipeline`, `/forecast`, `/timeline`, `/events`) | 630.5 | 1,597.7 | 1,597.7 |

`--now` sets the map page's step only; `/api/spots`, the conditions and the spot page read
"now" from the wall clock, so their figures move a little from one day to the next (a
re-run on 2026-10-22 at about 13:50 UTC: `/api/spots` 15,732.4 kB, spot page 1,545.7 kB
for 599.9 kB; the index and the 18 field requests were the same, 1.019× in total).

A field now costs 1.015 to 1.025 times its payload in database reads (it was 2.05 to 2.09
times). Behind the CDN an immutable field URL is read from the database about once per
CDN cache and run, and the index at most about once a minute per cache.

**Against Neon Free's 5 GB a month** (as measured by that package; the egress integration
below did items 1 to 3 and re-measured everything). At about 6 syncs a day (4 GFS, 2
ECMWF; D3), a pass
like the replayed one above (it re-scores all three runs, so a real pass reads somewhat
less) costs about 57 MB × 6 × 30 = 10.3 GB a month after this package, against 30.4 GB
before; the demo pass extrapolates to 3.5 GB (before: 33.7 GB). So the sync alone still
exceeds Neon Free's egress, and the largest remaining readers are outside the map:

1. `/api/spots` reads 15.7 MB for a 301 kB response (by the code, `spot_views.
   _current_and_best` loads whole predictions with their forecasts, the forecast twice,
   for every spot and 7 days, to pick two hours per spot), so one
   uncached map page view is about 17.5 MB: about 285 such views exhaust 5 GB.
2. Scoring (`surf_quality/predict.py`, `confidence_context.py`, `tides/stage.py`) reads
   37 MB per replayed pass.
3. The `SurfQualityPrediction.forecast` relationship is `lazy="joined"`: every query that
   selects predictions together with `WaveForecast` reads each forecast row twice.

These are recorded as follow-ups for their owners; this package changed the archive,
detection, matching's step reads and the map endpoints only.

How to reproduce (from the repository root, with Postgres and Redis running):

```bash
backend/.venv/bin/python scripts/measure_db_egress.py pipeline --redis-url redis://localhost:6379/11
GEFS_ENABLED=true backend/.venv/bin/python scripts/measure_db_egress.py pipeline \
  --template swell_u3i_live_run --providers open_meteo,open_meteo_ecmwf \
  --replay --no-acquire --searches 3 --now 2026-10-16T06:00:00Z \
  --redis-url redis://localhost:6379/11
backend/.venv/bin/python scripts/measure_db_egress.py pages --template swell_u3i_live_run \
  --providers open_meteo,open_meteo_ecmwf --publish --now 2026-10-16T06:00:00Z \
  --redis-url redis://localhost:6379/11
```

Each run creates a scratch database (`--database`, default `swell_egress_<pid>`), drops it
afterwards unless `--keep-db`, never writes to the template, and prints the table; `--json
FILE` saves it. `--backend-dir` measures another checkout of `backend/` (the "before"
numbers above), and `--part MODULE:ATTR:'<stage> > <name>'` counts one more function.

### Egress integration: one sync, the periodic jobs and a page view (2026-10-22)

The integration narrowed what the public read paths and scoring read, put the heavy public
GETs behind a snapshot cache in Redis, gave every public GET one CDN window, and applies
the forecast-run retention after every publishing sync. Every number below was measured
with `scripts/measure_db_egress.py` on this machine against a copy of `swell_u3i_live_run`
(the real 2026-10-16 00Z run: 50 spots, Open-Meteo GFS and ECMWF spot runs, their blend,
GEFS statistics, 9 map hours) or the demo provider, with the app's clock frozen
(`--now`). "Before" is the base commit 3a2b4005 run from an extracted copy
(`--backend-dir`); "after" is this branch.

**What changed**

* **Public read paths** (`app/services/spot_views.py`, `timeline.py`): the hours to show
  are chosen from a few columns, and only the chosen hours are read in full;
  predictions are read together with their forecasts in one join, each forecast column
  once (`predictions_with_forecasts`, `contains_eager`; `SurfQualityPrediction.forecast`
  stays `lazy="joined"` for swell detection's peak query). A run's attribution links
  are read once per list of events; a blend's member sources by the five columns shown.
  `/api/spots` takes 12 statements whatever the number of spots (57 before, for 50).
* **Scoring** (`surf_quality/predict.py`, `confidence_context.py`, `tides/stage.py`):
  the scoring forecasts by the columns and `extra` keys scoring uses; each spot's tide
  stations and predictions once per pass for every run scored (`shared_tide_reads`);
  the research assessments, decisions and ensemble indexes once per pass; a blend's
  comparisons reduced in SQL to the members' families. Predictions are identical: a
  replayed clone of `swell_u3i_live_run` scored with this branch and with the base gave
  25,000 rows, 3,816,821 compared values, 0 mismatches, max abs diff 0.0.
* **Public snapshot cache** (`app/services/public_cache.py`): `/api/spots`,
  `/api/map/conditions`, the spot detail, forecast, timeline and events and
  `/api/events` are computed for the start of the current UTC hour, at most once per
  key `public:v1:<epoch>:<marker>:<YYYYmmddTHH>:<route>[:<sha256 of the params, 24 hex>]`,
  stored zlib-compressed for 2 hours (`PAYLOAD_TTL_SECONDS`) and kept in each
  process's memo (up to 32 MB). `marker` is the sync's (`note_published`, 24 h TTL) or,
  without one, derived from the database in one small statement; while a sync's marker
  is stored, a change made outside a sync (the development endpoints, a CLI pipeline or
  rescore) shows from the next hour. `prewarm` computes the spot list and the conditions
  now for the hour (the sync can call it after the hook; with Redis unavailable it reads
  nothing). The first request for
  a key takes its lock (`SET NX EX 60`); concurrent ones poll for its result (about
  6.4 s at most, then compute unstored). Redis down: every request computes directly
  and Redis is not asked again for 30 s. A spot page's forecast and timeline read the
  same hours and are computed together under one lock (`Together`). `?at=` is cached
  only for a whole UTC hour within 2 days before and 17 days after now (the map's
  steps); any other value is computed as before, unstored. The bodies are the ones
  FastAPI's response models give (tested byte for byte; 156 responses at 4 in-hour
  times equal the base commit's at the hour starts).
* **CDN windows**: every public GET and the map-field index send `Cache-Control:
  public, max-age=60` and `CDN-Cache-Control: public, s-maxage=300,
  stale-while-revalidate=3600`; a run-keyed field URL stays immutable (a year at the
  CDN); errors and user-specific responses stay `no-store`, and a response that sets a
  cookie never keeps shared-cache headers (it becomes `private, no-store`).
* **Retention per pass**: `maintenance.prune_pass` (the sync's prune stage with
  `SYNC_PRUNE_EACH_PASS=true`) now also deletes the forecast runs older than
  `RETENTION_FORECAST_DAYS`, as the daily cleanup does; its result gained the key
  `forecast_runs` (the count deleted).

**Behaviour change: the hour floor.** The cached routes compute "now" as the start of the
current UTC hour, so within an hour every visitor sees the same answer: the current
conditions are the stored step nearest to the hour's start, and the forecast and timeline
windows start 3 hours before it (one more early step from the first minute of a step
hour on). `generated_at` of the timeline is the hour's start.

**One sync pass**, DB → client, MB (`pipeline --replay --replay-sources CODE
--no-acquire --searches 3 --prune --now 2026-10-16T06:00:00Z`: the newest run of that
family and the blend are re-blended and re-scored, as after a new model run; one
SYNTHETIC account with three saved searches for every spot):

| Pass | Before | After | After, no saved searches |
|------|-------:|------:|-------------------------:|
| GFS sync (`open_meteo_noaa` and the blend: 20,800 predictions) | 49.19 | 30.57 | 27.20 |
| ECMWF sync (`open_meteo_ecmwf` and the blend: 14,600 predictions) | 47.10 | 29.00 | 25.63 |
| Both families and the blend (25,000 predictions) | 57.24 | 31.41 | |
| Demo pass with acquisition (fresh `init-db`, 19,200 predictions) | 19.45 | 14.24 | |

The GFS sync by stage (MB, before → after): blending 7.80 → 7.80; predictions 34.96 →
16.41, of which the archive 5.96 → 5.96, scoring two runs 28.99 → 10.00 (the confidence
context 8.11 → 1.58, tide stages 5.57 → 2.62, ensemble indexes 0.68 → 0.16); detection
2.69 → 2.69 (its `load_steps` 1.68); matching 0.48 → 0.48; flights 1.65 → 1.65; alerts
1.26 → 1.19; delivery 0.35 → 0.35; prune 0.01 → 0.01. No Redis command during a pass.
What remains is mostly outside the files this integration owns: the blend reads both
members' forecasts whole (7.16 MB of 7.80), the archive reads each prediction's
`confidence_inputs` (5.95 MB for 4,050 rows), detection, flights and alerts.

**The periodic jobs between syncs** (`jobs --replay --searches 3 --now
2026-10-16T07:00:00Z`: each job of the daemon's hourly batch once, after a pass), DB →
client, MB, before → after: `matching.match` 0.88 → 0.88, `flights.discover` 0.31 →
0.31, `alerts.generate` 1.06 → 0.97, `notifications.deliver` 0.00 → 0.00,
`forecasts.check_staleness` 0.02 → 0.02, `ops.data_health` 0.05 → 0.05 (2 Redis
commands): **the hourly batch 2.32 → 2.23 MB** (0.35 MB without saved searches); the
daily `maintenance.cleanup` 0.01 MB. Most of it is whole `opportunity_matches` rows
(about 7 to 8.5 kB each, read by matching and alerts) and whole predictions in alerts.

**One page view, anonymous, no CDN in front** (`pages --publish --now
2026-10-16T06:00:00Z`; the requests the frontend sends for it). DB → API, kB, and Redis:

| Page view | Before | After, cold | After, new instance (`warm-redis`) | After, warm instance |
|-----------|-------:|------------:|-----------------------------------:|---------------------:|
| home `/` (4 requests) | 1,177.3 | 403.7 (11 commands, 10.3 kB) | 22.7 (4 commands, 9.2 kB) | 22.7 (2 commands, 0.2 kB) |
| map `/map` (15 requests) | 40,527.9 | 2,520.2 (16, 41.4 kB) | 750.4 (6, 39.8 kB) | 750.4 (3, 0.3 kB) |
| spot page (6 requests) | 3,041.6 | 1,187.5 (18, 102.6 kB) | 22.7 (8, 100.9 kB) | 22.7 (4, 0.4 kB) |

"Cold": the snapshot cache emptied; "new instance": only this process's memo emptied.
What a warm view still reads: `/api/system/status` (22.7 kB, `no-store`, on every page)
and, on the map, the index (5.3 kB) and 9 fields (722.4 kB, 1.02 times their payload),
which the CDN keeps (300 s and a year). Without the sync's hook (`--no-hook`) each cached
request also reads the database marker, about 0.6 kB (home warm 23.9 kB, spot 25.1 kB).

What one key costs when it is computed (at most once per key and hour): `/api/spots`
1,054.3 kB; `/api/map/conditions` 357.7 kB (now, and again for each map step asked);
`/api/events?limit=4&min_score=57` 23.3 kB; a spot page (detail 66.8, forecast and
timeline together 1,077.9, events 20.1) 1,164.8 kB. A map field is about 80.3 kB per CDN
cache and run.

**Monthly budget against Neon Free (5 GB egress) and Upstash Free (500K commands, 10 GB
bandwidth, 250 MB).** 30 days; the background rows are the measured passes and jobs times
the cadence; the page rows are the measured unit costs times the traffic stated (the
traffic is an example, not a measurement).

| Item | Cadence | Before | After |
|------|---------|-------:|------:|
| GFS syncs | 4 a day (hosted cadence, docs/sync.md) | 5.90 GB | 3.67 GB |
| ECMWF syncs | 2 a day | 2.83 GB | 1.74 GB |
| Hourly batch (3 saved searches) | 24 a day (`JOB_INTERVAL_FLOOR_MINUTES=60`) | 1.67 GB | 1.61 GB |
| Daily cleanup | 1 a day | 0.0003 GB | 0.0003 GB |
| **Background, no visitors** | | **10.40 GB (208 %)** | **7.02 GB (140 %)** |
| Background without saved searches | | | 5.06 GB (101 %) |
| Pages, "quiet": 4 hours a day with a map and home view, 1 new spot page in each, 100 views a day, 9 fields a run, 1 CDN cache | | | 0.47 GB |
| Pages, "steady": every hour, 2 new spot pages an hour, 1,000 views a day, 9 fields a run, 3 CDN caches | | | 3.78 GB |

So the sync and the hourly batch alone exceed Neon Free at the documented cadence, before
any visitor; extra syncs (a GFS domain published late makes another one) add a pass each.
A page view costs the database only when its key is computed (once per key and hour) plus
22.7 kB for `/api/system/status`; "steady" is 1.29 GB for the map and home keys every
hour, 1.68 GB for 1,440 spot-page hours, 0.68 GB for the status and 0.13 GB for fields.

**What the owner must change to stay on Neon Free** (settings only, no code; superseded by
round 2, "Free database budget" below, which lowered the costs and made these settings the
free-database profile's defaults):

1. `SYNC_TRIGGER_SOURCES=map`: a sync only for a new map cycle (00Z and 12Z,
   `MAP_FIELD_CYCLES`), which also acquires the newest spot runs of both families. Two
   passes a day like the measured both-family pass: 2 × 31.41 MB × 30 = **1.88 GB**.
   Cost: spot runs are acquired only at those two syncs a day (the newest run each
   family offers then), so most GFS runs are never used and an ECMWF run can wait up to
   12 h; GFS (`open_meteo_noaa`) is stale after 17.2 h, so with the publication lags
   measured on 2026-10-14/15 (5.3 to 8.2 h after the cycle) the spot forecasts are
   flagged stale (confidence capped at Moderate) for up to about 3 h of every 12 before
   the next sync, and longer when a GFS spot run comes more than SYNC_COALESCE_MINUTES
   (90) after its map cycle's layers (it then waits for the next map cycle); alerts pause
   only at twice the stale-after age. Not tried on a live stack.
2. `JOB_INTERVAL_FLOOR_MINUTES=360`: the hourly batch every 6 hours: 4 × 2.23 MB × 30
   = **0.27 GB**. Cost: matching, flight refresh, alerts and delivery between syncs, the
   staleness check and data health run every 6 hours (each sync still runs matching to
   delivery itself; "match my search" and "refresh flights" kicks still run within a
   minute, SYNC_DAEMON_POLL_SECONDS).
3. With both, the background is **2.15 GB a month (43 %)** and about 2.85 GB remain for
   page views: the map and home keys every hour (1.29 GB) and about 1,340 spot-page
   hours a month (about 45 a day), less 22.7 kB for every page view that reaches the
   API. "Quiet" fits (2.62 GB in all); "steady" does not (5.93 GB).

Otherwise Neon Launch (500 GB egress, docs/deploy-vercel.md) holds every row above many
times over. The remaining code reductions are listed as follow-ups for their owners (the
blend's and the archive's whole-row reads, `/api/system/status`, `opportunity_matches`
in matching and alerts).

**Upstash.** The cache sends 1 command for a request answered from the process's memo
(the epoch and marker, one MGET), 2 from Redis (MGET, GET) and 5 to 6 when it computes a
key (MGET, GET, SET NX, SET, EVAL; one more SET for the companion of a spot page). The
daemon itself sends about 13,000 commands a month for its checks and 43,200 for its kick
polls (docs/sync.md). "Steady" above: about 43,000 commands for computed keys (12 keys an
hour at about 5 each), and its 1,000 views a day make at most about 3,000 cached requests
(home 2, map 3, a spot page 4) of at most 2 commands each, 180,000 a month if the CDN
answered none: about 280,000 a month with the daemon's, under 500K. Bandwidth: a
new-instance view moves 9.2 (home), 39.8 (map) or 100.9 kB (spot page) and a warm one
under 0.5 kB; at most about 1.5 GB a month at 1,000 views a day of 50 kB, plus about
0.2 GB of stored payloads: under 10 GB. Data: the payloads of one hour are about 250 kB
for "steady" and about 10 MB if every spot page were asked every hour (2-hour TTL),
against 250 MB. Every request that reaches the API costs at least one command (a 404 for
an unknown spot about four: MGET, GET, SET NX and the lock's EVAL), so a flood of URLs the
CDN has not cached costs commands as it costs function invocations; these routes have no
rate limit.

**How to reproduce** (from the repository root, Postgres and a scratch Redis database
running; `--json FILE` saves each result, `--backend-dir DIR` measures another checkout
of `backend/`):

```bash
PY=backend/.venv/bin/python; S=scripts/measure_db_egress.py
LIVE="--template swell_u3i_live_run --providers open_meteo,open_meteo_ecmwf"
$PY $S pipeline --redis-url redis://localhost:6379/11 --prune
$PY $S pipeline $LIVE --replay --replay-sources open_meteo_noaa --no-acquire --searches 3 \
  --now 2026-10-16T06:00:00Z --prune --redis-url redis://localhost:6379/11
$PY $S jobs $LIVE --replay --searches 3 --now 2026-10-16T07:00:00Z \
  --redis-url redis://localhost:6379/11
$PY $S pages $LIVE --publish --now 2026-10-16T06:00:00Z --redis-url redis://localhost:6379/11
```

**Limitations of these numbers.** Acquisition on live data was not measured (the replayed
passes use `--no-acquire`; the demo pass's acquisition read 0.61 MB), nor the `map` stage
(its GRIB download writes fields but its reads were not counted), nor `sync.tick` and the
API's rate limiter. The traffic of the page rows is an example. The page views were made
with FastAPI's TestClient in one process, without Vercel's CDN; how often the CDN answers
instead of the API is not measured here. A signed-in visitor's own requests (`/api/auth/me`
and the user-specific routes, `no-store`) were not measured. TLS to the local Postgres is
counted as Neon's would be, but Neon's own protocol overhead may differ.

### Free database budget: round 2 of the egress integration (2026-10-23)

Round 2 narrowed the remaining heavy reads, served the last DB-backed reads every page view
made (`/api/system/status`, the map-field index) from the public snapshot cache, wired the
cache's publication hook, and moved the free-database profile to a sync cadence that fits.
The same measurement setup as above (`scripts/measure_db_egress.py`, a replayed copy of
`swell_u3i_live_run`, clock frozen at 2026-10-16 06:00Z, one SYNTHETIC account with three
saved searches for every spot); "before" is the integration base f3c15d6 run from an
extracted copy (`--backend-dir`).

**What changed in the code**

* The blend reads its members' forecasts by the columns it combines and the two `extra`
  keys it reads (gust definition, interpolated mark): 7.80 → 3.52 MB per pass.
* The prediction archive reads, with `ARCHIVE_CONFIDENCE_DIGEST` on (the default), only
  the parts of each prediction's `confidence_inputs` its digest keeps
  (`archive.DIGEST_READS`): 5.96 → 2.45 MB for 4,050 rows. Off, it still reads them whole.
* Matching reads its matches without their joined search, event, spot and best offer, and
  the events without their `history`; flight discovery reads its matches without the
  event and best offer, each flight search's offers once per run, and the airports by the
  four columns used, once; alerts read the event, the best offer and the forecast run
  once each for all candidates, the peak hour by the eight columns an alert shows, the
  flight provider and each run's model-run lines once per job; delivery reads only the
  search's status. Per pass: matching 0.47 → 0.35, flights 1.63 → 0.57, alerts 1.16 →
  0.22, delivery 0.35 → 0.15 MB.
* `/api/system/status` (on every page: the demo ribbon and the forecast freshness) reads
  its job list in one statement and is served from the public snapshot cache with the
  public headers (it was `no-store`): 22.7 kB on every page view before, 16.3 kB once per
  publication and hour now. Its job list is that of the hour's first request (new after
  every sync).
* The map-field index is served from the public snapshot cache too (its age and staleness
  judged at the start of the UTC hour); the database marker now also follows the map
  fields.
* The publication hook (`public_cache.note_published`) is called by every sync after its
  publication and at its end, by `run_pipeline` (the developer endpoints and `python -m
  app.cli pipeline`) and by `python -m app.cli sync publish`. `SYNC_PREWARM_PUBLIC_CACHE`
  (default false) also computes the spot list and the conditions right after a sync
  (about 1.4 MB a publication).
* Every output is unchanged: one replayed pass (blend, 25,000 predictions, the archive,
  detection, matching, flights, alerts, delivery) and the hourly jobs an hour later, run
  with the base and with this code (deterministic ids and tokens, frozen clocks), left
  identical rows in all 15 tables compared (wave_forecasts 25,000, forecast_comparisons
  10,400, surf_quality_predictions 25,000, prediction_archive 4,050 by content,
  swell_events 98, opportunity_matches 57, opportunity_offers 231, flight_searches 32,
  flight_offers 179, notifications 28, and the rest) and identical stage results; the
  archive digest computed from the parts equals the one from the whole inputs for all
  25,000 stored predictions. `tests/integration/test_free_tier_egress.py` repeats the
  comparisons on synthetic data.

**One sync pass**, DB → client, MB (as above):

| Pass | Before (f3c15d6) | After | After, no saved searches |
|------|-------:|------:|-------------------------:|
| GFS sync (`open_meteo_noaa` and the blend) | 30.55 | 20.43 | 19.27 |
| ECMWF sync (`open_meteo_ecmwf` and the blend) | 28.99 | 18.87 | 17.71 |
| Both families and the blend (what a map-cycle sync acquires) | 31.36 | 21.24 | 20.11 |
| Demo pass with acquisition (fresh `init-db`) | 14.25 | 11.17 | |

By stage (both families, after): blending 3.52, scoring 10.85 (the scored runs' forecasts 5.82,
tide predictions 2.61, the blend's comparisons 1.58), archive 2.45, detection 2.69,
matching 0.35, flights 0.57, alerts 0.22, delivery 0.15, prune 0.01; one Redis command
(the hook).

**The hourly batch** (`jobs`): 2.22 → 0.87 MB (matching 0.87 → 0.41, flights 0.30 → 0.13,
alerts 0.99 → 0.26; staleness 0.02 and data health 0.05 unchanged); 0.35 → 0.22 MB without
saved searches. Daily: `maintenance.cleanup` 0.01 MB, `tides.acquire` 0.77 MB (56 stations,
real NOAA CO-OPS requests), the daemon's start-up `init-db` on a populated database 2.89 MB,
the daily watchdog (one `ops.data_health`) 0.05 MB.

**One page view, anonymous, no CDN** (`pages`): home 397.4 kB cold, 0.0 kB warm (22.7
before: the status); map 2,513.8 kB cold, 722.4 kB warm (the 9 fields, 80.3 kB each, which
the CDN keeps a year per run: the index now reads nothing); spot page 1,181.1 kB cold,
0.0 kB warm. Computed once per publication and hour: the spot list 1,054.3 kB, the
conditions 357.7 kB now and per map step, the next swells 23.3 kB, the status 16.3 kB, the
index 5.3 kB, a spot page 1,164.8 kB.

**The sync cadence of the free-database profile.** `SYNC_TRIGGER_SOURCES=map` and
`MAP_FIELD_CYCLES=06,18` (`app.core.profiles.FREE_DATABASE_PROFILE`; the full-retention
profile restores `spots,map` and `00,12`). A sync then runs only for a new map cycle, twice
a day, and acquires the newest spot runs of both families with it, so each sync is a
both-families pass. Why 06Z and 18Z (estimated from the lags measured in
docs/FORECAST_SOURCES.md, not observed on a live stack): a map cycle's sync starts about 5
to 6.5 h after the cycle (the map's 5 h lag, then up to SYNC_COALESCE_MINUTES for the GFS
spot run of that cycle), when Open-Meteo already serves the ECMWF run of that day's 00Z or
12Z (published 7.0-8.5 h after its cycle). That run is then 11-12.5 h old, and 23-24.5 h
old at the next sync, just under its 24.7 h stale age. With 00Z and 12Z map cycles the
ECMWF run would be 17-18.5 h old when acquired and stale for about 4-6 h of every 12.

What this costs in freshness (the owner's trade-off):

* Spot forecasts change twice a day (06Z and 18Z GFS runs, the 00Z and 12Z ECMWF runs);
  the 00Z and 12Z GFS runs are not used, and a GFS run reaches its 17.2 h stale age 17-18.5
  h after its cycle, so the forecast reads stale (confidence capped at Moderate, flagged)
  for up to about 1.3 h before each sync, longer when Open-Meteo publishes a GFS domain late
  (the run's issue time is its oldest domain). Alerts pause only at twice the stale age.
* A spot run that comes after its map cycle's sync waits for the next map cycle (12 h),
  and while the map layers cannot be acquired (NOAA unreachable), no sync runs at all: the
  spot forecasts then age until the map recovers (data health reports it).
* Matching, flights, alerts and delivery still run at every sync and hourly in between
  (`JOB_INTERVAL_FLOOR_MINUTES=60`, unchanged).
* `SYNC_TRIGGER_SOURCES=spots,map` (and `MAP_FIELD_CYCLES=00,12`) restores a sync for every
  new model run: 4.32 GB a month in the table below, so Neon Launch then.

**Monthly budget, Neon Free (5 GB egress), 30 days.** Background rows: the measured passes
and jobs times the cadence; page rows: the measured unit costs times the traffic stated
(examples, not measurements).

| Item | Cadence | Before (f3c15d6, old cadence) | After the code, old cadence | After, default profile |
|------|---------|------:|------:|------:|
| Syncs | old: 4 GFS + 2 ECMWF a day; profile: 2 map cycles a day, both families | 5.41 GB | 3.58 GB | 1.27 GB |
| Hourly batch (3 saved searches) | 24 a day | 1.60 GB | 0.62 GB | 0.62 GB |
| Daily cleanup, tides, watchdog | 1 a day each | 0.03 GB | 0.03 GB | 0.03 GB |
| Daemon start-ups (`init-db`) | 30 a month (a deploy a day, assumed) | 0.09 GB | 0.09 GB | 0.09 GB |
| **Background, no visitors** | | **7.12 GB (142 %)** | **4.32 GB (86 %)** | **2.01 GB (40 %)** |
| Background without saved searches | | | | 1.48 GB (30 %) |
| Pages, "quiet": 4 hours a day with a map and home view and 1 new spot page, 100 views a day, 9 fields a run, 1 CDN cache | | 0.47 GB | | 0.40 GB |
| Pages, "steady": every hour, 2 new spot pages an hour, 1,000 views a day, 9 fields a run, 3 CDN caches, the keys again after each publication | | 3.78 GB (without the publications) | | 3.36 GB |

(The "before" background includes the daily items measured with this code; the
init-db, tides and watchdog reads did not change.) With the defaults the background leaves
2.99 GB a month for visitors: "quiet" fits (2.41 GB in all), and with the map and home keys
computed every hour (1.31 GB), the fields (0.13 GB) and the keys again after each
publication (0.25 GB), about 1,120 spot-page hours a month (37 a day) more; "steady" (2 new
spot pages every hour) does not fit (5.37 GB). A spot page key set costs 1.16 MB, most of
it the 10-day forecast and timeline.

**Neon CU-hours (Free: 100 a month; 0.25 CU by default; compute suspends after 5 minutes
idle, research-2.json).** Assumptions: the compute runs at 0.25 CU; it is active from a
wake's first statement until 5 minutes after its last; nothing else wakes it (the daemon's
checks and kick polls use only Redis, docs/sync.md); wakes never overlap (an upper bound).

| Wake | Count a month | Active per wake | Hours | CU-hours |
|------|--------------:|----------------:|------:|---------:|
| Sync: the pass (28.8 s here), the spot acquisition (6.5 s live, round 1) and the map stage (165-287 s for 61 map hours, docs/sync.md) | 60 | 8.3-10.4 min | 8.3-10.4 | 2.1-2.6 |
| Hourly batch (its jobs took 1.15 s here) | 720 | about 5.02 min | 60.2 | 15.1 |
| Daily: cleanup, tides (20.1 s), watchdog, a daemon start (init-db 1.3 s) | 120 | about 5.1 min | 10.2 | 2.5 |
| **Background** | | | **79-81** | **19.7-20.2** |
| Pages "quiet": the keys of 4 hours a day (computed in under 0.5 s here) | 120 | about 5.0 min | 10.0 | 2.5 |
| Pages "steady": the keys of every hour and after each publication | 780 | about 5.0 min | 65.0 | 16.3 |

Background and "steady" visitors: at most about 37 CU-hours a month. Not counted, and able
to keep the compute awake all month (720 h × 0.25 CU = 180 CU-hours) if they reach the
database at least every 5 minutes: signed-in visitors (each request reads the session),
`/api/health/data` on the status page (shared by the CDN for 60 s), and visitor-chosen URLs
the CDN has not cached (unknown spots, arbitrary `?at=` times). Neon's autoscaling up to
2 CU, if turned on, multiplies only the active minutes it scales.

**Upstash Free (500K commands, 10 GB bandwidth, 250 MB data).** Commands per page view,
measured: home 3 (warm instance) / 6 (new instance) / 16 (cold), map 5 / 10 / 26, spot
page 5 / 10 / 23 (the status and the index now count one each). The daemon: about 13,000
for checks and 43,200 for kick polls (docs/sync.md), 2 per job lock (SET NX, EVAL):
8,640 for the hourly jobs, 1,440 for data health's own, 1,440 for the stages of 60 syncs,
120 for the daily jobs, and 120 for the hook: about 68,000 a month. "Steady" (1,000 views a
day, a third each, every request reaching the API on a new instance): 260,000, plus about
47,500 for the keys computed (12 an hour, about 5.5 each): about 375,000 a month (75 %).
"Quiet": about 100,000. Bandwidth: at most about 51.6 kB a view on a new instance (1.55
GB a month at 1,000 views a day) plus about 0.19 GB of stored payloads; data: about 265 kB
of payloads an hour, kept 2 hours.

**How to reproduce** (as above, plus): `python full_dump.py` and `compare_dumps.py` of
the identity check and `daily_egress.py` (init-db and tides) are scratch tools of this
round, kept with its results outside the repository; the measurements are the
`measure_db_egress.py` commands above with `--backend-dir` for the base.

**Limitations.** As above: the map stage's own reads, acquisition on live data and signed-in
visitors were not measured; page traffic is an example; TestClient in one process stood in
for Vercel's CDN, Neon and Upstash. The freshness of the new cadence is estimated from the
measured publication lags and was not observed on a live stack. The CU-hour rows are
arithmetic on stated assumptions; sync durations are this sandbox's (local Postgres).

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
  Southern Ocean. Byte 254 of a period therefore means "25.4 s or more", and the map's
  readout shows it as "≥25.4 s" (`formatLayerValue`, `PERIOD_CEILING_S`).
* Pressure over high terrain (Antarctica, Greenland, Tibet) is reduced to sea level and
  is not a real surface pressure.
* **Freshness with two cycles a day.** `age_hours` counts from the model cycle. The job
  takes a cycle once `NOAA_AVAILABILITY_LAG_HOURS` (5 h) have passed and at its next run
  after that (every `FORECAST_REFRESH_MINUTES`, 3 h by default), so under normal
  operation the served cycle is 12 h + 5 h + up to 3 h, i.e. 17 to 20 h old just before
  the next map cycle replaces it. The packages' first default, 12 h, called the layers
  stale for 5 to 8 h of every 12 (seen live at 13:40 UTC on 2026-10-15, when the newest
  map cycle (00Z) was 13.67 h old). 18 h still showed the badge for up to the last 2 h
  before the next cycle when a cycle was picked up late in its 3-hour acquisition window.
  The default is now **21 h**, above the normal maximum with `MAP_FIELD_CYCLES=00,12`, so
  the badge means a cycle was missed or is late (the cost: a real miss is flagged up to 3 h
  later than with 18 h). Adding `06,18` (about 0.97 GB a day more) is not enough on its own: the
  normal maximum is then 6 + 5 + 3 = 14 h, so about 15 h would be needed.
* **Publications that stop.** Under `SYNC_PUBLISH_GATE` a map run that a sync stores but
  never publishes is not shown, and retention keeps the last published run. If
  publications stop (every sync failing in its publish stage), the map stays on that run,
  flagged stale after `MAP_FIELD_STALE_AFTER_HOURS`, until its forecast run is deleted
  by the run retention (`RETENTION_FORECAST_DAYS`, keeping the two newest runs of the
  source); then the newest servable run is shown. `SYNC_PUBLISH_GATE=false` shows the
  newest run at once.
* GFS v17 (proposed) is expected to rename files, drop the 0.5° and 1° atmosphere files
  and move WW3 to an unstructured grid. The path templates are configuration and the
  atmosphere falls back to `pgrb2.0p25` per hour, but the `global.0p25` wave grid may
  change or disappear. A geometry change triggers a warning.
