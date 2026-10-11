# Direct NOAA GFS-Wave + GFS ingestion

`FORECAST_PROVIDERS=noaa_gfs` reads NOAA GFS-Wave (WAVEWATCH III) and the GFS
atmosphere straight from the NOAA Open Data Dissemination (NODD) bucket on AWS. It
downloads only the GRIB2 records it needs, using HTTP `Range` requests and the byte
offsets in each file's `.idx`. It needs no API key and no NOMADS access.

* Code: `backend/app/services/forecasts/noaa.py` (`NoaaGfsProvider`, source code
  `noaa_gfs_direct`, family `ncep_gfs`). GRIB index parsing, range fetching, decoding and
  land-aware point sampling come from the shared `forecasts/grib.py`.
* Tests: `backend/tests/unit/test_noaa_provider.py` and
  `backend/tests/integration/test_noaa_ingestion.py`. They run against **synthetic**
  GRIB2 files built with the ecCodes samples (`backend/tests/synthetic_gfs.py`) and
  served by `httpx.MockTransport`.
* Live check: the 2026-10-10 18Z cycle. See [Verified live](#verified-live).

Same family as Open-Meteo `ncep_gfswave*` and Windy `gfsWave`: these sources run the
same model with the same forcing, so their agreement is not independent evidence
(`forecasts/families.py`).

## File layout (GFS v16, as published on 2026-10-10)

Bucket: `NOAA_GFS_BASE_URL`, default `https://noaa-gfs-bdp-pds.s3.amazonaws.com`.

| Product | Path (cycle `YYYYMMDD/HH`, forecast hour `FFF`) |
|---------|--------------------------------------------------|
| Waves | `gfs.YYYYMMDD/HH/wave/gridded/gfswave.tHHz.{grid}.fFFF.grib2` (+ `.idx`) |
| Atmosphere | `gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF` (+ `.idx`) |
| Station bulletins (used for the cross-check only) | `gfs.YYYYMMDD/HH/wave/station/bulls.tHHz/gfswave.{ID}.bull` |

* **Cadence.** Both products are published hourly to f120, then every 3 h to f384. f121,
  f122, f124 and f125 return HTTP 404 for both (checked live). `fetch` reads f000 to
  `min(NOAA_MAX_LEAD_HOURS, FORECAST_DAYS × 24, 384)` every `NOAA_STEP_HOURS`. A step
  that falls between published hours after f120 moves forward to the next published
  hour, so a 1 or 2 h step becomes 3-hourly after f120.
* **`.idx` lines** look like `5:2899197:d=2026101018:HTSGW:surface:6 hour fcst:`. f000
  reads `anl`. Record numbers shift between steps (`UGRD:10 m above ground` is record
  585 at f000 and 588 at f006), so records are matched by `VAR:LEVEL`, never by number.
* **Wave records.** Each grid file has 19 records: WIND, WDIR, UGRD, VGRD (not used),
  then HTSGW, PERPW, DIRPW, WVHGT, SWELL 1-3, WVPER, SWPER 1-3, WVDIR, SWDIR 1-3. The 15
  records used are contiguous, so one range request per grid and step fetches all of
  them. Swell partitions use the level `"1 in sequence"`, `"2 in sequence"` and
  `"3 in sequence"`.
* **Atmosphere records.** The file has 743 records (696 at f000). The provider uses
  `PRMSL:mean sea level` (record 1), `GUST:surface` (14), `UGRD/VGRD:10 m above ground`
  (588/589, adjacent) and `LAND:surface` (728). LAND is fetched only until the
  land-sea mask is known, which in practice means once per cycle.
* **Sanity checks.** Each decoded HTSGW and UGRD record must carry the requested cycle
  date, cycle time and step. Every field in a file must share one grid geometry. A file
  that fails either check is treated as a failed file. The decoded latitude range of
  each wave grid is compared with the expected one, and a mismatch adds a warning.

## Grids

| Grid file | WW3 grid | Resolution | Coverage (decoded) | Used for |
|-----------|----------|------------|--------------------|----------|
| `global.0p16` | gnh_10m, **native** | 1/6° (2160 × 406) | 52.5N to 15S, all longitudes | forecast points from 15S to 52.5N (32 of 50 spots) |
| `gsouth.0p25` | gsh_15m, **native** | 1/4° (1440 × 277) | 10.5S to 79.5S | forecast points south of 15S (16 spots) |
| `global.0p25` | glo_15mxt, **interpolated** from the native grids | 1/4° (1440 × 721) | global | fallback: points outside both native grids, i.e. north of 52.5N (thurso-east, mullaghmore-head) |
| `pgrb2.0p25` (atmosphere) | GFS | 1/4° (1440 × 721) | global | 10 m wind, gust, MSL pressure, land-sea mask |

The grid is chosen from the latitude of the spot's forecast point. The two native
grids overlap between 10.5S and 15S; `global.0p16` wins there. A grid file is
downloaded only if at least one spot uses it. The regional grids (`atlocn`, `epacif`,
`wcoast`) are interpolated from the same native grid, so they add no detail and are not
used. `arctic.9km` is polar stereographic, which `grib.py` does not decode.

## Fields and conventions

| `wave_forecasts` column | Source | Notes |
|-------------------------|--------|-------|
| `sig_wave_height_m` | HTSGW | Offshore Hs of the combined sea state. **Not the breaking (surf) height.** |
| `wave_period_s` | PERPW | Peak period Tp. `wave_period_kind = "tp"` |
| `wave_direction_deg` | DIRPW | Direction at the spectral peak |
| `primary/secondary/tertiary_swell_*` | SWELL / SWPER / SWDIR, 1 to 3 in sequence | Partition height, peak period and direction |
| `wind_wave_*` | WVHGT / WVPER / WVDIR | Wind-sea partition |
| `wind_speed_kmh` | \|(UGRD, VGRD)\| at 10 m | m/s × 3.6 |
| `wind_direction_deg` | (270 − atan2(v, u)) mod 360 | Degrees FROM. A calm wind has no direction |
| `wind_gust_kmh` | GUST × 3.6 | A boundary-layer mixing diagnostic, not an observed gust |
| `pressure_msl_hpa` | PRMSL / 100 | Pa to hPa |
| `sea_level_m` | none | GFS has no tide, so this is always NULL |

* **Directions** in the gridded GRIB files are nautical degrees FROM. WW3 `ww3_grib`
  writes `MOD(630 − RADE·θ, 360)`, so no +180 is applied. The station `.bull` files use
  "going to" and need +180. The live cross-check below confirms both conventions to
  within 1°.
* **Swell partitions are ranked by height** at each hour (WW3 PTM=1: one wind-sea
  partition, then swells ordered by height). "Primary" is the largest swell at that
  hour, not a swell tracked through time. Indices can swap between hours, runs and
  models. Match swells by period and direction, not by index.
* **Missing partitions** are missing values in the GRIB bitmap and are stored as NULL,
  not 0. In the live run, 208 of 450 rows had a third swell partition.
* **Peak period jumps.** Tp switches between partitions: at Pipeline it went from 10.4 s
  to 19.0 s between f003 and f006 on 2026-10-10 18Z when the long-period NW partition
  became the spectral peak.

## Point sampling

* **Waves.** The provider samples the nearest cell with a valid HTSGW value to the
  spot's `forecast_latitude/longitude`. Land is masked in the GRIB bitmap. If no valid
  cell lies within `GRIB_MAX_CELL_DISTANCE_KM` (default 60), it tries the break location.
  If that also fails, the spot gets an error and no records. Wave values are never
  interpolated across land.
* **Wind.** The provider samples the nearest GFS cell with `LAND < 0.5` to the break
  (the spot's `latitude/longitude`). For 13 of the 50 spots, the nearest GFS cell to the
  break is land, so the wind there would come from a land cell. Examples: Trestles,
  Nazaré, Kirra, Bells Beach, Thurso (live LAND field, 2026-10-10 18Z). A spot with no
  sea cell in range keeps its wave records with empty wind fields and a warning.
* Each choice is made once per grid geometry (`grib.PointSampler`) and reused for every
  step. If a later step masks a chosen wave cell (for example, sea ice), that hour is a
  gap and the spot gets a warning.
* **Recorded cells.** `ingestion.py` stores only per-record extras. The chosen cells
  therefore go into `ForecastRecord.extra` of each spot's **first** record, which ends up
  in `wave_forecasts.extra` at the earliest valid time:

  ```json
  {"grid": "global.0p16",
   "wave_cell": {"lat": 21.8333, "lon": -158.1663, "distance_km": 4.0, "anchor": "wave_point"},
   "wind_cell": {"lat": 21.75, "lon": -158.0, "distance_km": 10.9, "grid": "pgrb2.0p25"}}
  ```

  `anchor` is `wave_point` (the forecast point) or `wind_point` (the break, used as the
  fallback). The same dicts are kept on the provider instance as
  `provider.last_cells[spot_id]`, together with `last_hours`, `last_bytes_downloaded`
  and `last_step_bytes`.

### Sampling arbitrary points (verification)

`sample_gfs_points(points, cycle, fhour)` returns waves and wind at any positions, for
example buoys:

```python
from app.services.forecasts.noaa import SamplePoint, sample_gfs_points

s = sample_gfs_points([SamplePoint.at("ndbc:51201", 21.67, -158.12)], cycle, 24)
s.values["ndbc:51201"]["sig_wave_height_m"], s.cells["ndbc:51201"]
```

It uses the same fields, units and conventions as the provider. A point with no valid
wave cell is absent from `values`, and its `cells` entry has `wave_cell: None`. Failed
files are listed in `failed`. To read many steps of one cycle, use `GfsPointReader`
directly (`read_steps(hours, workers)`), so that cell choices and the land-sea mask are
reused.

## Run discovery

`current_run(now)` starts at `nominal_gfs_cycle(now, NOAA_AVAILABILITY_LAG_HOURS)` and
steps back up to 4 cycles. A cycle counts as complete when the `.idx` of its **last
needed step** exists for the atmosphere file and all three wave grids. Uploads arrive
roughly in step order but not strictly (research notes: 45 of 120 hourly `.idx` files
appeared before the previous hour's). A step that is still missing becomes a gap in
`fetch`.

* Run key: `noaa-gfs:YYYYMMDDHH`.
* `issued_at` = `wave_model_run_at` = `atmosphere_model_run_at` = cycle time.
* `metadata_source`: `noaa-s3-idx`.
* A network failure raises `TransientProviderError` instead of falling back to an older
  cycle. If no cycle is complete, the call also raises `TransientProviderError`, and the
  message names the first missing file of each cycle.

Measured availability (research notes, 11 cycles): f000 at +3.5 to +3.8 h, f120 at
+4.1 to +4.4 h, f384 at +5.2 to +5.3 h after the cycle. The default 5 h lag suits leads
up to f240. The 18Z cycle's f240 was present at 23:31 UTC (+5.5 h).

## Requests and bandwidth (measured live, 2026-10-10 18Z)

Per forecast hour, independent of the number of spots:

| File | Records fetched | Bytes |
|------|-----------------|-------|
| `global.0p16` | 15 contiguous wave records, 1 range | 8.26 MB |
| `global.0p25` | 15 contiguous wave records, 1 range | 8.64 MB |
| `gsouth.0p25` | 15 contiguous wave records, 1 range | 3.59 MB |
| `pgrb2.0p25` | PRMSL 0.99 MB + GUST 0.63 MB + UGRD/VGRD 1.92 MB, 3 ranges; LAND 32 kB once | 3.54 MB |
| `.idx` files | 4 | about 50 kB (atmosphere) + 1 kB per wave grid |

* **Per step with all three grids:** 24.0 MB in 10 requests (11 on the first step,
  because of LAND). Without spots north of 52.5N, `global.0p25` is skipped and a step
  costs about 15.4 MB. The two northern spots cost 36% of the bandwidth.
* **Per 10-day run** (default 3-hourly to f240, 81 steps): about 1.95 GB and about 810
  requests. Hourly to f120 and then 3-hourly (`NOAA_STEP_HOURS=1`, 161 steps) costs
  about 3.9 GB. Each new cycle is ingested once (run-key dedup), so at 4 cycles a day the
  default costs about 7.8 GB a day. NODD egress is free to the user.
* **Time.** 9 steps for 50 spots took 33.9 s with 6 workers, including the database
  insert (about 6.4 MB/s through this sandbox's proxy). That projects to about 5 min for
  81 steps, inside the Celery soft time limit of 25 min. This projection was not
  measured.

Concurrency: `GRIB_DOWNLOAD_WORKERS` threads over steps share one `httpx.Client`
(`FORECAST_HTTP_TIMEOUT_SECONDS`). The first step runs alone so that the land-sea mask
and the cell choices are computed once. Fields are decoded one at a time, because a
decoded `global.0p25` field is 8 MB as float64. `FetchResult.request_count` counts the
index and range GETs made by `fetch`, with each retried request counted once. The
`current_run` HEAD probes are not counted.

## Failure modes

| What happens | Effect |
|--------------|--------|
| Transient HTTP failure (timeout, 429, 5xx) | Retried 4 times with exponential backoff and jitter (`request_with_retries`). If every attempt fails, that file is a failed file for that step. |
| File not published (HTTP 403/404) | Failed file for that step. In `current_run` it means "not complete yet", and the previous cycle is tried. |
| Wave file of a step fails | That hour is a gap for the spots on that grid. The warnings list it, e.g. `f012 wave global.0p16: ... not published (HTTP 404)`, plus `N of M forecast hours incomplete: ...`. |
| Atmosphere file of a step fails | That hour keeps its wave values with NULL wind fields, plus a warning. |
| `.idx` lacks HTSGW, LAND, UGRD or VGRD | Failed file. If another field is missing, its column is NULL and a warning is added. |
| Record from another cycle or step, or mixed grid geometries | Failed file (`holds date/time/step ...`). |
| Decoded latitude range differs from the expected one | Warning (`file layout changed?`). Sampling still uses the decoded geometry. |
| Spot without a valid wave cell within the distance limit | Error for that spot, no records, run `PARTIAL`. |
| Spot without a sea cell near the break | Wave records kept with NULL wind, plus a warning. |
| No file readable for any step | `TransientProviderError`. The run is `FAILED` and is retried on the next schedule. |

The first warning of every run is the bandwidth line, for example
`downloaded 216.0 MB in 91 requests for 9 forecast hours f000-f024 (24.0 MB per hour)`.
Ingestion keeps only the first 50 warnings, so the summary lines come first.

## GFS v17 rename risk

GFS v17 is only proposed: PNS 26-29 (2026-04-15) targets October 2026, and EMC lists
Q1 FY27. It will change directory structure and file names, and WW3 moves to an
unstructured grid. The 0.5° and 1° atmosphere files are being removed (global-workflow
PR #5298), so only `pgrb2.0p25` is used.

* The bucket comes from `NOAA_GFS_BASE_URL`.
* The path templates are the settings `NOAA_GFS_WAVE_PATH_TEMPLATE` and
  `NOAA_GFS_ATMOS_PATH_TEMPLATE` (defaults `WAVE_PATH_TEMPLATE` / `ATMOS_PATH_TEMPLATE`
  in `noaa.py`), with the placeholders `{ymd}`, `{hh}`, `{grid}` and `{fhour:03d}`, so a
  rename is a configuration change.
* The grid names and latitude ranges are in `WAVE_GRIDS`. A geometry change triggers a
  warning, and missing records or files are reported, never silently filled.
* After a switch, start bias and skill statistics afresh rather than mixing v16 and v17
  errors.
* The GCP mirror `https://storage.googleapis.com/global-forecast-system` has the same
  layout (research notes; it was not tested from this sandbox).

## Verified live

All on 2026-10-10 against the 18Z cycle.

* **3 spots, f000 to f006 every 3 h.** 72.0 MB, 31 requests, 17.7 s.

  | Spot | Wave cell (grid, distance) | Wind cell (distance) | f000 values |
  |------|----------------------------|----------------------|-------------|
  | pipeline | `global.0p16` 21.8333N 158.1663W (4.0 km) | 21.75N 158.0W (10.9 km) | Hs 1.85 m, Tp 10.6 s, Dp 338°. Swells: 1.30 m 10.5 s 332°; 0.53 m 11.8 s 82°; 0.46 m 20.4 s 310°. Wind sea 1.03 m 6.3 s 85°. Wind 25.5 km/h from 114°, gust 31.7 km/h, 1015.2 hPa |
  | jeffreys-bay | `gsouth.0p25` 34.25S 25.0E (5.6 km) | 34.0S 25.0E (7.2 km) | Hs 2.45 m, Tp 11.8 s, Dp 224°. Swells: 2.30 m 11.8 s 222°; 0.50 m 8.9 s 99°. Wind 16.2 km/h from 80° |
  | thurso-east | `global.0p25` 58.75N 3.75W (4.1 km) | 58.75N 3.5W (16.4 km) | Hs 3.05 m, Tp 12.5 s, Dp 283°. Swells: 2.99 m 12.5 s 287°; 0.59 m 9.8 s 12°. Wind 25.3 km/h from 329°, 1001.2 hPa |

* **All 50 spots, f000.** 0 errors, 24.0 MB, 11 requests, 7.7 s. Grid use was 32 / 16 /
  2 spots. Every wave cell was anchored at the forecast point. The largest wave-cell
  distance was 16.8 km (dungeons) and the largest wind-cell distance 26.5 km
  (anchor-point).
* **`acquire_from_provider`** into a scratch database, all 50 spots, f000 to f024 every
  3 h. Status `success`: 450 `wave_forecasts` rows, 0 spot errors, 91 requests,
  216.0 MB, 33.9 s. All rows had `wave_period_kind = "tp"` and wind. 208 rows had a
  tertiary swell. The cells were stored in `extra` of each spot's first row.
* **Cross-check against the station bulletin** `gfswave.51201.bull` (Waimea, 21.67N
  158.12W):

  | Valid time | Bulletin Hst | Gridded at the 51201 position (`global.0p16` cell 21.6666N 158.1663W, 4.8 km) | Pipeline forecast-point cell |
  |------------|--------------|------------------------------------------------------------------------------|------------------------------|
  | 2026-10-10 18Z (f000) | 1.41 m | 1.40 m | 1.85 m |
  | 2026-10-11 00Z (f006) | 1.34 m | not sampled | 1.70 m |
  | 2026-10-11 18Z (f024) | 0.93 m | 0.91 m | not sampled |

  * **Partitions at f000.** Bulletin, converted from "going to" by +180°: 1.19 m 10.5 s
    332°; 0.49 m 6.4 s 73°; 0.41 m 20.4 s 311°. Gridded: 1.19 m 10.5 s 332.5°; 0.47 m
    6.5 s 71.6°; 0.40 m 20.5 s 310.9°.
  * **Partitions at f024.** Bulletin: 0.52 m 9.2 s 335°; 0.52 m 6.2 s 63°; 0.40 m
    15.6 s 312°. Gridded: swell 0.52 m 9.2 s 335.0°; swell 0.39 m 15.6 s 311.9°; wind
    sea 0.50 m 6.2 s 61.3°.
  * **Pipeline.** The Pipeline forecast point is about 16 km NNW of the buoy, and the
    two grid cells are 18.5 km apart. The Pipeline point is more exposed to the ENE
    trade-wind sea (1.03 m wind-sea partition at f000; none at 51201), so its Hs is
    higher. The numbers are similar but not identical, as expected for a different
    point.

These live runs downloaded about 340 MB in total. Nothing downloaded is committed. The
test fixtures are synthetic and generated at test time.

### Mock-only (not exercised live)

* The retry and backoff path (HTTP 5xx and 429 responses).
* `current_run` falling back to an older cycle. Live, the nominal cycle was always
  complete.
* Steps that are missing in the middle of a cycle.
* The step and geometry sanity checks failing.
* Spots without a valid wave cell or a sea cell.
* A full 81-step (10-day) run. Its bandwidth and duration above are extrapolated from
  the measured per-step figures.

## Limitations

* `forecast_point` choice matters. A wave cell 4-17 km from the point can still sit
  behind headlands or islands. Compare Pipeline and Waimea above: the same model gives
  1.85 m and 1.40 m in cells 18.5 km apart.
* 10 m winds come from a 0.25° model: about 28 km cells, and the effective resolution is
  several grid lengths. Sea breezes, land breezes and terrain winds are under-resolved.
  Treat morning-glass and afternoon-onshore timing as low confidence.
* No directional spreading and no 2-D spectra. The station `.spec` files would provide
  both for spots near a bulletin point; that is not implemented here.
