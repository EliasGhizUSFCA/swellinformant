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
marked "confirm at checkout". The hosting services of Upgrade 4 (Vercel, Neon, Upstash,
section 1.4) were read on **2026-10-18** and partly re-read on **2026-10-22**; every quote
about them is listed with its URL and date in `docs/deploy-vercel.md`, section 11.

Upgrade 4 (2026-10) added the hosted setup: Vercel for the site, the API and the sync
daemon, Neon Postgres and Upstash Redis from the Vercel Marketplace. What each costs now and
after a paywall is in 1.4; every new setting is in section 5.

## 1. Summary for the owner

### 1.1 Works with no account and no key

Nothing to sign up for. The recommended live setup (section "Recommended live
configuration" below) uses only these.

| Area | Service | Cost | Licence, and what the app shows | Limits and notes |
|---|---|---|---|---|
| Spot forecasts | Open-Meteo free tier: NOAA GFS + GFS-Wave (`open_meteo`), chosen by `WEATHER_PROVIDER=auto` until Windy is licensed | Free | Data CC BY 4.0; a credit and licence link is printed next to every value derived from it (2.1) | **Non-commercial use only** (1.3). Per IP address: 600 calls a minute, 5,000 an hour, 10,000 a day, 300,000 a month. Measured (Swell 2.0, live, 2026-10-28/29): 125 weighted calls for a refresh of both components, 75 for a wave-only refresh; a check with nothing new costs none. At 4 GFS cycles a day that is about 500 weighted calls a day (5 %; arithmetic on the measured calls) |
| Forecast uncertainty (optional) | NOAA GEFS-Wave ensemble statistics | Free | NOAA open data, "can be used as desired"; credited "(modified)" (2.3) | 69.4 MB per run, 4 runs a day; off in the recommended configuration and the hosted profile (`GEFS_ENABLED=false`) |
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
| 7 | Size the database alert from the disk (no account) | The data-health database-size alert | None | — | `DATA_HEALTH_DATABASE_MAX_GB` (default 20, above the computed two-year steady state of about 17 GB with the default retention; lower it for a small disk, docs/operations.md; 0.85 on Neon Free) |
| 8 | **Hosting on Vercel** (optional; the classic Docker Compose stack needs none of it) | The public website | A Vercel account (Hobby), and from the Vercel Marketplace a Neon Postgres and an Upstash Redis database | $0 on the free plans now; the paid plans in 1.4 once a paywall arrives | The hosted profile (section 5): `JOB_RUNNER`, `MIGRATE_ON_BUILD`, `CLIENT_IP_HEADER`, `CRON_SECRET` and the rest; `DATABASE_URL` and `DATABASE_URL_UNPOOLED` are set by the Marketplace, and `KV_URL` should be (if only the REST variables KV_REST_API_URL and KV_REST_API_TOKEN appear, copy the database's `rediss://` URL into `REDIS_URL`, `docs/deploy-vercel.md` step 4) |
| 9 | A domain you own, for email from the hosted site | Account emails to other people from `*.vercel.app` | A registrar account (cost not researched here) and the email provider's DNS records (row 2) | registrar's price | `EMAIL_FROM` on that domain, `APP_BASE_URL` |
| 10 | **A scheduler for the forecast refresh** (hosted setup) | Spot forecasts that follow every new model run (spec section 9); without one the hosted site refreshes once a day | On Vercel Hobby: an Upstash QStash schedule (the Upstash account of row 8, its QStash token; `docs/deploy-vercel.md` step 7b). On Vercel Pro: a second Vercel Cron entry instead, no account | QStash Free: 1,000 messages a day; a 5-minute schedule uses 288 (https://upstash.com/pricing/qstash) | `CRON_SECRET` (the schedule forwards it as the bearer token); the QStash token stays on your machine, never in the app or the repository |

Not needed to start: **Windy** (2.9, step by step in `docs/WINDY_API_SETUP.md`). It is the
preferred provider once it is licensed for this use, and Open-Meteo serves until then and
stays the fallback. Windy's standard terms forbid storing the data and building derived
databases, which is what this app does, so the provider is used only with
`WINDY_STORAGE_AUTHORIZED=true` (old name WINDY_TERMS_ACKNOWLEDGED, still read), set once a
written licence allows this use; the professional plan costs 990 EUR a year. Copernicus
Marine (2.5) is not implemented.

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
50M+ calls/month." [pricing](https://open-meteo.com/en/pricing); the app's about 15,000
weighted calls a month, about 500 a day at 4 GFS cycles, fit the Standard plan). The data licence (CC BY 4.0 with a link next to
the data) applies on every plan and the app already meets it. The only change is
`OPEN_METEO_API_KEY` (the adapter then uses the `customer-api` and `customer-marine-api`
hosts; mock-tested, not tried with a real key). Whether a given business model is commercial
is the owner's call, not a technical one.

Check what is missing for the configured providers:

```bash
cd backend && python -m app.cli check-config
```

It prints `{"missing": [...]}` and exits with 1 if anything is missing.

### 1.4 Hosting on Vercel: what it costs now and after a paywall

The hosted setup (`docs/deploy-vercel.md`; test it first on your machine,
`docs/local-vercel-stack.md`). "What this app uses" comes from measurements on this
development machine (live Open-Meteo refreshes, demo-data refreshes through the refresh
endpoint, the local Vercel-style stack) and from arithmetic on them, as cited; nothing here
was measured on a real Vercel deployment yet. Swell 2.0 (2026-10-29) recomputed this table:
the spot forecasts refresh in the API function for every new GFS and GFS-Wave run, called
every 5 minutes by a scheduler; the daemon is optional and refreshes only the map layers.

| Service | Now: free | What this app uses of it | Later: paywall |
|---|---|---|---|
| **Vercel** (the website, the API as one Python function with the forecast refresh, the daily cron, the optional map-layer daemon) | **Hobby**, $0: "restricted to non-commercial personal use only". Daemons: "1 GiB or 2 GiB (1 vCPU) \| 5 Active CPU-hours per month" (https://vercel.com/docs/daemons/limits, re-read 2026-10-27); "When a Hobby team reaches the quota, Vercel stops the team's daemons until the first of the next month (UTC) or until the team upgrades to Pro." Cron: "Hobby accounts are limited to daily cron jobs." Functions: 4 Active CPU-hours a month. Runtime logs are kept 1 hour. On Hobby Vercel "may use Your Content to train our artificial intelligence" | Function CPU: a spot refresh measured 8.5-11.3 CPU seconds on this machine (demo and live data, `docs/sync.md` "Measured (2.0)"); at about 8 component refreshes a day that is about 0.6-0.8 CPU-hours a month (arithmetic, not a Vercel measurement), and a check with nothing new under 0.02 CPU seconds. The daemon is optional and refreshes only the map layers; its CPU on Vercel is not measured. The API function measured 128.2 MiB in the offline Vercel build (limit 500 MiB), `maxDuration` 300 s | **Pro** before the site asks for any payment: "$20/month Pro platform fee", "Every Pro plan has $20 in monthly credit." Daemons on Pro are "billed for Active CPU and Provisioned Memory" (rate not read; check the Usage page after a week) |
| **Neon Postgres** (Vercel Marketplace) | **Free**: "100 CU-hours/project, 1 GB Postgres storage per project ... and 5 GB of egress per project." Out of CU-hours or egress, the compute is suspended until the next billing period; over 1 GB, writes fail | With the free-database profile (section 5). **Egress**: one spot refresh sends 6.2-9.5 MB from the database to the app (measured on demo data through the refresh endpoint, 2026-10-27 and 29: `docs/sync.md` "Measured (2.0)", `scripts/measure_refresh_egress.py`), almost all of it the scoring and swell detection reading the new run back; a check with nothing new reads nothing. At 4 GFS cycles a day with the wave and the atmosphere refreshed separately (8 refreshes) that is about 1.5-2.3 GB a month (30-46 %, arithmetic), up to about 3.4 GB when the late GFS 0.25 domain makes a third refresh per cycle. Visitors add to it: a quiet site (100 views a day) added about 0.40 GB a month in the Upgrade 4 measurement (`docs/map-layers.md`, not re-measured for 2.0); steady traffic (1,000 views a day) may not fit. **Compute**: every refresh wakes the database; at 8-12 wakes a day (refreshes and the daily cron) and 0.25 CU until 5 idle minutes after each, about 5-8 CU-hours a month (arithmetic on those assumptions). The hourly data-health check that emails the admins (`DATA_HEALTH_CHECK_MINUTES=60`; run by the refresh endpoint without the daemon, by the daemon with it) wakes it up to 24 times a day more: up to about 15 CU-hours a month (arithmetic, 0.25 CU for about 5 minutes per wake; less where a check falls next to a refresh), so about 20-23 in all, inside the 100; the optional daemon's other hourly periodic jobs fall into the same wakes (Upgrade 4 measured about 15 for them); signed-in views are extra (`/api/auth/me` 5 statements, a dashboard about 27, measured) | **Launch**: "$0.106 per CU-hr $0.35 per GB-month", 500 GB of egress included; then the full-retention profile (section 5) |
| **Upstash Redis** (Vercel Marketplace) | **Free**: "Free tier includes 250MB data size and 500K commands per month." | Swell 2.0, without the daemon: **refresh checks** about 60,500 a month (a 5-minute schedule is 8,640 checks a month × 7 commands: 3 measured on a live check plus the stand-in's 4, one for pending kicks, one for a due email retry and two for the hourly data-health gate, the daemon heartbeat and the last check time, counted in the code); a refresh adds about 41 (measured). **Version polling** (`GET /api/forecasts/version`): the CDN shares the answer for `FORECAST_VERSION_CDN_SECONDS` (15), so the origin sees at most one request per 15 s per CDN region while any tab is open, each at most one Redis GET (a 5-second memo per process, measured): up to about 172,800 a month per region with a tab open all month, far less for a quiet site (3 hours of open tabs a day: about 21,600). **Public pages**: about 32,000 a month with a quiet site and about 307,000 with steady visitors plus at most 17,000 for the per-visitor miss limit (the Upgrade 4 estimate, `docs/map-layers.md`, not re-measured for 2.0). In all: about 114,000 a month (23 %) for a quiet site; with steady visitors and one region polled all month about 557,000, **more than Free's 500K**. The optional daemon adds about 76,000 (kick polls, checks, hourly jobs; Upgrade 4 figure). All arithmetic on the measured unit costs; not measured on Upstash | pay-as-you-go "$0.2 per 100K" commands with a budget cap (557,000 commands cost about $1.11 a month), or a Fixed plan from $10 a month. `FORECAST_VERSION_CDN_SECONDS=30` halves the version polling at the cost of up to 15 s more before an open page sees a new version (the target is 60 s) |
| **Upstash QStash** (the refresh scheduler on Hobby, `docs/deploy-vercel.md` step 7b) | **Free**: 1,000 messages a day, 10 active schedules (https://upstash.com/pricing/qstash) | 288 messages a day for a 5-minute schedule (29 %), plus retries of a failed delivery | pay-as-you-go; on Vercel Pro a Vercel Cron entry replaces it |
| **Resend** (email) and a domain | "$0 / mo 3,000 emails / mo 100 emails a day" (2.11), with a sending domain you control (SPF, DKIM, DMARC) | Verification, password reset, swell alerts, admin alerts. Before you have a domain: `EMAIL_FROM=Swell Travel Agent <onboarding@resend.dev>`, which Resend delivers only to the address of your Resend account (register, verify and `make-admin` with it; `OPS_ALERT_EMAILS` too). Sign-up stays open: visitors can create accounts, but their verification, password-reset and alert emails reach nobody until the domain is verified, so do not publicise the site yet | Pro "$20 / mo 50,000 emails / mo" above 3,000 emails a month |
| **Open-Meteo** (spot forecasts) | free API, non-commercial only (1.3) | 125 weighted calls per refresh of both components and 75 per wave-only refresh (measured live, 2026-10-28 and 29; the atmosphere alone is the difference, about 50); about 500 a day at 4 GFS cycles (5 % of the 10,000; arithmetic), about 15,000 a month. Checks for new runs read only the free `meta.json` files (no weighted calls) | `COMMERCIAL_USE=true` plus `OPEN_METEO_API_KEY` (Standard plan: 1M calls a month; **confirm the price at checkout**) |
| **GitHub** (CI, and the manual emergency `data-sync.yml` workflow) | "GitHub Actions usage is free for self-hosted runners and for public repositories that use standard GitHub-hosted runners." The repository stays public | CI on every push and pull request; `data-sync.yml` only when run by hand (no schedule, `docs/deploy-vercel.md` section 8) | Actions minutes of a private repository were not researched |

The paywall itself (a payment provider, plans, access control for paying accounts) is not
built; for the hosting it is a configuration change only (`docs/deploy-vercel.md`, sections
2 and 4, and `docs/UPGRADE_REPORT.md`, "Owner checklist").

### Recommended live configuration

```env
WEATHER_PROVIDER=auto
FORECAST_PROVIDERS=
FORECAST_BLEND_ENABLED=false
GEFS_ENABLED=false
FORECAST_DAYS=10
MAP_FIELDS_ENABLED=
MAP_FIELD_STEPS=0-120:3,120-240:6
MAP_FIELD_CYCLES=00,12
MAP_FIELD_PARTITIONS=on
TIDE_PROVIDERS=noaa_coops,harmonic_model
NOAA_COOPS_APPLICATION=SwellTravelAgent
OPEN_METEO_API_KEY=
```

Swell 2.0 (design D5, spec section 2: "do not repeatedly cross-reference numerous weather
providers"): one provider. `WEATHER_PROVIDER=auto` uses Windy once it is licensed
(`WINDY_API_KEY` set and `WINDY_STORAGE_AUTHORIZED=true`, `docs/WINDY_API_SETUP.md`) and
Open-Meteo's NOAA GFS + GFS-Wave until then; Open-Meteo stays the fallback. With one family
the wave and the wind refresh independently, each as soon as its own model run is servable,
and the other component is carried forward with its original model time.
`FORECAST_PROVIDERS` stays empty (it is derived from `WEATHER_PROVIDER`). An empty
`MAP_FIELDS_ENABLED` means automatic (standalone map layers with a live provider);
`NOAA_COOPS_APPLICATION` takes an application name, for example "SwellTravelAgent
(+https://your.site)"; `OPEN_METEO_API_KEY` stays empty unless the app is commercial (1.3).
The Vercel setup uses the hosted profile of section 5, which sets the same provider choice.

**Advanced, off by default: the two-family blend and the GEFS ensemble.** Setting
`FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf` with `FORECAST_BLEND_ENABLED=true` blends
Open-Meteo's GFS and ECMWF families, and `GEFS_ENABLED=true` adds the GEFS-Wave ensemble
spread (Hs and Tp only; 69.4 MB per run). The blend replaces the per-component refreshes
with family-level syncs (no wave-only or wind-only refresh, no carry-forward: the Upgrade 4
behaviour, design D5) and adds the ECMWF family's calls (124.3 weighted calls per run,
measured 2026-10-14). It is not recommended for Swell 2.0.

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
  22:04 UTC. On 2026-10-23 it refused this sandbox five times between 18:36 and 19:01 UTC
  and accepted at 18:53 and 19:02 (the proxy probably leaves through several addresses).
  On shared hosting a paid key avoids depending on other users of the address. The run
  then ends FAILED with that message and the previous run stays in use. In the synced
  pipeline (docs/sync.md) that source then waits until 00:00 UTC (the first of the next
  month for a monthly limit) instead of spending its SYNC_MAX_ATTEMPTS retries; that the
  free tier resets at 00:00 UTC is our assumption, Open-Meteo does not say so.
* **On Vercel** the daemon's outbound IP address may be shared with other Vercel projects,
  whose calls then count against the same free daily limit. `OPEN_METEO_API_KEY` (a paid
  plan, counted per key) removes that risk; until then an occasional refusal delays a sync
  to the next UTC day for that source while the other sources sync normally.
* Open-Meteo's servers can disagree for a few minutes about whether a run is published
  (seen live on 2026-10-21): the sync records that component `deferred`, counts no failed
  attempt and takes the run at a later check (docs/FORECAST_SOURCES.md, "Publication
  timing").
* `COMMERCIAL_USE=true` without `OPEN_METEO_API_KEY`: both Open-Meteo sources refuse to run
  before sending any request, `/status` reports the `licence` problem (failing) and
  `python -m app.cli check-config` lists the missing key.

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

### 2.9 Windy Point Forecast API (preferred once licensed; gated)

| | |
|---|---|
| Purpose | The preferred spot forecasts once licensed: Windy's `gfs` (wind, gust, pressure) and `gfsWave` (waves, swell, wind sea), one request of each per spot; Open-Meteo stands in while Windy is not eligible and when a Windy refresh fails (`docs/WINDY_API_SETUP.md`) |
| Variables | Waves, swell and wind sea (height, period, direction; e.g. the wind-sea keys `wwaves_height-surface`, `wwaves_period-surface`, `wwaves_direction-surface`), wind and gusts |
| Docs | <https://api.windy.com/point-forecast/docs> |
| Account / key | Paid key, `WINDY_API_KEY` |
| Free tier / limits | None used by this app (paid plan, see Cost) |
| Terms | "Redistribution of any part of the API or the included data to any third party is forbidden." ([terms](https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use)); the same terms forbid storing the data and building derived databases. This app stores runs, blends them and archives predictions, so Windy is refused unless `WINDY_STORAGE_AUTHORIZED=true` (old name WINDY_TERMS_ACKNOWLEDGED, still read), which should be set only with a written licence that allows this use |
| Cost | 990 EUR per year for the professional plan ([pricing](https://api.windy.com/point-forecast/pricing)) |
| Env vars | `WINDY_API_KEY`, `WINDY_STORAGE_AUTHORIZED`, `WEATHER_PROVIDER`, `WINDY_CHECK_MINUTES`, `WINDY_DAILY_REQUEST_LIMIT` |
| Setup | Only with a written licence that allows storing and deriving: set `WINDY_API_KEY` and `WINDY_STORAGE_AUTHORIZED=true`; `WEATHER_PROVIDER=auto` (the hosted default) then uses Windy, or set `WEATHER_PROVIDER=windy` |
| Test | Health check (stores nothing): `python -m app.cli forecast provider-check --provider windy`; mocks: `pytest tests/unit/test_windy_provider.py tests/unit/test_forecast_providers.py -k windy` |

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

* **Upgrade 4, 2026-10-23 (integration):** the local Vercel-style stack with the hosted
  profile, real NOAA NODD map layers (135 fields, 73.3 MB downloaded) and, after several
  daily-limit refusals of this sandbox's shared address, real Open-Meteo GFS point forecasts
  for the 50 spots (10,264 records); the ECMWF family was "not served yet" (Open-Meteo was
  serving its 06Z run). The cron token (200 with it, 401 wrong or missing, 404 without
  `CRON_SECRET`), the client-IP header and the PgBouncer path were checked against that
  stack. Not checked: Vercel, Neon and Upstash themselves (no account was used), Resend
  (no key), the Open-Meteo commercial hosts (no key).

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

## 5. Hosting, runner and sync settings (Upgrade 4)

**Swell 2.0 settings** (`backend/app/core/config.py`, design B of the Swell 2.0 upgrade;
every one is also in `.env.example` and `.env.vercel.example`)

| Setting | Default | Hosted | What it does |
|---|---|---|---|
| `WEATHER_PROVIDER` | `auto` | `auto` | The weather provider: `auto` = Windy when `WINDY_API_KEY` is set and `WINDY_STORAGE_AUTHORIZED=true`, else Open-Meteo (GFS and GFS-Wave); `open_meteo`; `windy` (production refuses it until both Windy settings are in place); `demo` (synthetic data, the local default of `.env.example` and both compose stacks) |
| `FORECAST_PROVIDERS` | empty: derived from `WEATHER_PROVIDER` | empty | Advanced override, an ordered list (for example `open_meteo,open_meteo_ecmwf` with `FORECAST_BLEND_ENABLED=true`) |
| `WINDY_STORAGE_AUTHORIZED` (old name WINDY_TERMS_ACKNOWLEDGED) | `false` | `false` until a licence allows storing Windy data | A key is not a licence: Windy data are stored, scored and published only when this is true |
| `WINDY_CHECK_MINUTES`, `WINDY_DAILY_REQUEST_LIMIT` | 15, 10000 | the defaults | Windy's sentinel check interval and its requests a UTC day |
| `OPEN_METEO_COMMERCIAL` (alias of `COMMERCIAL_USE`) | `false` | `false` now, `true` with a paywall | The same setting as `COMMERCIAL_USE`; it wins when both are set. Production refuses `true` without `OPEN_METEO_API_KEY` |
| `FORECAST_DAYS` | 10 | 10 | The rolling forecast horizon (3-16 days) |
| `FORECAST_REFRESH_ENABLED` | `true` | `true` | Kill switch of every refresh runner |
| `FORECAST_STALE_THRESHOLD_MINUTES`, `FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL` | 360, empty | the defaults | A spot is stale once its data are older than the model's update interval plus this; per-model overrides as a JSON object |
| `FORECAST_MIN_VALID_FRACTION` | 0.8 | 0.8 | A spot passes validation only when this fraction of its hours carry a wave height and wind |
| `FORECAST_VERSION_CDN_SECONDS` | 15 | 15 | CDN cache of `GET /api/forecasts/version` |
| `NEXT_PUBLIC_APP_URL` (alias of `APP_BASE_URL`) | empty | the site's URL | The web build's metadataBase; the backend reads it when `APP_BASE_URL` is empty |
| `NEXT_PUBLIC_FORECAST_VERSION_POLL_MS` (frontend build) | 20000 | 20000 | How often an open page asks for a new forecast version (only while visible) |
| `SYNC_DAEMON_KINDS` | `spots,ensemble,map` | `map` | What the sync daemon refreshes itself; hosted, the spot refreshes run in a Vercel Function |
| `PREDICTION_ARCHIVE_ENABLED` | `false` | `false` (full-retention profile: `true`) | Archive every published prediction at each lead time |
| `SCORING_WEIGHTS_JSON` | empty | empty | Optional JSON object overriding the quality score's component weights |
| `FLIGHT_USER_DAILY_QUOTA` | 20 | 20 | Provider flight searches a day for one user |
| `ALERT_LEAD_DEFAULT_MIN_DAYS`, `ALERT_LEAD_DEFAULT_MAX_DAYS` | 2, 10 | the defaults | Default alert lead times of a new saved search |
| `SHORT_NOTICE_MIN_DAYS`, `SHORT_NOTICE_MAX_DAYS` | 3, 7 | the defaults | The short-notice ranking window |
| `DETECTION_LEAD_MIN_DAYS` | 1 (was 5) | 1 | The global lead-time floor of detection and matching |
| `QUOTA_ALERT_FRACTION` | 0.8 | 0.8 | Admin alert once an API quota reaches this fraction |
| `REFRESH_SCHEDULER_EXPECTED_MINUTES` | 30 | 30 | Admin alert when no refresh check ran for this long (0 = off) |

Every setting Upgrade 4 added, with its default (`backend/app/core/config.py`) and the value
of the hosted profile (`backend/app/core/profiles.py`, `profile_env("hosted",
"free-database")`, the block to paste into Vercel in `docs/deploy-vercel.md` step 5 and the
values of `.env.vercel.example`). The Settings defaults are those of the classic Docker
Compose stack, so a deployment that sets nothing behaves as before. Each one is also in
`.env.example` with a one-line comment.

**Runner, deployment and security**

| Setting | Default | Hosted | What it does |
|---|---|---|---|
| `JOB_RUNNER` | `celery` | `daemon` | Who runs the background jobs: Celery worker and beat (classic stack) or one `python -m app.cli sync daemon` process (a Vercel Daemon). Under `daemon` the API never imports Celery, sends account emails and SMS inline (3 attempts, 0.5 s and 1.5 s apart) and hands "match my search" and "refresh flights" to the daemon as Redis kick flags |
| `MIGRATE_ON_BUILD` | `off` | `production` | `python -m app.cli migrate --on-build`, the api service's build command: `off` never migrates, `production` only when Vercel's system variable VERCEL_ENV is production, `all` on every build (also Neon preview branches). A failed migration fails the build |
| `CLIENT_IP_HEADER` | empty: X-Forwarded-For with `TRUSTED_PROXY_COUNT` | `x-vercel-forwarded-for` | The request header that carries the client IP for rate limits, set by the platform and not overwritable by clients (first entry, at most 64 characters) |
| `CRON_SECRET` | empty: the watchdog route answers 404 | 16 or more random characters (yours) | Bearer token Vercel Cron sends to `GET /api/internal/cron/daily` (401 on a wrong token). Required in production under the daemon (`check-config` lists it) |
| `COMMERCIAL_USE` | `false` | `false` now, `true` with a paywall | `true` makes the Open-Meteo sources require `OPEN_METEO_API_KEY`; without it they refuse to run and health reports `licence` (failing) |
| `OPEN_METEO_API_KEY` | empty | only with a paid Open-Meteo plan | Selects Open-Meteo's commercial hosts (2.1) |
| `OPS_ALERT_EMAILS` | empty | optional, recommended | Comma-separated addresses the daily watchdog emails, at most once per UTC day, when the database itself cannot be reached (for example Neon's compute suspended after its quota): the admin accounts are read from the database, so they cannot be told then. Logs show counts only |
| `API_ROUTING` (frontend build) | empty: `platform` on Vercel (Vercel sets VERCEL=1), else `proxy` | `platform` (automatic) | `proxy`: Next.js rewrites `/api/*` to `BACKEND_URL` (classic stack); `platform`: the platform routes `/api` (Vercel Services, the local Vercel-style stack). Read when the web service is built |

**Database (Neon)**

| Setting | Default | Hosted | What it does |
|---|---|---|---|
| `DATABASE_URL` | the local compose database | Neon's pooled URL (set by the Marketplace) | The API's connection; `postgres://` and `postgresql://` are rewritten to the psycopg driver, keeping the query string (sslmode) |
| `DATABASE_URL_UNPOOLED` | empty: `DATABASE_URL` | Neon's direct URL (set by the Marketplace) | Migrations, and the whole process with `DB_USE_UNPOOLED=true` (the daemon). Used only when it names the same database as `DATABASE_URL`, otherwise ignored with a warning |
| `DB_USE_UNPOOLED` | `false` | set by the daemon image (Containerfile); do not set it for the project | This process connects through `DATABASE_URL_UNPOOLED` |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | 5, 10 | 2, 3 | SQLAlchemy pool per process |
| `DB_POOL_RECYCLE_SECONDS` | 1800 | 240 | Replace connections older than this (a pooler or a scaled-to-zero database closes idle ones) |
| `DB_PREPARED_STATEMENTS` | `true` | `false` | `false` = never prepare statements, required behind a PgBouncer in transaction mode such as Neon's pooled URL |

**Redis (Upstash)**

| Setting | Default | Hosted | What it does |
|---|---|---|---|
| `REDIS_URL`, alias `KV_URL` | `redis://localhost:6379/0` | `KV_URL` from the Upstash integration (a `rediss://` URL) | `REDIS_URL` wins when both are set. If the Marketplace sets neither, copy the database's TLS URL into `REDIS_URL` yourself (`docs/deploy-vercel.md`, step 4) |
| `REDIS_CONNECT_TIMEOUT_SECONDS`, `REDIS_SOCKET_TIMEOUT_SECONDS` | 5, 5 | 1.5, 2 | An unreachable Redis costs a request at most this long (rate limits then fail open) |

**The synced pipeline** (`docs/sync.md`)

| Setting | Default | Hosted | What it does |
|---|---|---|---|
| `SYNC_CHECK_MINUTES` | 10 | 10 | Model-run checks (provider metadata only, no database access in the daemon) |
| `SYNC_TRIGGER_SOURCES` | `spots,map` | `spots` (free-database profile; `map` until Swell 2.0) | Component kinds whose new run starts a sync. `map` = a sync only for a new map cycle, which brings the newest spot runs of both families with it; when the setup has no map component, new spot runs trigger instead |
| `MAP_FIELD_CYCLES` | `00,12` | `06,18` (free-database profile) | The GFS cycles whose map layers are read (and, with `map` triggers, when syncs run) |
| `SYNC_COALESCE_MINUTES` | 0 (90 until Swell 2.0) | 0 | How long the late model domains of one component wait for each other (within a component only) |
| `SYNC_RETRY_MINUTES`, `SYNC_MAX_ATTEMPTS` | 15, 6 | 15, 6 | Wait after a failed acquisition; failed attempts per model run before waiting for the next run |
| `SYNC_LEASE_MINUTES` | 45; 15 under `JOB_RUNNER=daemon` when not set | 15 (not set) | The one-sync-at-a-time lease, renewed after every stage and extended while a stage runs; a lapsed lease is taken over |
| `SYNC_PUBLISH_GATE` | `true` | `true` | The map serves the map run of the newest publication, so the map and the spots switch together |
| `SYNC_PRUNE_EACH_PASS` | `false` | `true` (free-database profile) | Prune superseded predictions and map fields after every sync, not only daily |
| `SYNC_PREWARM_PUBLIC_CACHE` | `false` | `false` | After a publishing sync, compute the public spot list and conditions at once (about 1.4 MB of reads per publication) instead of at the first visitor's request |
| `SYNC_DAEMON_POLL_SECONDS` | 60 | 60 | How often the daemon picks up the API's kicks |
| `SYNC_DAEMON_INIT_DB` | `true` | `true` | The daemon runs `init-db` (migrate, seed, tide catalog; idempotent) at every start |
| `SYNC_DAEMON_CATCH_UP` | `true` | `true` | At start the daemon runs, once, every periodic job without a success within its longest gap (a new deployment's tide predictions at once, not at the next 02:20 UTC); a job started within the last hour is skipped |
| `SYNC_CPU_BUDGET_HOURS` | none | 5 | Monthly CPU budget of the sync runner; health warns above 80 % (`sync_cpu_budget`) |
| `JOB_INTERVAL_FLOOR_MINUTES` | 0 | 60 | Every periodic job runs at most this often, so a scale-to-zero database is woken at most hourly between syncs |

The public snapshot cache (the anonymous spot list, map conditions, spot pages, status and
map-field index, computed at most once per publication and UTC hour and kept in Redis for
2 hours) has two settings: `SYNC_PREWARM_PUBLIC_CACHE` above, and
`PUBLIC_CACHE_MISS_LIMIT_PER_HOUR` (default 120, the hosted profile keeps it): the requests
of one client IP an hour that miss the cache and so read the database; more answer 429
(with `RATE_LIMIT_ENABLED`; cache hits are not counted).

**Forecast, retention and health values of the hosted profile** (all existed before)

| Setting | Default | Hosted | Why |
|---|---|---|---|
| `FORECAST_PROVIDERS` | empty (derived from `WEATHER_PROVIDER`) | empty (`WEATHER_PROVIDER=auto`; `open_meteo,open_meteo_ecmwf` until Swell 2.0) | point forecasts for the 50 surf spots only, one provider |
| `MAP_FIELDS_ENABLED` | empty (automatic) | `true` | the worldwide map layers, synced with the spot forecasts |
| `MAP_FIELD_STEPS`, `MAP_FIELD_MAX_LEAD_HOURS` | `0-120:3,120-240:6`, 240 (61 map hours) | the defaults | `MAP_FIELD_STEPS=0-24:3` with `MAP_FIELD_MAX_LEAD_HOURS=24` (9 map hours) for a short local test |
| `GEFS_ENABLED` | `false` | `false` | protects the daemon's 5 Active CPU-hours; `true` turns the ensemble spread on |
| `PROVIDER_RESPONSE_CACHE_ENABLED` | `true` | `false` | Upstash Free stores 250 MB |
| `RETENTION_FORECAST_DAYS`, `RETENTION_ARCHIVE_DAYS`, `RETENTION_JOB_LOG_DAYS` | 10, 730, 30 | 1, 7, 7 (free-database profile) | Neon Free's 1 GB; the full-retention profile restores 10, 730, 30 |
| `DATA_HEALTH_CHECK_MINUTES` | 15 | 60 | at most one database wake an hour for the check |
| `DATA_HEALTH_DATABASE_MAX_GB` | 20 | 0.85 (free-database profile) | warn before Neon Free's 1 GB fills |
| `DATA_HEALTH_HEARTBEAT_MINUTES` | 30 | 30 | under the daemon its heartbeat is stale after max(3 × `SYNC_CHECK_MINUTES`, this) |
| `DATA_HEALTH_ALERTS_ENABLED`, `DATA_HEALTH_ALERT_REPEAT_HOURS`, `DATA_HEALTH_REDIS_MAX_FRACTION`, `DATA_HEALTH_OBSERVATION_STALE_HOURS` | true, 12, 0.8, the 48 h look-back | unchanged | `docs/operations.md`, section 3 |

**Full-retention profile** (after moving Neon to Launch): `RETENTION_FORECAST_DAYS=10`,
`RETENTION_ARCHIVE_DAYS=730`, `RETENTION_JOB_LOG_DAYS=30`, `SYNC_PRUNE_EACH_PASS=false`,
`DATA_HEALTH_DATABASE_MAX_GB=20`, `SYNC_TRIGGER_SOURCES=spots,map`, `MAP_FIELD_CYCLES=00,12`,
`PREDICTION_ARCHIVE_ENABLED=true` (the full-retention profile of `backend/app/core/profiles.py`,
equal to the Settings defaults except the prediction archive, off by default since Swell 2.0).

**The local Vercel-style stack's own variables** (`scripts/vercel-local.sh` and
`docker-compose.vercel.yml`, never read by the app; `docs/local-vercel-stack.md`):

| Variable | Default | Where it is read | What it does |
|---|---|---|---|
| `VERCEL_LOCAL_ENV_FILE` | `.env.vercel.local` | your shell | the env file the stack uses |
| `VERCEL_LOCAL_COMPOSE_FILES` | none | your shell | extra compose override files, space-separated (for example a proxy and its CA for image builds) |
| `VERCEL_LOCAL_SYNC_WAIT_SECONDS` | 1200 | your shell | how long `scripts/vercel-local.sh sync` waits while another sync holds the lease (0 = exit 3 at once) |
| `VERCEL_LOCAL_DATABASE_URL`, `VERCEL_LOCAL_DATABASE_URL_UNPOOLED` | the stack's own PgBouncer and Postgres | `.env.vercel.local` | a Neon development branch's pooled and direct URLs instead |
| `VERCEL_LOCAL_REDIS_URL` | the stack's own Redis | `.env.vercel.local` | an Upstash database instead |

The stack's host ports (edge 3100, Postgres 55432, PgBouncer 56432, Redis 56379, Mailpit
18025) are set in `.env.vercel.local` too; `docs/local-vercel-stack.md` names them.
