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
  | `aodn:tweed-heads` | 1995-01 to 2022-12 (`Tweed Heads`, 211 files) | 2024-01 to 2026-10 |
  | `aodn:gold-coast` | 1987-02 to 2022-12 (`Gold Coast`, 213 files) | 2024-01 to 2026-10 |
  | `aodn:apollo-bay` | 2019-10 to 2026-06 (`APOLLO-BAY`, 108 files) | 2024-01 to 2025-05 (`Apollo Bay`), 2025-06 to 2026-09 (`APOLLO-BAY`) |
  | `aodn:tweed-offshore` | none | 2024-01 to 2026-10 (`Tweed Offshore`) |

  The GFS-Wave bulletin archive starts with the 2021-03-22 18Z cycle, so delayed-QC
  observations overlap archived bulletin forecasts only at Cape Naturaliste (bulletin
  56006), for about nine months (2021-03-22 to 2021-12-31). The Apollo Bay realtime
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

### NOAA NDBC (US), NOT verified live

`www.ndbc.noaa.gov` is blocked from the sandbox this was built in (the proxy answers
`403 Forbidden` to CONNECT). The adapter follows NDBC's documented formats and was tested
only with synthetic fixtures. A live `ingest_all` run reported it as a transient provider
failure (see below), as designed.

* Realtime, last ~45 days: `{NDBC_BASE_URL}/data/realtime2/{ID}.txt` (standard
  meteorological) and `{ID}.spec` (NDBC's own swell / wind-sea split; `SwD` is a 16-point
  compass name, stored as the sector centre). Missing values are `MM`.
* History: `view_text_file.php?filename={id}h{YYYY}.txt.gz&dir=data/historical/stdmet/`.
  Missing values are runs of 9s with a meaning that depends on the column: WVHT, DPD and
  APD 99.00; MWD and WDIR 999; WSPD and GST 99.0; PRES 9999.0; ATMP, WTMP and DEWP 999.0;
  VIS 99.0; TIDE 99.00. A blanket mask would delete a real 99-degree wind direction. Old
  yearly layouts (2-digit years, no minute column, `WD`/`BAR` names) are handled. Months
  of the current year older than ~45 days are only in NDBC monthly files, which are not
  implemented; requests reaching into that gap get a warning.
* WVHT is stored as `spectral_hm0` (NDBC computes it from the spectrum), DPD as `tp_s`,
  APD as `tm_s` (`tm_kind = apd`), MWD (the direction at DPD) as `peak_direction_deg`,
  and WSPD x 3.6 / WDIR as wind.
* Positions: the catalog uses the GFS-Wave output-point position for the same WMO id
  (`position_verified = false`). The first successful NDBC ingest replaces it from
  `activestations.xml`, and re-seeding keeps the replacement.
* Licence: US Government work, public domain. Credit the operator where NDBC lists a
  partner.

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
  at 17:25 UTC). A cycle that is not published yet is counted as `missing` and picked up
  by the next run.
* Licence: NOAA data, public domain.

## Conventions on ingest

* Times are UTC, rounded to the nearest minute (two copies of one AODN record differ by
  a few seconds).
* Heights are in metres, with `hs_kind` `spectral_hm0` or `h_third` (they differ by
  several per cent).
* Periods are in seconds. `tp_s` is always the spectral peak period. `tm_s` carries a
  mean period whose definition is in `tm_kind`: `tm01`, `tm02`, `tz` (time-domain zero
  up-crossing) or `apd` (NDBC average period).
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
| NDBC historical | `probably_good` (final NDBC QC, no per-value flags). |

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
| pipeline / sunset-beach / waimea-bay | `ndbc:51201` | offshore_reference | 6.9 / 8.0 / 6.2 | 51201 |
| jaws-peahi | `ndbc:51205` | offshore_reference | 15.4 | 51205 |
| ocean-beach-sf, mavericks | `ndbc:46026` | offshore_reference | 27.1, 40.2 | 46026 |
| mavericks | `ndbc:46012` | offshore_reference | 36.5 | 46012 |
| rincon | `ndbc:46053` | offshore_reference | 37.3 | 46053 |
| trestles | `ndbc:46224` | offshore_reference | 25.1 | 46224 |

`metoffice:k1` and `metoffice:k2` have no nearby spot. They are kept for offshore model
verification (bulletins 62029 and 62081, 8.4 and 5.6 km away).

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
* **NDBC.** 51201 sits directly off Waimea Bay in deep water. The reefs add strong
  refraction and focusing (Caldwell & Aucan 2007), so buoy Hs is not surf height. 51205
  lies upstream of Pe'ahi for N-NW swell. 46026 and 46012 are offshore of Ocean Beach (SF
  Bar effects) and Mavericks (Pillar Point reef focusing). 46053 is in the Santa Barbara
  Channel, partly sharing Rincon's island and Point Conception shadow. 46224, about 25 km
  south-southeast of Trestles inside the Southern California Bight, has comparable
  island shadowing, which changes quickly with direction. All NDBC positions are model
  output points until NDBC metadata replaces them.

Spots without a buoy in a reachable or documented feed have no pairing. No pairing was
invented to fill a gap.

## Operation

* Settings (`app/core/config.py`): `OBSERVATIONS_ENABLED`, `OBSERVATION_PROVIDERS`
  (default `aodn,metoffice,ndbc`), `OBSERVATION_LOOKBACK_HOURS` (48), `AODN_BASE_URL`,
  `METOFFICE_BASE_URL`, `NDBC_BASE_URL`, `VERIFICATION_ENABLED`, `POINT_FORECAST_SOURCES`
  (`gfswave_bulletin`), `NOAA_GFS_BASE_URL`, `FORECAST_DAYS`.
* Celery: `observations.ingest` (hourly, when enabled) calls `ingest_all(db)`, and
  `verification.collect_point_forecasts` (00/06/12/18:50 UTC) calls `collect_recent(db)`.
  A provider that fails is reported in `failed` with `transient: true/false`, and the
  other providers still run. The job fails only when every provider fails.
* CLI (`python -m app.cli obs ...`):
  * `seed-stations [--catalog PATH]`
  * `ingest [--provider aodn|metoffice|ndbc] [--since ISO --until ISO] [--station CODE ...]`
    (runs even if `OBSERVATIONS_ENABLED` is false)
  * `list`
  * `collect-forecasts --start ISO --end ISO [--cycles 0,12] [--station CODE ...]`
    (backfill from 2021-03-22 18Z, idempotent, at most 8 parallel downloads, 404s counted
    as `missing`)

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

Verified only with mocks or fixtures: every NDBC request and parser (synthetic text in
the documented layouts), the `activestations.xml` position update, AODN delayed-mode QC
codes 3/4/9 and `WATCH_CIRCLE_flag` handling (the live runs rejected no AODN rows, and
the two real partitions inspected carry only flag 1 in delayed mode and flag 2 in
realtime), Met Office flagged-value handling (no flagged values appeared live), re-issued Met Office hours, and transient-failure paths other than the
NDBC proxy refusal.

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
* NDBC monthly files (the current year beyond ~45 days) are not implemented.
