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

Swell 2.0 (2026-10-26 to 29) re-read the pages that decide the setup on **2026-10-27**:
Windy's Point Forecast documentation, pricing and terms (2.9; the full activation is in
`docs/WINDY_API_SETUP.md`), Open-Meteo's terms, pricing and model-update pages (2.1), Vercel's
cron, function and daemon limits, Upstash QStash and cron-job.org (2.15), and the flight
APIs (2.10). Those quotes and their URLs are kept in the design research and repeated here
where they decide something. Section 6 lists **every** setting with its default.

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
| 3 | **Real flight prices** | Opportunities with bookable fares | Duffel (self-serve, live mode after its KYC review, and **Duffel's written confirmation** that this use is not the "metasearch" its agreement forbids, 2.10) or an Amadeus Enterprise agreement | Duffel: "$3.00 per order" plus "1% total order value" for managed content, searches beyond a 1500:1 search-to-book ratio "$0.005" each; Amadeus: contract (2.10) | `FLIGHT_PROVIDER`, `DUFFEL_ACCESS_TOKEN` or `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` |
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
50M+ calls/month." [pricing](https://open-meteo.com/en/pricing); the app's about
15,000-19,500 weighted calls a month, about 500-650 a day at 4 GFS cycles with the horizon
refetch, fit the Standard plan). The data licence (CC BY 4.0 with a link next to
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
| **Vercel** (the website, the API as one Python function with the forecast refresh, the daily cron, the optional map-layer daemon) | **Hobby**, $0: "restricted to non-commercial personal use only". Daemons: "1 GiB or 2 GiB (1 vCPU) \| 5 Active CPU-hours per month" (https://vercel.com/docs/daemons/limits, re-read 2026-10-27); "When a Hobby team reaches the quota, Vercel stops the team's daemons until the first of the next month (UTC) or until the team upgrades to Pro." Cron: "Hobby accounts are limited to daily cron jobs." Functions: 4 Active CPU-hours a month. Runtime logs are kept 1 hour. On Hobby Vercel "may use Your Content to train our artificial intelligence" | Function CPU: a spot refresh measured 8.5-11.8 CPU seconds on this machine (demo and live data, `docs/sync.md` "Measured (2.0)"); at about 8 component refreshes a day that is about 0.6-0.8 CPU-hours a month (arithmetic, not a Vercel measurement), and a check with nothing new under 0.02 CPU seconds. The daemon is optional and refreshes only the map layers; its CPU on Vercel is not measured. The API function measured 128.3 MiB in the offline Vercel build (limit 500 MiB), `maxDuration` 300 s | **Pro** before the site asks for any payment: "$20/month Pro platform fee", "Every Pro plan has $20 in monthly credit." Daemons on Pro are "billed for Active CPU and Provisioned Memory" (rate not read; check the Usage page after a week) |
| **Neon Postgres** (Vercel Marketplace) | **Free**: "100 CU-hours/project, 1 GB Postgres storage per project ... and 5 GB of egress per project." Out of CU-hours or egress, the compute is suspended until the next billing period; over 1 GB, writes fail | With the free-database profile (section 5). **Egress**: one spot refresh sends 6.3-9.5 MB from the database to the app (measured on demo data through the refresh endpoint, 2026-10-27 and 29: `docs/sync.md` "Measured (2.0)", `scripts/measure_refresh_egress.py`), almost all of it the scoring and swell detection reading the new run back; a check with nothing new reads nothing. At 4 GFS cycles a day with the wave and the atmosphere refreshed separately (8 refreshes) that is about 1.5-2.3 GB a month (30-46 %, arithmetic), up to about 3.4 GB when the late GFS 0.25 domain makes a third refresh per cycle. Visitors add to it: a quiet site (100 views a day) added about 0.40 GB a month in the Upgrade 4 measurement (`docs/map-layers.md`, not re-measured for 2.0); steady traffic (1,000 views a day) may not fit. **Compute**: every refresh wakes the database; at 8-12 wakes a day (refreshes and the daily cron) and 0.25 CU until 5 idle minutes after each, about 5-8 CU-hours a month (arithmetic on those assumptions). The hourly data-health check that emails the admins (`DATA_HEALTH_CHECK_MINUTES=60`; run by the refresh endpoint without the daemon, by the daemon with it) wakes it up to 24 times a day more: up to about 15 CU-hours a month (arithmetic, 0.25 CU for about 5 minutes per wake; less where a check falls next to a refresh), so about 20-23 in all, inside the 100; the optional daemon's other hourly periodic jobs fall into the same wakes (Upgrade 4 measured about 15 for them); signed-in views are extra (`/api/auth/me` 5 statements, a dashboard about 27, measured) | **Launch**: "$0.106 per CU-hr $0.35 per GB-month", 500 GB of egress included; then the full-retention profile (section 5) |
| **Upstash Redis** (Vercel Marketplace) | **Free**: "Free tier includes 250MB data size and 500K commands per month." | Swell 2.0, without the daemon: **refresh checks** about 60,500 a month (a 5-minute schedule is 8,640 checks a month × 7 commands: 3 measured on a live check plus the stand-in's 4, one for pending kicks, one for a due email retry and two for the hourly data-health gate, the daemon heartbeat and the last check time, counted in the code); a refresh adds about 41 (measured). **Version polling** (`GET /api/forecasts/version`): the CDN shares the answer for `FORECAST_VERSION_CDN_SECONDS` (15), so the origin sees at most one request per 15 s per CDN region while any tab is open, each at most one Redis GET (a 5-second memo per process, measured): up to about 172,800 a month per region with a tab open all month, far less for a quiet site (3 hours of open tabs a day: about 21,600). **Public pages**: about 32,000 a month with a quiet site and about 307,000 with steady visitors plus at most 17,000 for the per-visitor miss limit (the Upgrade 4 estimate, `docs/map-layers.md`, not re-measured for 2.0). In all: about 114,000 a month (23 %) for a quiet site; with steady visitors and one region polled all month about 557,000, **more than Free's 500K**. The optional daemon adds about 76,000 (kick polls, checks, hourly jobs; Upgrade 4 figure). All arithmetic on the measured unit costs; not measured on Upstash | pay-as-you-go "$0.2 per 100K" commands with a budget cap (557,000 commands cost about $1.11 a month), or a Fixed plan from $10 a month. `FORECAST_VERSION_CDN_SECONDS=30` halves the version polling at the cost of up to 15 s more before an open page sees a new version (the target is 60 s) |
| **Upstash QStash** (the refresh scheduler on Hobby, `docs/deploy-vercel.md` step 7b) | **Free**: 1,000 messages a day, 10 active schedules (https://upstash.com/pricing/qstash) | 288 messages a day for a 5-minute schedule (29 %), plus retries of a failed delivery | pay-as-you-go; on Vercel Pro a Vercel Cron entry replaces it |
| **Resend** (email) and a domain | "$0 / mo 3,000 emails / mo 100 emails a day" (2.11), with a sending domain you control (SPF, DKIM, DMARC) | Verification, password reset, swell alerts, admin alerts. Before you have a domain: `EMAIL_FROM=Swell Travel Agent <onboarding@resend.dev>`, which Resend delivers only to the address of your Resend account (register, verify and `make-admin` with it; `OPS_ALERT_EMAILS` too). Sign-up stays open: visitors can create accounts, but their verification, password-reset and alert emails reach nobody until the domain is verified, so do not publicise the site yet | Pro "$20 / mo 50,000 emails / mo" above 3,000 emails a month |
| **Open-Meteo** (spot forecasts) | free API, non-commercial only (1.3) | 125 weighted calls per refresh of both components and 75 per wave-only refresh (measured live, 2026-10-28 and 29; the atmosphere alone is the difference, about 50); about 500 a day at 4 GFS cycles, plus the horizon refetch of the other component (its same run, so the 10-day horizon keeps rolling) on up to two refreshes a day, 50 or 75 more each: about 500-650 a day (5-6.5 % of the 10,000; arithmetic), about 15,000-19,500 a month. Checks for new runs read only the free `meta.json` files (no weighted calls) | `COMMERCIAL_USE=true` plus `OPEN_METEO_API_KEY` (Standard plan: 1M calls a month; **confirm the price at checkout**) |
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
| Purpose | Point forecasts for the 50 spots: NOAA GFS + GFS-Wave by default (`WEATHER_PROVIDER=auto` or `open_meteo`, source `open_meteo_noaa`); ECMWF IFS + ecWAM (`open_meteo_ecmwf`) only through the advanced `FORECAST_PROVIDERS` override |
| Variables | Marine: significant height, peak period, mean direction; primary, secondary and tertiary swell and wind sea (height, period, direction) for GFS-Wave; ECMWF WAM: height, direction, peak period (`wave_peak_period`) and mean period. Forecast API: 10 m wind speed and direction, gust, MSL pressure |
| Models | `ncep_gfswave016` (0.16°, about 15°S-52.5°N) or `ncep_gfswave025` elsewhere; `gfs_global`; `ecmwf_wam025`; `ecmwf_ifs025`. Only the models' own time steps are stored (GFS hourly to 120 h then 3-hourly; IFS/WAM 3-hourly to 144 h then 6-hourly) |
| Docs | <https://open-meteo.com/en/docs>, <https://open-meteo.com/en/docs/marine-weather-api>, <https://open-meteo.com/en/docs/gfs-api>, <https://open-meteo.com/en/docs/ecmwf-api>, <https://open-meteo.com/en/docs/model-updates> |
| Account / key | None on the free tier. A paid plan gives an API key: set `OPEN_METEO_API_KEY` and the adapter uses the `customer-api` and `customer-marine-api` hosts |
| Free development configuration | Nothing to sign up for: `WEATHER_PROVIDER=auto` (or `open_meteo`), `OPEN_METEO_API_KEY` empty, `OPEN_METEO_COMMERCIAL=false` (old name `COMMERCIAL_USE`). Only for non-commercial use (1.3). Local development and the tests use `WEATHER_PROVIDER=demo` instead, which calls no provider |
| Commercial subscription and customer endpoint | "A subscription grants a commercial use licence and an API key for the dedicated customer endpoint at customer-api.open-meteo.com. The API syntax is identical to the free tier — only the domain and key parameter differ." and "requests include &apikey=abc123" ([pricing](https://open-meteo.com/en/pricing), read 2026-10-27). The Marine API takes the same `apikey` with the `customer-` prefix (`customer-marine-api.open-meteo.com`, [marine docs](https://open-meteo.com/en/docs/marine-weather-api)). Setup: subscribe, set `OPEN_METEO_API_KEY` and `OPEN_METEO_COMMERCIAL=true`, raise `OPEN_METEO_CALLS_PER_DAY` / `OPEN_METEO_CALLS_PER_MONTH` to the plan, redeploy; `python -m app.cli forecast provider-check --provider open_meteo` then reads `meta.json` from the customer host. Production refuses `OPEN_METEO_COMMERCIAL=true` without a key. Not tried with a real key here (mock-tested) |
| Free tier | "You may only use the free API services for non-commercial purposes." and "Less than 10'000 API calls per day, 5'000 per hour and 600 per minute." ([terms](https://open-meteo.com/en/terms)); 300,000 per month |
| Call weighting | Requests "for data covering more than 10 weather variables or extending over a period of more than 2 weeks for a single location are considered multiple API calls" ([pricing](https://open-meteo.com/en/pricing)). The app charges `locations x max(1, variables/10) x max(1, days/14)` per request to a Redis budget before sending it (`backend/app/services/forecasts/quota.py`). Metadata calls are free: they are "not counted toward daily or monthly request limits" ([model updates](https://open-meteo.com/en/docs/model-updates)) |
| Measured use | One run for all 50 spots (live, 2026-10-14 12Z): GFS family, 16 days, 142.9 weighted calls (waves 85.7, wind 57.1) and 345.6 kB on the wire including the metadata reads; ECMWF family, 15 days, 124.3 weighted calls and 175.1 kB: the **Upgrade 3 two-family measurement**, about 820 weighted calls a day with that schedule, 8 % of the free tier. **Swell 2.0** (one family, GFS + GFS-Wave, 10 days; measured live 2026-10-28 and 29): 125 weighted calls for a refresh of both components, 75 for a wave-only refresh (the atmosphere alone is the difference, 50), about 500 a day at 4 GFS cycles, about 500-650 with the horizon refetch (section 1.4; docs/sync.md, "One refresh"). The full numbers are in `docs/FORECAST_SOURCES.md` ("API-first acquisition") |
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
* **On Vercel the free tier can run out early.** Open-Meteo's terms (read 2026-10-27, new
  since the 2026-10-15 copy) say: "Rate limits are applied by IP addresses and application.
  If you are using providers like Vercel, Netlify, Render, or Google Sheets you might be
  rate-limited earlier then expected." ([terms](https://open-meteo.com/en/terms)). The
  refresh runs in a Vercel Function whose outbound address is shared with other Vercel
  projects, whose calls then count against the same free daily limit. `OPEN_METEO_API_KEY`
  (a paid plan, counted per key) removes that risk; until then a refusal ("Daily API request
  limit exceeded") makes that component wait until 00:00 UTC while the site keeps serving
  the published version, its spots labelled stale once they age, and the admins get the
  `provider_unavailable` or `refresh_failures` alert. Open-Meteo's own suggestion there,
  Cloudflare Workers, is not used by this app.
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

The step-by-step activation (no code change) is `docs/WINDY_API_SETUP.md`. Everything below
was read on 2026-10-27 on the pages linked.

| | |
|---|---|
| Purpose | The preferred spot forecasts once licensed: Windy's `gfs` (wind, gust, pressure) and `gfsWave` (waves, swell, wind sea), one request of each per spot (Windy has no multi-location request: 100 requests for a full refresh of the 50 spots, 50 for one component); Open-Meteo stands in while Windy is not eligible and when a Windy refresh fails |
| Variables | `gfsWave`: waves, windWaves, swell1, swell2 (height, period, direction FROM; e.g. the wind-sea keys `wwaves_height-surface`, `wwaves_period-surface`, `wwaves_direction-surface`); `gfs`: wind (u/v, converted to speed and direction FROM), windGust, pressure. Steps are 3-hourly ("API offer 3 hours intervals/steps", Windy staff) |
| Docs | <https://api.windy.com/point-forecast/docs>, <https://api.windy.com/point-forecast/pricing> |
| Subscription | **Professional**, "990 € / year" ([pricing](https://api.windy.com/point-forecast/pricing)), "an in-advance annual subscription fee" charged "automatically each year until they cancel" ([terms](https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use)) |
| Key | A **Point Forecast** key, `WINDY_API_KEY`, kept server-side only: "Keys generated for other services, e.g. Map Forecast or Webcams, are a distinct category, and do not work for the Point Forecast API" (docs); "API Keys for Public Applications may be used only from User Domains" (terms), so prefer a private-application key |
| Commercial rights | The Professional terms allow integration into an application only if it "creates a significant added value for its users", forbid replicating Windy's services, and forbid offering the data "solely in the paid part of the User Application" (terms). ECMWF data is not available: "The ECMWF model is not included in point forecast due to licensing conditions." (pricing) |
| Storage rights | **Not granted by the standard terms**: the user may not "store, extract, modify, distribute, use the weather data or other content of the Services, create any weather works or databases derived therefrom", and "Redistribution of any part of the API or the included data to any third party is forbidden." (terms). This app stores every run and serves it to every visitor, so it needs a **written licence** from Windy first |
| Derived scoring rights | Not granted either (the "databases derived therefrom" clause above): the surf scores, swell events and alerts are derived data. The written licence must name them (`docs/WINDY_API_SETUP.md`, section 1, lists the five points to ask for) |
| The switch | `WINDY_STORAGE_AUTHORIZED=true` (old name WINDY_TERMS_ACKNOWLEDGED, still read), set **only** once that licence exists; without it Windy data are never stored, scored or published, `WEATHER_PROVIDER=auto` uses Open-Meteo, and production refuses `WEATHER_PROVIDER=windy` |
| Quotas | Professional: 10,000 requests a day ("can be increased by agreement"; the terms count "Sessions"); "The reset of counter is at 0:00 UTC." (Windy staff). The app counts its own requests (`WINDY_DAILY_REQUEST_LIMIT`, default 10000) and needs about 550 a day at the defaults (computed: 144 sentinel checks plus about 400 for 8 component refreshes; `docs/WINDY_API_SETUP.md`, section 6). A quota answer makes Open-Meteo stand in until 00:00 UTC, and `quota_near` warns the admins at `QUOTA_ALERT_FRACTION` (0.8) |
| Testing key | Free, 500 requests a day, "Returns randomly shuffled and slightly modified data", "Development purpose only, not intended for production" (pricing). Its responses carry "The trial API version is for development purposes only. This data is randomly shuffled and slightly modified.": the adapter checks every response for it and refuses the data (reason `windy_testing_key`), so a testing key can be tried with `provider-check` but never feeds the site |
| Run detection | The response has no model run or update time and Windy documents no webhook or metadata endpoint, so a new run is found by a sentinel fingerprint every `WINDY_CHECK_MINUTES` (15) in each GFS cycle's expected window; its run times are labelled `estimated` (`docs/WINDY_API_SETUP.md`, section 8) |
| Attribution | "Contains data from the Windy database" with a link (shown from the published version), and the Windy logo, "unscaled and clickable", which the owner adds with the licence (terms; `docs/WINDY_API_SETUP.md`, section 7) |
| Env vars | `WINDY_API_KEY`, `WINDY_STORAGE_AUTHORIZED`, `WEATHER_PROVIDER`, `WINDY_CHECK_MINUTES`, `WINDY_DAILY_REQUEST_LIMIT` |
| Setup | Only with the written licence: set `WINDY_API_KEY` and `WINDY_STORAGE_AUTHORIZED=true` on the API (and the optional daemon), keep `WEATHER_PROVIDER=auto` (the hosted default) or set `windy`, redeploy, run the health check, trigger a refresh and check `/api/forecasts/version` (`"provider": "windy"`) |
| Test | Health check (stores nothing, one request): `python -m app.cli forecast provider-check --provider windy`; mocks: `pytest tests/unit/test_windy_provider.py tests/unit/test_provider_manager.py tests/integration/test_windy_acquisition.py`. Never run against a real key here |

### 2.10 Flight providers

No real flight API is connected: `FLIGHT_PROVIDER=demo` (the default) generates fares from
fictional carriers, labelled "Mock fares" everywhere (`is_mock`), the clearly labelled test
adapter of spec section 36. The provider research (read 2026-10-27, URLs below) and the
matching rules are in `docs/MATCHING_AND_FLIGHTS.md`, sections 3 and 7.

| | Duffel | Amadeus |
|---|---|---|
| Purpose | Fares for surf trips (`FLIGHT_PROVIDER=duffel`) | Same, for Amadeus Enterprise customers (`FLIGHT_PROVIDER=amadeus`) |
| Docs | <https://duffel.com/docs/api/overview/test-mode> | <https://developers.amadeus.com/> |
| Account | Self-serve. Live mode only after Duffel's review: "Until we have reviewed and approved all such information (“Verification”), your Account will be available to you on a preliminary basis only, such that you can only access a test environment" ([services agreement](https://duffel.com/services-agreement)) | Enterprise agreement only: the Self-Service portal "has been decommissioned. Its developer site now provides Enterprise APIs, with access on request." (secondary source, <https://ignav.com/docs/amadeus-self-service-shutdown>; shut down on 2026-07-17 according to secondary sources; developers.amadeus.com/self-service redirects to the portal root) |
| Credentials | `DUFFEL_ACCESS_TOKEN` from app.duffel.com → Developers → Access tokens; "Testing tokens are easy to recognize: they start with duffel_test_" (sandbox airline "Duffel Airways") | `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` from the account manager; `AMADEUS_BASE_URL` |
| **Use clause** | The agreement forbids using the services "for metasearch purposes (including to build a metasearch on top of the Duffel Platform and/or to redistribute to a metasearch platform)" (services agreement). This app shows fares and links out to book elsewhere, which is at risk under that clause: **get Duffel's written confirmation before production**, or use Duffel as a seller (Orders, or Duffel Links: <https://duffel.com/docs/guides/duffel-links>) | Contract terms (not reviewed) |
| Limits | No numeric rate limit published; 429 `rate_limit_exceeded` with a `ratelimit-reset` header ([errors](https://duffel.com/docs/api/overview/errors)). Offers expire "typically within 30 minutes" ([offers](https://duffel.com/docs/api/offers)), so the shared cache ends at the first offer's expiry. Searches beyond a 1500:1 search-to-order ratio cost "$0.005" each, and "zero Orders shall be treated as one Order" (pricing, agreement): a search-only app gets about 1,500 free searches a period. The app's own budgets: `FLIGHT_DAILY_QUOTA` (300 a day), `FLIGHT_USER_DAILY_QUOTA` (20 a user a day), `FLIGHT_MAX_QUERIES_PER_MATCH` (3) | Contract |
| Display rule | "you should always show the full name of the operating carrier of each segment ... displayed prominently on the first screen where the offer is presented" ([offer requests](https://duffel.com/docs/api/offer-requests)): the opportunity page and the alerts show "Operated by …" | — |
| Markets | Australia → Bali/Indonesia and the USA → Central America first (spec 28); every spot's airports with IANA zones are in `data/surf_spots/airports.json`. Small regional airports (Lakey Peak BMU, G-Land BWX, Desert Point LOP, Bocas del Toro BOC, Puerto Escondido PXM) fall back to the spot's international gateway (DPS, SJO, LIR, PTY, MEX) when they return no offers. Duffel's airline pages list Jetstar, Qantas, Virgin Australia, Garuda Indonesia and Copa; Wings Air, Lion Air, Batik Air, Citilink, Air Panama and Sansa have no page (which does not prove they are unavailable) | Unknown (no account) |
| Cost | "$3.00 per order" plus "1% total order value" for managed content, and the search fee above ([pricing](https://duffel.com/pricing)) | Contract |
| Env vars | `FLIGHT_PROVIDER`, `DUFFEL_ACCESS_TOKEN`, `DUFFEL_BASE_URL`, `FLIGHT_DAILY_QUOTA`, `FLIGHT_USER_DAILY_QUOTA`, `FLIGHT_MAX_QUERIES_PER_MATCH`, `FLIGHT_CACHE_TTL_HOURS`, `FX_RATES_USD_JSON` | `FLIGHT_PROVIDER`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `AMADEUS_BASE_URL` |
| Setup | Set `FLIGHT_PROVIDER=duffel` and `DUFFEL_ACCESS_TOKEN`, then `python -m app.cli check-config`. Duffel prices in the organisation's billing currency: set `FX_RATES_USD_JSON` if users budget in another currency | Set the three `AMADEUS_*` variables and `FLIGHT_PROVIDER=amadeus` |
| Test | Mock tests: `pytest tests/unit/test_flight_providers.py`; with a `duffel_test_` token: `FLIGHT_PROVIDER=duffel python -m app.cli pipeline` on a scratch database (not run here: no token) | Mock tests only (api.amadeus.com could not be reached from here) |

**Affiliate options, researched and not built** (`docs/MATCHING_AND_FLIGHTS.md`, section 7):
Travelpayouts / Aviasales serves cached prices other users found
(<https://travelpayouts-data-api.readthedocs.io/>), and its real-time search requires that
"Each search query must be initiated by the user" (<https://travelpayouts.github.io/slate/>),
so it cannot drive background matching; KAYAK's flight APIs need an approved affiliate use
case ("Once your use case is approved as a full affiliate integration, you can request
production API keys", <https://affiliates.kayak.com/apis>); Skyscanner's Travel API is for
"an established business with a large audience"
(<https://www.partners.skyscanner.net/product/travel-api>); Kiwi.com's Tequila takes new
partners "on an invitation only basis" since May 2024
(<https://media.kiwi.com/articles-and-interviews/better-for-business-kiwi-com-takes-a-new-approach-to-partnerships/>);
Google has had no public flight API since QPX Express closed on 2018-04-10
(<https://www.engadget.com/2017-11-01-google-flight-data-api-shutdown.html>), and
google.com/robots.txt disallows `/travel/flights/search`, so Google Flights is not scraped. Without a provider the app
keeps its Google Flights and Skyscanner route-search links, labelled "route search —
fares may differ".

### 2.11 Email providers

| | Resend | SendGrid (Twilio) | SMTP |
|---|---|---|---|
| Purpose | Verification, password reset, swell alerts, admin data-health alerts | same | same (any SMTP service, or Mailpit locally) |
| Docs | <https://resend.com/docs> | <https://www.twilio.com/docs/sendgrid> | your provider |
| Account / key | Account, verified sending domain, `RESEND_API_KEY` | Account, verified sender, `SENDGRID_API_KEY` | Host and credentials |
| Free tier | "$0 / mo 3,000 emails / mo 100 emails a day" ([pricing](https://resend.com/pricing)) | "Sign up for a free trial—no credit card required." ([pricing](https://sendgrid.com/en-us/pricing)); plan prices not readable: confirm at signup | depends |
| Cost | Pro "$20 / mo 50,000 emails / mo" | confirm at signup | depends |
| Licence / terms | The provider's acceptable-use terms (not reviewed here); a verified sending domain with SPF, DKIM and DMARC | same | your provider's |
| Credentials | `RESEND_API_KEY` (resend.com → API keys), stored as a secret | `SENDGRID_API_KEY` (app.sendgrid.com → Settings → API keys) | `SMTP_USERNAME`, `SMTP_PASSWORD` |
| Verified domain | Add a domain you own in Resend and verify it; until then only the testing sender `onboarding@resend.dev`, which "can only send emails to the email address associated with your Resend account" | Verify a single sender or, better, authenticate the domain | the SMTP provider's own rule |
| DNS records | The provider shows the exact records to add at your DNS host: **SPF** (a TXT record, or an `include:` in your existing SPF record, authorising the provider's servers), **DKIM** (CNAME or TXT records with its signing keys), **DMARC** (a TXT record at `_dmarc.<your-domain>`, for example `v=DMARC1; p=none; rua=mailto:dmarc@<your-domain>` to start with) and, for Resend and SendGrid, a custom return-path (bounce) CNAME. Wait until the dashboard shows the domain verified; then set `EMAIL_FROM` to an address on it | same | same |
| Delivery webhooks (optional) | `${API_BASE_URL}/api/webhooks/resend`, signed with `RESEND_WEBHOOK_SECRET` (Svix) | `${API_BASE_URL}/api/webhooks/sendgrid`, signed Event Webhook with `SENDGRID_WEBHOOK_PUBLIC_KEY` | none: stops at "accepted" |
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

### 2.14 Database: Neon Postgres (hosted) or any PostgreSQL 16

| | |
|---|---|
| Purpose | All persistent state: spots, forecast runs and rows, forecast versions, predictions, swell events, users and sessions, saved searches, flight searches and offers, opportunities, notifications, job logs |
| Provider | **Neon** from the Vercel Marketplace (chosen over Supabase and Prisma Postgres in `docs/architecture_audit.md`, section 4.5); any PostgreSQL 16 with the `btree_gist` extension works (the Docker Compose stack runs `postgres:16-alpine`) |
| Free tier | "100 CU-hours/project, 1 GB Postgres storage per project ... and 5 GB of egress per project." (<https://neon.com/docs/introduction/plans>); "Scale to zero is always enabled (computes suspend after 5 minutes of inactivity)". Out of CU-hours or egress the compute is suspended until the next billing period; what this app uses of it is in 1.4 |
| Connection strings | The Marketplace sets `DATABASE_URL` ("Pooled connection string (PgBouncer)") and `DATABASE_URL_UNPOOLED` ("Direct connection string", <https://neon.com/docs/guides/vercel-managed-integration>). The API and the refresh use the pooled URL with `DB_PREPARED_STATEMENTS=false` (required behind a PgBouncer in transaction mode); migrations and the optional daemon use the direct one. `postgres://` and `postgresql://` URLs are rewritten to the psycopg driver, keeping `sslmode` and `channel_binding`. Locally: `postgresql+psycopg://swell:swell@localhost:5432/swell` |
| Migrations | Alembic, `backend/alembic/versions/` (migrations 0001 to `0009_forecast_versions`) (Swell 2.0: `forecast_versions` and the saved searches' alert lead days). On Vercel the api's build command `python -m app.cli migrate --on-build` migrates and seeds production builds (`MIGRATE_ON_BUILD=production`) through the direct URL; a failed migration fails the build. By hand, from `backend/`: `python -m app.cli migrate` (migrations only) or `python -m app.cli init-db` (migrations, the 50 spots, airports, research evidence, the tide catalog; idempotent), `docs/deploy-vercel.md` step 6. `alembic check` in CI proves the models and migrations agree |
| Profiles | free-database (Neon Free: `RETENTION_FORECAST_DAYS=1`, `RETENTION_ARCHIVE_DAYS=7`, `RETENTION_JOB_LOG_DAYS=7`, `SYNC_PRUNE_EACH_PASS=true`, `DATA_HEALTH_DATABASE_MAX_GB=0.85`) and full-retention (after moving to Neon Launch), section 5 |
| Backups | Neon's own history and branches on the hosted setup; `scripts/backup-db.sh` and `scripts/restore-db.sh` for any PostgreSQL (`docs/operations.md`, section 4) |
| Test | `GET /api/health/ready` (`"database": "ok"`); `pytest tests/integration` against a local PostgreSQL; `tests/integration/test_pgbouncer_mode.py` with TEST_PGBOUNCER_URL set (PgBouncer in transaction mode, as Neon's pooler: 3 passed against a local PgBouncer on 2026-10-29; Neon itself was not tried) |

### 2.15 Vercel: project, plan, scheduled refresh

The step-by-step deployment is `docs/deploy-vercel.md`, section 4; this is what each part
requires. Vercel pages read on 2026-10-27.

| | |
|---|---|
| Project setup | One project from the GitHub repository, **Root Directory at the repository root** (`vercel.json` defines the services `web`, Next.js, and `api`, FastAPI as one Python function, the optional `sync` daemon and the daily cron). Leave the build settings at their defaults; they belong to each service |
| Environment variables | The hosted-profile block of `docs/deploy-vercel.md` step 5 (`profile_env("hosted", "free-database")`, also `.env.vercel.example`) plus yours: `APP_ENV=production`, `SECRET_KEY`, `CRON_SECRET`, `COOKIE_SECURE=true`, `APP_BASE_URL`, `API_BASE_URL`, `CORS_ORIGINS`, the email provider and its key; the Marketplace sets `DATABASE_URL`, `DATABASE_URL_UNPOOLED` and `KV_URL`. Changes reach new deployments only. Every setting is in section 6 |
| Required plan | **Hobby** works for a non-commercial site: "Hobby teams are restricted to non-commercial personal use only. All commercial usage of the platform requires either a Pro or Enterprise plan." (<https://vercel.com/docs/limits/fair-use-guidelines>). Functions on Hobby run up to "300s default and maximum" with 2 GB (<https://vercel.com/docs/functions/limitations>); the api function measured 128.3 MiB of the 500 MB Python limit and sets `maxDuration` 300. Pro ($20 a month) before any paywall or ads, and for a Vercel Cron refresh |
| Cron frequency | Hobby: "Once per day" with "Per-hour (±59 min)" precision, and a sub-daily expression fails the deployment ("Hobby accounts are limited to daily cron jobs."); Pro: "Once per minute" (<https://vercel.com/docs/cron-jobs/usage-and-pricing>). So `vercel.json` keeps only the daily cron (`/api/internal/cron/daily`, `23 7 * * *`), and the 5-minute refresh comes from an external scheduler on Hobby. "Vercel will not retry an invocation if a cron job fails." and a run can be delivered twice (<https://vercel.com/docs/cron-jobs/manage-cron-jobs>); both are safe here (lease, idempotent refresh) |
| Secure scheduled endpoints | `GET\|POST /api/internal/refresh` and `GET /api/internal/cron/daily` require `Authorization: Bearer <CRON_SECRET>`, compared in constant time (`hmac.compare_digest`); 401 for a wrong or missing token, 404 when `CRON_SECRET` is not set (the same body as an unknown route), `Cache-Control: private, no-store`. `CRON_SECRET` must be 16 characters or more (the app refuses to start otherwise). Vercel Cron sends it by itself: "The value of the variable will be automatically sent as an `Authorization` header" (manage-cron-jobs). No public endpoint starts a refresh; the developer simulation (`/api/dev/*`) is refused in production |
| **QStash setup** (Hobby) | Upstash QStash Free: 1,000 messages a day (retries and every trigger count), 10 schedules, and it waits up to 15 minutes for the answer (<https://upstash.com/pricing/qstash>). 1. Open console.upstash.com (the Upstash account of the Redis database works), QStash tab, copy the QSTASH_TOKEN. 2. On your machine, `export QSTASH_TOKEN=...` and `export CRON_SECRET=...` (the deployment's value). 3. Create the schedule with the `curl` of `docs/deploy-vercel.md` step 7b (headers `Upstash-Cron: */5 * * * *` and `Upstash-Forward-Authorization: Bearer $CRON_SECRET`, URL `https://qstash.upstash.io/v2/schedules/https://<project>.vercel.app/api/internal/refresh`); QStash answers with a `scheduleId`. 4. After 5-10 minutes `/admin/freshness` shows the refresh scheduler `checking` and the QStash console's Logs a delivery every 5 minutes with HTTP 200. 5. If `CRON_SECRET` changes, delete the schedule and create it again. 288 messages a day; the token never goes into the app or the repository. Not deployed here |
| Other schedulers | **Vercel Cron on Pro**: add `{"path": "/api/internal/refresh", "schedule": "*/5 * * * *"}` to `crons` (it fails a Hobby deployment, so it is not committed). **cron-job.org**: free, "up to 60 times an hour", custom headers, but "If your URL/script does not finish after 30 seconds, it will timeout and we will close the connection" and a job is disabled "when it subsequently fails more than 25 times" (<https://cron-job.org/en/faq/>); not verified for refreshes longer than 30 s. **GitHub Actions** schedules are not used (5-minute minimum, delayed or dropped runs, and the terms' "serverless application" clause); `data-sync.yml` is manual only |
| Daemon (optional) | Hobby daemons get "5 Active CPU-hours per month" and are stopped "until the first of the next month (UTC)" at the quota (<https://vercel.com/docs/daemons/limits>); Public Beta, production only. The hosted profile runs it for the map layers only (`SYNC_DAEMON_KINDS=map`); without it set `MAP_FIELDS_ENABLED=false` |
| Test | Offline, no account: `scripts/vercel-build-check.sh --offline` (`RESULT: ok`, 2026-10-29); on a deployment: the checks of `docs/deploy-vercel.md` steps 7 and 7b. No real deployment was made yet |

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

The settings of the hosted setup, grouped; section 6 lists every setting with its default.

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

## 6. Settings reference: every variable

Every setting of `backend/app/core/config.py` (the `Settings` class), every variable of
`.env.example`, and the variables only Docker Compose, the frontend build or the local
Vercel-style stack read, with the default and, where it differs, the value of the hosted
profile (`profile_env("hosted", "free-database")`, the block of `docs/deploy-vercel.md` step 5
and `.env.vercel.example`). "empty" means unset: the code's own rule applies, as the row says.
Rows marked "Secret" hold credentials: put them only in `.env` (git-ignored) or the platform's
secret store, never in the repository. A unit test
(`backend/tests/unit/test_api_setup_doc.py`) checks that every `Settings` field has a row here,
that every default and hosted value below is the code's, and that every name is in
`.env.example`. The sections follow `.env.example`.

**General**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `APP_ENV` | `development` |  | `development`, `test` or `production`. Production refuses the development `SECRET_KEY`, dev endpoints, insecure cookies, capture-only email providers and `WEATHER_PROVIDER=windy` without the Windy licence settings |
| `LOG_LEVEL` | `INFO` |  | Python log level of the API and the workers (DEBUG, INFO, WARNING, ...); secrets and personal data are redacted from logs |
| `APP_NAME` | `Swell Travel Agent` |  | The app's name: the subject prefix of admin emails, the API title, and the application name sent to NOAA CO-OPS when `NOAA_COOPS_APPLICATION` is empty |
| `APP_BASE_URL` | `http://localhost:3000` |  | Public URL of the web app, used in every email link; empty = `http://localhost:3000` (in Docker `http://localhost:${FRONTEND_PORT}`). Set it when deployed behind a domain |
| `NEXT_PUBLIC_APP_URL` | alias of `APP_BASE_URL` |  | Alias of `APP_BASE_URL` under the name the frontend build reads (its `metadataBase`); the backend reads it when `APP_BASE_URL` is empty |
| `API_BASE_URL` | `http://localhost:8000` |  | Public URL of the API (unsubscribe header, provider webhook validation) |
| `SECRET_KEY` | `dev-insecure-secret-change-me` |  | Keys the rate-limit counters (an HMAC of the client key); production refuses the development default. Generate 48+ random bytes (`python3.13 -c "import secrets; print(secrets.token_urlsafe(48))"`). Secret |
| `ENABLE_DEV_ENDPOINTS` | `false` |  | Exposes `/api/dev/*` (simulate a swell or a model run, run the pipeline, read the outbox, send a test email); refused in production |

**Infrastructure (database, Redis, Celery, Docker Compose)**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://swell:swell@localhost:5432/swell` |  | The database (pooled on Neon); `postgres://` and `postgresql://` URLs are rewritten to the psycopg driver, keeping the query string. Secret on a deployment |
| `DATABASE_URL_UNPOOLED` | empty |  | Direct (not pooled) URL of the same database, for migrations and, with `DB_USE_UNPOOLED=true`, the whole process (the daemon image); Neon's integration sets it (POSTGRES_URL_NON_POOLING is read too). Empty = `DATABASE_URL`; ignored with a warning when it names another database |
| `DB_USE_UNPOOLED` | `false` |  | This process connects through `DATABASE_URL_UNPOOLED` (the daemon image sets it; do not set it for the project) |
| `DB_POOL_RECYCLE_SECONDS` | `1800` | `240` | Connections older than this are replaced (a pooler or a scaled-to-zero database closes idle ones) |
| `DB_PREPARED_STATEMENTS` | `true` | `false` | `false` = never prepare statements, required behind a PgBouncer in transaction mode such as Neon's pooled URL |
| `REDIS_URL` | `redis://localhost:6379/0` |  | Redis: rate limits, job locks, call budgets, the version marker, the public snapshot cache and, under Celery, the broker |
| `KV_URL` | alias of `REDIS_URL` |  | Alias of `REDIS_URL`, the name Vercel's Upstash integration sets (a `rediss://` URL); read only when `REDIS_URL` is empty |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | `5` | `1.5` | Redis connect timeout; an unreachable Redis costs a request at most this long (rate limits then fail open) |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `5` | `2` | Redis socket (read and write) timeout |
| `CELERY_BROKER_URL` | empty |  | Celery broker; empty = `REDIS_URL` (Docker Compose sets database 1 of its Redis) |
| `CELERY_RESULT_BACKEND` | empty |  | Celery result backend; empty = `REDIS_URL` (Docker Compose sets database 2) |
| `CELERY_TASK_ALWAYS_EAGER` | `false` |  | `true` = jobs run inside the calling process instead of a worker (the tests, single-process development) |
| `BEAT_RUN_ON_START` | `true` |  | When celery beat starts, queue the sync check (`sync.tick`) and, with `TIDE_PROVIDERS`, the tide job at once |
| `DB_POOL_SIZE` | `5` | `2` | SQLAlchemy connection pool per process |
| `DB_MAX_OVERFLOW` | `10` | `3` | Extra connections a pool may open above `DB_POOL_SIZE` |
| `POSTGRES_USER` | `swell` |  | Docker Compose: the PostgreSQL container's user |
| `POSTGRES_PASSWORD` | `swell` |  | Docker Compose: the PostgreSQL container's password (the local default only; never a deployment's) |
| `POSTGRES_DB` | `swell` |  | Docker Compose: the database the PostgreSQL container creates |
| `POSTGRES_PORT` | `5432` |  | Docker Compose: host port of PostgreSQL |
| `REDIS_PORT` | `6379` |  | Docker Compose: host port of Redis |
| `BACKEND_PORT` | `8000` |  | Docker Compose: host port of the API |
| `FRONTEND_PORT` | `3000` |  | Docker Compose: host port of the web app |
| `MAILPIT_UI_PORT` | `8025` |  | Docker Compose: host port of Mailpit's web inbox |
| `MAILPIT_SMTP_PORT` | `1025` |  | Docker Compose: host port of Mailpit's SMTP server |
| `API_WORKERS` | `2` |  | Docker image (`backend/docker-entrypoint.sh`): uvicorn worker processes of the `api` role |
| `WORKER_CONCURRENCY` | `2` |  | Docker image: Celery worker processes of the `worker` role |
| `WORKER_QUEUES` | `celery,bulk` |  | Queues the Celery worker consumes (data downloads go to `bulk`); the data-health routing check reads it too |
| `REDIS_MAXMEMORY` | `256mb` |  | Docker Compose: Redis memory cap (`noeviction`); the data-health check warns at `DATA_HEALTH_REDIS_MAX_FRACTION` of it |

**HTTP and security**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `CORS_ORIGINS` | `http://localhost:3000` |  | Comma separated allowed origins; the web app proxies `/api`, so CORS is rarely needed |
| `SESSION_TTL_DAYS` | `14` |  | Lifetime of a login session (cookie and server-side record) |
| `COOKIE_SECURE` | `false` |  | `true` in production (HTTPS-only cookies); production refuses `false` |
| `COOKIE_DOMAIN` | empty |  | Domain attribute of the cookies; empty = the host that set them |
| `SESSION_COOKIE_NAME` | `sta_session` |  | Name of the HttpOnly session cookie |
| `CSRF_COOKIE_NAME` | `sta_csrf` |  | Name of the CSRF double-submit cookie; keep the default, the frontend reads it by name (`frontend/lib/api.ts`) |
| `RATE_LIMIT_ENABLED` | `true` |  | Redis rate limits on login, registration, email verification, password reset, unsubscribe and the data-health page, and the public cache-miss limit; they fail open when Redis is down |
| `PUBLIC_CACHE_MISS_LIMIT_PER_HOUR` | `120` |  | Requests of one client IP an hour that miss the public snapshot cache (and so read the database); more answer 429. Cache hits are not counted |
| `LOGIN_MAX_FAILURES` | `8` |  | Failed logins before an account is locked |
| `LOGIN_LOCKOUT_MINUTES` | `15` |  | How long a locked account stays locked |
| `EMAIL_VERIFICATION_TTL_HOURS` | `48` |  | Lifetime of an email verification link |
| `PASSWORD_RESET_TTL_MINUTES` | `60` |  | Lifetime of a password-reset link |
| `TRUSTED_PROXY_COUNT` | `1` |  | Proxies in front of the API that append to X-Forwarded-For (for client IPs and rate limits); the Next.js proxy does not count |
| `CLIENT_IP_HEADER` | empty | `x-vercel-forwarded-for` | Header that carries the client IP, set by the platform and not by clients; empty = X-Forwarded-For with `TRUSTED_PROXY_COUNT` |

**Runner, deployment and refresh**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `JOB_RUNNER` | `celery` | `daemon` | Who runs the background jobs: `celery` (worker and beat, the classic stack) or `daemon` (one `python -m app.cli sync daemon` process, or on Vercel the refresh endpoint and the daily cron without a daemon) |
| `CRON_SECRET` | empty |  | Bearer token of `/api/internal/refresh` and `/api/internal/cron/daily` (at least 16 characters); empty = both answer 404. Secret |
| `MIGRATE_ON_BUILD` | `off` | `production` | `python -m app.cli migrate --on-build`: `off`, `production` (only when VERCEL_ENV is production) or `all` (also preview builds); it migrates and seeds, and fails the build on an error or on an empty database it cannot seed |
| `COMMERCIAL_USE` | `false` | `false` | `true` once the site is used commercially (a paywall, ads): the Open-Meteo sources then need `OPEN_METEO_API_KEY` (production refuses to start without it) |
| `OPEN_METEO_COMMERCIAL` | alias of `COMMERCIAL_USE` |  | Alias of `COMMERCIAL_USE` under its Swell 2.0 name; it wins when both are set |
| `SYNC_CHECK_MINUTES` | `10` |  | Minutes between model-run checks of the daemon and of Celery's `sync.tick` (provider metadata only); the refresh endpoint follows its scheduler instead |
| `SYNC_COALESCE_MINUTES` | `0` |  | How long the late model domains of one component wait for each other (within a component only; 0 = none) |
| `SYNC_LEASE_MINUTES` | `45` |  | The one-sync-at-a-time lease; empty = 45, or 15 under `JOB_RUNNER=daemon`; a refresh through the endpoint holds at most 10 |
| `SYNC_MAX_ATTEMPTS` | `6` |  | Failed attempts per model run of a component (and spot retries per run) before waiting for the next run |
| `SYNC_RETRY_MINUTES` | `15` |  | Wait after a failed component acquisition; also the shortest gap between two early data-health checks |
| `SYNC_TRIGGER_SOURCES` | `spots,map` | `spots` | Component kinds whose new run makes a sync due: subset of `spots`, `map`, `ensemble` |
| `SYNC_DAEMON_KINDS` | `spots,ensemble,map` | `map` | Component kinds the sync daemon refreshes itself: subset of `spots`, `ensemble`, `map` |
| `SYNC_PRUNE_EACH_PASS` | `false` | `true` | Prune superseded predictions and map fields after every publication, not only in the daily cleanup |
| `SYNC_PREWARM_PUBLIC_CACHE` | `false` |  | After a publishing sync, compute the public spot list and map conditions at once (about 1.4 MB of reads) instead of at the first visitor's request |
| `SYNC_PUBLISH_GATE` | `true` |  | The map serves the map run of the newest publication |
| `SYNC_DAEMON_POLL_SECONDS` | `60` |  | How often the daemon picks up the API's kicks ("match my search", "Re-check flights") |
| `SYNC_DAEMON_INIT_DB` | `true` |  | The daemon runs `init-db` (migrate, seed, tide catalog; idempotent) at every start |
| `SYNC_DAEMON_CATCH_UP` | `true` |  | At start the daemon runs, once, every periodic job without a success within its longest gap |
| `SYNC_CPU_BUDGET_HOURS` | empty | `5` | Monthly CPU budget of the sync runner in hours; data health warns above 80 % (`sync_cpu_budget`); empty = no budget |
| `JOB_INTERVAL_FLOOR_MINUTES` | `0` | `60` | Every periodic job runs at most this often (0 = as scheduled), so a scale-to-zero database is woken at most hourly between refreshes |

**Forecasts and providers**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `WEATHER_PROVIDER` | `auto` | `auto` | The weather provider: `auto` (Windy once `WINDY_API_KEY` is set and `WINDY_STORAGE_AUTHORIZED=true`, else Open-Meteo GFS + GFS-Wave), `open_meteo`, `windy` (production refuses it until both Windy settings are in place) or `demo` (synthetic data; `.env.example`, Docker Compose and the tests) |
| `FORECAST_PROVIDERS` | empty |  | Advanced override of `WEATHER_PROVIDER`, comma separated, first = primary: `demo`, `noaa_gfs`, `ecmwf_ifs`, `open_meteo`, `open_meteo_ecmwf`, `windy`; `demo` is never mixed with live ones. Empty = derived from `WEATHER_PROVIDER` |
| `FORECAST_DAYS` | `10` | `10` | The rolling forecast horizon in days (3-16) |
| `FORECAST_REFRESH_ENABLED` | `true` |  | Kill switch of every refresh runner: `false` = nothing is acquired (the refresh endpoint answers `disabled`), the published data stay |
| `FORECAST_STALE_THRESHOLD_MINUTES` | `360` |  | A spot is stale once a component is older than its model's update interval plus this many minutes (design D10) |
| `FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL` | empty |  | Per-model overrides of the threshold above, a JSON object such as `{"ncep_gfswave025": 240}`; invalid JSON is refused at start |
| `FORECAST_MIN_VALID_FRACTION` | `0.8` |  | A spot passes validation only when this fraction of its hours carry a wave height and a wind speed |
| `FORECAST_VERSION_CDN_SECONDS` | `15` |  | CDN cache (`s-maxage`) of `GET /api/forecasts/version`; browsers never cache it |
| `FORECAST_REFRESH_MINUTES` | `180` |  | No longer a timer (refreshes follow the model runs); still part of each source's run-stale limit (`docs/FORECAST_SOURCES.md`, "Staleness") |
| `FORECAST_STALE_HOURS` | `18` |  | Run-stale limit of the sources without a cycle rule of their own (demo, Windy, the blend fallback) |
| `FORECAST_BATCH_SIZE` | `50` |  | Spots per Open-Meteo request (1-50; 50 = all spots in one request per model) |
| `FORECAST_HTTP_TIMEOUT_SECONDS` | `30` |  | HTTP timeout of the forecast providers' data requests (metadata reads use 10 s) |
| `FORECAST_DAILY_REQUEST_QUOTA` | `5000` |  | Declared but not read by any code (the per-provider budgets `OPEN_METEO_CALLS_*` and `WINDY_DAILY_REQUEST_LIMIT` replaced it); kept so an old env file loads |
| `OPEN_METEO_API_KEY` | empty |  | Key of a paid Open-Meteo plan: requests then go to the `customer-api` and `customer-marine-api` hosts. Secret |
| `OPEN_METEO_GFS_WAVE_MODELS` | `ncep_gfswave016,ncep_gfswave025` |  | Open-Meteo GFS-family wave models in routing order (`ncep_gfswave016` covers about 15°S-52.5°N; `ncep_gfswave025` is global) |
| `OPEN_METEO_GFS_ATMOS_MODEL` | `gfs_global` |  | Open-Meteo GFS-family atmosphere model (`gfs_seamless`, HRRR + GFS, is also accepted) |
| `OPEN_METEO_ECMWF_WAVE_MODEL` | `ecmwf_wam025` |  | Open-Meteo ECMWF-family wave model (`open_meteo_ecmwf`, advanced) |
| `OPEN_METEO_ECMWF_ATMOS_MODEL` | `ecmwf_ifs025` |  | Open-Meteo ECMWF-family atmosphere model |
| `OPEN_METEO_WAVE_MODEL` | `ncep_gfswave025` |  | Deprecated single-model alias: when set it overrides `OPEN_METEO_GFS_WAVE_MODELS` (one wave model for every spot). Not in `.env.example`; delete it from an old `.env` |
| `OPEN_METEO_ATMOS_MODEL` | `gfs_seamless` |  | Deprecated single-model alias: when set it overrides `OPEN_METEO_GFS_ATMOS_MODEL`. Not in `.env.example`; delete it from an old `.env` |
| `OPEN_METEO_CELL_SELECTION` | `sea` |  | `sea`, `nearest` or `land`: which grid cell Open-Meteo samples for a spot |
| `OPEN_METEO_AVAILABILITY_WAIT_MINUTES` | `10` |  | A new model run is read only this many minutes after Open-Meteo reports it available (its servers are eventually consistent) |
| `OPEN_METEO_NATIVE_STEPS_ONLY` | `true` |  | Keep only the models' own output steps (no Open-Meteo-interpolated hours) |
| `OPEN_METEO_CALLS_PER_MINUTE` | `600` |  | Open-Meteo weighted-call budget a minute, charged in Redis before each request (the free tier's limit) |
| `OPEN_METEO_CALLS_PER_HOUR` | `5000` |  | Weighted-call budget an hour |
| `OPEN_METEO_CALLS_PER_DAY` | `10000` |  | Weighted-call budget a UTC day; `quota_near` warns the admins at `QUOTA_ALERT_FRACTION` |
| `OPEN_METEO_CALLS_PER_MONTH` | `300000` |  | Weighted-call budget a month |
| `PROVIDER_RESPONSE_CACHE_ENABLED` | `true` | `false` | Cache provider responses in Redis for one model run, so a retry or a second process never repeats a request |
| `PROVIDER_RESPONSE_CACHE_MAX_BYTES` | `2000000` |  | Responses larger than this are not cached (bytes) |
| `PROVIDER_RESPONSE_CACHE_MAX_KEYS` | `200` |  | At most this many cached responses |
| `WINDY_API_KEY` | empty |  | Windy Point Forecast key (paid plan); used only with `WINDY_STORAGE_AUTHORIZED=true`. Secret, server only |
| `WINDY_STORAGE_AUTHORIZED` | `false` |  | A key is not a licence: Windy data are stored, scored and published only when this is `true`, set only with a written licence that allows it (old name WINDY_TERMS_ACKNOWLEDGED, still read) |
| `WINDY_CHECK_MINUTES` | `15` |  | Minutes between Windy's sentinel checks inside a cycle's expected window (four times less often outside it) |
| `WINDY_DAILY_REQUEST_LIMIT` | `10000` |  | The app's own count of Windy requests a UTC day; reaching it makes Open-Meteo stand in until 00:00 UTC |
| `DEMO_NATURAL_SWELLS` | `true` |  | Demo provider only: `false` = calm seas, so only simulated swells create events (the e2e tests) |

**Forecast engine (blend, direct GRIB, map layers)**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `FORECAST_BLEND_ENABLED` | `true` | `false` | Blend two independent model families when `FORECAST_PROVIDERS` names two (advanced; with one provider nothing is blended) |
| `NOAA_GFS_BASE_URL` | `https://noaa-gfs-bdp-pds.s3.amazonaws.com` |  | Bucket of NOAA GFS and GFS-Wave open data (direct GRIB by byte range: `noaa_gfs` and the map layers) |
| `NOAA_STEP_HOURS` | `3` |  | Forecast step of the direct `noaa_gfs` provider (hours) |
| `NOAA_MAX_LEAD_HOURS` | `240` |  | Last lead hour read by the direct `noaa_gfs` provider |
| `NOAA_AVAILABILITY_LAG_HOURS` | `5` |  | Hours after a GFS cycle its files are expected (also part of the run-stale limit) |
| `NOAA_GFS_WAVE_PATH_TEMPLATE` | `gfs.{ymd}/{hh}/wave/gridded/gfswave.t{hh}z.{grid}.f{fhour:03d}.grib2` |  | GFS-Wave file layout below the bucket (GFS v16), placeholders `{ymd}`, `{hh}`, `{grid}`, `{fhour:03d}`; change it if a GFS upgrade renames the files |
| `NOAA_GFS_ATMOS_PATH_TEMPLATE` | `gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.0p25.f{fhour:03d}` |  | GFS atmosphere file layout below the bucket, same placeholders |
| `GEFS_ENABLED` | `false` | `false` | GEFS-Wave ensemble mean and spread for the uncertainty ranges (69.4 MB a run) |
| `NOAA_GEFS_BASE_URL` | `https://noaa-gefs-pds.s3.amazonaws.com` |  | Bucket of NOAA GEFS open data |
| `GEFS_STAT_FIELDS` | `HTSGW,PERPW` |  | GEFS-Wave records read per step |
| `GEFS_CYCLES` | `00,06,12,18` |  | GEFS cycles read (UTC hours) |
| `GEFS_STEP_HOURS` | `6` |  | GEFS-Wave step (hours) |
| `GEFS_MAX_LEAD_HOURS` | `240` |  | Last GEFS-Wave lead hour read |
| `GEFS_AVAILABILITY_LAG_HOURS` | `7.5` |  | Hours after a cycle its GEFS-Wave files are expected |
| `ECMWF_BASE_URL` | `https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com` |  | Bucket of ECMWF open data (CC BY 4.0; the direct `ecmwf_ifs` provider) |
| `ECMWF_MAX_LEAD_HOURS` | `240` |  | Last ECMWF lead hour read |
| `ECMWF_AVAILABILITY_LAG_HOURS` | `8` |  | Hours after a cycle its ECMWF files are expected |
| `ECMWF_WAVE_PARAMS` | `swh,mwd,pp1d,mwp` |  | ECMWF wave parameters read (GRIB short names) |
| `GRIB_DOWNLOAD_WORKERS` | `6` |  | Parallel byte-range downloads of the GRIB ingesters |
| `GRIB_MAX_CELL_DISTANCE_KM` | `60` |  | A GRIB or Open-Meteo cell farther than this from the forecast point is not used |
| `MAP_FIELDS_ENABLED` | empty | `true` | Map ocean layers: empty = automatic (on with a live provider, off with demo only), `true` = always, `false` = off (the map then shows "Spot forecasts only"; set it on Vercel without the daemon) |
| `MAP_FIELD_ATMOS_GRID` | `0p50` |  | GFS atmosphere grid of the map's wind and pressure (`0p25`, `0p50`, `1p00`) |
| `MAP_FIELD_ATMOS_PATH_TEMPLATE` | `gfs.{ymd}/{hh}/atmos/gfs.t{hh}z.pgrb2.{grid}.f{fhour:03d}` |  | Its file layout (`{grid}` = `MAP_FIELD_ATMOS_GRID`) |
| `MAP_FIELD_STEPS` | `0-120:3,120-240:6` |  | Map hours per cycle as `start-end:step` ranges in forecast hours |
| `MAP_FIELD_CYCLES` | `00,12` | `06,18` | GFS cycles whose map layers are read (UTC hours) |
| `MAP_FIELD_MAX_LEAD_HOURS` | `240` |  | The last map hour read (a short test: `MAP_FIELD_STEPS=0-24:3` with 24 = 9 map hours) |
| `MAP_FIELD_PARTITIONS` | `on` |  | `on` or `off`: primary swell, secondary swell and wind-sea layers (about 5 MB more per map hour) |
| `MAP_FIELD_MAX_BYTES_PER_RUN` | `600000000` |  | A map-field run stops before downloading more than this (bytes) |
| `MAP_FIELD_STALE_AFTER_HOURS` | `21` |  | The map layers are labelled stale once their cycle is older than this (hours) |
| `MAP_FIELD_RESOLUTION_DEG` | `0.5` |  | The map layers' grid in degrees (0.25-2) |

**Scoring**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `WIND_GUST_SCORING` | `off` |  | `off` = gusts shown only; `scored` = used by the wind score where the source's gust definition is known |
| `SCORING_WEIGHTS_JSON` | empty |  | Optional JSON object overriding the quality score's component weights, e.g. `{"wind": 0.35}` (non-negative, at least one above 0; names checked at start) |

**Observations, verification, ML and the archive**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `OBSERVATIONS_ENABLED` | `false` |  | Buoy observations (AODN, Met Office, NDBC) and their hourly ingest |
| `OBSERVATION_PROVIDERS` | `aodn,metoffice,ndbc` |  | Buoy providers read, comma separated |
| `NDBC_USE_5DAY` | `true` |  | Read NDBC's 5-day files when the look-back window fits in them |
| `OBSERVATION_LOOKBACK_HOURS` | `48` |  | Hours of buoy observations each ingest reads back; also the window of observations the tide API returns with a spot's tides |
| `AODN_BASE_URL` | `https://aodn-cloud-optimised.s3.ap-southeast-2.amazonaws.com` |  | Where AODN buoy data are read |
| `METOFFICE_BASE_URL` | `https://met-office-marine-observations-data.s3.eu-west-2.amazonaws.com` |  | Where Met Office buoy data are read |
| `NDBC_BASE_URL` | `https://www.ndbc.noaa.gov` |  | Where NDBC buoy data are read |
| `OBSERVATION_RETENTION_DAYS` | `3650` |  | Quality-controlled buoy observations are kept this many days |
| `VERIFICATION_ENABLED` | `false` |  | Verification jobs: forecasts archived at the buoys and accuracy metrics |
| `POINT_FORECAST_SOURCES` | `gfswave_bulletin` |  | Forecasts archived at the observation stations for verification |
| `ML_CORRECTIONS_ENABLED` | `false` |  | Apply an ACTIVE ML correction of offshore Hs (only one that passed the deployment criteria; none is active by default) |
| `PREDICTION_ARCHIVE_ENABLED` | `false` | `false` | Archive every published prediction at each lead time (verification and ML training data) |
| `ML_ARTIFACT_DIR` | `/tmp/swell-ml` |  | Where ML model files are written (use a persistent volume in production) |
| `RETENTION_ARCHIVE_DAYS` | `730` | `7` | Archived predictions are kept this many days |
| `ARCHIVE_CONFIDENCE_DIGEST` | `true` |  | Archive a short digest of the confidence inputs instead of the full inputs |
| `PRUNE_SUPERSEDED_MEMBER_PREDICTIONS` | `true` |  | The daily cleanup deletes the predictions of superseded runs no spot is served from (derived data, recomputable) |

**Tides**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `TIDE_PROVIDERS` | `noaa_coops,harmonic_model` |  | Tide sources, first = preferred: `noaa_coops` (US stations) and `harmonic_model` (committed constants); empty = no tides |
| `NOAA_COOPS_BASE_URL` | `https://api.tidesandcurrents.noaa.gov` |  | NOAA CO-OPS API host |
| `NOAA_COOPS_APPLICATION` | empty |  | The `application` name sent to CO-OPS (an app name and URL, never a personal address); empty = `APP_NAME` |
| `TIDE_MODEL` | `eot20` |  | `eot20` or `fes2022`: the harmonic constants used by `harmonic_model` |
| `TIDE_CONSTANTS_PATH` | `data/tides/constants_eot20.json` |  | Their file (relative to the backend directory or absolute) |
| `TIDE_HORIZON_DAYS` | `16` |  | Days of tide predictions computed ahead |
| `TIDE_RETENTION_PAST_DAYS` | `2` |  | Past tide predictions kept (days) |

**Swell detection and alert lead times**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `DETECTION_LEAD_MIN_DAYS` | `1` |  | Global lead-time floor of swell detection and matching (days ahead); each saved search narrows the window |
| `DETECTION_LEAD_MAX_DAYS` | `10` |  | Global lead-time ceiling |
| `ALERT_LEAD_DEFAULT_MIN_DAYS` | `2` |  | Default earliest alert lead time of a new saved search (whole days, 0-16) |
| `ALERT_LEAD_DEFAULT_MAX_DAYS` | `10` |  | Default latest alert lead time of a new saved search |
| `SHORT_NOTICE_MIN_DAYS` | `3` |  | Short-notice ranking boost from this many days ahead |
| `SHORT_NOTICE_MAX_DAYS` | `7` |  | ... to this many days ahead |
| `SWELL_EVENT_MIN_SCORE` | `40` |  | Score an hour needs to belong to a swell event (40 = the "Fair" threshold, so every user threshold is satisfiable) |
| `SWELL_EVENT_MIN_HOURS` | `3` |  | Shortest swell event (qualifying hours) |
| `SWELL_EVENT_BREAK_DAYLIGHT_HOURS` | `9` |  | An event ends once this many consecutive daylight hours fail to qualify |

**Flights**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `FLIGHT_PROVIDER` | `demo` |  | `demo` (mock fares, labelled), `duffel` or `amadeus` |
| `DUFFEL_ACCESS_TOKEN` | empty |  | Duffel access token (`duffel_test_...` for the sandbox). Secret |
| `DUFFEL_BASE_URL` | `https://api.duffel.com` |  | Duffel API host |
| `AMADEUS_CLIENT_ID` | empty |  | Amadeus Enterprise client id (the Self-Service portal is closed) |
| `AMADEUS_CLIENT_SECRET` | empty |  | Amadeus Enterprise client secret. Secret |
| `AMADEUS_BASE_URL` | `https://api.amadeus.com` |  | Amadeus API host (or your contracted one) |
| `FLIGHT_HTTP_TIMEOUT_SECONDS` | `45` |  | HTTP timeout of flight searches (above Duffel's 20 s supplier timeout) |
| `FLIGHT_CACHE_TTL_HOURS` | `6` |  | A cached flight search is reused at most this long, and never past its first offer's expiry |
| `FLIGHT_DAILY_QUOTA` | `300` |  | Provider flight searches a UTC day, all users |
| `FLIGHT_MAX_QUERIES_PER_MATCH` | `3` |  | Flight queries per match (the first-choice date of every origin is always searched) |
| `FLIGHT_MAX_OFFERS_PER_SEARCH` | `50` |  | Offers kept from one provider answer |
| `FLIGHT_USER_DAILY_QUOTA` | `20` |  | Provider flight searches a UTC day for one user (all of their saved searches) |
| `FX_RATES_USD_JSON` | empty |  | Optional currency table, USD per 1 unit, e.g. `{"EUR":1.08,"GBP":1.27}`; without it offers in another currency than the budget's are not compared with it |

**Email, SMS and alerts**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `EMAIL_PROVIDER` | `console` |  | `mailpit` or `console` (capture only, refused in production), `smtp`, `resend` or `sendgrid`; empty = the default (`console`; Docker Compose sets `mailpit`) |
| `EMAIL_FROM` | `Swell Travel Agent <alerts@localhost>` |  | Sender; for real delivery an address on a domain verified with the provider |
| `DEV_INBOX_URL` | empty |  | Shown in the app when email is captured; empty = Mailpit's web UI |
| `RESEND_API_KEY` | empty |  | Resend API key. Secret |
| `SENDGRID_API_KEY` | empty |  | SendGrid API key. Secret |
| `RESEND_WEBHOOK_SECRET` | empty |  | Svix signing secret of the Resend delivery webhook (`/api/webhooks/resend`); without it a status stops at "accepted". Secret |
| `SENDGRID_WEBHOOK_PUBLIC_KEY` | empty |  | Public key of SendGrid's signed Event Webhook (`/api/webhooks/sendgrid`) |
| `SMTP_HOST` | `localhost` |  | SMTP server; empty = `localhost` (in Docker the Mailpit service) |
| `SMTP_PORT` | `1025` |  | SMTP port |
| `SMTP_USERNAME` | empty |  | SMTP user |
| `SMTP_PASSWORD` | empty |  | SMTP password. Secret |
| `SMTP_USE_TLS` | `false` |  | STARTTLS after connecting |
| `SMTP_USE_SSL` | `false` |  | Implicit TLS (SMTPS) |
| `SMS_PROVIDER` | `console` |  | `console`, `twilio` or `disabled` |
| `TWILIO_ACCOUNT_SID` | empty |  | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | empty |  | Twilio auth token (also verifies the inbound STOP webhook). Secret |
| `TWILIO_FROM_NUMBER` | empty |  | Sending number (or use a messaging service) |
| `TWILIO_MESSAGING_SERVICE_SID` | empty |  | Twilio messaging service instead of a number |
| `NOTIFICATION_MAX_ATTEMPTS` | `5` |  | Delivery attempts of a notification with temporary failures (exponential backoff) |
| `NOTIFICATION_SENDING_TIMEOUT_MINUTES` | `10` |  | A notification stuck in `sending` this long is reclaimed (a crashed worker never loses or doubles an alert) |
| `ALERT_PRICE_DROP_PCT` | `10` |  | A price-drop follow-up needs a drop of at least this percentage ... |
| `ALERT_PRICE_DROP_MIN_AMOUNT` | `50` |  | ... and at least this amount (same currency) |
| `ALERT_SCORE_IMPROVEMENT` | `10` |  | An "improved" follow-up needs the peak score to rise this much (or the label to rise) |
| `OUTBOX_DIR` | `/tmp/swell-outbox` |  | Where the console email and SMS providers write their messages |

**Ranking and retention**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `RANKING_WEIGHTS_JSON` | empty |  | Optional JSON override of the opportunity ranking weights per priority (`docs/MATCHING_AND_FLIGHTS.md`) |
| `RETENTION_FORECAST_DAYS` | `10` | `1` | Forecasts and predictions kept (days); runs a published or previous version references are always kept |
| `RETENTION_FLIGHT_DAYS` | `30` |  | Flight searches and offers kept (days) |
| `RETENTION_JOB_LOG_DAYS` | `30` | `7` | Job logs and sync runs kept (days) |
| `RETENTION_NOTIFICATION_DAYS` | `365` |  | Notifications kept (days) |

**Data health and admin alerts**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `DATA_HEALTH_CHECK_MINUTES` | `15` | `60` | How often the data-health check (`ops.data_health`) runs and emails the admins about new problems |
| `DATA_HEALTH_ALERTS_ENABLED` | `true` |  | `false` = data-health problems are reported but not emailed |
| `DATA_HEALTH_ALERT_REPEAT_HOURS` | `12` |  | At most one email per problem in this many hours, plus a recovery notice |
| `DATA_HEALTH_HEARTBEAT_MINUTES` | `30` |  | Celery: no finished job for this long = worker or beat down; daemon: its heartbeat is stale after max(3 × `SYNC_CHECK_MINUTES`, this) |
| `DATA_HEALTH_OBSERVATION_STALE_HOURS` | empty |  | A buoy provider is stale after this many hours; empty = `OBSERVATION_LOOKBACK_HOURS` |
| `DATA_HEALTH_DATABASE_MAX_GB` | `20` | `0.85` | Degraded above this database size (an alert only, nothing is deleted); size it from the disk |
| `DATA_HEALTH_REDIS_MAX_FRACTION` | `0.8` |  | Degraded once Redis uses more than this fraction of `REDIS_MAXMEMORY` |
| `QUOTA_ALERT_FRACTION` | `0.8` |  | Admin alert `quota_near` once an API quota (Open-Meteo day or month, Windy day, flights day) reaches this fraction |
| `REFRESH_SCHEDULER_EXPECTED_MINUTES` | `30` |  | Admin alert `refresh_scheduler_silent` when no refresh check ran for this many minutes; 0 = off |
| `OPS_ALERT_EMAILS` | empty |  | Optional, comma separated: addresses the daily watchdog emails when the database itself cannot be reached (logs show counts only) |

**Frontend build (not Settings)**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `BACKEND_URL` | `http://localhost:8000` |  | Next.js build: where `/api/*` is proxied in the classic stack (`API_ROUTING=proxy`); Docker Compose sets the backend service |
| `API_ROUTING` | empty |  | Next.js build: `proxy` (Next.js rewrites `/api/*` to `BACKEND_URL`) or `platform` (the platform routes `/api`); empty = `platform` on Vercel, else `proxy` |
| `NEXT_PUBLIC_CARTO_API_KEY` | empty |  | Next.js build: a CARTO key switches the optional Streets basemap to CARTO Voyager (visible in the browser by design; restrict it to your origins) |
| `NEXT_PUBLIC_MAP_TILE_URL` | empty |  | Next.js build: any XYZ tile URL template for the Streets basemap (wins over the CARTO key) |
| `NEXT_PUBLIC_MAP_ATTRIBUTION` | empty |  | Next.js build: the attribution the tile provider requires |
| `MAP_TILE_HOSTS` | empty |  | Next.js build: extra CSP image hosts, space separated (the tile URL's own host is allowed automatically) |
| `NEXT_PUBLIC_FORECAST_VERSION_POLL_MS` | `20000` |  | Next.js build: how often an open, visible page asks `GET /api/forecasts/version` for a new forecast version (ms) |

**The local Vercel-style stack (not Settings)**

| Variable | Default | Hosted | What it does |
|---|---|---|---|
| `VERCEL_LOCAL_ENV_FILE` | empty |  | `scripts/vercel-local.sh` (your shell): the env file the local Vercel-style stack uses; empty = `.env.vercel.local` |
| `VERCEL_LOCAL_COMPOSE_FILES` | empty |  | `scripts/vercel-local.sh` (your shell): extra compose override files, space separated |
| `VERCEL_LOCAL_SYNC_WAIT_SECONDS` | `1200` |  | `scripts/vercel-local.sh sync` (your shell): how long to wait while another sync holds the lease (0 = exit 3 at once) |
| `VERCEL_LOCAL_DATABASE_URL` | empty |  | `.env.vercel.local`: a Neon development branch's pooled URL instead of the stack's own database |
| `VERCEL_LOCAL_DATABASE_URL_UNPOOLED` | empty |  | `.env.vercel.local`: that branch's direct URL |
| `VERCEL_LOCAL_REDIS_URL` | empty |  | `.env.vercel.local`: an Upstash database instead of the stack's own Redis |

Not in the tables, because they are not configuration of the app: the old alias names
WINDY_TERMS_ACKNOWLEDGED (of `WINDY_STORAGE_AUTHORIZED`) and POSTGRES_URL_NON_POOLING (of
`DATABASE_URL_UNPOOLED`); Vercel's system variables VERCEL and VERCEL_ENV (read by the build
command and `API_ROUTING`); the classic stack's image and backup variables in
`docker-compose.yml` and `backend/docker-entrypoint.sh` (BACKEND_IMAGE, FRONTEND_IMAGE,
RUN_MIGRATIONS, BULK_WORKER_CONCURRENCY and the BACKUP_* settings of the `backup` profile,
`docs/operations.md` section 4); the local stack's host ports (EDGE_PORT, VERCEL_PG_PORT,
VERCEL_PGBOUNCER_PORT, VERCEL_REDIS_PORT, VERCEL_MAILPIT_PORT in `.env.vercel.example`); and
the test runners' variables (TEST_DATABASE_URL, TEST_REDIS_URL, TEST_PGBOUNCER_URL,
LEAN_PYTHON, ALLOW_ANY_TEST_DB_NAME and the E2E_* variables of `scripts/run-e2e.sh`, README
section J).
