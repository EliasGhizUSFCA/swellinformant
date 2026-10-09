# Swell Travel Agent

> Instead of choosing a destination and hoping for waves, let the waves determine the destination.

## A. Project introduction

Swell Travel Agent watches the surf forecast at 50 of the world's best breaks. When good
waves are forecast about a week out, it finds flights that get you there in time and
emails or texts you the trip.

You save a search once: which waves you want (size, quality, wind), where you would go,
when you can travel, your budget and home airports. Background workers then run around
the clock, without the browser open:

1. fetch NOAA wave + wind forecasts for every spot (centrally, once for all users);
2. score each forecast hour for surf quality and estimate breaking wave height;
3. detect upcoming **swell events** 5–10 days ahead;
4. match events against each saved search;
5. search flights that land **before** the swell (default: arrive 2 days early, leave
   1 day after), within budget, with time zones handled properly;
6. send one alert per opportunity, with follow-ups only when something material
   changes (better forecast, price drop, dates moved).

Everything runs locally in **demo mode** with no API keys: synthetic forecasts and mock
fares, clearly labelled as such everywhere. Live providers switch on through environment
variables.

## B. Features

**Accounts and security**

* Email + password registration with email verification, login, logout, password reset
  and change, and account deletion.
* Argon2id password hashing.
* Server-side sessions in HttpOnly cookies; CSRF double-submit token.
* Redis rate limiting and per-account login lockout.
* Security headers / CSP; secrets and personal data redacted from logs.

**Surf data**

* Curated dataset of 50 spots with 153 airports (`data/surf_spots/`). Each spot has a
  swell window, optimal swell and wind directions, period, size range, tide preference,
  seasonality, hazards and transfer times.

**Forecast pipeline**

* Open-Meteo adapter (NOAA GFS + GFS-Wave) and Windy Point Forecast adapter.
* Deterministic demo generator.
* Model-run tracking, staleness detection, retention.

**Surf scoring**

* Documented 0–100 quality score (direction, period, size, wind, tide, consistency, plus
  limiting-factor gates). Labels Poor → Exceptional.
* Breaking-height estimate (Komar–Gaughan), always marked as uncalibrated.
* Separate forecast-confidence score.
* Daylight-only windows from NOAA sunrise and sunset.

**Swell detection**

* Multi-day events that update in place, merge and downgrade.
* A database constraint prevents overlapping active events at one spot.

**Saved searches**

* 6-step wizard: surf preferences, destinations, availability, flight budget,
  notifications, review.
* Edit, pause/resume, duplicate, delete.
* Flexible or fixed dates; all spots, chosen spots, or regions.

**Matching and flights**

* Per-search sub-windows; travel windows in real UTC instants.
* Duffel and Amadeus Enterprise adapters, plus a demo provider.
* Shared flight cache, daily quotas, budget/layover/duration/arrival-window filters.
* Rejection reasons and cheapest-over-budget fare shown in the UI.
* Offers re-validated before alerting.

**Alerts**

* Email (console / SMTP / Resend / SendGrid) and SMS (console / Twilio).
* De-duplication, follow-ups (improved, price drop, schedule change), daily cap.
* One-click unsubscribe; Twilio STOP handling; preferences re-checked at send time.

**Background jobs A–H**

* Celery worker and beat schedule.
* Redis job locks, retries with backoff, `acks_late`, a job log table and a status page.

**Web app** (Next.js)

* Landing page and auth pages.
* Dashboard; search list, wizard and detail.
* Live world map; spot detail with quality, swell and wind charts and a daily outlook.
* Opportunity detail with flight options and the travel timeline.
* Notification history, settings, and system status.

## C. Architecture

```
Browser ──▶ frontend (Next.js :3000) ──/api/* proxy──▶ backend (FastAPI :8000) ──▶ PostgreSQL 16
                                                          │      ▲
                                                          ▼      │
                                         Redis 7 (broker, locks, rate limits, quotas)
                                                          │
                       celery beat (schedule) ──▶ celery worker (jobs A–H) ──▶ Open-Meteo / Windy
                                                                         ──▶ Duffel / Amadeus
                                                                         ──▶ SMTP / Resend / SendGrid / Twilio
```

* The browser only talks to the Next.js server. It proxies `/api/*` to FastAPI, so
  cookies stay same-origin and **no API key or backend secret reaches the client bundle**.
* FastAPI handles auth, searches, spots, opportunities and webhooks. All business logic
  lives in `backend/app/services/` and is shared by the API, the Celery tasks and the
  tests.
* Monitoring runs only in Celery.
  * Beat triggers each job on its schedule, and also once at startup.
  * Each stage chains to the next when it succeeds: forecasts → quality → detection →
    matching → flights → alerts → delivery.

Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (jobs table and design
decisions), [`docs/SURF_SCORING.md`](docs/SURF_SCORING.md),
[`docs/MATCHING_AND_FLIGHTS.md`](docs/MATCHING_AND_FLIGHTS.md),
[`docs/FORECAST_SOURCES.md`](docs/FORECAST_SOURCES.md), and
[`data/surf_spots/README.md`](data/surf_spots/README.md) (dataset).

```
backend/    FastAPI app, SQLAlchemy models, Alembic migrations, services, Celery, tests
frontend/   Next.js 16 app (App Router), components, Vitest unit tests, Playwright e2e
data/       surf spot + airport dataset (JSON)
docs/       architecture and algorithm documentation
scripts/    e2e runner, test-database reset
```

## D. Prerequisites

**With Docker (recommended):** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
for Mac (Apple silicon or Intel) with Compose v2. Verified with Docker Engine 29.8 and
Compose 5.6. Allocate at least 4 GB of memory to Docker.

**Without Docker for the app processes** (you still need PostgreSQL and Redis):

| Tool | Version | macOS install |
|------|---------|---------------|
| Python | 3.12 or newer (verified with 3.12 in Docker and 3.13 locally) | `brew install python@3.12` |
| Node.js | 22 LTS (verified 22.22/22.23; Next.js 16 needs ≥ 20.9) | `brew install node@22` |
| PostgreSQL | 16 (needs the bundled `btree_gist` extension) | `docker compose up -d postgres` or `brew install postgresql@16` |
| Redis | 7 | `docker compose up -d redis` or `brew install redis` |

## E. Installation

### 1. Clone and configure

```bash
git clone https://github.com/EliasGhizUSFCA/swellinformant.git
cd swellinformant
cp .env.example .env        # optional: every value has a working demo default
```

`.env` is git-ignored. For demo mode you don't need to edit anything. To try the
developer tools described below (simulate a swell, read captured emails), set
`ENABLE_DEV_ENDPOINTS=true`.

### 2a. Everything in Docker (recommended)

```bash
docker compose up --build
```

On first start, the `backend` container:

* applies the Alembic migrations;
* seeds the 50 spots and 153 airports (`python -m app.cli init-db`, idempotent);
* reports healthy.

Then `worker`, `beat` and `frontend` start, and beat immediately queues a full pipeline
run. On an empty database the services are healthy after about 30 seconds (once the
images are built), and the map shows forecasts and swell events about 15 seconds later. No manual
database or seeding step is needed. To re-run them by hand:

```bash
docker compose exec backend python -m app.cli init-db     # migrations + seed (idempotent)
docker compose exec backend python -m app.cli pipeline    # run jobs A→G once, synchronously
```

### 2b. App processes on your Mac, PostgreSQL + Redis in Docker

```bash
docker compose up -d postgres redis

# backend
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m app.cli init-db          # create schema (Alembic) and seed spots/airports
python -m app.cli pipeline         # optional: fill forecasts/events right away
uvicorn app.main:app --reload --port 8000
```

In three more terminals:

```bash
cd backend && source .venv/bin/activate && celery -A app.workers.celery_app worker --loglevel=INFO
cd backend && source .venv/bin/activate && celery -A app.workers.celery_app beat --loglevel=INFO
cd frontend && npm ci && npm run dev
```

The backend reads `../.env` (the repository root) when started from `backend/`. The
default `DATABASE_URL` / `REDIS_URL` match the compose ports 5432 and 6379.

**Running without the worker.** Set `CELERY_TASK_ALWAYS_EAGER=true`. Jobs then run inside
the API process; this is used by the tests.

**Homebrew PostgreSQL instead of Docker.** Create the role and databases first:

```bash
psql postgres -c "CREATE ROLE swell LOGIN PASSWORD 'swell' CREATEDB"
createdb -O swell swell
createdb -O swell swell_test      # for the backend tests
```

A non-superuser database owner can create the `btree_gist` extension, since it is a
trusted extension in PostgreSQL 13+.

## F. Running the application

```bash
docker compose up --build            # foreground; Ctrl-C to stop
docker compose up --build -d         # background
docker compose ps                    # health of every service
docker compose logs -f worker beat   # watch the jobs
docker compose down                  # stop (keeps the database volume)
docker compose down -v               # stop and delete all data
```

| What | URL |
|------|-----|
| Web app | http://localhost:3000 |
| Backend API | http://localhost:8000 (health: `/api/health`, readiness: `/api/health/ready`) |
| API documentation (OpenAPI / Swagger UI) | http://localhost:8000/docs |
| System status (jobs, data freshness, provider modes) | http://localhost:3000/status |
| Mailpit inbox (optional) | http://localhost:8025, after `docker compose --profile mail up -d` with `EMAIL_PROVIDER=smtp` in `.env` |

### Demo walkthrough

1. Open http://localhost:3000 and register. In console mode, emails are not actually sent;
   they are written to the outbox. Get the verification link with:

   ```bash
   docker compose exec worker cat /tmp/swell-outbox/outbox.jsonl
   ```

   Or, with `ENABLE_DEV_ENDPOINTS=true`, open http://localhost:3000/api/dev/outbox while
   signed in. Without Docker, the file is `/tmp/swell-outbox/outbox.jsonl`.
2. **Searches → New search.** Pick, for example, Jeffreys Bay, home airport SFO, a
   $5,000 budget.
3. Force a swell to see the full flow:

   ```bash
   docker compose exec backend python -m app.cli simulate-swell jeffreys-bay --days-ahead 7
   docker compose exec backend python -m app.cli pipeline
   ```

   With dev endpoints enabled, `POST /api/dev/simulate-swell` does the same from the
   API docs page.
4. The dashboard shows the opportunity with **MOCK** fares. The outbox contains the alert
   email.

## G. API configuration

| Service | Variables | Where credentials come from | Demo alternative |
|---------|-----------|-----------------------------|------------------|
| Forecasts: Open-Meteo (NOAA GFS / GFS-Wave) | `FORECAST_PROVIDERS=open_meteo`, optional `OPEN_METEO_API_KEY` | No key for non-commercial use; commercial keys at open-meteo.com/en/pricing | `FORECAST_PROVIDERS=demo` (default) |
| Forecasts: Windy Point Forecast | `FORECAST_PROVIDERS=windy` (or `open_meteo,windy`), `WINDY_API_KEY` | api.windy.com/keys (paid Point Forecast plan) | — |
| Flights: Duffel | `FLIGHT_PROVIDER=duffel`, `DUFFEL_ACCESS_TOKEN` | app.duffel.com → Developers → Access tokens (`duffel_test_…` = sandbox) | `FLIGHT_PROVIDER=demo` (default) |
| Flights: Amadeus Enterprise | `FLIGHT_PROVIDER=amadeus`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `AMADEUS_BASE_URL` | Your Amadeus Enterprise account manager | — |
| Email: SMTP | `EMAIL_PROVIDER=smtp`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`/`SMTP_USE_SSL`, `EMAIL_FROM` | Your mail provider; Mailpit locally needs none | `EMAIL_PROVIDER=console` (default) |
| Email: Resend | `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `EMAIL_FROM` on a verified domain | resend.com → API keys | console |
| Email: SendGrid | `EMAIL_PROVIDER=sendgrid`, `SENDGRID_API_KEY`, `EMAIL_FROM` (verified sender) | app.sendgrid.com → Settings → API keys | console |
| SMS: Twilio | `SMS_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID` | console.twilio.com | `SMS_PROVIDER=console` (default) or `disabled` |

Inbound STOP/START: set your Twilio number's "A message comes in" webhook to
`POST ${API_BASE_URL}/api/webhooks/twilio/sms`. Requests are verified with
`X-Twilio-Signature`, so `API_BASE_URL` must be the exact public URL Twilio calls.

### Switching from demo to live

1. Put the credentials in `.env` (never commit it), for example:

   ```env
   FORECAST_PROVIDERS=open_meteo
   FLIGHT_PROVIDER=duffel
   DUFFEL_ACCESS_TOKEN=duffel_test_xxx
   EMAIL_PROVIDER=resend
   RESEND_API_KEY=re_xxx
   EMAIL_FROM=Swell Travel Agent <alerts@your-domain.com>
   ```

2. Check that nothing required is missing (exit code 1 lists the missing variables):

   ```bash
   docker compose run --rm backend python -m app.cli check-config
   ```

3. Restart: `docker compose up -d`. The demo ribbon disappears once neither forecasts
   nor flights are in demo mode. `/status` shows the active providers.

Notes:

* `demo` cannot be mixed with live forecast providers; the settings validator refuses
  to start.
* Forecast processing reads only the configured sources, so stored demo runs are
  ignored once you switch. Events, opportunities and mock fares created earlier from
  demo data would linger until the first live runs replace them. Start live mode on a
  fresh database: `docker compose down -v`, then `docker compose up --build`.
* Credentials are read only from environment variables and are used only by the
  backend and worker. The frontend never sees them.

## H. Running tests

Tests need PostgreSQL and Redis. With Docker:

```bash
docker compose up -d postgres redis
docker compose exec postgres createdb -U swell swell_test     # once
```

**Backend** (unit + integration). Integration tests use the real database `swell_test`
and Redis db 15, start a real Celery worker thread, and round-trip the Alembic
migrations on a scratch database:

```bash
cd backend
source .venv/bin/activate
pytest                          # all tests
pytest tests/unit               # pure unit tests, no database needed
ruff check . && ruff format --check .
mypy app
```

Override the test database with `TEST_DATABASE_URL` / `TEST_REDIS_URL` if needed.

**Frontend:**

```bash
cd frontend
npm ci
npm test                 # Vitest unit tests
npm run lint             # ESLint
npm run typecheck        # tsc --noEmit
npm run build            # production build
```

**End-to-end** (Playwright). The script:

* recreates the `swell_e2e` database;
* starts the backend on :8100 in demo mode;
* builds and starts the production frontend on :3100;
* runs the browser tests, including register → verify → login → create a search →
  simulated swell → mock flights → alert queued → alert sent → dashboard → pause →
  no new alerts.

```bash
cd frontend && npx playwright install chromium && cd ..   # once
./scripts/run-e2e.sh
```

`scripts/run-e2e.sh` expects the backend virtualenv at `backend/.venv` and frontend
dependencies installed. If Chromium is installed somewhere else, set
`PLAYWRIGHT_CHROMIUM_PATH=/path/to/chrome`.

**Regenerate frontend API types** after changing backend schemas. This writes
`frontend/types/openapi.json` and `openapi.d.ts`, including the dev routes:

```bash
./scripts/export-openapi.sh
```

## I. Debugging

| Symptom | Cause and fix |
|---------|---------------|
| `connection refused … 5432` / `/api/health/ready` returns 503 with `"database": "unavailable"` | PostgreSQL isn't running or `DATABASE_URL` is wrong. Run `docker compose up -d postgres` and `docker compose ps` (wait for *healthy*). Outside Docker, `DATABASE_URL` must use `localhost`; inside compose it is set to `postgres` automatically |
| `extension "btree_gist" is not available` during migration | A PostgreSQL build without contrib modules. Use the `postgres:16-alpine` image or Homebrew `postgresql@16`, which include it |
| Redis `Connection refused` / readiness shows `"redis": "unavailable"` | Run `docker compose up -d redis`. Rate limiting fails open, but Celery and job locks need Redis |
| Forecasts never appear / the spot page says "No forecast yet" | Check `/status` → *forecast.acquire*. `docker compose logs worker` shows provider errors. Live Open-Meteo errors (timeout, 429, 5xx) retry automatically; a 400 names the rejected variable or coordinate. Run `docker compose exec backend python -m app.cli pipeline` to see the result synchronously |
| "Forecast data is stale" banner | The last successful run is older than `FORECAST_STALE_HOURS`. Usually the provider is unreachable or beat isn't running. Alerts pause until fresh data arrives |
| Opportunity is "surf only" with "flight provider error" or "flight search failed" | Invalid or expired flight credentials (HTTP 401/403 are permanent errors and are not retried). Fix `DUFFEL_ACCESS_TOKEN` / `AMADEUS_*`, run `check-config`, then press **Refresh flights** on the opportunity |
| Opportunity says "quoted in EUR (no FX rate configured)" | Offers in another currency are never guessed into your budget. Set `FX_RATES_USD_JSON`, or use the budget currency the provider quotes |
| Backend exits with `ValidationError` on start | A setting is invalid (e.g. `APP_ENV=production` with the default `SECRET_KEY`, `demo` mixed with live providers, or an unknown provider). The message names the field |
| `Bind for 0.0.0.0:5432 failed: port is already allocated` | Another Postgres, Redis or app is using the port. Change the host port in `.env` (`POSTGRES_PORT`, `REDIS_PORT`, `BACKEND_PORT`, `FRONTEND_PORT`), or stop the local service (`brew services stop postgresql@16`) |
| Background jobs not executing | `docker compose ps` should show `worker` healthy and `beat` up. `docker compose logs beat` should list "Sending due task". `/status` shows each job's last run. A job stuck "running" holds its Redis lock until the lock TTL expires; restart the worker if it crashed. Outside Docker, remember the separate `celery … worker` and `celery … beat` processes |
| No verification / alert email | Console mode writes to the outbox (see the walkthrough). Real providers log `PermanentProviderError` with the provider's reason. Check `/notifications` for the status and last error |
| Pages show "The service is temporarily unavailable" / `/api/*` returns 500 | The Next.js server can't reach the backend: check `docker compose ps backend`. If the backend is up, `BACKEND_URL` is wrong. It is baked in at build time: rebuild the frontend after changing it (`docker compose build frontend`) |
| Map tiles blank | Tile host blocked by the network or CSP. Set `NEXT_PUBLIC_MAP_TILE_URL` and `MAP_TILE_HOSTS`, then rebuild the frontend |

## J. Deployment

A production deployment needs the same five processes:

| Component | Managed option |
|-----------|----------------|
| PostgreSQL 16 with `btree_gist` | AWS RDS / Aurora, Google Cloud SQL, Neon, Supabase, Crunchy Bridge |
| Redis 7 | ElastiCache, Memorystore, Upstash (TLS: use `rediss://` URLs), Redis Cloud |
| API (`backend` image, `api` role) | ECS/Fargate, Cloud Run (min instances ≥ 1), Fly.io, Render, Kubernetes |
| Worker + beat (same image, `worker` / `beat` roles) | **Persistent** containers: ECS services, Fly machines, Render background workers. Not serverless functions. Run **exactly one** beat instance; scale workers horizontally |
| Frontend (`frontend` image, or Vercel) | Any Node host; build with `BACKEND_URL` pointing at the API's private URL |

Checklist:

* `APP_ENV=production`, a long random `SECRET_KEY`, `COOKIE_SECURE=true`, and
  `ENABLE_DEV_ENDPOINTS=false`. The app refuses to start otherwise.
* Serve everything over HTTPS behind one domain. The Next.js server proxies `/api`, so
  `APP_BASE_URL` and `API_BASE_URL` can both be `https://your-domain`. Set
  `TRUSTED_PROXY_COUNT` to the number of proxies in front of the API, so rate limiting
  sees real client IPs.
* Run migrations once per release: `docker run … backend migrate`, or keep
  `RUN_MIGRATIONS=true` on a single API instance.
* Secrets come from the platform's secret manager as environment variables.
* Use commercial terms for Open-Meteo, live Duffel or Amadeus Enterprise, a verified
  sending domain (SPF/DKIM) for email, and a registered Twilio sender.
* Monitoring:
  * `/api/health/ready` for load balancers;
  * `background_job_logs` and `/api/system/status` for job health;
  * alert on `forecasts.acquire` failures and on stale data.
* Backups: managed Postgres snapshots. Redis holds only transient state (queue, locks,
  counters).

## K. Limitations

**Fully implemented and tested here**

* Accounts, sessions, CSRF, rate limits, account deletion.
* Spot dataset and seeding.
* Scoring, detection, matching, travel windows, filtering and ranking.
* Alert generation, de-duplication and delivery.
* All Celery jobs, plus the web app and its pages.
* The Docker Compose stack: verified building and running, including a real Celery
  worker delivering alerts.

**Demo-only**

* The demo forecast is synthetic. It is shaped like real swells but is not a
  forecast of anything.
* Demo fares are generated mock data.
* Both are labelled DEMO / MOCK in the UI, the API (`is_demo`, `is_mock`) and every alert.
* The `simulate-swell` tool works only with the demo forecast provider.

**Implemented but not verified against the real services** (outbound network to these
APIs was blocked in the build environment). Each adapter is unit-tested against
documented response shapes with mocked HTTP. Expect to adjust details on first live use.

| Service | Needs |
|---------|-------|
| Open-Meteo | nothing, or `OPEN_METEO_API_KEY` |
| Windy | `WINDY_API_KEY` |
| Duffel | `DUFFEL_ACCESS_TOKEN` |
| Amadeus Enterprise | `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` |
| Resend | `RESEND_API_KEY` |
| SendGrid | `SENDGRID_API_KEY` |
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |

SMTP is verified end to end: the Docker stack delivered a verification email to Mailpit.

**Not implemented**

* Booking or payment: the app links to the provider or a public search page.
* Surfline or other proprietary surf forecasts: no authorized API.
* Buoy or observation ingestion, and fitted per-spot calibration (the schema supports
  it; see SURF_SCORING.md §7).
* Social login, two-factor authentication, multi-language UI.
* Push notifications.
* Hotel or car search.
* An admin UI: use the API docs and the status page.

**Forecast and price uncertainty**

* Wave forecasts 5–10 days ahead routinely shift in timing (± a day) and size.
  Confidence is shown separately from quality for this reason.
* Breaking heights come from a generic shoaling formula with a hand-set spot factor.
  They can be wrong by a factor of 2 at reef and point breaks with unusual bathymetry.
* Weights, thresholds and labels are reasoned defaults, not fitted to observations.
* Fares change constantly. Every price is a quote with a timestamp, re-validated
  before alerting when the provider supports it, and may differ at booking time.
* Nothing here is travel, safety or financial advice. Check local conditions, hazards,
  visas and entry rules yourself.
