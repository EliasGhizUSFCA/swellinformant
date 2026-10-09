# Forecast and travel data sources

Every source below is an official, documented API, used within its terms. Nothing in this
project scrapes websites, bypasses anti-bot systems or calls private endpoints.

## Forecasts

| Source | Status in this repo | Data | Access |
|--------|---------------------|------|--------|
| **Open-Meteo**, serving **NOAA GFS** + **NOAA GFS-Wave** (WAVEWATCH III, 0.25°) | Implemented (`forecasts/open_meteo.py`). Unit-tested against recorded-shape responses with `httpx.MockTransport`. **Not exercised live in this build environment** (outbound network to the API is blocked here) | Hourly, up to 16 days: combined sea, primary/secondary swell, wind sea, 10 m wind + gusts, MSL pressure, sea level (tide proxy) | Free for non-commercial use without a key; `OPEN_METEO_API_KEY` switches to the commercial `customer-*` hosts |
| **Windy Point Forecast API v2** | Implemented (`forecasts/windy.py`), mock-tested only | `gfsWave` waves/swell1/swell2/wind waves, `gfs` wind/gust/pressure | Paid key, `WINDY_API_KEY`. Trial keys return randomised data and must never drive real alerts |
| **Demo provider** | Implemented, default | Synthetic but physically plausible (swell pulses with dispersion, diurnal winds, M2+S2 tides, lead-time-dependent perturbation) | None. Labelled DEMO in the UI, API and every alert |
| NOAA NOMADS GRIB2 directly | Not implemented | Same models as above | Would need GRIB tooling (eccodes/cfgrib). Open-Meteo provides the same NOAA output as JSON with multi-point batching |
| Copernicus Marine (CMEMS) global wave analysis/forecast | Not implemented | 10-day wave forecast | Free account; a good second model for run-to-run agreement. Adapter interface (`ForecastProvider`) is ready for it |
| Surfline | **Not integrated** | — | No public or partner API is available to this project. Scraping it would violate its terms, so it is not used |

### How ingestion works (job A)

* **One centralised fetch** for all active spots, independent of how many users exist.
  Spots are batched (`FORECAST_BATCH_SIZE`, default 10 coordinates per Open-Meteo request).
* Each spot is forecast at `forecast_latitude/longitude`, a point just offshore in open
  water. Nearshore grid cells are often land-masked or sheltered in a 0.25° wave model.
* **Model run time.**
  * Open-Meteo: read from `/data/{model}/static/meta.json`
    (`last_run_initialisation_time`). If that is unavailable, the nominal GFS cycle is
    used (00/06/12/18 UTC with a ~5 h publication delay).
  * The run key (`ncep_gfswave025:2026100906|gfs_seamless:2026100906`) makes ingestion
    idempotent. A run already stored is skipped.
* **Failure isolation.**
  * Optional variables are dropped and retried if the API rejects them.
  * When a batch is rejected (HTTP 400), its spots are retried one by one, so one bad
    coordinate cannot block the other 49.
  * Transient errors (timeouts, 429, 5xx) retry with backoff. Celery also retries the
    whole job (3×, exponential backoff).
  * A failed run never replaces the last good data.
* **Quota.** `FORECAST_DAILY_REQUEST_QUOTA` is a daily counter of provider requests
  (Redis).
* **Staleness.** A spot's latest run older than `FORECAST_STALE_HOURS` (18) is flagged as
  stale:
  * the UI shows a warning;
  * confidence is multiplied by 0.8;
  * alerts are not generated from stale data.
  * The `forecasts.check_staleness` job and `/api/system/status` report it.
* **Retention.** Forecast runs older than `RETENTION_FORECAST_DAYS` are deleted by the
  nightly maintenance job, along with their records and predictions. The two newest successful runs per
  source are always kept, because confidence compares consecutive runs.

### Switching to live forecasts

```env
FORECAST_PROVIDERS=open_meteo          # or "open_meteo,windy" (first = primary)
OPEN_METEO_API_KEY=                    # optional; required for commercial use
WINDY_API_KEY=                         # only if "windy" is listed
```

`demo` cannot be combined with a live provider. The settings validator rejects that, so
synthetic and real data never mix.

Open-Meteo's free API is for **non-commercial** use, with daily, hourly and per-minute
fair-use limits. Check open-meteo.com/en/terms before deploying. A deployment that
charges users needs a commercial API key. With 50 spots, one acquisition is about 15 HTTP
requests (5 batches × 3 endpoints) plus 2 metadata calls. Open-Meteo, however, *counts*
each coordinate, and requests with many variables or more than two weeks of data, as
additional calls. At the default 3-hour refresh, expect a few thousand counted calls per
day. Lower `FORECAST_DAYS` or raise `FORECAST_REFRESH_MINUTES` if that is too much.

## Flights

| Provider | Status | Notes |
|----------|--------|-------|
| **Duffel** (`FLIGHT_PROVIDER=duffel`) | Implemented: offer requests, offer revalidation (`GET /air/offers/{id}`), booking link. Mock-tested, **not exercised live here** | Self-serve signup. `duffel_test_…` tokens hit Duffel's sandbox ("Duffel Airways" test data); live tokens need an activated account. Duffel's pricing and search-to-book limits apply; check them before going live |
| **Amadeus Enterprise** (`FLIGHT_PROVIDER=amadeus`) | Implemented: OAuth2 client credentials, Flight Offers Search, Flight Offers Price (`/v1/shopping/flight-offers/pricing`) for revalidation. Mock-tested only | Amadeus Self-Service has been discontinued (see ARCHITECTURE.md), so this needs an Enterprise agreement. `AMADEUS_BASE_URL` is configurable |
| **Demo** (`FLIGHT_PROVIDER=demo`) | Default | Deterministic mock offers. `is_mock = true`, IDs prefixed `demo_`, a "Mock fares" badge in the UI and a "MOCK FARE" note in every alert |
| Skyscanner, Google Flights, Kiwi | Not integrated as data sources | No openly available API for this project. Only ordinary public search URLs are generated as "compare" links |

Mock offers are never presented as real: they carry `is_mock`, a separate provider code and
explicit UI and alert labelling. The app never fabricates fares for a live provider. If a
live provider fails or returns nothing, the opportunity becomes "surf only" with the reason
shown.

## Notifications

| Provider | Setting | Status |
|----------|---------|--------|
| Console (writes to `OUTBOX_DIR/outbox.jsonl` + log line) | `EMAIL_PROVIDER=console`, `SMS_PROVIDER=console` | Default; fully tested |
| SMTP (e.g. local Mailpit, Postmark SMTP, SES SMTP) | `EMAIL_PROVIDER=smtp` + `SMTP_*` | Implemented; unit-tested with a fake SMTP server and verified end to end against Mailpit in Docker Compose |
| Resend | `EMAIL_PROVIDER=resend` + `RESEND_API_KEY` | Implemented; mock-tested only |
| SendGrid | `EMAIL_PROVIDER=sendgrid` + `SENDGRID_API_KEY` | Implemented; mock-tested only |
| Twilio SMS | `SMS_PROVIDER=twilio` + `TWILIO_*` | Implemented (send + signature-verified inbound STOP/START webhook); mock-tested only |
