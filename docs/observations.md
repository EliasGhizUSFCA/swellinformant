# Buoy observations and model forecasts at buoys

This document covers what the observation subsystem stores, where the data comes from,
the licence each source carries, the conventions applied on ingest, how quality flags are
handled, which buoy stands in for which surf spot (and why), and what was checked against
live data versus only with mocks.

Code: `backend/app/services/observations/` (station catalog, provider adapters, ingest,
CLI) and `backend/app/services/verification/point_forecasts.py` (model forecasts archived
at the stations). Catalog: `data/observation_stations.json`. Tests:
`backend/tests/unit/test_observation_parsers.py`,
`backend/tests/unit/test_point_forecasts.py`,
`backend/tests/integration/test_observations.py`.

**Buoy significant wave height is an offshore sea-state measurement, not surf height.**
Every pairing below is an offshore or regional reference. Converting it to breaking
height is the nearshore model's job (`app/services/surf_quality/`), and the reefs,
points and shelves involved change the numbers a lot.

## What is stored

| Table | Contents |
| --- | --- |
| `buoy_stations` | One row per buoy (`code` such as `aodn:cape-naturaliste`, `metoffice:k5`, `ndbc:51201`), position, depth, licence, attribution. Everything else (provider ids, site names, `bulletin_id`, `paired_spots` with justifications, coverage, caveats, last reported position) is in `metadata`. |
| `buoy_observations` | Measurements only, one row per station and time (`UNIQUE(station_id, observed_at)`), with `hs_kind`, `tm_kind`, `qc_flag` and `dataset`. |
| `point_forecasts` | GFS-Wave station-bulletin forecasts at the stations: one row per station, source, cycle and valid time (`source_code = gfswave_bulletin`, `family = ncep_gfs`). |

## Sources

### AODN / IMOS wave buoys (Australia), live-verified

* Bucket `s3://aodn-cloud-optimised` (`AODN_BASE_URL`,
  `https://aodn-cloud-optimised.s3.ap-southeast-2.amazonaws.com`), anonymous HTTPS.
* `wave_buoy_delayed_qc.parquet/` (delayed mode, quality controlled, the National Wave
  Archive) and `wave_buoy_realtime_nonqc.parquet/` (near real time, not quality
  controlled).
* Hive partitions `site_name=<escaped>/timestamp=<epoch s>/polygon=<wkb hex>/`, with the
  timestamp at the start of a calendar month. Keys contain a literal `%20`
  (`site_name=Cape%20Naturaliste`), so object URLs encode it again as `%2520`. Listing
  uses ListObjectsV2 (`?list-type=2&prefix=...`, following `NextContinuationToken`), and
  only the partitions of the needed sites and months are downloaded and read with pyarrow
  from memory.
* Variables. The attributes come from each dataset's `_common_metadata`, read at run time:

  | Column | Meaning | Stored as |
  | --- | --- | --- |
  | `WSSH` | spectral significant wave height (Hm0) | `hs_m`, `hs_kind = spectral_hm0` |
  | `WHTH` | time-domain H1/3, used only when `WSSH` is missing | `hs_m`, `hs_kind = h_third` |
  | `WPPE` | spectral peak period; `0` is a fill value | `tp_s` |
  | `WPDI` | peak direction, `standard_name = sea_surface_wave_from_direction_at_variance_spectral_density_maximum`, `reference_datum = true north` | `peak_direction_deg` (FROM) |
  | `SSWMD` | mean direction, `sea_surface_wave_from_direction`, true north | `mean_direction_deg` (FROM) |
  | `WPFM` / `WPSM` / `WPMH` | Tm01 / Tm02 / time-domain mean (zero up-crossing) period | `tm_s` with `tm_kind` `tm01` / `tm02` / `tz` |

  The datasets carry no wind. If a dataset ever declared a `to_direction` standard name,
  180 degrees would be added.
* The Queensland realtime feeds (Tweed Offshore, Tweed Heads, Gold Coast) and the WA
  realtime feed (Cape Naturaliste) publish only `WHTH` (H1/3). Their heights are stored
  as `h_third` and must not be compared with model Hm0 without accounting for that.
* Coverage of the paired sites, from partition listings on 2026-10-11. The delayed-QC
  periods matter for a later backtest:

  | Station | Delayed QC (site name) | Realtime (site name) |
  | --- | --- | --- |
  | `aodn:cape-naturaliste` | 2004-04 to 2021-12 (`Cape Naturaliste 02`, 228 files) | 2024-01 to 2026-10 (`Cape Naturaliste`) |
  | `aodn:cape-naturaliste-01` (inactive) | 1999-05 to 2004-04 (`Cape Naturaliste 01`, 65 files) | none |
  | `aodn:rottnest-island-02` (inactive, verification only) | 2004-09 to 2021-12 (`Rottnest Island 02`, 225 files) | not configured |
  | `aodn:esperance-04` (inactive, verification only) | 2006-06 to 2021-12 (`Esperance 04`, 195 files) | not configured |
  | `aodn:tweed-heads` | 1995-01 to 2022-12 (`Tweed Heads`, 211 files) | 2024-01 to 2026-10 |
  | `aodn:gold-coast` | 1987-02 to 2022-12 (`Gold Coast`, 213 files) | 2024-01 to 2026-10 |
  | `aodn:apollo-bay` | 2019-10 to 2026-06 (`APOLLO-BAY`, 108 files) | 2024-01 to 2025-05 (`Apollo Bay`), 2025-06 to 2026-09 (`APOLLO-BAY`) |
  | `aodn:tweed-offshore` | none | 2024-01 to 2026-10 (`Tweed Offshore`) |

  The GFS-Wave bulletin archive starts with the 2021-03-22 18Z cycle, so delayed-QC
  observations overlap archived bulletin forecasts at a co-located bulletin only at Cape
  Naturaliste (56006), Rottnest Island 02 (56005) and Esperance 04 (56010), for about nine
  months (2021-03-22 to 2021-12-31). The Tweed Heads and Gold Coast delayed-mode files of
  2021-2022 carry `WHTH` only (`WSSH` is present as a column but empty, checked on every
  partition of the window on 2026-10-11), so they hold no spectral Hm0 in that overlap.
  Apollo Bay's delayed-mode Hm0 runs to 2026-06, but its nearest bulletin point (NKING)
  is 50 km away in other exposure (docs/ml-evaluation.md). The Apollo Bay realtime
  Parquet copy ended on 2026-09-21 although the source NetCDF for October 2026 exists in
  `imos-data`, so the cloud-optimised dataset can lag.
* Licence: CC BY 4.0. Acknowledgement text, copied from the source NetCDF
  `acknowledgement` attribute and stored on each station:
  "Data was sourced from Australia's Integrated Marine Observing System (IMOS) - IMOS is
  enabled by the National Collaborative Research Infrastructure Strategy (NCRIS)."
  Citation format given by the files: "Australian Ocean Data Network (AODN).
  [year-of-datadownload], [Title], [Data access URL], accessed [date-of-access]".
  The DoT WA delayed-mode files (Cape Naturaliste 01/02) carry their own
  acknowledgement, which is recorded verbatim on those stations: "These data were
  collected and Quality controlled by the Department of Transport (DoT), Western
  Australian Government. These data were made freely available by the AODN - a data
  facility of the Integrated Marine Observing System [IMOS]. IMOS is enabled by the
  National Collaborative Research Infrastructure Strategy (NCRIS)." Operators, from the
  source files: Queensland Department of Environment and Science (Tweed Offshore with the
  Tweed River Sand Bypassing Project, Datawell DWR4), Department of Transport WA (Cape
  Naturaliste, Datawell DWR MkIII), IMOS Coastal Wave Buoys Facility (Apollo Bay, SOFAR
  Spotter-V3; earlier files by Deakin University).

### Met Office marine observations (UK and NE Atlantic), live-verified

* Bucket `met-office-marine-observations-data` (`METOFFICE_BASE_URL`, eu-west-2).
* Layout `{issue}_{start}_{end}/{station}_buoy_{uuid}.csv` with `YYYYMMDDHHMM` UTC stamps.
  Each folder covers one hour (HH:00 to HH:59, issued about 75 minutes after HH:00), each
  buoy file holds that hour's single row, and the bucket keeps a rolling window of about
  eight days (198 folders on 2026-10-11). Files for automatic weather stations are
  ignored. If an hour were ever re-issued, the newest issue would win.
* Pipe-delimited, with a header. Columns used:
  `significant_wave_height_hm_0` (Hm0; `..._h_third` only when Hm0 is missing or flagged,
  stored as `h_third`), `..._hm_0_swell` and `..._hm_0_windsea`, `wave_peak_period`
  (`tp_s`), `wave_peak_period_swell` and `wave_peak_direction_swell` (swell partition),
  `wave_mean_period_tm_02` (`tm_kind = tm02`), `wave_peak_direction` and
  `wave_mean_direction` (FROM), `wind_speed_marine` (m/s x 3.6 = km/h) and
  `wind_direction_marine` (FROM).
* Units are not written in the files. Wind is taken as m/s and directions as FROM after
  comparison with GFS-Wave output for the same buoys (module docstring of
  `metoffice.py`). The live pairing below confirms the direction convention: the median
  difference from the bulletin partition-1 FROM direction is 15 degrees. Wind is at
  anemometer height and is not adjusted to 10 m.
* Licence: CC BY-SA 4.0 (https://creativecommons.org/licenses/by-sa/4.0/). Attribution
  stored on each station: "Source: Met Office marine observations (AWS Open Data bucket
  met-office-marine-observations-data), licensed under CC BY-SA 4.0. Changes: units
  converted (wind m/s to km/h), values failing the Met Office QC flags removed.
  Adaptations must be shared under CC BY-SA 4.0." `metadata.share_alike = true`. Anything
  derived from these rows and redistributed (exports, published statistics) has to carry
  the attribution and the same licence.

### NOAA NDBC (US), live-verified 2026-10-12

Verified against the real service (`www.ndbc.noaa.gov` is reachable since 2026-10-12)
and, for the CDIP-operated buoys, against the CDIP THREDDS realtime files of the same
buoys. Every statement below was checked on files downloaded that day; the NDBC
documents cited are *Measurement Descriptions and Units*
(<https://www.ndbc.noaa.gov/faq/measdes.shtml>), *How are significant wave height,
dominant period, average period, and wave steepness calculated?*
(<https://www.ndbc.noaa.gov/faq/wavecalc.shtml>), *Acquisition Time*
(<https://www.ndbc.noaa.gov/faq/acq.shtml>), the wave glossary
(<https://www.ndbc.noaa.gov/waveobs.shtml>) and the *NDBC Web Data Guide* (June 2023,
<https://www.ndbc.noaa.gov/docs/ndbc_web_data_guide.pdf>).

**Files** (all names seen live; station ids are lower case in archive file names):

| Period | URL | Format |
| --- | --- | --- |
| last 45 days | `{NDBC_BASE_URL}/data/realtime2/{ID}.txt` and `{ID}.spec` | text, missing = `MM` (`.spec` also `-99`, and `NO` = no swell found) |
| last 5 days | `{NDBC_BASE_URL}/data/5day2/{ID}_5day.txt` and `{ID}_5day.spec` | the same rows in the same layout (46026 and 46053 on 2026-10-15: every 5-day row equals the realtime2 row of the same time); read when `NDBC_USE_5DAY` is on and the window starts at most 4.5 days ago |
| complete years | `{NDBC_BASE_URL}/data/historical/stdmet/{id}h{YYYY}.txt.gz` | gzip (read directly: about 9 times smaller than the `view_text_file.php` answer the adapter used before, 174 KB vs 1.53 MB for 51201 in 2025) |
| months of the current year | `{NDBC_BASE_URL}/data/stdmet/{Mon}/{id}{m}{YYYY}.txt.gz`, `m` = 1-9 | gzip; quality controlled (Web Data Guide, section 4) |
| newest month | `{NDBC_BASE_URL}/data/stdmet/{Mon}/{id}.txt` | uncompressed (`Sep/51201.txt` on 2026-10-12) |

The month codes for October to December (`a`, `b`, `c`) follow NDBC's naming but could
not be seen live: those directories were empty on 2026-10-12 (presumably emptied once
the previous year's yearly file was published; not checked). A wrong guess would show as a 404 warning, not as
missing data passed off as complete. Early in a year, before the previous year's yearly
file is published (HTTP 404), the adapter reads that year's monthly files instead. A file
missing under every name is a coverage warning; a file that answers 200 but is not gzip,
is a truncated gzip, or would decompress to more than 64 MB is an error, not data (a
defensive check: every missing file seen live answered 404, including the old
`view_text_file.php` name; the largest yearly file read live, 46026 in 2024, is 350 KB
compressed and 4.4 MB decompressed; the bound was added in review on 2026-10-13). Live, two of the 110 yearly and
monthly files requested for the 24-month window were missing: 51201 March and April 2026.

**Values and units** (columns `#YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ...`,
units line `degT m/s m/s m sec sec degT hPa`):

* **WVHT is the spectral significant wave height Hm0** = 4·sqrt(m0), m0 summed over the
  non-directional spectrum: wavecalc.shtml, "WVHT = 4 × sqrt(m0) ... variance is calculated
  using the nondirectional wave spectrum". measdes.shtml describes WVHT as "the average
  of the highest one-third of all of the wave heights", which is the traditional meaning,
  not the computation. Stored as `hs_kind = spectral_hm0`. Live check: NDBC WVHT equals
  CDIP `waveHs` (CF `sea_surface_wave_significant_height`) exactly at six CDIP buoys
  (51201, 51205, 46224, 46214, 46277, 46237; 449-902 reports each). For the NDBC-processed
  46026, 4·sqrt(m0) recomputed from the realtime `.data_spec` densities agrees with WVHT
  to 0.03 m (median of 2,130 reports; the densities are rounded to 3 decimals, so this is
  a consistency check, not a proof).
* **DPD** is the dominant (peak) period, 1/fp of the band with the largest spectral
  density (wavecalc.shtml), stored as `tp_s`. Live: equal to CDIP `waveTp` within 0.005 s.
* **APD depends on who processes the buoy** (wavecalc.shtml): sqrt(m0/m2) = Tm02 for
  NDBC's own systems, m0/m1 = Tm01 for Scripps (CDIP) buoys, a zero-crossing period for
  UK, Irish and French buoys shown by NDBC. The catalog states it per station
  (`apd_kind`), and `tm_kind` is stored accordingly (`tm01` or `tm02`; `apd` only when a
  station does not say). Live: NDBC APD equals CDIP `waveTa` (first moment) within
  0.005 s at all six CDIP buoys and differs from CDIP `waveTz` (second moment) by
  0.53-1.32 s (medians). Before this check every NDBC period was stored as `apd`.
* **MWD is the direction from which the waves at DPD come**, degrees true (measdes), i.e.
  a peak direction FROM, stored as `peak_direction_deg`. Live: equal to CDIP `waveDp`
  (`sea_surface_wave_from_direction_at_variance_spectral_density_maximum`) within
  0.5 degree. No conversion is needed.
* WDIR is the wind direction FROM (degrees true), WSPD the 8-minute mean (m/s, stored
  x 3.6 as km/h) at the anemometer height (3.2-4.1 m on these NDBC hulls, station
  pages), not adjusted to 10 m. The CDIP Waveriders report no wind.
* `.spec`: SwH / SwP / SwD swell and WWH / WWP / WWD wind-sea split; SwD and WWD are
  16-point compass names of the direction the waves come FROM (measdes: "The direction
  from which the swell waves ... are coming"), stored as the sector centre.
* Missing values in the yearly and monthly files are runs of 9s with a per-column meaning
  (measdes: "a variable number of 9's ... depending on the data type"): WVHT, DPD and APD
  99.00; MWD and WDIR 999; WSPD and GST 99.0; PRES 9999.0; ATMP, WTMP and DEWP 999.0; VIS
  99.0; TIDE 99.00 (all seen live). A blanket mask would delete a real 99-degree wind.
* Precision: realtime files print heights to 0.1 m, DPD in whole seconds and APD to 0.1 s;
  the archives print 0.01 m and 0.01 s. `.txt` and `.spec` round ties differently (an
  archived 0.95 m appears as 1.0 in `.txt` and 0.9 in `.spec`).

**Times** are UTC ("Both Realtime and Historical files show times in UTC only",
measdes.shtml). For CDIP buoys NDBC's time is the end of CDIP's sample: CDIP `waveTime`
(the "UTC sample start time" of a 1600 s sample) + 26 min at 51201, 51205, 46224 and
46214, + 30 min at 46277 and 46237 (DWR-M4), in every one of 449-902 matched reports.
NDBC's own buoys report waves every 30 min at :10 and :40 in `.spec` and in the
archives.

**Realtime copies (bug fixed).** The realtime `.txt` of NDBC's own buoys prints each
20-minute wave report on the 10-minute meteorological rows next to the wave time ("wave
information is displayed on the same lines as meteorological data", acq.shtml): 10 min
before, at, or 10 min after the `.spec` time (live at 46026, 46053, 46054, 46086 and
51101: 3,793 wave rows for 2,139 reports at 46026), often with DPD on only one copy.
The adapter stored every copy, so a 30-min record became two or three records 10 min
apart (8 records for 4 reports in the 46026 fixture). Now the copies of a report (same
height within one 0.1 m rounding step, same APD and MWD, within 15 min) become one
record at the `.spec` time, the time the QC'd archive uses, each value taken from the
closest copy that has it; `parse.duplicates_merged` counts the folded rows. Checked on
the overlap of the realtime files with the August 2026 monthly files (2026-08-28 to
08-31, re-run on the afternoon of 2026-10-12): at 46026, 328 realtime wave rows became
192 records, 191 of them at an archived time (the other at 16:09 next to the archived
16:10 report) and none more than 0.05 m from the archived height; at 51201 (CDIP, no
copies) all 192 records fall on archived times and agree within 0.05 m.

**QC**: realtime rows have passed NDBC's automated checks only and carry no flags
(`not_qc`); the yearly and monthly files are NDBC's quality-controlled archive
(`probably_good`, no per-value flags either), so they supersede realtime rows of the same
report.

**Positions and depths.** `activestations.xml` (current deployment of every active
station: id, lat, lon, name, owner, program, type) replaces the catalog position on every
ingest; it has no water depth. The catalog now carries the activestations positions
(`position_verified: true`), the water depth from each station page
(`station_page.php?station=ID`, "Water depth"), the deployment date from
`metadata/stationmetadata.xml`, and for CDIP buoys CDIP's own depth (`metaWaterDepth`;
46224: 223 m at CDIP vs 238 m at NDBC, 46237: 16.46 vs 16.76 m). The previous catalog
positions were the GFS-Wave output points, 0.2-2.8 km from the buoys.

**Platforms.** activestations.xml lists 46012 (Half Moon Bay) as type `usv`: since
2023-08 it is an uncrewed surface vehicle (station page: "Uncrewed Surface Vehicle",
earlier Saildrones SD-1073, SD-1077 and SD-1063), with a watch circle of 20,254 yards
(18.5 km), no APD and no MWD in its 2026 files and no yearly files for 2024 or 2025. A
moving platform without positions in the data is not comparable with a fixed model
point, so `ndbc:46012` is kept in the catalog inactive and unpaired.

**Requests**: a window that starts at most 4.5 days ago (the scheduled 48 h job always
does) reads the 5-day files `data/5day2/{ID}_5day.txt` and `_5day.spec` instead of the
45-day realtime2 files (`NDBC_USE_5DAY`, default on). For 46026 on 2026-10-15 02:40 UTC
these were 67,210 + 16,871 = 84,081 bytes against 600,190 + 148,961 = 749,151 bytes for the
realtime2 pair: 8.9 times less. A 404 on `_5day.txt` falls back to the realtime2 file; a
404 on `_5day.spec` is normal for a buoy that reported no waves in those 5 days (46053
since 2026-10-06) and is not retried elsewhere. Longer windows (backfills) read realtime2
and the archives as before. `activestations.xml` (273 KB) is read at most once a day per
worker process (the parsed positions are cached), not on every hourly run.
Measured live on 2026-10-15 02:49 UTC (own database, `obs ingest --provider ndbc`, 48 h):
11 stations, 23 requests, 850,254 bytes, 844 records, 0 errors. The same job read
5,586,387 bytes from the realtime2 files on 2026-10-12 (below): about 6.6 times more.
Computed from these two runs, the hourly job in a long-running worker (activestations.xml
once a day) moves about 0.58 MB per run, about 14 MB a day, instead of about 134 MB
(5.6 MB x 24).
These byte counts are the decoded files: NDBC also compresses them on the wire. On a fresh
database on 2026-10-16 08:17 UTC the same 48 h job (11 stations, 23 requests with
`activestations.xml`, 864 records) transferred 120,816 bytes for 859,171 bytes of files,
so the hourly job moves at most about 0.12 MB per run, about 2.9 MB a day (computed). Since
then `bytes_downloaded` counts the bytes as transferred (its definition in `records.py`).

* A station keeps what its readable files gave: a file that fails to download or parse is
  reported in the station's `errors` while the other files' records are still stored.
  After a transient failure the station's remaining files are skipped (host down).
  Station ids are checked (alphanumeric only) before they go into a URL.
* Licence: NDBC data are a US Government work, public domain. For the CDIP buoys the CDIP
  files state "These data may be redistributed and used without restriction." and carry
  an acknowledgment, copied verbatim into each station's `attribution` (e.g. "CDIP is
  primarily supported by the U.S. Army Corps of Engineers (USACE). Station partner:
  PACIOOS ; Field operator: PACIOOS/UH").

### GFS-Wave station bulletins (model forecasts at the buoys), live-verified

* `{NOAA_GFS_BASE_URL}/gfs.YYYYMMDD/HH/wave/station/bulls.tHHz/gfswave.{ID}.bull`. The
  first available cycle is 2021-03-22 18Z (06Z and 12Z that day return 404). The 2021
  files have the same layout as current ones.
* The header `Location : 56006 (33.36S 114.78E)` gives the model output point, and
  `Cycle : 20261010  0 UTC` the cycle. There are 385 hourly rows (0 to 384 h), each
  `| DD HH | Hst n x | Hs Tp dir | ... |` with up to six partitions. A `*` before a
  partition marks a probable local wind sea. Partition `dir` is the **mean** direction of
  that wave field and Tp its peak period.
* `.bull` directions are **going-to**. `tests/unit/test_point_forecasts.py` proves this
  on real files: for every partition of the same station and cycle, the `.cbull`
  ("from", Hs in feet) direction equals `.bull` dir + 180 (mod 360).
* Stored per row: `hs_m` = Hst (total-sea Hm0), `swell1_*` = the first partition **as
  listed** (it can be the wind sea: `extra.swell1_is_wind_sea`), and all partitions in
  `extra.partitions` with their `wind_sea` flag. `tp_s`, `direction_deg` and wind stay
  NULL because the bulletin has no total-sea peak period, no total direction and no
  wind. `model_latitude` and `model_longitude` come from the header, and `distance_km`
  is the distance from the buoy. Leads are kept hourly to 120 h and 3-hourly after that,
  up to `FORECAST_DAYS`.
* Bulletins appear about 5.4 h after the cycle (the 12Z files of 2026-10-10 were written
  at 17:25 UTC). A cycle that is not published yet (HTTP 404; the bucket allows public
  listing, so a missing key is a 404) is counted as `missing` and picked up by the next
  run. Any other status, including 403, is an error: when no bulletin at all could be
  read, `collect_recent` fails the job instead of reporting success. Table rows or
  partition cells that do not match the layout are skipped and counted in
  `malformed_cells` (with samples in `error_samples`). Bulletin ids are checked
  (alphanumeric only) before they go into a URL.
* Licence: NOAA data, public domain.

### GEFS-Wave station statistics (ensemble mean and spread at the buoys), live-verified 2026-10-12

* `{noaa-gefs-pds}/gefs.YYYYMMDD/HH/wave/station/gefs.wave.tHHz.bull_tar`: one tar per
  cycle (3.7 MB on 2026-10-11 00Z) with one `gefs.wave.{ID}.bull` per station, read in
  memory (only members named exactly `gefs.wave.{bulletin_id}.bull`; nothing is
  extracted). `obs collect-forecasts --source gefswave_station --start ISO --end ISO
  [--cycles 0] [--station CODE ...]` archives them as point forecasts with source
  `gefswave_station`, family `ncep_gefs` (`verification/gefs_bulletins.py`).
* Each row `| DD HH | Hs avg | Hs spr | Tp avg | Tp spr | U10avg | U10spr | P(Hs>1 m) ... |`,
  3-hourly to 240 h then 6-hourly: `avg` is the ensemble mean, `spr` the spread
  (standard deviation of the members, as the file's legend says). Stored: `hs_m` = Hs
  mean (never a member and not the deterministic run), `hs_spread_m`, `tp_s` = mean of
  the members' peak periods, `wind_speed_kmh` = U10 mean x 3.6; the other spreads and the
  exceedance probabilities go to `extra`. There are no directions.
* **Only cycles from 2026-02-26 00Z carry the spreads.** Earlier files (checked on
  2025-01-15 and 2026-02-25, `tests/fixtures/observations/gefs.wave.51201.20250115t00z.averages-only.bull`)
  print three numbers per row under the same twelve-column header; which three cannot be
  read from the file, so they are refused, never stored or guessed.
* The header longitude is signed degrees east with a trailing `W` on every station
  (52200, Guam: `144.788W` = 144.79 E), so the sign is used and the letter ignored.
* Files exist for 6 of the 10 NDBC stations of the backtest (51201, 51205, 51101,
  46214, 46086, 46054); none for 46026, 46224, 46053, 46237.
* Live: 115 cycles (00Z every second day, 2026-02-26 to 2026-10-12), 425,113,600 bytes,
  55,890 rows, 0 missing, 0 errors, 0 malformed rows (`swell_fval_live`). Used only to
  validate the app's ensemble-based range (docs/ml-evaluation.md section 12); never
  pooled with the deterministic rows. Licence: NOAA data, public domain.

## Conventions on ingest

* Times are UTC, rounded to the nearest minute (two copies of one AODN record differ by
  a few seconds).
* Heights are in metres, with `hs_kind` `spectral_hm0` or `h_third` (they differ by
  several per cent).
* Periods are in seconds. `tp_s` is always the spectral peak period. `tm_s` carries a
  mean period whose definition is in `tm_kind`: `tm01`, `tm02`, `tz` (time-domain zero
  up-crossing) or `apd` (an NDBC average period of a station whose APD definition is not
  in the catalog; NDBC's own buoys give Tm02 and CDIP buoys Tm01, stored as such).
* Directions are nautical degrees FROM in [0, 360) (360 is stored as 0). Going-to
  sources get +180. Directions are never averaged as plain numbers.
* Wind is in km/h (m/s x 3.6).
* Values outside physical ranges and the table CHECK constraints (Hs in (0, 30) m, Tp in
  (0, 40) s, directions in [0, 360], wind in [0, 400) km/h) are dropped and counted in
  the ingest result (`parse.invalid_values`). A row left with no height and no period is
  not stored (`parse.no_wave_data`).

## Quality control and precedence

| Provider | Stored `qc_flag` |
| --- | --- |
| AODN delayed mode | from `WAVE_quality_control`, whose meanings are read from the data. In these datasets `flag_values [1, 2, 3, 4, 9]` mean `good not_evaluated questionable bad missing`, so **2 is "not evaluated", not "probably good"**: 1 becomes `good`, 2 `not_qc`, 3 and 4 `bad`, and 9 is skipped. `WATCH_CIRCLE_flag` (buoy off station) 4 becomes `bad`, and 3 downgrades `good` to `probably_good`. |
| AODN realtime | always `not_qc` (the dataset is declared non-QC). Rows flagged 3, 4 or 9 are dropped and counted. |
| Met Office | `probably_good` (automated real-time checks only). A value whose `<column>_qc` JSON is anything but `{"good": []}` is dropped and counted (`parse.flagged_values`). |
| NDBC realtime | `not_qc` (NDBC blanks failed values as `MM` but publishes no flags). |
| NDBC yearly and monthly archive | `probably_good` (NDBC's quality-controlled archive, no per-value flags). |

Precedence when two records describe the same measurement (same station, within 15
minutes; for example AODN realtime at :00 versus delayed mode at :11):
`good` > `probably_good` > `bad` > `not_qc`. A delayed-mode `bad` row is stored as a
rejection marker with every value NULL, so a realtime copy of a measurement that QC
rejected cannot come back. `not_qc` never overwrites anything. At the same timestamp the
database upsert (`ON CONFLICT ... WHERE`) fires only when the incoming rank is higher, or
equal with different values, which also holds with concurrent writers. Re-ingesting a
window reports `unchanged` rows.

## Station to spot pairings

Distances are great-circle km from the buoy to the break. `seed_stations` checks them
against the seeded spot positions (tolerance 0.5 km). Relations: `offshore_reference`
(directly offshore, comparable exposure), `nearshore_reference` (shallow buoy, already
transformed), `upstream_regional` (swell passes it first; timing and size context only)
and `historical_reference` (an earlier mooring).

| Spot | Station | Relation | km | Bulletin (km from buoy) |
| --- | --- | --- | --- | --- |
| snapper-rocks-superbank | `aodn:tweed-offshore` | offshore_reference | 14.3 | none within 30 km |
| kirra | `aodn:tweed-offshore` | offshore_reference | 16.4 | none |
| burleigh-heads | `aodn:tweed-offshore` | offshore_reference | 26.5 | none |
| snapper-rocks-superbank | `aodn:tweed-heads` | nearshore_reference | 3.3 | none |
| kirra | `aodn:tweed-heads` | nearshore_reference | 5.4 | none |
| burleigh-heads | `aodn:tweed-heads` | nearshore_reference | 15.8 | none |
| burleigh-heads | `aodn:gold-coast` | nearshore_reference | 13.6 | none |
| margaret-river-main-break | `aodn:cape-naturaliste` | offshore_reference | 53.1 | 56006 (19.5) |
| the-box | `aodn:cape-naturaliste` | offshore_reference | 51.1 | 56006 (19.5) |
| north-point-gracetown | `aodn:cape-naturaliste` | offshore_reference | 41.4 | 56006 (19.5) |
| margaret-river-main-break, the-box, north-point-gracetown | `aodn:cape-naturaliste-01` | historical_reference | 58.6-70.9 | none |
| bells-beach | `aodn:apollo-bay` | upstream_regional | 64.8 | none (nearest 45 km) |
| thurso-east | `metoffice:k7` | upstream_regional | 218.0 | 64046 (28.2) |
| thurso-east | `metoffice:k5` | upstream_regional | 475.3 | 64045 (18.4) |
| mullaghmore-head | `metoffice:k4` | upstream_regional | 285.5 | none (62105 is 81 km away) |
| la-graviere-hossegor | `metoffice:brittany` | upstream_regional | 692.2 | 62163 (2.3) |
| pipeline / sunset-beach / waimea-bay | `ndbc:51201` (200 m) | offshore_reference | 6.7 / 7.8 / 6.0 | 51201 (0.2) |
| pipeline / sunset-beach / waimea-bay | `ndbc:51101` (4,847 m) | upstream_regional | 507.6 / 507.5 / 507.9 | 51101 (2.6) |
| jaws-peahi | `ndbc:51205` (200 m) | offshore_reference | 15.3 | 51205 (0.2) |
| ocean-beach-sf / mavericks | `ndbc:46026` (53 m) | offshore_reference | 28.6 / 41.3 | 46026 (1.6) |
| ocean-beach-sf | `ndbc:46237` (16.8 m) | nearshore_reference | 11.1 | 46237 (0.4) |
| ocean-beach-sf / mavericks | `ndbc:46214` (600 m) | upstream_regional | 86.2 / 98.6 | 46214 (0.7) |
| trestles | `ndbc:46277` (303 m) | offshore_reference | 8.3 | none (no bulletin for 46277) |
| trestles | `ndbc:46224` (238 m) | offshore_reference | 25.2 | 46224 (0.3) |
| trestles | `ndbc:46086` (1,862 m) | upstream_regional | 106.0 | 46086 (2.8) |
| rincon | `ndbc:46053` (417 m) | offshore_reference | 36.3 | 46053 (1.0) |
| rincon | `ndbc:46054` (454 m) | upstream_regional | 91.6 | 46054 (1.7) |

`metoffice:k1` and `metoffice:k2` have no nearby spot. They are kept for offshore model
verification (bulletins 62029 and 62081, 8.4 and 5.6 km away). So are
`aodn:rottnest-island-02` and `aodn:esperance-04` (Department of Transport WA Datawell
Directional Waverider MK III buoys in 48 and 52 m, bulletins 56005 and 56010, 1.9 and
0.1 km away): their delayed-mode Hm0 overlaps the bulletin archive in 2021, which is
what the backtest in docs/ml-evaluation.md needs. Both are inactive (the delayed-mode
record ends in 2021-12 and their realtime feeds are not configured), so no scheduled job
touches them. The WW3 depth at the 56010 output point is 25.7 m against 52 m at the
mooring, so local sheltering and depth effects among the islands off Esperance are part
of the model error there.

Justifications and caveats (the full text of each is in the catalog `note`):

* **Gold Coast.** Tweed Offshore (60 m, east-southeast of Point Danger) samples the
  E-SE trade swell and S-SE groundswell before it wraps into the points. The breaks sit
  behind headlands, so southerly swell arrives refracted and reduced, and the points
  respond to direction very differently from the open buoy. Its feed is realtime only
  and reports H1/3. Tweed Heads (22-25 m) and Gold Coast (17 m) are shallow, so their
  waves are already partly shoaled and refracted. They are nearshore context, not
  offshore model truth, but their delayed-QC records back to 1995 and 1987 are the long
  reference.
* **Margaret River.** The Cape Naturaliste buoy (50 m) sees the same SW-W Southern Ocean
  groundswell, but it is 40-55 km north-northwest of the breaks, and reef shoaling and
  refraction are not represented. Its realtime feed is sparse (H1/3, 3-21 h gaps).
  Bulletin 56006 carries the buoy's WMO id, but its position is the 1999-2004 mooring,
  19.5 km north of the current one.
* **Bells Beach.** The Apollo Bay Spotter (30 m) is about 65 km west-southwest along
  the same coast and may be partly sheltered by Cape Otway from W-SW swell. It is a
  regional indicator only.
* **Thurso, Mullaghmore and Hossegor.** The Met Office buoys are hundreds of km
  upstream in deep water. Swell takes hours to arrive (about 5-8 h from K7 to Thurso,
  7-10 h from K4 to Mullaghmore, 15-20 h from Brittany to Hossegor), and the shelf,
  Orkney's shadow, Mullaghmore's reef and the Capbreton canyon transform it. Never
  compare same-time values for spot calibration.
* **NDBC** (positions from activestations.xml, depths from the NDBC station pages; the
  pairing table with bearings, the analysis-time check of each bulletin point and the
  stations considered and not used is in docs/ml-evaluation.md, section 11). 51201 (CDIP
  Waverider, 200 m) lies 6-8 km W-WNW of the three North Shore breaks in the same NW-N
  exposure; the reefs add strong refraction and focusing (Caldwell & Aucan 2007), so buoy
  Hs is not surf height. 51101 is a deep-ocean buoy 508 km NW, on the path of the NW
  swell (9-10 h ahead at 15 s): context only. 51205 (200 m) is 15 km WNW of Pe'ahi on
  the same open coast. 46026 (53 m) is offshore of Ocean Beach (SF Bar effects) and
  Mavericks (Pillar Point reef focusing); 46237 sits on the SF Bar in 17 m, so its waves
  are already transformed by the bar (nearshore context for Ocean Beach only); 46214 is
  86-99 km upstream for W-NW swell. 46277 (303 m) is 8 km SW of Trestles in the same
  exposure but has no GFS-Wave bulletin; 46224 is 25 km SSE inside the Southern
  California Bight with comparable island shadowing, which changes quickly with
  direction; 46086, 106 km SSW outside the island shadow, is upstream for
  southern-hemisphere swell. 46053 is in the Santa Barbara Channel, partly sharing
  Rincon's island and Point Conception shadow (no reports after 2026-10-06 05:50 UTC);
  46054 is at the channel's western entrance, upstream for W swell. Not used: 46012 (an
  uncrewed surface vehicle since 2023-08), 51001 (its GFS-Wave output point is 118 km
  from the mooring), 51000 (outside the NW-N windows; output point 31 km away), 46025
  (SSE of Rincon, outside its 250-300 degree window) and 46217 (Anacapa Passage: not in
  activestations.xml and no yearly, monthly or realtime file for 2024-2026).

Spots without a buoy in a reachable or documented feed have no pairing. No pairing was
invented to fill a gap.

## Observations on spot pages

`GET /api/spots/{slug}/observations` (and the timeline's `observations` block, schema
`ObservationsBlockOut` in app/schemas/tides.py, built by
`app.services.tides.api.spot_observations_block`) shows what the instruments near a spot
measured, kept apart from every forecast:

* Only stations paired with the spot as `offshore_reference` or `nearshore_reference`
  are listed, offshore first, then by distance. `upstream_regional` and
  `historical_reference` stations (hundreds of km away, or retired) are not.
* One item per station: its newest stored wave reading at or before the end of the window
  (`OBSERVATION_LOOKBACK_HOURS`, 48 h), with `kind = "instrument"`, station code and name,
  provider, relation, distance from the break, time, Hs and its definition (`hs_kind`),
  Tp, the direction the waves come FROM, wind speed, the QC flag, licence and attribution.
  A station without a reading in the window is still shown with its last reading.
* `stale` is set when the reading is older than its provider's limit (heuristic: NDBC and
  Met Office 3 h, AODN 4 h, others 6 h) or older than the window. The block's `status` is
  `available` when at least one item is fresh, `stale` when all are, `unavailable` (with
  a reason) when the spot has no reference station or none has a stored reading.
* Live, 2026-10-15 02:55 UTC (own database, the 48 h NDBC ingest above plus a 10-day
  backfill of 46053): Rincon `stale` (46053, last wave report 2026-10-06 05:40 UTC,
  OBS-TIDE-9); Jaws `stale` (51205, last report 2026-10-13 18:56 UTC); Ocean Beach
  `available` (46026 at 28.6 km and 46237 at 11.1 km); Pipeline and Trestles `available`;
  Nazare `unavailable` (no reference station).
* The same day at 20:31 UTC (review, another own database, 48 h NDBC ingest: 11 stations,
  23 requests, 855,389 bytes, 820 records, 0 errors) 46053 was reporting waves again (16
  wave readings in the window, the newest 19:40 UTC), so Rincon's block was `available`:
  the flag follows the stored readings. Jaws was `unavailable` there (51205 had no reading
  in the 48 h window and no backfill was run).

## Operation

* Settings (`app/core/config.py`): `OBSERVATIONS_ENABLED`, `OBSERVATION_PROVIDERS`
  (default `aodn,metoffice,ndbc`), `OBSERVATION_LOOKBACK_HOURS` (48), `AODN_BASE_URL`,
  `METOFFICE_BASE_URL`, `NDBC_BASE_URL`, `NDBC_USE_5DAY` (true: read NDBC's 5-day files
  when the window fits), `VERIFICATION_ENABLED`, `POINT_FORECAST_SOURCES`
  (`gfswave_bulletin`), `NOAA_GFS_BASE_URL`, `FORECAST_DAYS`.
* Celery: `observations.ingest` (hourly, when enabled) calls `ingest_all(db)`, and
  `verification.collect_point_forecasts` (00/06/12/18:50 UTC) calls `collect_recent(db)`.
  A provider that fails is reported in `failed` with `transient: true/false`, and the
  other providers still run. The job fails only when every provider fails.
* CLI (`python -m app.cli obs ...`):
  * `seed-stations [--catalog PATH]`
  * `ingest [--provider aodn|metoffice|ndbc] [--since ISO --until ISO] [--station CODE ...]`
    (runs even if `OBSERVATIONS_ENABLED` is false). `--provider aodn --aodn-dataset
    delayed_qc` reads only the delayed-mode QC'd dataset, for a backfill that must not
    contain realtime `not_qc` rows (they would otherwise fill the gaps QC left).
  * `list`
  * `collect-forecasts --start ISO --end ISO [--cycles 0,12] [--station CODE ...]`
    (backfill from 2021-03-22 18Z, idempotent, at most 8 parallel downloads, 404s counted
    as `missing`, other statuses as `errors`)
  * `collect-forecasts ... --station CODE --bulletin-id ID` archives another bulletin for
    one station, for diagnostics. Unless `ID` is the station's catalog `bulletin_id`, the
    rows are stored as source `gfswave_bulletin_proxy`, never pooled with the
    like-for-like `gfswave_bulletin` statistics, and never collected by the scheduled job.
    A proxy point can be tens of km away in other depth and exposure, so its error
    against the buoy mixes model error with the change in sea state between the places.

## Live verification (2026-10-11, own database, sandbox)

| Check | Result |
| --- | --- |
| `obs seed-stations` | 18 stations created. Catalog distances matched the seeded spots. |
| AODN, 7 days (2026-10-04 05:00 to 2026-10-11 05:00 UTC), realtime (delayed QC does not cover the period) | Cape Naturaliste: 1 partition, 19 records (H1/3, `not_qc`), latest 2026-10-10 21:00. Tweed Offshore: 325 records (half-hourly H1/3), latest 2026-10-11 03:30. 161 `WPPE = 0` fill values and their `WPDI` were dropped and counted. 6 HTTP requests. |
| Met Office, current files (2026-10-03 00:00 to 2026-10-11 04:00 UTC) | 197 hourly records each for K1, K2, K4, K5, K7 and Brittany (1,182 rows, Hm0, `probably_good`), 0 flagged values, 1,194 requests (listing pages plus one small file per buoy-hour). |
| GFS-Wave bulletins, 00Z and 12Z, 2026-10-08 00Z to 2026-10-11 00Z | 72 bulletins for 12 stations (6 cycles), 15,048 rows, 0 parse errors. The 12 bulletins of 11 Oct 00Z were not yet published at 05:27 UTC and were counted as `missing`. |
| Sample pairing at +24 h | Brittany, cycle 2026-10-09 12Z, valid 2026-10-10 12Z: forecast Hst 2.16 m with partition 1 at 9.5 s from 300 degrees; observed Hm0 2.2 m, Tp 9.8 s from 293 degrees. Cape Naturaliste at the same valid time: forecast 3.67 m (15.1 s from 232 degrees, model point 19.5 km away); observed H1/3 4.28 m, 15.5 s from 236 degrees (not QC'd, different height definition). |
| Leads 0-24 h, Met Office buoys (illustrative, 3 days, not a skill claim) | 710 pairs: bias -0.21 m, RMSE 0.45 m, median absolute direction difference 15 degrees (14 pairs over 90 degrees, where partition 1 is not the peak system). |
| `ingest_all` job body with `aodn,metoffice,ndbc` | `status = partial` in 30.5 s. AODN: 177 new rows (Tweed Heads 91, Gold Coast 86) and the rest already stored. Met Office: 282 rows already stored. NDBC: `ProxyError: 403 Forbidden after 3 attempts`, reported as transient. |
| Real-file parsing | Full 385-row bulletins (56006 from 2021-03-23 and 2026-10-10, 51201, 62163) parse with 0 malformed cells. A real AODN delayed-mode partition (Cape Naturaliste 02, December 2021) gives 1,189 `good` WSSH records at :11/:41. |

Verified only with mocks or fixtures: AODN delayed-mode QC codes 3/4/9 and
`WATCH_CIRCLE_flag` handling (the live runs rejected no AODN rows, and the two real
partitions inspected carry only flag 1 in delayed mode and flag 2 in realtime), Met
Office flagged-value handling (no flagged values appeared live), re-issued Met Office
hours, and transient-failure paths other than the NDBC proxy refusal. (At the time of
this table NDBC was verified only with synthetic text; see the next section.)

## Live verification of NDBC (2026-10-12, own database `swell_ndbc_live`)

| Check | Result |
| --- | --- |
| `obs seed-stations` with the updated catalog | 26 stations, of them 12 NDBC (11 active; `ndbc:46012` inactive). Every NDBC position equals activestations.xml after ingest (`position_verified: true`). |
| Realtime only, 2026-10-09 00:00 to 2026-10-12 12:00 UTC, 11 stations | 1,654 records, 23 requests, 5,586,387 bytes, 0 errors. At the NDBC-processed buoys (46026, 46054, 46086, 51101) 100-129 duplicate wave rows per station were folded into their report; the CDIP buoys had none. 46053: no record (no reports after 2026-10-06 05:50 UTC). |
| 24-month backfill, 2024-10-12 00:00 to 2026-10-12 12:00 UTC, 11 stations | 337,400 records (spectral Hm0), 135 requests (yearly 2024 and 2025, monthly January-August 2026, realtime), 16,983,432 bytes, 0 errors; 2 coverage warnings (51201 March and April 2026 monthly files: 404 under both names). 1,626 records already stored by the realtime run were `unchanged`; at most stations 189-192 realtime records of 2026-08-29 to 08-31 were superseded by the QC'd August file in the same run. |
| History for the long-term climatology, 2019-10-12 to 2024-10-12, 10 stations | 680,809 records, 61 requests (yearly files), 24,265,456 bytes, 0 errors. |
| GFS-Wave bulletins at the 10 NDBC stations with a bulletin | 366 cycles each (00Z on every second day, 2024-10-12 to 2026-10-12), 589,260 rows, 0 missing, 0 malformed cells. |
| `verify pair --full` | 585,970 forecasts scanned, 504,351 Hs pairs, all `observed_kind = spectral_hm0`, 81,619 without an observation within 30 min. |
| Backtest | docs/ml-evaluation.md, section 11. |

Not verified live: the October to December monthly-file codes (`a`, `b`, `c`; those
directories were empty), a status-200 answer that is not gzip, truncated or oversized
(never seen live; tested with synthetic bodies), the old yearly layouts with 2-digit years (synthetic fixture
only; the files read live were 2019-2026). The 5-day files were verified live on 2026-10-15
(above).

## Known limitations

* Realtime AODN pairs are H1/3 and not QC'd. Spectral Hm0 at the paired AODN sites
  exists only in delayed mode, which ends in 2021-2022 for the WA and QLD sites.
* Delayed-mode bursts are time-stamped at :11/:41 and Met Office rows at HH:00, while
  bulletins are hourly. Pairing must match the nearest time within a tolerance, not
  exact timestamps.
* Partition 1 in a bulletin is a height rank, not a persistent swell identity. Match
  swells by direction and period, not by index.
* Met Office ingestion downloads one small file per buoy and hour (about 288 requests per
  48 h lookback). Skipping hours already stored would cut that.
* NDBC: a month that lies wholly inside the realtime window (the last 44 days) is read
  from the realtime files (`not_qc`) even when NDBC has already published its
  quality-controlled monthly file (September 2026 on 2026-10-12). A later backfill whose
  window covers that month, run once it has left the realtime window, reads the monthly
  file, and its `probably_good` records then supersede the realtime ones. The scheduled
  48 h job never does that.
* NDBC realtime `.txt` prints heights to 0.1 m and DPD in whole seconds; the archive
  values (0.01 m, 0.01 s) replace them when they arrive.
