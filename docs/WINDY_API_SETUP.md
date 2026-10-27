# Windy Point Forecast API: activation without code changes

Windy is the preferred future forecast provider; Open-Meteo is the provider until Windy is
licensed, and stays the automatic fallback afterwards. The adapter is implemented
(`backend/app/services/forecasts/windy.py`) and switched on by configuration only. This page
is the owner's checklist: what to buy, what permission to obtain in writing, which variables
to set, and how to verify that the site really serves Windy data. Windy facts below were read
on 2026-10-27 from <https://api.windy.com/point-forecast/docs>,
<https://api.windy.com/point-forecast/pricing>, the
[terms of use](https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use)
and Windy staff answers on community.windy.com (quoted where they matter).

**Status here:** mock-tested only. This project has no Windy key, so no request to Windy was
made; every Windy response in the tests is synthetic, written from the documented format
(`backend/tests/windy_fixtures.py`). What only a paid key can show is listed under
[What is not verified yet](#what-is-not-verified-yet).

## 1. A key is not a licence

Windy's standard terms (in force since 1 September 2023) say the user may not "store,
extract, modify, distribute, use the weather data or other content of the Services, create
any weather works or databases derived therefrom", and "Redistribution of any part of the API
or the included data to any third party is forbidden." This app does exactly what those
sentences forbid: it stores every forecast run, computes surf scores from it (derived data),
keeps versions and publishes them to every visitor. The terms also require "significant added
value", forbid replicating Windy's services, and forbid offering the data "solely in the paid
part" of an application.

So the app uses Windy data only when **both** are true:

* `WINDY_API_KEY` is set (a Point Forecast key: "Keys generated for other services, e.g. Map
  Forecast or Webcams, are a distinct category, and do not work for the Point Forecast API");
* `WINDY_STORAGE_AUTHORIZED=true` (old name `WINDY_TERMS_ACKNOWLEDGED`, still read), which you
  set only after Windy has confirmed **in writing** that your use is allowed.

Without both, `WEATHER_PROVIDER=auto` uses Open-Meteo, and production refuses to start with
`WEATHER_PROVIDER=windy` (the message names what is missing, never the key).

### What the written permission must cover

Ask Windy (through the contact given on <https://api.windy.com/>) for a licence, or a written
amendment of the Professional terms, that explicitly allows:

1. **Storage**: keeping Point Forecast responses (normalized values per spot and time) in our
   database for the retention period (`RETENTION_FORECAST_DAYS`, 10 days by default, and the
   two newest runs per source beyond that), including previous versions.
2. **Derived scoring**: computing and storing surf-quality scores, swell events and travel
   opportunities from those values (derived data, kept in the prediction archive when
   `PREDICTION_ARCHIVE_ENABLED=true`).
3. **Display and distribution**: showing the values and the scores to every visitor of the
   public site and in alert e-mails to registered users (not only to paying users).
4. **Server-side use**: requests from our backend (a Vercel Function, optionally a Vercel
   Daemon) with one key, about 550 requests a day (section 6), and the caching of responses.
5. **Attribution**: how the logo and the credit line must appear (section 7).

Keep the confirmation with the deployment records. A forum remark by Windy staff suggesting a
backend that stores measurements (2025-06-12) is informal and is not a licence.

## 2. Testing keys: never production data

Windy's free **Testing** tier "Returns randomly shuffled and slightly modified data", is for
"Development purpose only, not intended for production", allows 500 requests a day, and its
terms say the trial version returns data "for random coordinates". Its responses carry this
warning (Windy staff example response, 2024-08-30):

> The trial API version is for development purposes only. This data is randomly shuffled and
> slightly modified.

The adapter checks **every** response for that warning. Testing data raise
`WindyTestingKeyError` with reason `windy_testing_key`: nothing is stored or published, the
refresh fails with that reason, the provider health records it and the next refresh runs on
Open-Meteo (section 9). A testing key is therefore safe to try with `provider-check`, but it
can never feed the site. Buy the Professional plan before activation.

## 3. Subscription and key

| Item | What Windy says |
|---|---|
| Plan | **Professional**, "990 € / year", an auto-renewing annual subscription collected through a third party |
| Requests | "10 000" a day, "can be increased by agreement" ("usually for the same price", Windy staff, 2026-09-01); the terms count "Sessions" |
| Daily reset | "The reset of counter is at 0:00 UTC." (Windy staff, 2023) |
| Models | `gfs` and `gfsWave` are in both tiers; ECMWF "is not included in point forecast due to licensing conditions" |
| Time steps | "API offer 3 hours intervals/steps" (Windy staff, 2026): no hourly data |
| Key types | "API Keys for Public Applications may be used only from User Domains"; a private-application key must be kept confidential |

1. Create an account at <https://api.windy.com/>, buy the Professional Point Forecast plan.
2. Create a **Point Forecast** key. Prefer a private (server-side) key: the key is used only
   by our backend and never sent to browsers. If only a public-application key is possible,
   register the production domain; whether a domain-restricted key works for server-side
   calls without a browser `Origin` is not documented, and the health check (section 5) shows
   it at once.
3. Store the key only in the hosting platform's secret settings, never in the repository,
   never under a `NEXT_PUBLIC_` name.

## 4. Environment variables

Set these on the **API service** (and on the optional sync Daemon, if one runs) in Vercel
(Project → Settings → Environment Variables, scope Production; mark the key as Sensitive),
or in `.env` for docker compose:

| Variable | Value | Notes |
|---|---|---|
| `WINDY_API_KEY` | your Point Forecast key | secret; server only |
| `WINDY_STORAGE_AUTHORIZED` | `true` | only after the written permission of section 1 |
| `WEATHER_PROVIDER` | `auto` (hosted default) or `windy` | `auto` uses Windy once it is eligible; `windy` makes production refuse to start while it is not |
| `WINDY_CHECK_MINUTES` | `15` (default, 5-360) | how often the sentinel check may run inside a model's availability window (section 8) |
| `WINDY_DAILY_REQUEST_LIMIT` | `10000` (default) | the app's own counter, reset 00:00 UTC; set it to your plan's limit, lower to keep a reserve |
| `FORECAST_PROVIDERS` | leave **unset** | the advanced override; when set it replaces the `WEATHER_PROVIDER` choice |

Keep the Open-Meteo settings in place: they serve the fallback. A commercial site also needs
`OPEN_METEO_API_KEY` and `OPEN_METEO_COMMERCIAL=true` (old name `COMMERCIAL_USE`) for the
fallback to run lawfully (docs/FORECAST_SOURCES.md, "Open-Meteo: free and commercial use").

## 5. Redeploy and run the provider health check

1. Redeploy (Vercel: Deployments → Redeploy, or push to the production branch; docker
   compose: `docker compose up -d` again). Settings are read at start.
2. Run the provider check with the production Windy values, from the backend directory of a
   checkout (`backend/.venv`, README "Local development"). Enter the key without writing it
   to the shell history or to a file:

   ```bash
   cd backend
   read -rs WINDY_API_KEY && export WINDY_API_KEY
   FORECAST_PROVIDERS= WEATHER_PROVIDER=auto WINDY_STORAGE_AUTHORIZED=true \
     .venv/bin/python -m app.cli forecast provider-check --provider windy
   unset WINDY_API_KEY
   ```

   Without a reachable Redis it still runs (it then shows no recorded health).

   It prints JSON and stores nothing (no forecast data, no health record); it sends **one**
   request to Windy (it counts against `WINDY_DAILY_REQUEST_LIMIT`) and never prints the key.
   Expect:

   * `selection.providers` = `["windy"]`, `selection.windy_eligible` = `true`,
     `selection.fallback` = `"open_meteo"`;
   * `health_checks[0].status` = `"ok"` with `info.steps` (about 80 three-hourly steps for 10
     days of `gfs`, per a Windy staff example), `info.units` and `info.fingerprint`.

   Other statuses: `testing_key` (a trial key, section 2), `credentials` (invalid key: Windy
   answers HTTP 400 `{"message":"Invalid API key"}` according to a user report, or 401/403),
   `quota` (HTTP 429 or a "limit" message, or our own counter), `unavailable` (timeouts,
   5xx), `not_configured` (no key). Without `--provider` the command checks the active primary
   and its fallback (`python -m app.cli forecast provider-check`); `--provider open_meteo`
   reads one Open-Meteo `meta.json`. Exit status 0 only when every checked provider is ok.

## 6. Trigger a test refresh

The scheduler calls the refresh endpoint every few minutes (docs/sync.md); to see Windy at
once, call it yourself with the cron secret:

```bash
curl -fsS -X POST -H "Authorization: Bearer $CRON_SECRET" https://<your-app>/api/internal/refresh
```

The answer is a JSON summary (`status`, the plan, the sync run, `version_ids`, timings). The
first Windy refresh fetches both components for every active spot: one `gfsWave` request at
the spot's offshore forecast point and one `gfs` request at the break, **100 requests for the
50 spots** (Windy has no multi-location request). Later refreshes fetch only the component
whose run changed: 50 requests.

**Request budget at the defaults** (computed, not measured):

| What | Requests a day |
|---|---|
| Sentinel checks: every 15 min for 4 h after each of the 4 GFS cycles, every 60 min otherwise, per model | 2 × (4 × 16 + 8) = 144 |
| Component refreshes: about 4 new `gfs` and 4 new `gfsWave` runs a day × 50 spots | about 400 |
| End-of-fetch check of a retry that does not include the sentinel spot | 1 per component |
| Total | **about 550**, under 6 % of the 10,000 a day |

A testing key's 500 a day would last for about one day of checks and refreshes, but testing
data are refused anyway.

## 7. Attribution

When Windy data are served, the published version carries the credit line **"Contains data
from the Windy database"** (`GET /api/forecasts/version` → `attribution`; the source's own
attribution says the same), and the pages show the version's attribution with the forecast.
The terms also require the **Windy logo**: "the Logo is
present in the User Applications and remains as it is, unscaled and clickable with a
hyperlink leading to the Provider's Website. The opacity of the Logo must remain 100%." The
logo is Windy's asset and is not in this repository: obtain it from Windy with the licence
and add it next to the credit line (owner step; the frontend shows the text attribution from
the version). Credit the data source as well: Windy's `gfs` and `gfsWave` are NOAA NCEP GFS and
GFS-Wave (WAVEWATCH III), named in the source's attribution.

## 8. How new Windy runs are detected

The Point Forecast response has only `ts`, `units` and an optional `warning`: no model run,
reference time or update time, and Windy documents no metadata endpoint or webhook ("Point
forecast returns exclusively the most recent forecast values"). Staff say the API updates at
the same times as windy.com and that gfsWave cannot update more often than every 6 hours. So:

* **Sentinel fingerprints.** One sentinel point per model (the Pipeline spot: its break for
  `gfs`, its offshore point for `gfsWave`) is requested at most every `WINDY_CHECK_MINUTES`
  from 4 h to 8 h after each 00/06/12/18Z cycle (the expected availability window) and four
  times less often outside it. The fingerprint is the sha256 of `ts` and the first 24 values
  of every requested series; it is kept in Redis (`windy:run-state:v1:<model>`), so an idle
  check reads no database and sends no request.
* **A changed fingerprint is a new run** of that component only: wind and waves are detected
  and refreshed independently. Run keys look like
  `gfsWave~3fa2-c1d0-9b7e:2026102706|gfs~81bd-04aa-7c19:2026102700`.
* **Run times are estimated**: the newest cycle whose expected availability (cycle + 4 h) has
  passed, never older than the previous run; a change seen 3-4 h after a cycle counts as that
  cycle. The run records `estimated = true` and every page that shows it says "estimated".
* **A fetch is checked at its end**: the sentinel spot is fetched last (otherwise one more
  request per component) and its fingerprint must still be the run's. Data that changed
  during the fetch are not stored; the refresh retries a minute later.
* The 4 h and 8 h are **estimates**: NOAA's GFS output is complete roughly 3.5-5 h after the
  cycle (Open-Meteo listed GFS-Wave 0.16° of 2026-10-27 06Z as available at 11:15 UTC, 5.3 h
  after the cycle, in the provider check run while writing this page), and Windy's own delay
  is unmeasured. With a paid key, record when the fingerprint changes for a few days and
  adjust `EXPECTED_LAG_HOURS` / `WINDOW_HOURS` in `windy.py` if needed.

## 9. Verify the publication

After the test refresh:

* `curl -fsS https://<your-app>/api/forecasts/version` shows `"provider": "windy"`,
  `"source_code": "windy_gfswave"`, `"wind_model": "gfs"`, `"wave_model": "gfsWave"`,
  `"wind_run_estimated": true`, `"wave_run_estimated": true` and the Windy credit in
  `attribution`;
* the admin freshness page (`/admin/freshness`, admins only) shows the active provider
  `windy`, no fallback, both model runs (estimated) and the Windy requests used today against
  `WINDY_DAILY_REQUEST_LIMIT`;
* open a spot page: the forecast-updated label and the attribution change within the
  browser's version poll (about 20 s) without a reload.

## 10. Fallback and rollback

**Automatic fallback.** While Windy is the primary, the provider manager
(`backend/app/services/forecasts/provider_manager.py`) builds Open-Meteo instead, with the
same normalized records, when Windy:

| Failure | Recorded as | Fallback holds |
|---|---|---|
| invalid, expired or wrong-type key (HTTP 400 "Invalid API key", 401, 403) | `credentials` | 60 min, then Windy is tried again |
| testing-key data | `testing_key` | 60 min, then tried again |
| HTTP 429, a "limit" message or `WINDY_DAILY_REQUEST_LIMIT` reached | `quota` | until 00:00 UTC (Windy's reset), at least 60 min |
| timeouts or 5xx after the retries, 3 times in a row | `unavailable` | 60 min |
| `WINDY_API_KEY` removed | (settings) | until it is set again |

A successful Windy check or fetch ends the fallback at once. The provider health is kept in
Redis (`provider-health:v1:windy`), so a Redis outage means Windy is simply tried. A version
published during the fallback records `provider` `open_meteo` and `fallback_from` `windy`, and
the admin alerts (docs/operations.md) report `provider_fallback` and, for a key problem,
`provider_credentials`.
`python -m app.cli forecast provider-check` shows the fallback, its reason and until when.

**Rollback to Open-Meteo** (licence ended, key revoked, or Windy unwanted): set
`WEATHER_PROVIDER=open_meteo` (or remove `WINDY_STORAGE_AUTHORIZED` / `WINDY_API_KEY`) and
redeploy. The next refresh publishes an Open-Meteo version; nothing else changes. Stored Windy
runs age out with the normal retention; if the licence requires deleting them sooner, delete
the runs of the `windy_gfswave` source. Check with `provider-check` and
`/api/forecasts/version` (`"provider": "open_meteo"`).

## What is not verified yet

Only a paid key can show these; until then they are open:

* Windy's delay between a GFS cycle and its availability in the Point Forecast API, and
  whether the fingerprint changes once per cycle (the window above is an estimate).
* Whether paid-key responses are deterministic for identical requests (an unanswered forum
  report of 2025-11-07 saw very different values seconds apart, perhaps from a trial key).
* The HTTP status and body of an exceeded daily limit and of an expired subscription (only
  the invalid-key body is known, from a user report); the adapter treats 429 and any 4xx
  "limit" message as the daily limit.
* Whether `pressure-surface` ("Air pressure") is reduced to sea level: it is stored as
  `pressure_msl_hpa` and labelled `extra.pressure_kind = "surface_unspecified"`.
* Which period definition `waves_period` is (stored with `period_kind` unknown), and the
  `gfsWave` horizon (the staff example shows 10 days of `gfs`).
* Whether a domain-restricted public key works for server-side calls.
* What Windy's licence for storage, derived scoring and publication costs.
