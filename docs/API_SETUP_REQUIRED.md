# External services: what to set up, what works without an account

Every external service the app can use, with its purpose, the variables it provides, its
documentation, whether it needs an account or key, its free tier, licence and attribution,
the expected cost, the environment variables, how to set it up and how to test it.
Settings live in `backend/app/core/config.py`; every variable named here is also in
`.env.example` (a unit test, `backend/tests/unit/test_api_setup_doc.py`, checks this).
No secret belongs in the repository: put keys in `.env` (not committed) or the deployment's
secret store.

Pages were read on **2026-10-14**; every sentence in quotation marks below was found
verbatim on the cited page that day (fetched fresh, not from a cache). The Open-Meteo terms,
pricing and licence quotes and the Natural Earth terms were read again on **2026-10-16** and
were still on those pages. Prices shown only by JavaScript widgets could not be read and are
marked "confirm at checkout".

## 1. Summary for the owner

### 1.1 Works with no account and no key

Nothing to sign up for. The recommended live setup (section "Recommended live
configuration" below) uses only these.

| Area | Service | Cost | Licence, and what the app shows | Limits and notes |
|---|---|---|---|---|
| Spot forecasts | Open-Meteo free tier: GFS family (`open_meteo`) and ECMWF family (`open_meteo_ecmwf`) | Free | Data CC BY 4.0; a credit and licence link is printed next to every value derived from it (2.1) | **Non-commercial use only** (1.3). Per IP address: 600 calls a minute, 5,000 an hour, 10,000 a day, 300,000 a month; the app uses about 820 weighted calls a day (8 %) |
| Forecast uncertainty | NOAA GEFS-Wave ensemble statistics | Free | NOAA open data, "can be used as desired"; credited "(modified)" (2.3) | 69.4 MB per run, 4 runs a day |
| Map layers | NOAA NODD byte ranges (GFS 0.5° atmosphere, GFS-Wave 0.25°) | Free | as GEFS-Wave (2.2) | About 0.96 GB a day with the partition layers |
| Spot forecasts, fallback | NOAA GFS / GFS-Wave and ECMWF open data, direct GRIB | Free | NOAA as above; ECMWF CC BY 4.0, commercial use allowed with attribution (2.4) | Off by default (about 1.9 GB per NOAA cycle) |
| Tides | NOAA CO-OPS (8 US spots), EOT20 harmonic constants (42 spots) | Free | CO-OPS public domain, NOS requests attribution; EOT20 CC BY 4.0, credited with its changes and DOI (2.7, 2.8) | No account; set an application name (`NOAA_COOPS_APPLICATION`) |
| Observations | NDBC (incl. CDIP buoys), AODN, Met Office | Free | Public domain; CDIP unrestricted; AODN CC BY 4.0; Met Office CC BY-SA 4.0 (2.6) | Off by default (`OBSERVATIONS_ENABLED`) |
| Basemap | Natural Earth, bundled with the app | Free | "All versions of Natural Earth raster + vector map data found on this website are in the public domain." ([terms](https://www.naturalearthdata.com/about/terms-of-use/)) | The default map; no tile server |
| Streets basemap | OpenStreetMap standard tiles | Free for light use | ODbL, "© OpenStreetMap contributors" shown (2.13) | Light use only; heavy traffic needs a tile provider |
| Email, SMS, flights in development | Console / Mailpit email, console SMS, demo flights | Free | — | No real email, SMS or fares: demo fares are labelled "Mock fares" |

### 1.2 What the owner must set up

| # | What | Needed for | Account and key | Expected cost | Variables |
|---|---|---|---|---|---|
| 1 | **Decide whether the app is commercial** (1.3); if it is, an Open-Meteo API plan | Lawful use of the spot forecasts | open-meteo.com account, API key | Standard plan: 1M calls a month, covers the Forecast and Marine APIs; monthly price not readable on the page, **confirm at checkout** (a 2023 announcement said $29 a month, not confirmed for 2026). Nothing else changes | `OPEN_METEO_API_KEY`, then raise `OPEN_METEO_CALLS_PER_DAY` / `OPEN_METEO_CALLS_PER_MONTH` to the plan's budget |
| 2 | **Real email**: verification, password reset, swell alerts, admin alerts | Accounts and alerts outside development | Resend or SendGrid account, or any SMTP service; a sending domain you control with SPF, DKIM and DMARC records | Resend: "$0 / mo 3,000 emails / mo 100 emails a day", Pro "$20 / mo 50,000 emails / mo"; SendGrid: confirm at signup (2.11) | `EMAIL_PROVIDER`, `EMAIL_FROM`, `RESEND_API_KEY` or `SENDGRID_API_KEY` or `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`; optional `RESEND_WEBHOOK_SECRET` / `SENDGRID_WEBHOOK_PUBLIC_KEY` |
| 3 | **Real flight prices** | Opportunities with bookable fares | Duffel (self-serve) or an Amadeus Enterprise agreement | Duffel: "$3.00 per order" plus "1% total order value" for managed content, searches beyond a 1500:1 search-to-book ratio "$0.005" each; Amadeus: contract (2.10) | `FLIGHT_PROVIDER`, `DUFFEL_ACCESS_TOKEN` or `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` |
| 4 | SMS alerts (optional) | Text-message alerts and STOP handling | Twilio account and a sending number (US numbers need A2P 10DLC registration) | US SMS "$0.0083" per segment plus carrier fees (2.12) | `SMS_PROVIDER`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID` |
| 5 | CO-OPS application name (no account) | Identifies the app in NOAA's logs | None | Free | `NOAA_COOPS_APPLICATION`, e.g. the app's name and a project URL; never a personal email address |
| 6 | A tile provider, only for a high-traffic Streets basemap | Heavy use of the optional Streets basemap | CARTO (free key) or any XYZ provider | Free CARTO keys (about 5M tiles a month, non-commercial); providers' plans otherwise (2.13) | `NEXT_PUBLIC_CARTO_API_KEY` or `NEXT_PUBLIC_MAP_TILE_URL` + `NEXT_PUBLIC_MAP_ATTRIBUTION` |
| 7 | Size the database alert from the disk (no account) | The data-health database-size alert | None | — | `DATA_HEALTH_DATABASE_MAX_GB` (default 5: reached after about 100 days with the default retention; about 17 GB at 730 days, docs/operations.md) |

Not needed and not recommended: **Windy** (2.9). Its terms forbid storing the data and
building derived databases, which is what this app does; the provider is refused unless
`WINDY_TERMS_ACKNOWLEDGED=true`, and it costs 990 EUR a year. Copernicus Marine (2.5) is not
implemented.

### 1.3 The commercial-use question for Open-Meteo

The recommended setup is free only while the app is non-commercial. Open-Meteo's terms
(read 2026-10-16, [terms](https://open-meteo.com/en/terms)):

* "You may only use the free API services for non-commercial purposes."
* Non-commercial examples include "Using our service for private or non-profit websites or
  apps that do not have subscriptions or advertising." and "Using our service for public
  research conducted at public institutions."
* Commercial examples include "Operating websites or apps that have subscriptions or display
  advertisements." and "Integrating our service into commercial products or promotional
  activities."
* The free tier is limited per IP address ("Less than 10'000 API calls per day, 5'000 per
  hour and 600 per minute."), so on shared hosting other users of the same address count
  against it. This sandbox's shared address was refused with "Daily API request limit
  exceeded. Please try again tomorrow." on 2026-10-14 and again on 2026-10-16 while the app
  itself had used 143 and 55 weighted calls.

So: a private, free app without ads or subscriptions fits the free tier. Ads,
subscriptions, a paid tier or earning from the flight offers point to a paid plan, and so
does a deployment on shared hosting (for the per-IP limit) ("Standard: 1M calls/month. Professional: 5M calls/month. Enterprise:
50M+ calls/month." [pricing](https://open-meteo.com/en/pricing); the app's about 24,600
weighted calls a month fit the Standard plan). The data licence (CC BY 4.0 with a link next to
the data) applies on every plan and the app already meets it. The only change is
`OPEN_METEO_API_KEY` (the adapter then uses the `customer-api` and `customer-marine-api`
hosts; mock-tested, not tried with a real key). Whether a given business model is commercial
is the owner's call, not a technical one.

Check what is missing for the configured providers:

```bash
cd backend && python -m app.cli check-config
```

It prints `{"missing": [...]}` and exits with 1 if anything is missing.

### Recommended live configuration

```env
FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf
FORECAST_BLEND_ENABLED=true
GEFS_ENABLED=true
GEFS_STAT_FIELDS=HTSGW,PERPW
GEFS_CYCLES=00,06,12,18
MAP_FIELDS_ENABLED=
MAP_FIELD_STEPS=0-120:3,120-240:6
MAP_FIELD_CYCLES=00,12
MAP_FIELD_PARTITIONS=on
TIDE_PROVIDERS=noaa_coops,harmonic_model
NOAA_COOPS_APPLICATION=SwellTravelAgent
OPEN_METEO_API_KEY=
```

Two independent model families are blended; GEFS-Wave adds the ensemble spread (Hs and Tp
only); an empty `MAP_FIELDS_ENABLED` means automatic (standalone map layers with a live
provider); `NOAA_COOPS_APPLICATION` takes an application name, for example
"SwellTravelAgent (+https://your.site)"; `OPEN_METEO_API_KEY` stays empty unless the app is
commercial (1.3). Apart from the application name, only `FORECAST_PROVIDERS` and
`GEFS_ENABLED` differ from the defaults.

**Upgrading an existing `.env`:** deployments whose `.env` was copied from an older
`.env.example` set `OPEN_METEO_WAVE_MODEL` and `OPEN_METEO_ATMOS_MODEL` explicitly. These
are deprecated aliases and, when set, they win: a single wave model for every spot and
`gfs_seamless` instead of `gfs_global`. Delete both lines to get the per-latitude wave
models (`OPEN_METEO_GFS_WAVE_MODELS`) and `OPEN_METEO_GFS_ATMOS_MODEL=gfs_global`.

## 2. Services

### 2.1 Open-Meteo Forecast API and Marine API (spot forecasts, default)

| | |
|---|---|
| Purpose | Point forecasts for the 50 spots, two independent model families: NOAA GFS + GFS-Wave (`open_meteo`, source `open_meteo_noaa`) and ECMWF IFS + ecWAM (`open_meteo_ecmwf`) |
| Variables | Marine: significant height, peak period, mean direction; primary, secondary and tertiary swell and wind sea (height, period, direction) for GFS-Wave; ECMWF WAM: height, direction, peak period (`wave_peak_period`) and mean period. Forecast API: 10 m wind speed and direction, gust, MSL pressure |
| Models | `ncep_gfswave016` (0.16°, about 15°S-52.5°N) or `ncep_gfswave025` elsewhere; `gfs_global`; `ecmwf_wam025`; `ecmwf_ifs025`. Only the models' own time steps are stored (GFS hourly to 120 h then 3-hourly; IFS/WAM 3-hourly to 144 h then 6-hourly) |
| Docs | <https://open-meteo.com/en/docs>, <https://open-meteo.com/en/docs/marine-weather-api>, <https://open-meteo.com/en/docs/gfs-api>, <https://open-meteo.com/en/docs/ecmwf-api>, <https://open-meteo.com/en/docs/model-updates> |
| Account / key | None on the free tier. A paid plan gives an API key: set `OPEN_METEO_API_KEY` and the adapter uses the `customer-api` and `customer-marine-api` hosts |
| Free tier | "You may only use the free API services for non-commercial purposes." and "Less than 10'000 API calls per day, 5'000 per hour and 600 per minute." ([terms](https://open-meteo.com/en/terms)); 300,000 per month |
| Call weighting | Requests "for data covering more than 10 weather variables or extending over a period of more than 2 weeks for a single location are considered multiple API calls" ([pricing](https://open-meteo.com/en/pricing)). The app charges `locations x max(1, variables/10) x max(1, days/14)` per request to a Redis budget before sending it (`backend/app/services/forecasts/quota.py`). Metadata calls are free: they are "not counted toward daily or monthly request limits" ([model updates](https://open-meteo.com/en/docs/model-updates)) |
| Measured use | One run for all 50 spots (live, 2026-10-14 12Z): GFS family, 16 days, 142.9 weighted calls (waves 85.7, wind 57.1) and 345.6 kB on the wire including the metadata reads; ECMWF family, 15 days, 124.3 weighted calls and 175.1 kB. About 820 weighted calls a day with the default schedule, 8 % of the free tier. The full numbers are in `docs/FORECAST_SOURCES.md` ("API-first acquisition") |
| Licence and attribution | "Attribution 4.0 International (CC BY 4.0)" and "You must include a link next to any location Open-Meteo data are displayed" ([licence](https://open-meteo.com/en/licence)). The API returns the links in `attribution_links` (Open-Meteo, its licence, NOAA NCEP or "Copyright ECMWF" with CC BY 4.0) for the spot forecast and timeline, the map snapshot and every swell event; they are printed next to the values on the spot page, the map's spot panel and best-spots list, the landing page's and dashboard's swell cards, the opportunity page and in alert emails |
| Cost | Free for non-commercial use. Paid plans: "Standard: 1M calls/month. Professional: 5M calls/month. Enterprise: 50M+ calls/month." ([pricing](https://open-meteo.com/en/pricing)); the monthly prices are shown by a widget that could not be read: **confirm at checkout**. Standard covers Forecast and Marine; "Historical, climate, ensemble, and satellite radiation APIs require the Professional API Plan or higher." |
| Env vars | `FORECAST_PROVIDERS`, `OPEN_METEO_API_KEY`, `OPEN_METEO_GFS_WAVE_MODELS`, `OPEN_METEO_GFS_ATMOS_MODEL`, `OPEN_METEO_ECMWF_WAVE_MODEL`, `OPEN_METEO_ECMWF_ATMOS_MODEL`, `OPEN_METEO_CELL_SELECTION`, `OPEN_METEO_AVAILABILITY_WAIT_MINUTES`, `OPEN_METEO_NATIVE_STEPS_ONLY`, `OPEN_METEO_CALLS_PER_MINUTE`, `OPEN_METEO_CALLS_PER_HOUR`, `OPEN_METEO_CALLS_PER_DAY`, `OPEN_METEO_CALLS_PER_MONTH`, `FORECAST_BATCH_SIZE`, `PROVIDER_RESPONSE_CACHE_ENABLED`, `PROVIDER_RESPONSE_CACHE_MAX_BYTES`, `PROVIDER_RESPONSE_CACHE_MAX_KEYS`; deprecated: `OPEN_METEO_WAVE_MODEL`, `OPEN_METEO_ATMOS_MODEL` |
| Setup | Free: nothing. Paid: subscribe, set `OPEN_METEO_API_KEY`, and raise `OPEN_METEO_CALLS_PER_DAY`/`OPEN_METEO_CALLS_PER_MONTH` to the plan's budget (the per-minute and per-hour limits of the free tier are the app's defaults) |
| Test | `FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf python -m app.cli pipeline` on a scratch database, then `GET /api/system/status`; mock tests: `pytest tests/unit/test_open_meteo_adapter.py tests/unit/test_open_meteo_ecmwf.py tests/unit/test_quota_weighting.py` |

Behaviour worth knowing:

* Grid cells: "sea prefers grid-cells on sea" ([docs](https://open-meteo.com/en/docs)); the
  app sends `cell_selection=sea` and records the cell Open-Meteo used. The chosen cell can
  still be masked by the wave model (no value in any hour: `ecmwf_wam025` at Keramas and
  Puerto Escondido on 2026-10-14); the app then reads the eight neighbouring cells and uses
  the nearest one with data (a few extra weighted calls, recorded in the run's warnings).
* Run identity: the run comes from each model's `meta.json`; a new run is read only after
  Open-Meteo's advice: "please wait 10 minutes after the availability time".
* Open-Meteo fills hours a model did not output: "values are interpolated from 3-hourly to
  1-hourly after 120 hours" (GFS) and IFS 0.25 "offers 0.25° resolution and 3- and 6-hourly
  data"; those hours are not stored (or are flagged `interpolated` when
  `OPEN_METEO_NATIVE_STEPS_ONLY=false`).
* Marine: "Wave directions are always reported as the direction the waves come from."
  The sea-level variable is "not suitable for coastal navigation" and is not used as a tide.
* The free tier counts calls per IP address. On 2026-10-14 the Forecast API answered this
  sandbox's shared egress address with "Daily API request limit exceeded. Please try again
  tomorrow." while the app itself had used 143 weighted calls, and again at 21:47 UTC
  between successful requests; a full acquisition of both families went through at
  22:04 UTC. On shared hosting a paid key avoids depending on other users of the address.
  The run then ends FAILED with that message, is not retried until the next schedule
  (which re-reads the responses already received from the cache), and the previous run
  stays in use.

### 2.2 NOAA NODD: GFS, GFS-Wave (map layers; optional spot-forecast fallback)

| | |
|---|---|
| Purpose | Map fields (wind, pressure, total sea and swell partitions) by HTTP byte range; optionally spot forecasts (`FORECAST_PROVIDERS=noaa_gfs`, off by default: about 1.9 GB per cycle) |
| Variables | GFS `pgrb2.0p50` UGRD/VGRD 10 m and PRMSL; GFS-Wave `global.0p25` HTSGW/PERPW/DIRPW plus partitions; spot fallback: `global.0p16`/`global.0p25` wave records, `pgrb2.0p25` wind, gust, pressure |
| Docs | <https://registry.opendata.aws/noaa-gfs-bdp-pds/>, `docs/noaa-direct-ingestion.md`, `docs/map-layers.md` |
| Account / key | None |
| Free tier / limits | No published limits. NOMADS (not used for bulk) asks "10 second wait between fetches" ([grib filter](https://nomads.ncep.noaa.gov/info.php?page=gribfilter)) |
| Licence and attribution | "NOAA data disseminated through NODD are open to the public and can be used as desired." ([registry](https://registry.opendata.aws/noaa-gfs-bdp-pds/)); modified data must not be presented as original NOAA data (the attribution says "modified") |
| Cost | Free (egress is paid by NOAA's open-data programme) |
| Env vars | `NOAA_GFS_BASE_URL`, `NOAA_STEP_HOURS`, `NOAA_MAX_LEAD_HOURS`, `NOAA_AVAILABILITY_LAG_HOURS`, `GRIB_DOWNLOAD_WORKERS`, `GRIB_MAX_CELL_DISTANCE_KM`, `MAP_FIELDS_ENABLED`, `MAP_FIELD_ATMOS_GRID`, `MAP_FIELD_ATMOS_PATH_TEMPLATE`, `MAP_FIELD_STEPS`, `MAP_FIELD_CYCLES`, `MAP_FIELD_PARTITIONS`, `MAP_FIELD_MAX_BYTES_PER_RUN`, `MAP_FIELD_STALE_AFTER_HOURS` |
| Setup | Nothing |
| Test | `python -m app.cli engine map-fields` (map fields, about 0.16-0.48 GB per cycle depending on partitions); `pytest tests/unit/test_noaa_provider.py tests/unit/test_grib.py` |

GRIB is decoded in memory; nothing is written to disk. A server that ignores the `Range`
header is refused before its body is read.

### 2.3 NOAA GEFS-Wave ensemble statistics (forecast uncertainty)

| | |
|---|---|
| Purpose | Ensemble mean and spread of Hs and Tp for the uncertainty ranges and forecast confidence (never a forecast) |
| Variables | `GEFS_STAT_FIELDS`, default HTSGW and PERPW (4 records per step, 1.69 MB measured on 2026-10-16); WIND, SWELL, SWPER on request |
| Docs | <https://registry.opendata.aws/noaa-gefs/> |
| Account / key | None |
| Free tier / limits | No published limits (NODD, as 2.2); the app reads one run per `GEFS_CYCLES` cycle, by byte range |
| Licence and attribution | "can be used as desired" (same NODD statement as 2.2); credited as "NOAA NCEP GEFS-Wave ensemble (modified)" |
| Cost | Free; 69.4 MB per run at 41 steps (69,357,546 B measured on 2026-10-16, `docs/FORECAST_SOURCES.md`) |
| Env vars | `GEFS_ENABLED`, `NOAA_GEFS_BASE_URL`, `GEFS_STAT_FIELDS`, `GEFS_CYCLES` |
| Setup | Set `GEFS_ENABLED=true`; nothing else |
| Test | `GEFS_ENABLED=true python -m app.cli engine ensemble`; `pytest tests/unit/test_ensemble.py` |

### 2.4 ECMWF open data (optional, key-free direct fallback)

| | |
|---|---|
| Purpose | ECMWF IFS + ecWAM spot forecasts read directly (`FORECAST_PROVIDERS=ecmwf_ifs`); off by default because Open-Meteo serves the same model for kilobytes |
| Variables | `ECMWF_WAVE_PARAMS` (default `swh,mwd,pp1d,mwp`, 3.69 MB per step) and `wmb` once per run; `10u`, `10v`, `10fg`, `msl` |
| Docs | <https://www.ecmwf.int/en/forecasts/datasets/open-data> |
| Account / key | None |
| Free tier / limits | "access to the Open-Data Portal is currently limited to 500 simultaneous connections" (the app keeps 4 in flight) |
| Licence and attribution | "Their use is governed by the Creative Commons CC-BY-4.0 licence and the ECMWF Terms of Use." Attribution "Copyright ECMWF ... (modified)" with a CC BY 4.0 link wherever ECMWF-derived values are shown (also through Open-Meteo): on the spot page, the map's spot panel and best-spots list, the landing page's and dashboard's swell cards, the opportunity page and alert emails (the API's `attribution_links`) |
| Cost | Free; about 0.48 GB per run when enabled (computed) |
| Env vars | `ECMWF_BASE_URL`, `ECMWF_MAX_LEAD_HOURS`, `ECMWF_WAVE_PARAMS` |
| Setup | Add `ecmwf_ifs` to `FORECAST_PROVIDERS` (e.g. `noaa_gfs,ecmwf_ifs`); nothing else |
| Test | `FORECAST_PROVIDERS=ecmwf_ifs python -m app.cli pipeline`; `pytest tests/unit/test_ecmwf_provider.py` |

### 2.5 Copernicus Marine (MFWAM): not implemented

A possible third independent wave family. Its licence allows derivative works "for any
purpose" with a visible credit ([licence](https://marine.copernicus.eu/user-corner/service-commitments-and-licence));
it needs a free account. If an Open-Meteo model backed by MFWAM (`meteofrance_wave`) is ever
configured, the spot API adds the Copernicus Marine credit link automatically.

### 2.6 NOAA NDBC, CDIP, AODN, Met Office (buoy observations)

| | |
|---|---|
| Purpose | Instrument measurements near spots (shown as measured, with station, distance and time) and forecast verification |
| Variables | Significant wave height, peak and mean period, wave direction, wind (where the station measures it) |
| Docs | NDBC <https://www.ndbc.noaa.gov/faq/measdes.shtml> ("Both Realtime and Historical files show times in UTC only."); CDIP buoys are read through NDBC; AODN and Met Office: `docs/observations.md` |
| Account / key | None |
| Free tier / limits | No rate limit is documented here (not checked); the app reads each station's file once an hour (`observations.ingest`) |
| Licence | NDBC: US Government, public domain. CDIP: "These data may be redistributed and used without restriction." (acknowledgement requested). AODN: CC BY 4.0. Met Office marine observations: CC BY-SA 4.0 (share-alike: derived data keep the licence). Stored per station in `buoy_stations`; the spot page names the provider in words, links the licence and shows the station's acknowledgement (e.g. IMOS's) as a tooltip |
| Cost | Free; about 15 MB/day with NDBC's 5-day files (computed) |
| Env vars | `OBSERVATIONS_ENABLED`, `OBSERVATION_PROVIDERS`, `NDBC_USE_5DAY`, `OBSERVATION_RETENTION_DAYS`, `VERIFICATION_ENABLED` |
| Setup | Set `OBSERVATIONS_ENABLED=true` (stations are seeded by `init-db`); `VERIFICATION_ENABLED=true` adds the verification jobs |
| Test | `OBSERVATIONS_ENABLED=true python -m app.cli obs ingest`; `pytest tests/unit/test_observation_parsers.py tests/unit/test_ndbc_real_files.py` |

### 2.7 NOAA CO-OPS tide predictions (US spots)

| | |
|---|---|
| Purpose | Tide curves and high/low times for the US spots, datum labelled (MLLW or MSL) |
| Variables | Predicted water level (`product=predictions`) at 30-minute spacing for harmonic stations, high/low waters (`interval=hilo`) for subordinate stations, and the station datums |
| Docs | <https://api.tidesandcurrents.noaa.gov/api/prod/>, <https://tidesandcurrents.noaa.gov/disclaimers.html> |
| Account / key | None. The `application` parameter identifies the caller: "Not including the parameter will make identifying issues through automated logs impossible." Set `NOAA_COOPS_APPLICATION` (falls back to the app name setting); do not put a personal email address in it |
| Limits | "Subordinate tide prediction stations can only provide tide predictions on a high / low interval." No published rate limit |
| Licence | Public domain; "NOS requests that attribution be given whenever NOS material is reproduced and re-disseminated." |
| Cost | Free; a few small requests per station per day |
| Env vars | `TIDE_PROVIDERS`, `NOAA_COOPS_BASE_URL`, `NOAA_COOPS_APPLICATION`, `TIDE_HORIZON_DAYS`, `TIDE_RETENTION_PAST_DAYS` |
| Setup | Set `NOAA_COOPS_APPLICATION`; `init-db` seeds the station catalog; the daily `tides.acquire` job (02:20 UTC) does the rest |
| Test | `python -m app.cli tides acquire` (live, 16 requests and about 140 kB for the 6 stations, measured on 2026-10-16), then `GET /api/spots/pipeline/tides` (status `available`, datum MLLW, station Haleiwa); mock tests: `pytest tests/unit/test_coops.py tests/unit/test_tide_catalog.py tests/unit/test_tide_stage.py` |

### 2.8 EOT20 harmonic tide constants (non-US spots)

| | |
|---|---|
| Purpose | Tides for the spots without a CO-OPS station, computed locally from committed harmonic constants (no network access at runtime). Labelled "model tide, relative to model mean sea level" |
| Variables | Amplitude and Greenwich phase of 17 tidal constituents at the EOT20 cell nearest each break (`data/tides/constants_eot20.json`) |
| Docs | <https://www.seanoe.org/data/00683/79489/>: "The final EOT20 version is provided open access and should be used for all applications." |
| Account / key | None |
| Free tier / limits | Not applicable: nothing is fetched at runtime (the one-off extraction reads the SEANOE archive by byte range) |
| Licence | CC BY 4.0 (the SEANOE record's licence, <https://creativecommons.org/licenses/by/4.0/>). Wherever the curve is shown the tide note credits "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE", states the changes (constants of the nearest cell extracted, amplitudes converted from cm to m, heights computed by this app) and links the dataset DOI and the licence (the tide block's `attribution` and `attribution_links`; the constants file records `licence_url`, `source_url` and `changes`) |
| Cost | Free; no runtime downloads |
| Env vars | `TIDE_MODEL`, `TIDE_CONSTANTS_PATH` |
| Setup | Nothing: the constants file is committed. `scripts/extract_tide_constants.py` re-extracts it |
| Test | `python -m app.cli tides acquire` computes the model tides locally, then `GET /api/spots/uluwatu/tides` (status `available`, datum `model_msl`, station "EOT20 model point near Uluwatu"); `pytest tests/unit/test_tide_harmonic.py tests/unit/test_tide_catalog.py` |

FES2022 (`TIDE_MODEL=fes2022`) needs a free AVISO+ account for the one-off extraction of the
constants; the credentials are used only by that script, never by the running app.

### 2.9 Windy Point Forecast API (gated, not recommended)

| | |
|---|---|
| Purpose | Alternative GFS-based point forecasts (`FORECAST_PROVIDERS=windy`) |
| Variables | Waves, swell and wind sea (height, period, direction; e.g. the wind-sea keys `wwaves_height-surface`, `wwaves_period-surface`, `wwaves_direction-surface`), wind and gusts |
| Docs | <https://api.windy.com/point-forecast/docs> |
| Account / key | Paid key, `WINDY_API_KEY` |
| Free tier / limits | None used by this app (paid plan, see Cost) |
| Terms | "Redistribution of any part of the API or the included data to any third party is forbidden." ([terms](https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use)); the same terms forbid storing the data and building derived databases. This app stores runs, blends them and archives predictions, so the token `windy` is refused unless `WINDY_TERMS_ACKNOWLEDGED=true`, which should be set only with a written licence that allows this use |
| Cost | 990 EUR per year for the professional plan ([pricing](https://api.windy.com/point-forecast/pricing)) |
| Env vars | `WINDY_API_KEY`, `WINDY_TERMS_ACKNOWLEDGED` |
| Setup | Only with a written licence that allows storing and deriving: set `WINDY_API_KEY`, `WINDY_TERMS_ACKNOWLEDGED=true` and add `windy` to `FORECAST_PROVIDERS` |
| Test | Mock only: `pytest tests/unit/test_forecast_providers.py -k windy` |

### 2.10 Flight providers

| | Duffel | Amadeus |
|---|---|---|
| Purpose | Fares and booking links for surf trips (`FLIGHT_PROVIDER=duffel`) | Same, for Amadeus Enterprise customers (`FLIGHT_PROVIDER=amadeus`) |
| Docs | <https://duffel.com/docs/api/overview/test-mode> | <https://developers.amadeus.com/> |
| Account / key | Self-serve account. Test tokens: "Testing tokens are easy to recognize: they start with duffel_test_" and test mode includes the sandbox airline "Duffel Airways" | Enterprise agreement only: Amadeus Self-Service was shut down on 2026-07-17 (`docs/ARCHITECTURE.md`; not re-verified on 2026-10-14, the page renders only with JavaScript) |
| Free tier / limits | Test mode with `duffel_test_` tokens (sandbox airline); the app's own budget is `FLIGHT_DAILY_QUOTA` and `FLIGHT_MAX_QUERIES_PER_MATCH` | None (contract) |
| Licence / terms | The provider's terms (not reviewed in this document); fares are shown with booking links, never resold | Contract |
| Cost | "$3.00 per order" plus "1% total order value" for managed content; searches beyond a 1500:1 search-to-book ratio cost "$0.005" each ([pricing](https://duffel.com/pricing)) | Contract |
| Env vars | `FLIGHT_PROVIDER`, `DUFFEL_ACCESS_TOKEN`, `DUFFEL_BASE_URL`, `FLIGHT_DAILY_QUOTA`, `FLIGHT_MAX_QUERIES_PER_MATCH` | `FLIGHT_PROVIDER`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `AMADEUS_BASE_URL` |
| Setup | app.duffel.com → Developers → Access tokens; set `FLIGHT_PROVIDER=duffel` and `DUFFEL_ACCESS_TOKEN`, then `python -m app.cli check-config` | Credentials from the Amadeus account manager; set the three `AMADEUS_*` variables and `FLIGHT_PROVIDER=amadeus` |
| Test | Mock tests: `pytest tests/unit/test_flight_providers.py`; with a `duffel_test_` token: `FLIGHT_PROVIDER=duffel python -m app.cli pipeline` | Mock tests only |

Without a key, `FLIGHT_PROVIDER=demo` produces mock offers that are always labelled as such.

### 2.11 Email providers

| | Resend | SendGrid (Twilio) | SMTP |
|---|---|---|---|
| Purpose | Verification, password reset, swell alerts, admin data-health alerts | same | same (any SMTP service, or Mailpit locally) |
| Docs | <https://resend.com/docs> | <https://www.twilio.com/docs/sendgrid> | your provider |
| Account / key | Account, verified sending domain, `RESEND_API_KEY` | Account, verified sender, `SENDGRID_API_KEY` | Host and credentials |
| Free tier | "$0 / mo 3,000 emails / mo 100 emails a day" ([pricing](https://resend.com/pricing)) | "Sign up for a free trial—no credit card required." ([pricing](https://sendgrid.com/en-us/pricing)); plan prices not readable: confirm at signup | depends |
| Cost | Pro "$20 / mo 50,000 emails / mo" | confirm at signup | depends |
| Licence / terms | The provider's acceptable-use terms (not reviewed here); a verified sending domain with SPF, DKIM and DMARC | same | your provider's |
| Env vars | `EMAIL_PROVIDER`, `EMAIL_FROM`, `RESEND_API_KEY`, `RESEND_WEBHOOK_SECRET` | `EMAIL_PROVIDER`, `EMAIL_FROM`, `SENDGRID_API_KEY`, `SENDGRID_WEBHOOK_PUBLIC_KEY` | `EMAIL_PROVIDER`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_USE_SSL` |
| Setup | Verify the sending domain and add the DNS records (README, "Email delivery: sender and DNS verification"); set `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `EMAIL_FROM`; optional delivery webhook with `RESEND_WEBHOOK_SECRET` | Verify a sender, set `EMAIL_PROVIDER=sendgrid`, `SENDGRID_API_KEY`, `EMAIL_FROM`; optional event webhook with `SENDGRID_WEBHOOK_PUBLIC_KEY` | Set the `SMTP_*` variables and `EMAIL_FROM` |
| Test | Mock tests: `pytest tests/unit/test_notification_providers.py`; SMTP end to end against Mailpit in Docker Compose (`EMAIL_PROVIDER=mailpit`) | | |

### 2.12 SMS (Twilio, optional)

| | |
|---|---|
| Purpose | Optional SMS swell alerts with STOP/START handling |
| Docs | <https://www.twilio.com/docs/messaging> |
| Account / key | Account, a sending number or messaging service; US numbers need A2P 10DLC registration |
| Free tier / limits | Not checked here: confirm at signup |
| Licence / terms | Twilio's terms and the carriers' messaging rules (A2P 10DLC in the US); recipients can reply STOP |
| Cost | US SMS "$0.0083" per segment for long codes, toll-free and short codes, plus carrier fees ([pricing](https://www.twilio.com/en-us/sms/pricing/us)) |
| Env vars | `SMS_PROVIDER`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `TWILIO_MESSAGING_SERVICE_SID` |
| Setup | Buy or port a number (or a messaging service), set the variables, and set the number's "A message comes in" webhook to `POST ${API_BASE_URL}/api/webhooks/twilio/sms` for STOP/START (README, "Inbound STOP/START") |
| Test | Mock tests only: `pytest tests/unit/test_notification_providers.py -k twilio` |

### 2.13 Map tiles

| | |
|---|---|
| Purpose | Tiles for the map's optional "Streets" basemap. The default basemap is a dark map drawn from Natural Earth data bundled with the app and needs no tile server |
| Variables | Not applicable (raster tiles) |
| Docs | OpenStreetMap tile usage policy <https://operations.osmfoundation.org/policies/tiles/>; CARTO basemaps <https://carto.com/basemaps/>; `docs/FORECAST_SOURCES.md` ("Map tiles") |
| Account / key | None for OpenStreetMap; a free CARTO key (`NEXT_PUBLIC_CARTO_API_KEY`) for CARTO Voyager; any XYZ provider's own key in its URL |
| Free tier / limits | OpenStreetMap's standard tiles are for light use under its policy; heavier traffic needs a provider |
| Licence / attribution | OpenStreetMap data are ODbL ("© OpenStreetMap contributors", shown in the map's attribution strip); CARTO and other providers add their own attribution (`NEXT_PUBLIC_MAP_ATTRIBUTION`) |
| Cost | Free for light use; providers' plans otherwise |
| Env vars | `NEXT_PUBLIC_CARTO_API_KEY`, `NEXT_PUBLIC_MAP_TILE_URL`, `NEXT_PUBLIC_MAP_ATTRIBUTION`, `MAP_TILE_HOSTS` |
| Setup | Build-time variables: set them, then rebuild the frontend (`docker compose build frontend`); `MAP_TILE_HOSTS` adds a tile host to the CSP |
| Test | Open `/map`, choose the Streets basemap in the map's settings and check that tiles load and the attribution names the provider; `npx vitest run tests/map-tiles.test.ts` |

## 3. Not used

Surfline, Magicseaweed and Windy's website are not integrated and are never scraped.
Stormglass and Meteomatics were reviewed and not recommended (non-commercial free tier and
contract pricing respectively; `docs/forecast_upgrade_audit.md`, section 4.3).

## 4. What was tested here, and how

* **Live, no account (2026-10-14):** Open-Meteo metadata for all six model domains; the
  Marine API for all 50 spots (GFS family); NOAA NODD byte ranges for the same-cycle
  comparison; every page quoted above. The full two-family acquisition is reported in
  `docs/FORECAST_SOURCES.md`.
* **Live, no account (2026-10-16):** every acquisition job of the recommended setup on a
  fresh database: both Open-Meteo families (329 kB and 172 kB), GEFS-Wave (69.4 MB), the map
  layers with partitions (7.82-7.98 MB per map hour), CO-OPS and EOT20 tides (140 kB) and
  NDBC (121 kB); `docs/FORECAST_SOURCES.md`, "Measured on 2026-10-16". Later that day the
  README's step-by-step commands were run again: tides (140,624 B), GEFS-Wave (69.4 MB) and
  the map fields (71.3 MB for 9 map hours) succeeded, and Open-Meteo refused the sandbox's
  shared address with its daily-limit message (the run failed with that message, as
  designed). The Open-Meteo terms, pricing and licence pages and the Natural Earth terms were
  re-read the same day.
* **Mock only (no credentials here):** the Open-Meteo commercial hosts, Windy, Duffel,
  Amadeus, Resend, SendGrid and Twilio. They are not claimed to work against the live
  services until the owner supplies keys.
