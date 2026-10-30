# Swell Travel Agent

> Instead of choosing a destination and hoping for waves, let the waves determine the destination.

## A. Project introduction

Swell Travel Agent watches the surf forecast at 50 of the world's best breaks. When good
waves are forecast about a week out, it finds flights that get you there in time and
emails or texts you the trip.

You save a search once: which waves you want (size, quality, wind), where you would go,
when you can travel, your budget and home airports. Background workers then run around
the clock, without the browser open:

1. detect each new weather model run automatically (a free metadata check every few
   minutes) and fetch wave and wind forecasts for every spot centrally, once per model run
   for all users: small Open-Meteo point requests (NOAA GFS and GFS-Wave; Windy once it is
   licensed), the wave and the wind each as soon as its own run is available, plus tide
   predictions; then publish a new forecast version that open pages pick up by themselves;
2. turn the offshore forecast into **our prediction** of breaking wave height at each break
   (a physical nearshore model with an uncertainty range) and a separate, spot-specific
   surf-quality score;
3. detect upcoming **swell events** from 1 to 10 days ahead (each saved search sets how far
   ahead it wants alerts, 2 to 10 days by default);
4. match events against each saved search;
5. search flights that land **before** the swell (default: arrive 2 days early, leave
   1 day after), within budget, with time zones handled properly;
6. send one alert per opportunity, with follow-ups only when something material
   changes (better forecast, price drop, dates moved).

The Docker Compose stack runs in **demo mode** with no API keys: synthetic forecasts and
mock fares, clearly labelled as such everywhere. Live providers switch on through
environment variables; the recommended live forecast setup needs no key for non-commercial
use (section F).

**What changed in Swell 2.0 (2026-10).** The forecasts now follow the weather models
automatically: a refresh check every 5 minutes reads only the providers' run metadata, and
when a new wave or wind run is available it refreshes just that component for the 50 spots,
keeps the other with its original model time, scores, publishes a **versioned** forecast and
re-runs detection, matching, flight searches and alerts (section F.2). Open pages notice the
new version within about 20 seconds and update the map markers, charts and lists without a
reload ("Forecast updated 14:32 · just now"). One weather provider (Open-Meteo GFS +
GFS-Wave now, Windy once licensed, by configuration only) replaces the multi-model blend;
each of the 50 spots has sourced research conditions with a research confidence kept apart
from the forecast confidence; flight searches are targeted per saved search with its own
alert lead time; admins get a freshness dashboard (`/admin/freshness`) and email alerts. The
architecture and what was built: [`docs/architecture_audit.md`](docs/architecture_audit.md);
the results and the remaining limits: [`docs/UPGRADE_REPORT.md`](docs/UPGRADE_REPORT.md),
"Swell 2.0", and section M.

What changed in each upgrade, with the measurements behind it:
[`docs/UPGRADE_REPORT.md`](docs/UPGRADE_REPORT.md). The forecasting upgrade of 2026-10 started
from the audit in [`docs/forecast_upgrade_audit.md`](docs/forecast_upgrade_audit.md).

**What changed in upgrade 4 (2026-10).** The app can now run as a website on **Vercel** (the
free Hobby plan for now), and you can test it on your own machine first in a Docker stack
that copies Vercel's layout (section E.6). The weather data is pulled **only when a forecast
model publishes a new run**: one coordinated sync fetches the point forecasts for the 50
surf spots (and only those) and the worldwide map layers of that run, scores them and
publishes them together, instead of fixed 3-hourly timers (section F.2). The wave layer
follows the owner's screenshot: a smooth blue → violet → magenta → pink wave-height field
and many short, soft crest dashes drifting with the waves (section H). A paywall later is a
configuration change of the hosting (Vercel Pro, an Open-Meteo key, Neon Launch); the
payment part itself is not built. The owner checklist (free now, paywall later) and the
evidence are in [`docs/UPGRADE_REPORT.md`](docs/UPGRADE_REPORT.md), "Upgrade 4". (Swell 2.0
replaced the coordinated sync for the spot forecasts with per-component refreshes and
versions, above.)

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

**Forecast pipeline** (section F)

* One weather provider for the spot forecasts (`WEATHER_PROVIDER=auto`): Open-Meteo's NOAA
  GFS + GFS-Wave point forecasts now, the Windy Point Forecast adapter once its licence
  allows storing the data (`WINDY_STORAGE_AUTHORIZED`), Open-Meteo as its fallback, and a
  demo generator. Automatic model-run detection, per-component refreshes (wave and wind
  independently) and versioned publication (section F.2).
* Advanced and off by default: Open-Meteo's ECMWF family and a two-family blend (no equal
  averaging; wind direction from the mean wind vector), the GEFS-Wave ensemble spread, and
  direct NOAA GFS-Wave + GFS and ECMWF IFS ingestion (GRIB2 by HTTP byte range, no keys) as
  a key-free fallback.
* Model-run tracking, per-source and per-spot staleness, retention, versioned publication
  (`forecast_versions`), and an optional immutable archive of every prediction issued, for
  verification at each lead time (`PREDICTION_ARCHIVE_ENABLED`: off by default since Swell
  2.0, on in the full-retention profile).
* Tides: NOAA CO-OPS predictions at the 8 US spots and EOT20 model tides at the other 42
  (relative to the model's mean sea level, not a chart datum), no keys.

**Spot forecast** (section G)

* One 10-day timeline whose rows share one time axis: our surf quality, our estimated
  breaking height with its likely range, the model's offshore wave systems, wind and the
  tide, with one selected time for every row.

**Surf prediction and scoring** (algorithm 2.2.0, [`docs/SURF_SCORING.md`](docs/SURF_SCORING.md))

* Breaking height from linear wave theory + Komar–Gaughan per swell partition, with
  directional spreading, shore normal, shadowing and a focusing prior; spot physical
  profiles carry a provenance flag for every parameter.
* Wind scored from the offshore component V·n against each spot's researched offshore
  direction: calm and light offshore score best, strong offshore and onshore lose points, and
  cross-shore wind gets worse with speed.
* An 80% range (p10–p90) labelled heuristic (or ensemble-based) until it is verified. Its
  offshore part was checked against NDBC buoys: it held the observed wave height 53 % of the
  time at 24 h and 85-89 % beyond 5 days instead of 80 % (most misses were above the range:
  the raw model runs low). A buoy-fitted offshore range (`ndbc-offshore-v1`) exists in the
  code but is not used by the prediction job yet
  ([`docs/ml-evaluation.md`](docs/ml-evaluation.md) §12.2).
* A documented 0–100 quality score kept separate from height and from forecast confidence
  (shown as "Excellent Surf — 8.9/10" with a "Why this rating?" breakdown).
* Daylight-only windows from NOAA sunrise and sunset.

**Two separate confidence assessments** (never merged into one number, spec 20)

* *Research confidence* per spot and per condition (swell direction, size, period, wind,
  tide, bottom, season, how the wave breaks): High / Moderate / Low from cited sources,
  their independence, type, agreement and age, with the gaps in plain language
  ([`docs/research-confidence.md`](docs/research-confidence.md), methodology research-v1.2).
  The committed evidence covers all 50 spots: 468 sources (403 read directly, 65 seen only
  as search summaries), 1,730 claims, including NOAA BlueTopo bathymetry for four US spots;
  35 spots are Moderate and 15 Low, none High.
* *Evidence-based spot profiles*: every profile value contradicted by the evidence has a
  decision record (changed / kept / disputed, with sources and the effect on a real run;
  [`data/surf_spots/profile_decisions.json`](data/surf_spots/profile_decisions.json)), a
  value changes only when two independent, directly read publishers outrank all direct
  support for the old one, and `python -m app.cli research lint` flags unexplained
  geometry. Where sources disagree about which wind is offshore, every forecast hour says so,
  and the app shows that note next to the wind.
* *Forecast confidence* per forecast step: lead time, ensemble spread, disagreement between
  independent model families only (wave height and wind direction), run-to-run change, data
  completeness, staleness and source
  ([`docs/forecast-confidence.md`](docs/forecast-confidence.md)). Documented classes, not
  calibrated probabilities. Research confidence and forecast confidence are shown side by
  side and never combined: the older combined class ("local confidence", the lower of the
  two) is no longer served by the API or shown on any page (Swell 2.0); the scoring job still
  records it in the prediction archive with every other assessment.

**Interactive map** (section H)

* Full-screen dark map with NOAA ocean layers: wave height, wave energy and wave period as
  colour fields with stylised wave crests that move the way each wave system travels, wind
  with animated particles, optional isobars, a timeline over the model's valid times, a
  legend and settings. Spot markers stay coloured by our surf prediction.

**Verification and ML post-processing**

* Buoy observations (AODN, Met Office, NDBC) and the GFS-Wave forecasts issued at those
  buoys, archived as issued, so accuracy is measured at every lead time without hindsight.
* Metrics (bias, MAE, RMSE, scatter index, HH, correlation, skill vs persistence and
  climatology, cycle-block bootstrap CIs) and rolling-origin backtests.
* An ML correction of offshore Hs with leakage guards, baselines, a model registry and
  explicit deployment criteria; a model is applied only if it beats the raw forecast on
  held-out observations.
* Accuracy dashboard (`/status/accuracy`, admin accounts): MAE/RMSE/bias by model, station
  and lead, scatter plots, error distributions, history and the model registry.

**Swell detection, saved searches, matching and flights, alerts**

* Multi-day swell events that update in place, merge and downgrade; a database constraint
  prevents overlapping active events at one spot.
* A 6-step search wizard (surf preferences, destinations, availability, flight budget,
  notifications, review); edit, pause/resume, duplicate, delete; flexible or fixed dates;
  all spots, chosen spots, or regions.
* Per-search sub-windows; travel windows in real UTC instants. Duffel and Amadeus
  Enterprise adapters plus a demo provider; shared flight cache, daily quotas,
  budget/layover/duration/arrival-window filters; rejection reasons and the cheapest
  over-budget fare shown in the UI; offers re-validated before alerting.
* Email (console / SMTP / Resend / SendGrid) and SMS (console / Twilio); de-duplication,
  follow-ups (improved, price drop, schedule change), a daily cap; one-click unsubscribe,
  Twilio STOP handling, preferences re-checked at send time.

**Background jobs and web app**

* Celery worker and beat schedule (classic stack); on Vercel the refresh endpoint called by a
  scheduler every 5 minutes, a daily cron and an optional daemon; Redis job locks, retries
  with backoff, `acks_late`, a job log table and a status page.
* Next.js web app: landing and auth pages, dashboard, searches, the world map, spot pages,
  opportunities with flight options and the travel timeline, notification history,
  settings, system status, and the admin freshness dashboard (`/admin/freshness`). Open
  pages pick up a new forecast version by themselves, without a reload.

## C. Architecture

```
Browser ──▶ frontend (Next.js :3000) ──/api/* proxy──▶ backend (FastAPI :8000) ──▶ PostgreSQL 16
                                                          │      ▲
                                                          ▼      │
                                         Redis 7 (broker, locks, rate limits, quotas, small cache)
                                                          │
                       celery beat (schedule) ──▶ celery worker ──▶ Open-Meteo (spot forecasts)
                                                  queues: celery  ──▶ NOAA NODD (GEFS-Wave, map layers)
                                                          bulk    ──▶ NOAA CO-OPS, NDBC (tides, buoys)
                                                                  ──▶ Duffel / Amadeus
                                                                  ──▶ SMTP / Resend / SendGrid / Twilio
```

* The browser only talks to the Next.js server. It proxies `/api/*` to FastAPI, so
  cookies stay same-origin and **no API key or backend secret reaches the client bundle**.
* FastAPI handles auth, searches, spots, opportunities and webhooks. All business logic
  lives in `backend/app/services/` and is shared by the API, the Celery tasks and the
  tests.
* Background work runs in one of two runners (`JOB_RUNNER`). In this Docker Compose stack
  it is Celery (`celery`): beat runs the sync check (`sync.tick`, every
  `SYNC_CHECK_MINUTES`) and the periodic jobs, and the worker runs them. On Vercel the spot
  refresh runs in the API function, `GET|POST /api/internal/refresh`, called every 5 minutes
  by a scheduler, and an optional long-lived **sync daemon** (`python -m app.cli sync
  daemon`, `daemon`) refreshes the map layers. A new model run of a component (wave or wind)
  goes through: fetch that component → carry the other forward → scores → stage and publish
  a forecast version → detection → matching → flights → alerts → delivery
  ([`docs/sync.md`](docs/sync.md)). Data downloads (`forecasts.*`, `tides.*`,
  `observations.*`, `verification.*`, `sync.tick`) run on the `bulk` queue, everything else
  (emails, alerts, matching) on `celery`.

**Hosted on Vercel** (Upgrade 4, Swell 2.0; [`docs/deploy-vercel.md`](docs/deploy-vercel.md)):

```
Browser ──▶ Vercel ──/api/*──▶ service "api" (FastAPI as one Python function) ──▶ Neon Postgres (pooled URL)
   │          │                                                    └────────▶ Upstash Redis (KV_URL)
   │          └── everything else ──▶ service "web" (Next.js)
   └── every 20 s while visible: GET /api/forecasts/version (CDN-cached 15 s) → new version → re-read the data
QStash (Hobby) or Vercel Cron (Pro), every 5 min ──▶ POST /api/internal/refresh (Bearer CRON_SECRET):
     metadata check; on a new wave or wind run: refresh that component, publish a version, match, alert
Vercel Cron, once a day ──▶ GET /api/internal/cron/daily (data health, overdue periodic jobs, refresh backstop)
daemon "sync" (optional) ──▶ the GRIB map layers (SYNC_DAEMON_KINDS=map) ──▶ NOAA NODD
```

Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (jobs table and design
decisions), [`docs/SURF_SCORING.md`](docs/SURF_SCORING.md),
[`docs/MATCHING_AND_FLIGHTS.md`](docs/MATCHING_AND_FLIGHTS.md),
[`docs/FORECAST_SOURCES.md`](docs/FORECAST_SOURCES.md),
[`docs/operations.md`](docs/operations.md) (capacity, monitoring, backups),
[`docs/decisions.md`](docs/decisions.md) (architectural decision records),
[`docs/surf-forecast-model-research.md`](docs/surf-forecast-model-research.md) (research and
sources) and [`data/surf_spots/README.md`](data/surf_spots/README.md) (dataset).

```
backend/    FastAPI app, SQLAlchemy models, Alembic migrations, services, sync, Celery, tests
frontend/   Next.js 16 app (App Router), components, Vitest unit tests, Playwright e2e
data/       surf spot, airport, tide station and buoy datasets (JSON)
docs/       architecture, algorithm, operations and deployment documentation
scripts/    e2e runner, local Vercel-style stack, Vercel build check, egress measurement,
            test-database reset, backups, health check, data extraction
vercel.json, Containerfile, docker-compose.vercel.yml, .env.vercel.example   the hosted setup
```

## D. Prerequisites

**With Docker (recommended):** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
for Mac (Apple silicon or Intel) with Compose v2. Verified with Docker Engine 29.8 and
Compose 5.6. Allocate at least 4 GB of memory to Docker.

**Without Docker for the app processes** (you still need PostgreSQL and Redis):

| Tool | Version | macOS install |
|------|---------|---------------|
| Python | 3.12 or newer (the Docker image, CI and local development use 3.13) | `brew install python@3.13` |
| Node.js | 22 LTS (verified 22.22/22.23; Next.js 16 needs ≥ 20.9) | `brew install node@22` |
| PostgreSQL | 16 (needs the bundled `btree_gist` extension) | `docker compose up -d postgres` or `brew install postgresql@16` |
| Redis | 7 | `docker compose up -d redis` or `brew install redis` |
| PostgreSQL client tools | 16 (`pg_dump`, `pg_restore`, `psql`), only for the backup scripts outside Docker | `brew install libpq` (or use `scripts/backup-db.sh --compose`) |

The shell blocks below contain no inline comments, so they can be pasted into zsh (the macOS
default shell) as well as bash.

## E. Running the app

### 1. Clone and configure

```bash
git clone https://github.com/EliasGhizUSFCA/swellinformant.git
cd swellinformant
cp .env.example .env
```

Copy `.env.example` for a demo setup: it sets the demo providers (synthetic forecasts, mock
fares, console email). Without a `.env`, Docker Compose still uses demo forecasts, but app
processes started by hand use the code defaults, and `WEATHER_PROVIDER=auto` then makes live
Open-Meteo calls (section E.3). `.env` is
git-ignored; never commit it. The backend reads `.env` from the repository root (also when
started from `backend/`), and Docker Compose reads it for variable substitution. To try the
developer tools described below (simulate a swell, read captured emails), set
`ENABLE_DEV_ENDPOINTS=true`. Every variable is described in `.env.example`; the external
services and their keys are in [`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md).

### 2. Everything in Docker (recommended)

```bash
docker compose up --build
```

On first start the `backend` container waits for PostgreSQL and Redis, applies the Alembic
migrations, seeds the 50 spots, 153 airports, the research evidence and the tide station
catalog (`python -m app.cli init-db`, idempotent) and reports healthy. Then `worker`,
`beat` and `frontend` start, and beat immediately queues the sync check (`sync.tick`, which
starts a sync as soon as a model run is due; docs/sync.md) and, with `TIDE_PROVIDERS`, the
tide job. No manual database or seeding step is needed.

Measured on 2026-10-16 on a 4-CPU Linux machine (demo mode, base images already pulled):
`docker compose up --build -d` took 3 min 11 s including both image builds; every service
reported healthy within a minute of the first container starting; and the first pipeline
run that beat queues at start (forecasts → blend → 19,200 demo predictions → detection →
matching → flights → alerts → delivery) finished 57 s after beat started. (That was the
16-day demo horizon of the time; with Swell 2.0's `FORECAST_DAYS=10` the demo run holds
12,000 records for the 50 spots.)

Everyday commands:

```bash
docker compose up --build -d
docker compose ps
docker compose logs -f worker beat
docker compose exec backend python -m app.cli init-db
docker compose exec backend python -m app.cli pipeline
docker compose down
docker compose down -v
```

`up --build -d` starts in the background, `ps` shows each service's health, `logs` follows
the jobs, `init-db` re-runs the migrations and seed (idempotent), `pipeline` runs every job
once synchronously and prints the result, `down` stops the stack and keeps the database
volume, and `down -v` also deletes all data.

**Workers and queues.** The `worker` service consumes both queues (`WORKER_QUEUES`,
default `celery,bulk`). To keep long downloads away from email and alert delivery, run a
second worker for the `bulk` queue only:

```bash
WORKER_QUEUES=celery docker compose --profile bulk-worker up -d
```

The main worker then takes `celery` and `worker-bulk` takes `bulk` (verified 2026-10-16).
Optional daily `pg_dump` backups: `docker compose --profile backup up -d backup`
([`docs/operations.md`](docs/operations.md)).

### 3. App processes on your Mac, PostgreSQL + Redis in Docker

```bash
docker compose up -d postgres redis
cd backend
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m app.cli init-db
uvicorn app.main:app --reload --port 8000
```

`init-db` creates the schema with Alembic (`python -m app.cli migrate` applies migrations
only) and seeds the reference data. Then, in three more terminals from the repository root:

```bash
cd backend && source .venv/bin/activate && celery -A app.workers.celery_app worker --loglevel=INFO
```

```bash
cd backend && source .venv/bin/activate && celery -A app.workers.celery_app beat --loglevel=INFO
```

```bash
cd frontend && npm ci && npm run dev
```

* A worker started without `-Q` consumes both the `celery` and the `bulk` queue. To split
  them, start one worker with `-Q celery` and another with `-Q bulk`.
* Beat writes its schedule file (`celerybeat-schedule*`, git-ignored) in the current
  directory. Run exactly one beat.
* The default `DATABASE_URL` / `REDIS_URL` match the compose ports 5432 and 6379. The Next.js
  dev server proxies `/api/*` to `BACKEND_URL` (default `http://localhost:8000`).
* **Running without the worker**: set `CELERY_TASK_ALWAYS_EAGER=true`; jobs then run inside
  the API process (used by the tests). `python -m app.cli pipeline` runs every job once.
* **Without Celery at all**: `python -m app.cli sync daemon` runs every background job in
  one process, as on Vercel (`JOB_RUNNER=daemon`; section F.2). Run either the daemon or
  worker and beat, never both.

**Install sets (Upgrade 4).** `backend/pyproject.toml` splits the dependencies, and
`backend/uv.lock` pins every version (the Vercel builder, the images and CI install from
it):

| Install | Adds | Used by |
|---|---|---|
| `pip install ./backend` (no extra) | the lean API set only: FastAPI, SQLAlchemy, psycopg, redis, numpy, Argon2 and the like; no Celery, pandas, ecCodes, pyarrow or scikit-learn | the Vercel API function and the local stack's API image. The CLI's `obs`, `verify`, `ml` and `engine` groups and the data jobs need `.[sync]` |
| `.[worker]` | Celery | the classic stack's worker and beat |
| `.[server]` | uvicorn's speed-ups (`uvicorn[standard]`) | a long-running API server |
| `.[sync]` | pandas, ecCodes (with its C library `eccodeslib`), pyarrow, scikit-learn, joblib | the sync daemon image (`Containerfile`), the data CLI groups |
| `.[all]` | worker + server + sync | the classic backend image (`backend/Dockerfile`) |
| `.[dev]` | all + pytest, ruff, mypy | development and CI (`pip install -e ".[dev]"`, as above) |

The lean set from the lock, as Vercel installs it, in `backend/.venv`:
`cd backend && uv sync --locked --no-dev` (132 MB measured). This makes `backend/.venv`
exactly the lean set: uv removes Celery, pandas, ecCodes and the dev tools from an existing
venv, such as the one of section E.3 (reinstall with `pip install -e ".[dev]"`
afterwards), so for a check next to your dev venv put it in its own:
`UV_PROJECT_ENVIRONMENT=.venv-lean uv sync --locked --no-dev`. uv is pinned at 0.11.32
everywhere; install that version with uv's pinned installer,
`curl -LsSf https://astral.sh/uv/0.11.32/install.sh | sh` (not `python3 -m pip install`,
which Homebrew's Python refuses under PEP 668, and not `brew install uv`, a newer version).
From the repository root,
`backend/.venv/bin/python scripts/check_api_bundle.py --max-mb 200` measures that set as the
Vercel function would hold it (163.2 of 200 MB on 2026-10-23).

**Updating dependencies.** Edit `backend/pyproject.toml`, then in `backend/` run uv 0.11.32
to update the lock, for one package `uv lock --upgrade-package <name>`, and commit
`backend/uv.lock`. `[tool.uv] exclude-newer` (2026-10-08 now) keeps releases younger than
that date out of the lock, so a package published days ago never reaches a deployment: move
it to a date at least two weeks back when you update. A security fix newer than that date
goes in `exclude-newer-package` with the advisory named next to it (certifi is there now,
for GHSA-6q48-6c7w-9gw2; drop the entry once `exclude-newer` has moved past it). CI runs
`uv lock --check` and the images run `uv sync --locked`, so a lock that no longer matches
`pyproject.toml` fails there.

Verified on 2026-10-16: `init-db` on an empty database (8 s), `alembic upgrade head` and
`alembic check`, uvicorn (`/api/health/ready` answered `database: ok, redis: ok`), a worker
started without `-Q` listing both queues, beat sending its start-up run and
`notifications.deliver` every 2 minutes, and `npm run dev` serving `/`, `/map` and the
proxied `/api/*`.

**Homebrew PostgreSQL instead of Docker.** Create the role and databases first:

```bash
psql postgres -c "CREATE ROLE swell LOGIN PASSWORD 'swell' CREATEDB"
createdb -O swell swell
createdb -O swell swell_test
```

A non-superuser database owner can create the `btree_gist` extension, since it is a
trusted extension in PostgreSQL 13+.

### 4. Where things are

| What | URL |
|------|-----|
| Web app | http://localhost:3000 |
| Backend API | http://localhost:8000 (health: `/api/health`, readiness: `/api/health/ready`, data health: `/api/health/data`) |
| API documentation (OpenAPI / Swagger UI) | http://localhost:8000/docs |
| System status (data health, every job's state, provider modes) | http://localhost:3000/status |
| Development inbox (Mailpit, Docker only): every email the stack sends | http://localhost:8025 |
| Forecast accuracy dashboard (admin accounts) | http://localhost:3000/status/accuracy |

`scripts/check-health.sh http://localhost:8000` prints the data-health reasons from a shell
(exit 0 ok, 1 degraded, 2 failing).

### 5. Demo walkthrough

1. Open http://localhost:3000 and register. In Docker Compose the verification email lands
   in the Mailpit inbox at http://localhost:8025 (the dashboard links to it and shows the
   message status: `captured` means a development inbox has it; nothing claims
   "delivered" without a provider's signed confirmation). Outside Docker with
   `EMAIL_PROVIDER=console`, emails are written to `/tmp/swell-outbox/outbox.jsonl`.
2. **Searches → New search.** Pick, for example, Jeffreys Bay, home airport SFO, a
   $5,000 budget.
3. Force a swell to see the full flow (demo forecasts only):

   ```bash
   docker compose exec backend python -m app.cli simulate-swell jeffreys-bay --days-ahead 7
   docker compose exec backend python -m app.cli pipeline
   ```

   With dev endpoints enabled, `POST /api/dev/simulate-swell` does the same from the
   API docs page.
4. The dashboard shows the opportunity with **MOCK** fares. The outbox contains the alert
   email.

### 6. Test locally (Vercel-style)

Before deploying, run the app on your machine the way Vercel runs it: an edge that routes
`/api` like Vercel and sets its client-IP headers, the lean API, the Next.js production
build, PgBouncer in transaction mode like Neon's pooler, Redis, the sync daemon built from
the same `Containerfile` Vercel builds, Mailpit and a cron helper. It needs Docker (Compose
v2.24 or newer), about 3 GB of disk for the images and, for the smoke test, the frontend's
dependencies (`cd frontend && npm ci && npx playwright install chromium`). Full guide:
[`docs/local-vercel-stack.md`](docs/local-vercel-stack.md).

```bash
cp .env.vercel.example .env.vercel.local
scripts/vercel-local.sh up
scripts/vercel-local.sh sync --force
scripts/vercel-local.sh smoke
scripts/vercel-local.sh down
```

`cp` makes the stack use live data (Open-Meteo for the spots, NOAA for the map layers);
without `.env.vercel.local` it runs synthetic demo forecasts and no map layers. `up` builds
and starts everything (the app at http://localhost:3100, every email it sends in Mailpit at
http://localhost:18025), `sync --force` runs one sync now, `smoke` runs 22 shell checks and 6
browser checks, `down` stops it (`down --volumes` also deletes its database). For a short
first test, add `MAP_FIELD_STEPS=0-24:3` and `MAP_FIELD_MAX_LEAD_HOURS=24` to
`.env.vercel.local` (9 map hours instead of 61; 73.3 MB downloaded for them on 2026-10-23).
Measured on 2026-10-23: `up` 216 s from scratch (with this sandbox's proxy overrides),
`smoke` 19 of 19 and 6 of 6 with demo data and again with live data; again for Swell 2.0 on
2026-10-29 (demo data): 19 of 19 and 6 of 6. The three shell checks added since (the
refresh endpoint 401 and 200, the version endpoint) have not been run against the stack.
The stack's daemon refreshes the spots itself (no scheduler), so it does not test the
daemon-less hosted path ([`docs/local-vercel-stack.md`](docs/local-vercel-stack.md)). If Open-Meteo
answers "Daily API request limit exceeded" (its free limit is per IP address), set `FORECAST_PROVIDERS=demo` in `.env.vercel.local`, run
`scripts/vercel-local.sh up --no-build` and `sync --force` again, and try live data later.

The build itself can be checked without a Vercel account:
`scripts/vercel-build-check.sh --offline` runs Vercel's own `vercel build` (CLI 64.5.0) on a
temporary copy and checks the bundle sizes (on 2026-10-29: `RESULT: ok`, API function
128.3 MiB of 500 MiB, `maxDuration` 300 s). CI runs it on every push.

### 7. Deploy to Vercel

Step by step, with what each step looks like and what to check:
[`docs/deploy-vercel.md`](docs/deploy-vercel.md), section 4. In short (steps 1-3 and 5 run
only on Vercel's website; steps 4 and 6 run on your machine):

1. Vercel, Hobby plan: add a project from this GitHub repository and **leave the Root
   Directory at the repository root** (`vercel.json` defines the `web` and `api` services,
   the optional `sync` daemon and the daily cron).
2. Vercel Marketplace: add **Neon Postgres** (Free, AWS us-east-1) and connect it to the
   project, and **Upstash Redis** (Free), connected to Production and Development only;
   they set `DATABASE_URL` and `DATABASE_URL_UNPOOLED`, and `KV_URL`. Check: if only
   `KV_REST_API_URL` and `KV_REST_API_TOKEN` appear, copy the database's `rediss://` URL
   into `REDIS_URL` (docs/deploy-vercel.md step 4).
3. Project Settings, Environment Variables: paste the hosted-profile block of
   docs/deploy-vercel.md step 5, then add `APP_ENV`, `SECRET_KEY` and `CRON_SECRET`
   (`python3.13 -c "import secrets; print(secrets.token_urlsafe(48))"`, or
   `openssl rand -base64 48`), `COOKIE_SECURE=true`, `APP_BASE_URL`, `API_BASE_URL`,
   `CORS_ORIGINS` and the email provider.
4. The database: a production build migrates and seeds it (the api's build command
   `python -m app.cli migrate --on-build` with `MIGRATE_ON_BUILD=production`: the 50 spots,
   airports, the tide catalog; a build that cannot read the repository's `data/` fails
   on an empty database instead of deploying a site without spots). When it is needed, run
   `init-db` by hand (from `backend/` with the dev venv of section E.3, point `DATABASE_URL` and
   `DATABASE_URL_UNPOOLED` at Neon's direct URL, two `export` lines, docs/deploy-vercel.md
   step 6, then `.venv/bin/python -m app.cli init-db`; idempotent; checked here against a
   local PostgreSQL, not against Neon):
   * **Required** when the build failed at `"stage": "seed"`, **whether or not you keep the
     daemon**: a failed build deploys nothing, the daemon included, so its `init-db` never
     runs. Run `init-db`, then redeploy (step 5): the next build finds the spots and skips
     the seed. (Or set `MIGRATE_ON_BUILD=off` and keep the daemon, whose `init-db` seeds at
     start: docs/deploy-vercel.md section 9.)
   * **Required** when the build's log says `skipped` and you do not keep the daemon.
   A database without spots publishes nothing; `/api/health/data` then reports
   `no_spots_seeded`.
5. Redeploy production, then check `https://<project>.vercel.app/api/health/ready` and run
   the first refresh by hand:
   `curl -X POST -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/internal/refresh`
   answers `"status": "success"` with a version id (`not_due` once one is published), and
   `/api/forecasts/version` names it.
6. **Schedule the refresh every 5 minutes** (docs/deploy-vercel.md step 7b). Without this
   step the spot forecasts refresh only once a day (the daily cron's backstop). On Hobby,
   Upstash QStash (free: 1,000 messages a day; this uses 288):

   ```bash
   curl -XPOST -H "Authorization: Bearer $QSTASH_TOKEN" \
     -H "Upstash-Cron: */5 * * * *" \
     -H "Upstash-Forward-Authorization: Bearer $CRON_SECRET" \
     "https://qstash.upstash.io/v2/schedules/https://<project>.vercel.app/api/internal/refresh"
   ```

   On Pro, add `{"path": "/api/internal/refresh", "schedule": "*/5 * * * *"}` to `crons`
   in `vercel.json` instead (Pro runs cron "Once per minute", Hobby "Once per day", and a
   sub-daily entry fails a Hobby deployment: https://vercel.com/docs/cron-jobs/usage-and-pricing,
   read 2026-10-27). A third option, not verified here: cron-job.org (free, "up to 60 times
   an hour", custom headers for the `Authorization: Bearer` line), but "If your URL/script
   does not finish after 30 seconds, it will timeout and we will close the connection"
   (https://cron-job.org/en/faq/, read 2026-10-27); a refresh that fetches and scores took
   12-20 s here plus the metadata check, and whether the function finishes after the caller
   has hung up was not tested (docs/sync.md, "Scheduling options"). Check after 5-10
   minutes: `/admin/freshness` (signed in as an
   admin) shows the refresh scheduler `checking`, its last check by `refresh endpoint
   (refresh)` less than 5 minutes ago, and `/status` a recent check. The `sync` daemon is
   optional: it only refreshes the map layers. **If you do not keep it**, scale it to 0 and
   set `MAP_FIELDS_ENABLED=false`: the map then shows the spot markers only (a "Spot
   forecasts only" note, no layer tabs and no outage notice) and data health stays `ok`. With the profile's `MAP_FIELDS_ENABLED=true` and no daemon, data health stays
   degraded (`map_fields_missing`, `sync_heartbeat_stale`) and the admins get a "Data
   health DEGRADED" email every 12 h for as long as it lasts.
7. **Verify production** (docs/deploy-vercel.md steps 7 and 7b list what each answer should
   look like): `/api/health/ready` (database and Redis `ok`); `/api/forecasts/version` (a
   `version_id`, the wave and wind model runs, `spots_updated` 50); `/api/forecasts/status`
   and `/status` (each component's run, stale spots, the last refresh check);
   `/api/health/data` (`ok`, or the reasons); `/admin/freshness` signed in as an admin (the
   active provider, both model runs, the refresh scheduler, API usage); open a spot page and
   leave it open: after the next model run its "Forecast updated" label changes without a
   reload. None of this has been run against a real Vercel deployment yet (section M).

Free now: no payments, no ads (Vercel Hobby and the free Open-Meteo API are
non-commercial). What each service costs now and after a paywall:
[`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md), section 1.4; the checklist for
both phases: [`docs/deploy-vercel.md`](docs/deploy-vercel.md) sections 2 and 4, and
[`docs/UPGRADE_REPORT.md`](docs/UPGRADE_REPORT.md), "Owner checklist".
Email to other people needs a domain you own verified with the email provider. Before you
have one: `EMAIL_PROVIDER=resend`, `RESEND_API_KEY` from a free Resend account and
`EMAIL_FROM=Swell Travel Agent <onboarding@resend.dev>`. Resend then delivers only to the
address of your Resend account ("The resend.dev domain is only available for testing
purposes and can only send emails to the email address associated with your Resend
account"): register on the site with that address, verify it, run `make-admin` and set
`OPS_ALERT_EMAILS` to it; switch `EMAIL_FROM` to your verified domain once you have one.
Sign-up stays open: visitors can create accounts, but verification, password-reset and
alert emails reach nobody (with the onboarding@resend.dev sender, only you) until the
domain is verified. Do not publicise the site yet.

## F. Weather data

Sources, licences, measurements and limitations in full:
[`docs/FORECAST_SOURCES.md`](docs/FORECAST_SOURCES.md); every external service with its
terms, free tier, cost and setup: [`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md).

### 1. Providers, and what needs a key

| Data | Provider (setting) | Key or account | Used for |
|---|---|---|---|
| Spot forecasts (the default) | Open-Meteo Marine + Forecast API: NCEP GFS-Wave 0.16°/0.25° and GFS (`WEATHER_PROVIDER=auto` or `open_meteo`) | None for non-commercial use; commercial use needs a paid plan and `OPEN_METEO_API_KEY` | Waves with swell partitions, wind, gust, pressure at each spot |
| Spot forecasts, ECMWF family (advanced, off) | Open-Meteo: ECMWF WAM and IFS 0.25° (`open_meteo_ecmwf` in `FORECAST_PROVIDERS`) | as above | A second model family for the blend; replaces the per-component refreshes with family-level syncs |
| Forecast uncertainty (optional, off) | NOAA GEFS-Wave ensemble mean and spread (`GEFS_ENABLED=true`) | None (NOAA open data) | Uncertainty ranges |
| Map layers | NOAA GFS-Wave `global.0p25` and GFS `pgrb2.0p50` by HTTP byte range (`MAP_FIELDS_ENABLED`, automatic) | None | Wind, pressure, wave height, period, direction and partitions on the map |
| Tides | NOAA CO-OPS (8 US spots), EOT20 harmonic constants computed locally (42 spots) (`TIDE_PROVIDERS`) | None; set `NOAA_COOPS_APPLICATION` to an app name | Tide curve, high/low waters, tide scoring where research supports it |
| Buoy observations | NDBC (and CDIP via NDBC), AODN, Met Office (`OBSERVATIONS_ENABLED=true`) | None | Measured waves shown as measurements; forecast verification |
| Key-free fallback | NOAA GFS-Wave + GFS and ECMWF open data, GRIB2 by byte range (`noaa_gfs`, `ecmwf_ifs`) | None | Same models, much larger downloads (below) |
| Preferred once licensed | Windy Point Forecast (`windy`; `WEATHER_PROVIDER=auto` picks it when eligible) | Paid key; used only with `WINDY_STORAGE_AUTHORIZED=true`, set once a written licence allows storing it (its standard terms forbid that); see docs/WINDY_API_SETUP.md | Spot forecasts in place of Open-Meteo, which stays the fallback |

Every value shown from these sources carries its credit and licence link (Open-Meteo CC BY
4.0, "Copyright ECMWF … (modified)", NOAA, EOT20 CC BY 4.0, buoy providers): on the spot
page, the map panel and best-spots list, swell cards, opportunity pages and alert emails.

### 2. Weather setup and automatic updates (Swell 2.0)

**Choosing the provider.** One weather provider serves the spot forecasts:

| `WEATHER_PROVIDER` | Uses |
|---|---|
| `auto` (the default) | Windy once it is eligible (`WINDY_API_KEY` set and `WINDY_STORAGE_AUTHORIZED=true`, set only with a written licence that allows storing the data; [`docs/WINDY_API_SETUP.md`](docs/WINDY_API_SETUP.md)), else Open-Meteo's NOAA GFS + GFS-Wave. Open-Meteo stays the fallback when Windy fails |
| `open_meteo` | Open-Meteo GFS + GFS-Wave (no key for non-commercial use) |
| `windy` | Windy only (refused without the key and the storage authorisation) |
| `demo` | synthetic forecasts, labelled as such (Docker Compose, `.env.example` and the tests set it) |

`FORECAST_DAYS` (10) is the horizon. `FORECAST_PROVIDERS` is an advanced override and stays
empty; the two-family blend and the GEFS ensemble are off by default and not recommended
(they replace the per-component refreshes with family-level syncs:
[`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md), "Recommended live
configuration"). Check what the settings select, with one free metadata request:

```bash
cd backend && python -m app.cli forecast provider-check
```

It prints the selection and its reasons, Windy's eligibility, the recorded provider health
and a live check (Open-Meteo: one `meta.json`; Windy: one sentinel request) as JSON, and
stores nothing; exit 0 when every checked provider is healthy, 1 otherwise
(`--provider open_meteo` or `--provider windy` checks one).

**Detection.** The app never downloads a forecast to find out whether it changed. A
**refresh check** reads only the providers' metadata: Open-Meteo's `meta.json` per model
domain (free, not charged to the call budget; its availability time, used 10 minutes later,
`OPEN_METEO_AVAILABILITY_WAIT_MINUTES`, because Open-Meteo's servers can disagree for a
while), or Windy's sentinel fingerprint. The wave and the wind (atmosphere) are separate
components with their own model runs. A component is new only when every one of its model
domains is at or past the stored run, so a lagging server never makes the site go back to an
older run. A check with nothing new makes no database query and no forecast request.

**Check frequency.**

* Hosted (Vercel): `GET|POST /api/internal/refresh` with `Authorization: Bearer
  $CRON_SECRET`, **every 5 minutes**, called by Upstash QStash on Hobby or a Vercel Cron
  entry on Pro (set it up in [`docs/deploy-vercel.md`](docs/deploy-vercel.md), step 7b). The
  daily Vercel cron (`/api/internal/cron/daily`) runs one more check as a backstop.
* Docker Compose and local processes: Celery beat's `sync.tick` or the daemon
  (`python -m app.cli sync daemon`) every `SYNC_CHECK_MINUTES` (10).
* `FORECAST_REFRESH_ENABLED=false` is the kill switch: checks answer `disabled` and nothing
  is fetched.

**Processing.** When a component has a newer servable run, the refresh fetches that
component for the 50 spots (one request per model domain), keeps the other component with
its **original model time** (never labelled as the new run; hours the carried component does
not have are dropped). About once or twice a day (with Open-Meteo GFS, on the first
component refresh after the UTC request window moved) the other component is fetched again
**from its same run** with it, so the 10-day horizon keeps rolling; that refetch is best
effort: when it fails, the new run is published with the other component carried and its
own end ([`docs/sync.md`](docs/sync.md), "One refresh"). Then it normalises, scores every hour, validates the spots, stores the result
as a **staged version** and publishes it (`forecast_versions`: version id, published time,
wave and wind model runs, spots updated and stale). Then it detects swell events, matches the
saved searches, searches flights where justified and sends alerts. Spots whose fetch failed
keep their previous forecast, are marked stale and are retried (5, 15, 45 minutes later); a
version is never published with incomplete records as complete. Superseded versions are kept
`RETENTION_FORECAST_DAYS` (1 on Neon Free, 10 by default). Measured on this machine: a live
Open-Meteo refresh of both components 125 weighted calls, a wave-only refresh 75 and 20.3 s;
a demo refresh 12-15 s and 8.5-11.3 CPU s; a check with nothing new under 2 s
([`docs/sync.md`](docs/sync.md), "Measured (2.0)").

**Cache invalidation.** The published version id lives in Redis (the version marker). Every
public read (`/api/spots`, `/api/map/conditions`, a spot's forecast and timeline, events) is
cached per version (the snapshots that depend on the time also per UTC hour), and the
browser asks with `?v=<version_id>`: a URL that
names the current version is cacheable by the CDN (`s-maxage=300`), one that names another
version answers `no-store`, so no cache serves an old forecast as the new one.

**Browser updates.** An open, visible tab polls `GET /api/forecasts/version` (a small JSON;
the CDN shares it for `FORECAST_VERSION_CDN_SECONDS`, 15) every 20 seconds
(`NEXT_PUBLIC_FORECAST_VERSION_POLL_MS`). On a new version it re-reads the data on screen
under the new version's URLs and swaps it in without a reload, keeping the selected spot and
time: map markers, ratings, charts, swell and wind, tide, confidence, best windows and the
signed-in lists (opportunities, alerts). The label "Forecast updated 14:32 AEDT · 5 min ago"
shows when the forecast on screen was published (on a spot page: when that spot was last
updated, in the spot's zone), is highlighted briefly, and screen readers hear "New forecast
loaded". Once an hour the tab also re-reads the public data, so a spot that went out of date
by age is shown as such. Measured in the local Vercel-style stack (no CDN): from publication
to the new data on screen p50 13.8 s (16 samples, 9.7-22.9 s).

**Status checks.**

| Where | What |
|---|---|
| `GET /api/forecasts/version` | the published version: id, published time, wave and wind model runs, spots updated and stale |
| `GET /api/forecasts/status`, `/status` | public freshness: each component's model run, stale spots, the last refresh check |
| `/admin/freshness` (admin accounts) | model runs and their expected next run, refresh jobs and their timings, the refresh scheduler, stale spots, provider health and fallbacks, API usage (Open-Meteo, Windy, flights), how fast open pages saw each version |
| `/api/health/data` | data health with the problems admins are emailed about |

**When the provider is unavailable.** The site keeps serving the last published version and
labels stale spots "Forecast last updated N hours ago. Not a current forecast."; it never
calls stale data current. A check that cannot read the metadata leaves that component
`unknown` until the next check. Windy failures (key, quota, outage) fall back to Open-Meteo
for a while and the version says so. Admins are emailed for `provider_unavailable` (30
minutes), `model_run_late`, `refresh_failures` (3 in a row), `forecast_stale_spots`,
`provider_credentials`, `quota_near` and `refresh_scheduler_silent` (no check for 30
minutes) when the data-health check finds them. That check runs hourly on Vercel: without
the daemon the refresh endpoint runs it (and sooner after a failed or partial refresh or a
component whose metadata became unreadable), with it the daemon. A scheduler that stopped calling the endpoint stops that check too, so
without the daemon `refresh_scheduler_silent` arrives only with the daily cron (up to about
25 hours on Hobby; docs/deploy-vercel.md step 7b names an external check). When the email
provider refuses its own key, the alert goes by SMS to admins with a verified phone number
(`SMS_PROVIDER`), else it shows on `/admin/freshness` and `/api/health/data` only. The
runbooks are in [`docs/operations.md`](docs/operations.md).

**Try it locally (demo provider).** With the demo provider, `ENABLE_DEV_ENDPOINTS=true` and
a `CRON_SECRET` of 16 or more characters in `.env`, and the API on `localhost:8000`: run a
first refresh, register on the site (section E.5), then make the demo provider offer a new
wave run and refresh again. Open a spot page or the map in the browser first: it switches to
the new version within about 20 seconds, without a reload.

```bash
export CRON_SECRET='the value in .env'
API=http://localhost:8000
curl -s -X POST -H "Authorization: Bearer $CRON_SECRET" "$API/api/internal/refresh"
# sign in (the dev endpoint needs an account), with the CSRF cookie the API sets
J=$(mktemp)
curl -s -c "$J" "$API/api/auth/csrf" > /dev/null
curl -s -b "$J" -c "$J" -H "X-CSRF-Token: $(awk '$6 == "sta_csrf" {print $7}' "$J")" \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"your password"}' "$API/api/auth/login"
curl -s -b "$J" -H "X-CSRF-Token: $(awk '$6 == "sta_csrf" {print $7}' "$J")" \
  -X POST "$API/api/dev/simulate-model-run?component=wave"
curl -s -X POST -H "Authorization: Bearer $CRON_SECRET" "$API/api/internal/refresh"
curl -s "$API/api/forecasts/version"
```

The first refresh answers `"status": "success"` with version 1 (or `not_due` when a runner
already published it); the second, after the simulated run, `"status": "success"` with the
next version and, in its `plan`, the wave component `new` and the atmosphere `current`
(carried forward). `component=atmosphere` or `both` simulate the others. Run on 2026-10-29
against a local API (demo provider, `JOB_RUNNER=daemon`): the first refresh published version
1, the wave-only refresh version 2 in 15.2 s (10.1 CPU s), and `/api/forecasts/version` then
named version 2. Re-run after review r2 (same day, with the test suites running beside it):
version 1, then version 2 with the wave `new` and the atmosphere `current` in the plan, and
401 for a wrong token. Without a valid token the endpoint answers 401 (404 when
`CRON_SECRET` is not set). The whole flow, including the browser, is tested by `frontend/e2e/live-update.spec.ts`.

**What still runs on a timer**, and why: `tides.acquire` daily at 02:20 UTC (tide predictions
change slowly), `observations.ingest` hourly when `OBSERVATIONS_ENABLED`, the verification
jobs when `VERIFICATION_ENABLED`, `maintenance.cleanup` daily at 03:30 UTC (retention), the
matching housekeeping daily at 03:45 UTC, alerts every 15 minutes (held alerts), delivery
every 2 minutes (retries), and the staleness and data-health checks; each at least
`JOB_INTERVAL_FLOOR_MINUTES` apart (60 on Vercel). There is no flight-search timer: flights
are searched after a publication's matching and on "Re-check flights". On a hosted
deployment without the daemon, the refresh endpoint runs "Re-check flights", "match my
search", due email retries and the hourly data-health check, and the daily cron runs the
periodic jobs that are overdue
([`docs/MATCHING_AND_FLIGHTS.md`](docs/MATCHING_AND_FLIGHTS.md), section 0).

The map layers (`MAP_FIELDS_ENABLED`, NOAA NODD GRIB) have their own runs: a new cycle of
`MAP_FIELD_CYCLES` is fetched by the daemon or Celery (never by the refresh endpoint); the
map overlay is labelled out of date when it ages.

The sync from the command line (from `backend/`; each prints JSON or a plan):

```bash
python -m app.cli sync check
python -m app.cli sync run
python -m app.cli sync run --force
python -m app.cli sync daemon
python -m app.cli sync publish
python -m app.cli sync status
python -m app.cli migrate --on-build
```

`sync check` prints the plan (what is new, waiting or current) from the metadata alone;
`sync run` syncs whatever has a newer run (exit 0 success, partial or skipped; 1 failed; 3
another sync holds the lease), `--force` runs every stage now (runs already stored are still
skipped), `--only` limits the acquisitions to `spots`, `ensemble` or `map`; `sync daemon`
runs every background job in one process until stopped (`--once` for one iteration);
`sync publish` publishes what is stored; `sync status` lists the newest syncs;
`migrate --on-build` is the Vercel build step (it migrates and seeds where
`MIGRATE_ON_BUILD` allows, and fails the build when it finds no `data/` and the database
has no spot). The full rules: [`docs/sync.md`](docs/sync.md).

The spot timeline shows the model's own steps: GFS hourly to 120 h, then 3-hourly. Hours
Open-Meteo itself interpolated are not stored (`OPEN_METEO_NATIVE_STEPS_ONLY=true`).

### 3. Caching and storage limits

* **Provider responses**: cached in Redis for one model run (`PROVIDER_RESPONSE_CACHE_ENABLED`,
  at most `PROVIDER_RESPONSE_CACHE_MAX_KEYS` = 200 entries of up to
  `PROVIDER_RESPONSE_CACHE_MAX_BYTES` = 2 MB each, compressed), so a retry or a second process
  never repeats a request. Open-Meteo weighted calls are charged to a Redis budget before each
  request (`OPEN_METEO_CALLS_PER_MINUTE/HOUR/DAY/MONTH`, the free tier's limits by default);
  `python -m app.cli engine budget` prints what has been used.
* **No files on disk**: GRIB records are decoded in memory and nothing is written (strace over
  every acquisition job on 2026-10-16 found no GRIB or temporary file).
* **Redis**: `maxmemory` 256 MB (`REDIS_MAXMEMORY`) with `noeviction` in
  `docker-compose.yml`; the data-health check warns above `DATA_HEALTH_REDIS_MAX_FRACTION`.
  Measured: 1.90 MB before and 2.43 MB after one full live pass.
* **Map fields in the browser**: an LRU of 48 decoded fields, at most 64 MB.
* **Public responses** (Upgrade 4): the anonymous spot list, map conditions, spot pages,
  `/api/system/status` and the map-field index are computed at most once per publication
  and UTC hour and kept in Redis for 2 hours (`app/services/public_cache.py`); they carry
  `Cache-Control: public, max-age=60` and `CDN-Cache-Control: public, s-maxage=300,
  stale-while-revalidate=3600`, and a map field URL that names its model run is cached by
  the CDN for a year. Routes that depend on the signed-in user are `no-store`. The hosted
  profile turns the provider response cache off (`PROVIDER_RESPONSE_CACHE_ENABLED=false`:
  Upstash Free stores 250 MB). Measured page views with a warm cache (anonymous, no CDN):
  the home page and a spot page read 0 bytes from the database
  ([`docs/map-layers.md`](docs/map-layers.md), "Free database budget").
* **Retention** (job `maintenance.cleanup`, daily): forecasts and predictions for
  `RETENTION_FORECAST_DAYS` (10), superseded member-run predictions pruned
  (`PRUNE_SUPERSEDED_MEMBER_PREDICTIONS`), 2 map-field runs, tides 2 past days, the archive of
  issued predictions for `RETENTION_ARCHIVE_DAYS` (730, stored as a compact confidence
  digest). On Neon Free the free-database profile keeps about 1 day of forecasts
  (`RETENTION_FORECAST_DAYS=1`), 7 days of archive and job logs, and prunes after every
  sync (`SYNC_PRUNE_EACH_PASS=true`); the full-retention profile restores the values above
  after moving to Neon Launch ([`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md),
  section 5).
* **Database size** (Upgrade 3 measurement, 2026-10-16, with the then-recommended blend of
  two model families; Swell 2.0 serves one provider, so it writes fewer rows per run, not
  re-measured as a daily size): about 0.30 GB of blend rows a day; 10 forecast days hold
  about 3.3 GB, and the archive grows about 18.5 MB a day, to about 17 GB at 730 days
  (computed from row sizes measured on 2026-10-16). The default `DATA_HEALTH_DATABASE_MAX_GB` alert (20 GB, an
  alert only, nothing is deleted) sits above that two-year steady state, so it means
  unexpected growth; lower it if your disk is smaller ([`docs/operations.md`](docs/operations.md), "Capacity").

### 4. Download volumes, before and after

Swell 2.0 (one provider, Open-Meteo GFS + GFS-Wave): 125 weighted calls for a refresh of both
components and 75 for a wave-only refresh (measured live, 2026-10-28 and 29; an
atmosphere-only refresh is the difference, 50), about 500 a day at 4 GFS cycles, plus the
horizon refetch of the other component on up to two refreshes a day (50 or 75 more each):
about 500-650 a day, 5-6.5 % of Open-Meteo's daily limit (arithmetic); a check with nothing new
reads only the free `meta.json` files ([`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md),
section 1.4). The table below is the **Upgrade 3 measurement** (2026-10-16, the then-default
schedule with the GFS and ECMWF families and GEFS-Wave; 00Z cycles, 50 spots, fresh
database; per day computed with that schedule; the map layers' per-cycle size is
extrapolated from 9 measured map hours):

| Stream | Per run (measured) | Per day |
|---|---|---|
| Open-Meteo GFS family | 329 kB, 142.9 weighted calls | 1.3 MB (4 runs) |
| Open-Meteo ECMWF family | 172 kB, 124.3 weighted calls | 0.34 MB (2 runs) |
| GEFS-Wave mean and spread | 69.4 MB | 0.28 GB (4 cycles) |
| Map layers with swell partitions | 7.82-7.98 MB per map hour; about 0.48 GB per cycle | about 0.96 GB (2 cycles) |
| Tides (CO-OPS) | 140 kB | 0.14 MB |
| NDBC buoys (if enabled) | 121 kB | at most 2.9 MB |
| **All** | | **about 1.25 GB a day** |

Before the upgrade the README's recommended setup read NOAA and ECMWF GRIB directly: 1,913.8
MB per NOAA cycle, about 0.85-0.9 GB per ECMWF cycle and 201.6 MB per GEFS-Wave cycle, about
**10.2 GB a day** (audit, 2026-10-13). The spot forecasts alone went from about 2.8 GB to
0.5 MB per set of cycles; the map layers are now three quarters of the remaining bandwidth
(about 0.6 GB a day without the partition layers, `MAP_FIELD_PARTITIONS=off`). With that
two-family schedule the free tier use was about 820 weighted calls a day, 8 % of
Open-Meteo's daily limit (about 500-650 with Swell 2.0's single provider, above).

### 5. Real forecast data, step by step

Recommended setup (Swell 2.0): one provider, Open-Meteo GFS + GFS-Wave until Windy is
licensed, the map layers (standalone, automatic with a live provider) and tides. In Docker,
put this in `.env` and restart on a fresh database (`docker compose down -v`, then
`docker compose up --build -d`); beat then checks for new model runs every 10 minutes and
runs every job on its own:

```env
WEATHER_PROVIDER=auto
FORECAST_PROVIDERS=
GEFS_ENABLED=false
NOAA_COOPS_APPLICATION=SwellTravelAgent
```

To run each job once by hand and see what it fetched, use a scratch database. Create it
once (from the repository root):

```bash
docker compose up -d postgres redis
docker compose exec postgres createdb -U swell swell_live
```

Then run the jobs in order (tides and the ensemble first, so the predictions can use them):

```bash
cd backend
source .venv/bin/activate
export DATABASE_URL=postgresql+psycopg://swell:swell@localhost:5432/swell_live
export WEATHER_PROVIDER=open_meteo FORECAST_PROVIDERS=
python -m app.cli init-db
python -m app.cli tides acquire
python -m app.cli pipeline
python -m app.cli engine map-fields --max-lead-hours 24
python -m app.cli engine budget
```

Each command prints JSON with its records, errors, bytes and timings. `pipeline` publishes
what it stored; `engine map-fields` stores map layers without publishing them, so the map
keeps showing the published run (`SYNC_PUBLISH_GATE`) until the next sync or
`python -m app.cli sync publish`. `--max-lead-hours 24`
reads the first 9 map hours (about 71 MB); leave it out to read the whole cycle (61 map
hours, about 0.48 GB). In the same terminal, `uvicorn app.main:app --port 8000` serves that
database to the frontend, and the refresh endpoint (section F.2) keeps it current once
`CRON_SECRET` is set. The single-provider refresh was run live on 2026-10-28 and 29
(`orchestrator.refresh_check`, what the endpoint runs; docs/sync.md "Measured (2.0)"): both
components 125 weighted calls, a wave-only refresh 75. The block itself was last verified on
2026-10-16 with the then-recommended two-family setup, by running it in an interactive zsh
(against the compose PostgreSQL, on a shifted host port): `init-db`,
tides (56 stations, 140,624 B from CO-OPS), GEFS-Wave (2,050 rows, 69.4 MB), the map fields
(9 of 9 hours with partitions, 71.3 MB) and the budget all succeeded. That afternoon
Open-Meteo answered this sandbox's shared address with "Daily API request limit exceeded.
Please try again tomorrow."; the run was recorded as failed with that message, nothing was
stored for it, and the pipeline went on. The same Open-Meteo runs had succeeded earlier that
day and on 2026-10-14 (section M).

Upgrading an older `.env`: delete `OPEN_METEO_WAVE_MODEL` and `OPEN_METEO_ATMOS_MODEL` if
present. They are deprecated single-model aliases and, when set, override the per-latitude
GFS wave models.

**Key-free GRIB fallback.** `FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs` with `GEFS_ENABLED=true`
reads the same models from the NOAA and ECMWF open-data buckets. Each GFS cycle downloads
about 1.9 GB (81 steps × about 24 MB for the 50 spots) and each ECMWF cycle about 0.48 GB
(trimmed to the variables used; computed). Reduce with `NOAA_STEP_HOURS=6` or shorter
`NOAA_MAX_LEAD_HOURS` / `ECMWF_MAX_LEAD_HOURS`. With `noaa_gfs` as a provider the map layers
come from the files it reads anyway.

### 6. Map layer settings

The map's global layers (offshore significant wave height, not surf height; peak period
and direction; the primary swell, secondary swell and wind-sea partitions; 10 m wind;
sea-level pressure) are NOAA model output regridded to 0.5° and served by
`GET /api/map/fields`. Without them the map shows the spots only, with a one-line notice.

```env
MAP_FIELDS_ENABLED=
MAP_FIELD_STEPS=0-120:3,120-240:6
MAP_FIELD_CYCLES=00,12
MAP_FIELD_PARTITIONS=on
MAP_FIELD_STALE_AFTER_HOURS=21
MAP_FIELD_RESOLUTION_DEG=0.5
MAP_FIELD_MAX_LEAD_HOURS=240
```

`MAP_FIELDS_ENABLED` empty means automatic: standalone with any live weather provider,
from the `noaa_gfs` files when that provider is configured, off with demo only; `true`
forces real layers next to demo spot forecasts, `false` turns them off. The values above are
the defaults. The hosted free-database profile reads `MAP_FIELD_CYCLES=06,18` (section F.2).
`MAP_FIELD_STEP_HOURS` is no longer read; an old `.env` that sets it still loads (unknown
keys are ignored). Fetch a cycle now with
`python -m app.cli engine map-fields [--cycle YYYYMMDDHH] [--max-lead-hours N] [--force]`.
Encoding, sources, licence and measurements: [`docs/map-layers.md`](docs/map-layers.md).

### 7. Tides

No key is needed. The 8 US spots use NOAA CO-OPS tide predictions; the other 42 use the
EOT20 harmonic tide model at a model point 1-13 km from the break, relative to the model's
mean sea level (not a chart datum, so heights cannot be compared with local tide tables).
These are predicted astronomical tides, not observed water levels. `init-db` loads the
station catalog; `tides.acquire` fills the predictions.

```bash
python -m app.cli tides seed
python -m app.cli tides acquire
python -m app.cli tides list
python -m app.cli tides prune
```

`seed` (re)loads the catalog, `acquire` fetches the CO-OPS predictions and computes the EOT20
tides, `list` shows each station with its predictions, `prune` deletes old ones. Sources,
validation against CO-OPS and limitations: [`docs/tides.md`](docs/tides.md).

### 8. Observations, verification and ML post-processing

Forecast accuracy is measured against buoys, never assumed. Everything here is off by
default and runs as background jobs once enabled:

```env
OBSERVATIONS_ENABLED=true
VERIFICATION_ENABLED=true
ML_CORRECTIONS_ENABLED=true
```

`OBSERVATIONS_ENABLED` runs `observations.ingest` (AODN, Met Office, NDBC buoys),
`VERIFICATION_ENABLED` the jobs `verification.collect_point_forecasts` and
`verification.run`, and `ML_CORRECTIONS_ENABLED` applies an ACTIVE correction (only one that
passed the criteria).

* Stations and their paired surf spots: `data/observation_stations.json`
  ([`docs/observations.md`](docs/observations.md): sources, licences, QC, pairing
  justifications).
* Model forecasts at the buoys come from the GFS-Wave station bulletins (archived from
  2021-03-22), so historical forecasts are what was actually issued at each lead time.
* Metrics, backtests, leakage guards, the deployment criteria and the model registry:
  [`docs/ml-pipeline.md`](docs/ml-pipeline.md). Results: [`docs/ml-evaluation.md`](docs/ml-evaluation.md).

Command line (from `backend/`, or `docker compose exec backend …`):

```bash
python -m app.cli obs seed-stations
python -m app.cli obs ingest --provider aodn --since 2021-03-22T00:00:00Z --until 2022-12-31T00:00:00Z
python -m app.cli obs collect-forecasts --start 2021-03-22T00:00:00Z --end 2022-12-31T00:00:00Z
python -m app.cli verify pair
python -m app.cli verify backtest --source gfswave_bulletin --folds 5
python -m app.cli ml train --candidates linear,rf,gbt
python -m app.cli ml list
python -m app.cli ml activate VERSION_ID
python -m app.cli ml export-manifest VERSION_ID DIR
python -m app.cli ml import-manifest DIR/manifest.json
```

`ml train` registers every candidate with its decision; `ml activate` is refused unless
every criterion passed; the manifest commands move a model between environments.

**Result so far** ([`docs/ml-evaluation.md`](docs/ml-evaluation.md)): against QC'd
spectral Hm0, raw GFS-Wave is biased low by about 0.23 m both at three Western Australian
buoys (2021; RMSE 0.40 m at 24 h, 1.01 m at 240 h) and at ten NDBC buoys off Hawaii and
California (2025-02 to 2026-10; RMSE 0.38 m at 24 h, 0.64 m at 240 h). Two corrections
are shipped in `backend/ml_models/offshore_hs_residual/`, and **neither is active by
default**: v6 (random forest, held-out RMSE at five NDBC buoys 0.427 → 0.320 m, approved for
seven US spots; activating it is an operator step, docs/ml-evaluation.md §12.4) and v4 (ridge
at Cape Naturaliste, -8 %, kept inactive because its inputs differ at the spots). A
correction is applied only at spots paired with its training buoys, within its trained lead
range and to the model family it was trained on ([`docs/decisions.md`](docs/decisions.md)
ADR-012). The breaking-height model itself is not corrected or validated by any of this:
there are no nearshore surf observations yet.

### 9. Source limitations

* All forecasts are model output: the spot page and map say "forecast, not observed".
  Breaking heights and ratings are our own predictions from that output, kept separate from
  the raw model values.
* Open-Meteo's free API is for non-commercial use only and counts calls per IP address:
  shared hosting can hit "Daily API request limit exceeded" because of other users of the
  address. A paid plan (`OPEN_METEO_API_KEY`) avoids both limits.
* The ECMWF family has no swell partitions; the blend takes them from GFS-Wave. A partition a
  model does not have is left out, never drawn as 0.
* Where GFS and ECMWF are hourly and 3-hourly, the blend aligns the coarser member in time;
  such steps say "ECMWF value aligned in time" (5,750 of 10,400 blend rows on 2026-10-16).
* Map layers are 0.5° box means of a global model: they smooth coastlines and fronts and can
  differ from a spot's own point forecast (10 of 50 spots by more than 1.5 m/s of wind in the
  audit).
* EOT20 model tides are relative to the model's mean sea level, 1-13 km from the break, and
  cannot be compared with chart-datum tide tables; CO-OPS subordinate stations give only high
  and low waters (the curve between them is interpolated and labelled so).

## G. Forecast charts (spot page)

The spot page (`/spots/{slug}`) loads the whole horizon once
(`GET /api/spots/{slug}/timeline?days=10`) and draws every row on one time axis, in the
spot's own time zone with its name ("7:00 AM SAST"). Selecting or hovering never sends
another request. Details, contract and tests: [`docs/forecast-charts.md`](docs/forecast-charts.md).

| Part | What it shows |
|---|---|
| Selected-time summary | Day and time of the selected step and its length ("3-hour step"); surf quality (class, x.x/10, a rank glyph); breaking waves (typical–sets, central value, likely range); every wave system with the one that drives the surf first; wind (speed, FROM direction, relation to this break, strength, favourability, gust on its own line); tide (height, rising or falling, datum, next high or low); the forecast confidence badge (research confidence is under "About this break"; the two are never combined). "Why this forecast?" expands the scoring engine's own record: the explanation, factor fits, the gates that held the score back and the points lost per factor |
| Timeline controls | One button per local day (best daylight class and score, good hours = hours of light under Good-or-better steps), previous / next day, horizontal scroll (about 72 h in view on wide screens, 24 h on phones), click, drag or tap to select, hover tooltip. Keyboard: the chart is a slider, ArrowLeft / ArrowRight move one step, Page Up / Page Down one day later / earlier, Home / End the first / last step of the day. `?t=<UTC ISO>` links to a step. A "Now" marker, night bands from first to last light, day separators in every row. On phones a one-line readout of the selected step stays in view |
| Surf quality strip | Our spot-specific rating per step (Poor, Fair, Good, Very Good, Excellent, Exceptional) on one ordinal colour ramp, deeper = better, each class written in the strip. Every class has at least 3:1 contrast and the labels 4.5:1. Never derived from wave height in the browser |
| Surf (breaking waves) | Bar = our central breaking-height estimate, pale band = the likely range (heuristic, "not yet verified", not a probability), dark tick = sets. Toggle "Offshore Hs (model)" adds the wave model's offshore height as a dashed level and buoy measurements as hollow diamonds at their own times |
| Swell | One lane per wave system the model sent (primary, secondary, third swell, wind sea); the system that drives the surf is solid, the others outlined |
| Wind | Bar = mean speed, tick = gust, colour and hatching = favourability at this break (favourable / workable / unfavourable), the relation (offshore, cross-offshore, …) written under the bars. Units km/h, mph, kn or m/s, shared with the map |
| Tide | Predicted tide curve with high and low waters and their times, the datum, the station or model point with its distance and credit, and the spot's researched preference or "Tide preference not well established." |
| Table | "Table of every forecast step": every value as an HTML table |

**Conventions.**

* Text gives directions FROM ("SW 225°"); arrows on the charts and the map point where wind
  and waves travel TO. The legend says so and shows a compass example.
* Each step is drawn as wide as it lasts: hourly steps are thin, 3-hour steps three times as
  wide. Nothing is drawn between stored steps, and 3- or 6-hourly values are never shown as
  hourly; the caption states the spacing ("1-hour steps, then 3-hour steps from Tue 2:00 PM ·
  no values drawn between steps") and names both zones when the clocks change.
* Raw offshore model values, our breaking prediction and our quality rating are separate
  objects in the API and separate marks on the page.

**Two confidences, kept apart (spec 20).** *Forecast confidence* (per step: lead time,
ensemble spread, agreement between independent model families, run-to-run change,
completeness, staleness) is the badge in the summary; *research confidence* (how well the
break's ideal conditions are documented) is under "About this break". They are never
combined into one class: the page shows no "confidence at the break". Both are documented
heuristic classes (High / Moderate / Low), not probabilities.

## H. Map visualizations

The world map (`/map`) draws NOAA model fields under the spot markers. Rendering:
[`docs/map-rendering.md`](docs/map-rendering.md); navigation and the map shell:
[`docs/map-navigation.md`](docs/map-navigation.md); data:
[`docs/map-layers.md`](docs/map-layers.md).

**Layer controls.** Tabs *Wind Speed / Wave Height / Wave Energy / Wave Period*, a *Pressure*
toggle (isobars every 4 hPa), *Settings* (units, animated flow on or off, wind on the wave
layers, dark or Streets basemap, which wave systems to draw, spot filters), a *Legend*,
locate-me, and a bottom timeline over the model's valid times with the model run and lead
time ("GFS 18Z Oct 11 · +6 h", or "analysis" at the run's own time). The best-spots list
names its time ("Best spots · 9am Sun PDT"). A chip reads "Forecast out of date — model run
N h old" once the layers' cycle is older than `MAP_FIELD_STALE_AFTER_HOURS` (21 h).

**Wind animation.** Particles move with the GFS 10 m wind (u, v at 0.5°, from `pgrb2.0p50`),
at a visual speed of 12 CSS px per second per m/s at every zoom (not ground speed, as the
legend says), time-based so they move the same at 60, 120 and 144 Hz. The colour and the
readout use the speed of the interpolated vector. Directions in the readout say "from"; the
particles travel downwind. Firefox, Safari and iOS draw explicit trails (no 8-bit veil).
With reduced motion the particles become still arrows. On the dark basemap land is
translucent on the Wind layer so offshore flow at the coast stays visible.

**Wave colours (Upgrade 4).** The Wave Height layer paints offshore significant wave height
with its own ramp, anchored in metres and interpolated in metres: muted navy for calm seas
(#151d42 at 0 m), slate blue at 1 m, violet at 2 m, magenta-purple at 2.5-3 m, magenta at
4-5 m, pink at 6-8 m and pale pink at 10 m (#f3c4dc; higher seas keep that colour). Its
lightness rises with every anchor, so a bigger sea is always lighter and the scale still
reads in grey. Legend ticks: 0, 1, 2, 3, 4, 6, 8 and 10 m (0-30 ft). Wave Period, Wave
Energy and Wind Speed keep the shared ramp. On the dark basemap land is mid grey with a dark
coastline. The look follows the owner's reference screenshot; it is our own ramp, shapes and
code (no third-party map code, icons or palettes).

**Crest dashes.** Over the colour field, many short soft dashes drift with the waves: each
is one blob, a lighter tint of the colour under it, with a crisp leading edge on the crest
line and a short smear behind it (the smear and the fading in and out are display choices).
By default they show the **combined sea** ("Combined sea (peak)", closest to the reference);
the primary swell, secondary swell and wind sea are chosen in **Settings** when the model
run has them, told apart by shape and colour (light tinted blobs, split amber bars, short
cyan blobs), and a system the run lacks is disabled in the chooser. The physics is kept and
stated: deep water, L = gT²/(2π); each dash is born on a crest line and moves with it at the
phase speed, so in an even swell the dashes line up in rows one crest spacing apart (in real
model fields, where period and direction vary, they sit at random phases, as in the
reference). **Stated exaggeration**: the spacing is 0.12 px per metre of wavelength (12 px
at 8 s, 48 px at 16 s, clamped to 8-64 px), so it is exaggerated by E = 0.12 × metres per
pixel (about 4,700× at zoom 2 at the equator, 73× at zoom 8), and time runs 4× faster than
real; the legend prints E for the current view. Bigger seas draw longer, brighter dashes;
nothing is drawn under 0.2 m, and dashes stop at the coast on both basemaps. One dash per 12
px cell (13 for the secondary swell, 18 for the wind sea) keeps the density the same at
every zoom, and a draw budget that adapts to the device (up to 7,000 dashes a frame, at
least 1,500) holds the median frame near 2.5 ms (the e2e draw p95 was 3.1 and 3.4 ms on
2026-10-23). With reduced motion one still frame is drawn, with arrowheads. Text and
readouts give directions FROM; the dashes travel TO (a swell from the SW moves NE). The
map's display settings are kept in the browser under the key `swell.map.settings.v2`:
settings saved under v1 carry over (layer, units, basemap, animation, overlays) except the
wave systems, which reset to the combined sea.

**Navigation bounds.** Latitude stays inside 84°N–78.5°S with a hard edge (the wave grid's
extent), so a drag never shows blank rows; the minimum zoom is one world width for the
screen, re-measured on resize and rotation; maximum zoom 8 on the dark basemap. Longitude is
continuous across the date line: markers sit in the world copy nearest the view centre and
flights take the short way (Cloudbreak to Pipeline crosses 25° of longitude, not 335°). On
phones a selected spot lands between the layer tabs and the bottom sheet. All 50 spots fit
one world copy at 1440×807.

**Data sources.** NOAA GFS-Wave `global.0p25` and GFS `pgrb2.0p50` (`pgrb2.0p25` as a
per-hour fallback), regridded to 0.5° area-weighted means (directions as unit vectors) and
quantised to bytes; spot markers come from our predictions. The legend names the source,
model cycle, valid time with lead and "(forecast, not observed)". The default basemap is a
dark map drawn from bundled Natural Earth data (no tile server); the optional Streets
basemap uses OpenStreetMap tiles (section I).

**Limitations.** The crest dashes do not model refraction, shoaling or depth (deep water is
assumed) and their rows are a drawing device; 0.5° fields smooth fronts and coasts; where the
swell direction turns fast (storms, partition swaps) crests are left out rather than drawn
through a direction no wave has; partitions mixing in a 0.5° box are dropped by a heuristic
guard. Firefox and WebKit were not run here (only Chromium and its Pixel 7 emulation, with
software rendering; no GPU-backed browser or real phone was measured). At the
one-world desktop zoom the best-spots list covers the Philippines and Japan, including the
Cloud 9 marker (an open layout decision, docs/map-navigation.md).

## I. Other services and credentials

Every external service, with its terms, free tier, cost, attribution and setup steps:
[`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md).

| Service | Variables | Where credentials come from | Demo alternative |
|---------|-----------|-----------------------------|------------------|
| Forecasts: Open-Meteo GFS + GFS-Wave (the default live provider) | `WEATHER_PROVIDER=auto` (the code default) or `open_meteo`; `OPEN_METEO_API_KEY` and `OPEN_METEO_COMMERCIAL=true` for commercial use | No key for non-commercial use; commercial use needs a paid plan (open-meteo.com/en/pricing). CC BY 4.0: the attribution links are shown automatically | `WEATHER_PROVIDER=demo` (`.env.example`, Docker Compose, the tests) |
| Forecasts: Open-Meteo ECMWF family and the two-family blend (advanced, off) | `FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf` with `FORECAST_BLEND_ENABLED=true` (replaces the per-component refreshes with family-level syncs, F.2) | as above | — |
| Forecasts: NOAA GFS-Wave + GFS, direct (key-free fallback) | `FORECAST_PROVIDERS=noaa_gfs` (with `ecmwf_ifs` for a second family), `NOAA_*` tuning | None: NOAA open data on AWS | `WEATHER_PROVIDER=demo` |
| Forecasts: ECMWF IFS open data, direct (key-free fallback) | `FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs`, `ECMWF_*` tuning | None: CC BY 4.0 open data (attribution is shown automatically) | — |
| Forecast uncertainty: GEFS-Wave ensemble | `GEFS_ENABLED=true` | None: NOAA open data | Heuristic ranges without it |
| Forecasts: Windy Point Forecast (preferred once licensed) | `WINDY_API_KEY`, `WINDY_STORAGE_AUTHORIZED=true` (only with a written licence to store the data; refused without it), `WEATHER_PROVIDER=auto` or `windy`; check with `python -m app.cli forecast provider-check --provider windy` (docs/WINDY_API_SETUP.md) | api.windy.com/keys (paid Point Forecast plan) | Open-Meteo (the fallback) |
| Tides | `TIDE_PROVIDERS=noaa_coops,harmonic_model` (default), `NOAA_COOPS_APPLICATION` (an identifier such as the app's name and URL; an e-mail address is refused and replaced by `APP_NAME`) | None: NOAA CO-OPS (US spots) and EOT20 constants (CC BY 4.0, "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE", shown with the tide) | — |
| Flights: Duffel | `FLIGHT_PROVIDER=duffel`, `DUFFEL_ACCESS_TOKEN` | app.duffel.com → Developers → Access tokens (`duffel_test_…` = sandbox) | `FLIGHT_PROVIDER=demo` (default) |
| Flights: Amadeus Enterprise | `FLIGHT_PROVIDER=amadeus`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`, `AMADEUS_BASE_URL` | Your Amadeus Enterprise account manager | — |
| Email: SMTP | `EMAIL_PROVIDER=smtp`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`/`SMTP_USE_SSL`, `EMAIL_FROM` | Your mail provider; Mailpit locally needs none | `EMAIL_PROVIDER=console` (default) |
| Email: Resend | `EMAIL_PROVIDER=resend`, `RESEND_API_KEY`, `EMAIL_FROM` on a verified domain | resend.com → API keys | console |
| Email: SendGrid | `EMAIL_PROVIDER=sendgrid`, `SENDGRID_API_KEY`, `EMAIL_FROM` (verified sender) | app.sendgrid.com → Settings → API keys | console |
| SMS: Twilio | `SMS_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` or `TWILIO_MESSAGING_SERVICE_SID` | console.twilio.com | `SMS_PROVIDER=console` (default) or `disabled` |
| Map tiles (optional Streets basemap) | `NEXT_PUBLIC_CARTO_API_KEY`, or `NEXT_PUBLIC_MAP_TILE_URL` + `NEXT_PUBLIC_MAP_ATTRIBUTION`; optional `MAP_TILE_HOSTS`. Build time: run `docker compose build frontend` after changing | None for the default basemap (a dark map drawn from bundled Natural Earth data). The Streets basemap uses OpenStreetMap tiles (OSM tile usage policy); a free key from carto.com/basemaps/apikey switches it to CARTO Voyager | Bundled Natural Earth basemap (default) |

Inbound STOP/START: set your Twilio number's "A message comes in" webhook to
`POST ${API_BASE_URL}/api/webhooks/twilio/sms`. Requests are verified with
`X-Twilio-Signature`, so `API_BASE_URL` must be the exact public URL Twilio calls.

### Flight API

The default flight provider is `FLIGHT_PROVIDER=demo`: generated fares from fictional
carriers, labelled "Mock fares" / `is_mock` everywhere, never presented as real prices. No
real flight API is connected yet; this is the clearly labelled test adapter that spec
section 36 allows. Full rules and the provider research: [`docs/MATCHING_AND_FLIGHTS.md`](docs/MATCHING_AND_FLIGHTS.md);
accounts and costs: [`docs/API_SETUP_REQUIRED.md`](docs/API_SETUP_REQUIRED.md), section 2.10.

* **When flights are searched** (Swell 2.0, design D15): only for a new match of a saved
  search, a match that changed materially (its recommended travel dates or destination
  airport changed, or its surf window moved so the stored flights no longer fit, or an
  expired match qualifies again), "Re-check flights" on an opportunity, and once more right
  before an alert is sent when the best fare expired, sold out or rose above the budget.
  There is no flight timer, and an unchanged forecast never searches again. Identical trips
  share one cached search across users until the earlier of `FLIGHT_CACHE_TTL_HOURS` (6)
  and the expiry of its first offer.
* **Budgets**: `FLIGHT_DAILY_QUOTA` (300 provider searches a provider and UTC day, all
  users), `FLIGHT_USER_DAILY_QUOTA` (20 a day for one user; users are served round-robin)
  and `FLIGHT_MAX_QUERIES_PER_MATCH` (3; the first-choice date of every origin is always
  searched). Offers in a currency other than the budget's are compared only with
  `FX_RATES_USD_JSON` set, never guessed.
* **Preferences** per saved search: up to 5 origin airports, destinations (spots or
  regions), wave height range, minimum quality, maximum airfare, fixed or flexible dates,
  up to 3 layovers, maximum flight time, arrival and departure buffers (default: arrive 2
  days early, leave 1 day after), travellers, and the alert lead time (2-10 days by
  default). Swells 3-7 days ahead get a short-notice ranking boost
  (`SHORT_NOTICE_MIN_DAYS` / `_MAX_DAYS`). The initial markets are Australia to
  Bali/Indonesia and the USA to Central America; every spot's airports are in
  `data/surf_spots/airports.json`.
* **Duffel** (`FLIGHT_PROVIDER=duffel`, `DUFFEL_ACCESS_TOKEN`): self-serve, test tokens start
  with `duffel_test_`, live mode after Duffel's KYC check. Its Services Agreement forbids
  using it "for metasearch purposes" (https://duffel.com/services-agreement, read
  2026-10-27), and this app shows fares and links out: get Duffel's written confirmation
  before using it in production. The adapter is tested with recorded responses only.
* **Amadeus** (`FLIGHT_PROVIDER=amadeus`): its Self-Service portal was shut down on
  2026-07-17 according to secondary sources (docs/MATCHING_AND_FLIGHTS.md, section 7); the adapter is
  for an Amadeus Enterprise contract and is untested.
* **Affiliate APIs** (Travelpayouts, KAYAK, Skyscanner) were researched and are not built
  (docs/MATCHING_AND_FLIGHTS.md, section 7).
* Check the configuration with `python -m app.cli check-config` (exit 1 lists what is
  missing) and the adapters with `pytest tests/unit/test_flight_providers.py` (from
  `backend/`).

### Email delivery: sender and DNS verification

Local development needs nothing: Mailpit captures everything. For real delivery:

1. **Pick a provider** (`EMAIL_PROVIDER=resend`, `sendgrid` or `smtp`) and set its key and
   `EMAIL_FROM` to an address on a domain you control, e.g.
   `Swell Travel Agent <alerts@mail.your-domain.com>`.
2. **Verify the sending domain** with the provider. It gives you DNS records to add:
   * **SPF**: a TXT record (or an `include:` in your existing SPF record) authorising the
     provider's servers;
   * **DKIM**: one or more CNAME/TXT records with the provider's signing keys;
   * **DMARC**: a TXT record at `_dmarc.your-domain.com`, e.g.
     `v=DMARC1; p=none; rua=mailto:dmarc@your-domain.com` to start with;
   * for Resend and SendGrid, a custom return-path/bounce domain (CNAME) is recommended.
   Wait until the provider dashboard shows the domain as verified; unverified senders are
   rejected or land in spam.
3. **Delivery confirmation webhooks** (optional, recommended). The app marks an email
   `accepted` when the provider takes it and only moves it to `delivered`, `bounced` or
   `complained` when a signed webhook says so:
   * Resend: add a webhook to `${API_BASE_URL}/api/webhooks/resend` and set
     `RESEND_WEBHOOK_SECRET` (its Svix signing secret);
   * SendGrid: enable the signed Event Webhook to `${API_BASE_URL}/api/webhooks/sendgrid`
     and set `SENDGRID_WEBHOOK_PUBLIC_KEY`.
4. **Test from the app**: make your account an admin
   (`python -m app.cli make-admin you@example.com`), verify your email, then use
   `POST /api/dev/test-email` (dev endpoints): it sends only to your own verified address.
5. Production refuses capture-only providers (`console`, `mailpit`) at start-up.

What is sent: account emails (the verification link expires after
`EMAIL_VERIFICATION_TTL_HOURS`, 48, and can be sent again through
`POST /api/auth/resend-verification`; a password-reset link expires after
`PASSWORD_RESET_TTL_MINUTES`, 60), swell alerts (spot, swell dates, quality, breaking
height, wind, forecast confidence, travel dates, fare, airports, booking and dashboard
links, one-click unsubscribe; content and rules in
[`docs/MATCHING_AND_FLIGHTS.md`](docs/MATCHING_AND_FLIGHTS.md), sections 4 and 6), and the
admin alerts of section F.2, which go to verified admin accounts only, never to users.
Alerts are de-duplicated per user, swell, channel, kind and fingerprint, and a new model
run alerts only on a new match or a material change. Each email has an idempotency key
(Resend's `Idempotency-Key` header). Temporary failures are retried with backoff up to
`NOTIFICATION_MAX_ATTEMPTS` (5); SMTP replies are classified by code, 4xx retried, 5xx
permanent; a refused key raises the admin problem `provider_credentials:email_<provider>`.

Real provider delivery was not verified from the build environment (no credentials or
DNS); Mailpit delivery is verified end to end.

### Map tiles

The map works without configuration: the default basemap is a dark vector map drawn from
Natural Earth data bundled with the app (no tile server). The "Streets" basemap in the
map's settings uses OpenStreetMap tiles (attribution shown, light use only under the OSM
tile policy). For more traffic on that basemap use a tile provider:

* CARTO: `NEXT_PUBLIC_CARTO_API_KEY=<key>` (free keys at carto.com/basemaps/apikey;
  key-less CARTO raster tiles now show "API KEY REQUIRED").
* Any XYZ provider: `NEXT_PUBLIC_MAP_TILE_URL=https://…/{z}/{x}/{y}.png` and
  `NEXT_PUBLIC_MAP_ATTRIBUTION="© …"`.
* These are build-time variables: run `docker compose build frontend` after changing them.
  The tile host is added to the Content-Security-Policy automatically; extra hosts go in
  `MAP_TILE_HOSTS`.

### Switching from demo to live

1. Put the settings and credentials in `.env` (never commit it), for example (Swell 2.0:
   one weather provider, section F.2):

   ```env
   WEATHER_PROVIDER=auto
   FORECAST_PROVIDERS=
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

## J. Testing

Tests need PostgreSQL and Redis. With Docker, create the test database once:

```bash
docker compose up -d postgres redis
docker compose exec postgres createdb -U swell swell_test
```

**Backend** (from `backend/`). Unit tests need no database. Integration tests use the real
database `swell_test` and Redis db 15, start a real Celery worker thread and round-trip the
Alembic migrations on a scratch database; run them as a separate process, as CI does:

```bash
cd backend
source .venv/bin/activate
ruff format --check .
ruff check .
mypy app
alembic upgrade head
alembic check
pytest tests/unit
pytest tests/integration
```

Override the test database with `TEST_DATABASE_URL` / `TEST_REDIS_URL` if needed (the
integration tests drop that database's schema, so they refuse a name without `test` in it
unless `ALLOW_ANY_TEST_DB_NAME=1`). Read exit codes directly, not through a pipe
(`pytest … | tail` reports `tail`'s exit code). The backup-script tests need the PostgreSQL
16 client tools and are skipped without them.

**Frontend:**

```bash
cd frontend
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

**End-to-end** (Playwright). `scripts/run-e2e.sh` recreates the `swell_e2e` database,
starts the backend on :8100 in demo mode, builds and starts the production frontend on
:3100 and runs the browser tests in two projects: desktop Chromium and a Pixel 7 phone
(`mobile`). They cover register → verify → login → map → spot → forecast timeline (days,
selected step, waves, wind direction, tide, confidence) → saved search → simulated swell →
mock flights → alert queued → alert sent → dashboard → pause, the map's bounds, date line,
layers and wavefronts, and the 15-step upgrade scenario (`e2e/scenario.spec.ts`).

```bash
cd frontend
npx playwright install chromium
cd ..
./scripts/run-e2e.sh
./scripts/run-e2e.sh e2e/scenario.spec.ts
E2E_MAILPIT=1 ./scripts/run-e2e.sh
```

`npx playwright install chromium` is needed once. The script expects the backend
virtualenv at `backend/.venv` and the frontend dependencies installed; set
`PLAYWRIGHT_CHROMIUM_PATH=/path/to/chrome` for a Chromium installed elsewhere, and
`E2E_BACKEND_PORT`, `E2E_FRONTEND_PORT`, `E2E_DATABASE_URL`, `E2E_REDIS_URL` to run beside
another stack. `E2E_MAILPIT=1` sends the emails through a Mailpit container (needs Docker)
instead of the console capture file. The e2e suite uses the demo forecast, synthetic map
fields and tides from route mocks, and mock flights only; it never claims real email
delivery or real fares.

**Latest full run** (Swell 2.0, the final gate; the Upgrade 4 run is in
docs/UPGRADE_REPORT.md section 40). Run on 2026-10-30 by the final gate on `wf6/int`, on
this development machine, on commit c1490b3: the code and tests of 0899b83 (the last commit that changes code or
tests) plus documentation. The two commits after it change only documentation: two
places that still said the daily cron acquires the tides without the daemon (ffca5d3),
then these results (this one, docs/UPGRADE_REPORT.md section 49 and docs/architecture_audit.md 8.4), the file list of docs/architecture_audit.md 8.2,
the limitations (docs/UPGRADE_REPORT.md section 51, docs/architecture_audit.md 8.5)
and docs/local-vercel-stack.md. Each check ran once on that code (the map-performance
spec once more, as its timing bound allows); the backend's unit and integration suites
as separate runs, every exit code read directly. The e2e suite was started a second time
because the machine rebooted during the first run (76 tests had passed and none had
failed); the complete second run is counted.

| Check | Result |
|---|---|
| `ruff format --check .`, `ruff check .`, `mypy app` | pass (425 files formatted, 219 files type-checked) |
| `alembic upgrade head`, `downgrade -1`, `upgrade head`, `alembic check` on an empty database | pass: 0001 to 0009, no new operations |
| Upgrade of a copy of a real database (at 0006: 50 spots, 25,000 forecast rows and predictions) to 0008, then from 0008 to head | pass: row counts and a checksum (md5) of the forecast rows identical before and after; `alembic check` clean; no version until the first refresh |
| `pytest tests/unit` | 2,911 passed, 6 skipped (the lean-install tests), 84 s |
| `pytest tests/unit/test_lean_imports.py` with `LEAN_PYTHON` (a lean install from `uv.lock`) | 32 passed, a demo spot-only refresh in the lean install included |
| `pytest tests/integration` | 671 passed, 3 skipped (the PgBouncer tests, without `TEST_PGBOUNCER_URL`), 19.0 min. `test_map_fields.py::test_a_fields_only_run_never_changes_spot_forecasts`, order-dependent until 24d114e, passed in it, and also alone, first, right after the Redis-down tests, with its own file, and last (after the 72 other integration files: 631 passed, 3 skipped) |
| `npx tsc --noEmit`, `npx eslint . --max-warnings=0`, `npx vitest run` | pass; Vitest 628 passed (53 files) |
| `npm ci`, `npm run build` | pass |
| `./scripts/run-e2e.sh` (Chromium and Pixel 7) | 154 passed, 1 failed (18.7 min). `e2e/live-update.spec.ts` (through the real refresh, in Chromium: publication to the open map 4.3 s and to the spot page 4.7 s with the e2e build's 2 s poll) and `live-update-mocked.spec.ts` (Chromium and Pixel 7) passed. Failed: `e2e/map-performance.spec.ts`, the crest-dash frame draw time p95 6.6 ms against its 5 ms bound; re-run once, alone, on the idle machine (load 0.7): 5.2 ms, failed again. No frontend code changed since review round 4, where it measured 4 ms, and every frame cost the test records rose alike (crest dashes 1.28 to 2.17 ms of script per frame, wind particles 22.3 to 28.8-32.1 ms of task time per frame), which points to this machine's speed after its reboot rather than the code; the bound was not changed (docs/UPGRADE_REPORT.md section 51) |
| `scripts/vercel-build-check.sh --offline` | `RESULT: ok`, API function 128.3 MiB (3,469 files; limit 500 MiB), `maxDuration` 300 s (2026-10-30); the daily cron is in the output config; the function's files hold no `data/` (docs/UPGRADE_REPORT.md section 51, the tides) |
| `backend/.venv/bin/python scripts/check_api_bundle.py --max-mb 200` | 163.7 of 200 MB |
| `docker compose config -q` (`docker-compose.yml`, `docker-compose.vercel.yml`) | pass, each with `.env.example`, `.env.vercel.example` and no env file |
| shellcheck 0.11 on `scripts/run-e2e.sh`, `scripts/vercel-local.sh`, `scripts/vercel-build-check.sh`, `scripts/export-openapi.sh` and (`-s sh`) the operations scripts | clean (the CI's two commands; `run-e2e.sh` and `vercel-local.sh` are the shell scripts 2.0 changed) |
| `npm audit --omit=dev` | 1 moderate advisory (fast-uri below 3.1.9, under @hookform/resolvers and ajv; docs/decisions.md ADR-037). Dev-only: Vitest 3's tinypool (2 critical) and @vitest/mocker (moderate), not upgraded |
| The local Vercel-style stack (2026-10-30, demo data, docs/local-vercel-stack.md) | built and run on this code: the build command migrated and seeded an empty database; `/api/internal/refresh` 401 without or with a wrong token; six simulated model runs refreshed through the edge, versions 2 to 7, an open map and spot page updated without a reload 12 of 12 times; register and verify through Mailpit; the daily cron 200 with its `jobs` summary; `smoke` 22 of 22 shell checks and 6 of 6 browser checks after one fix (0899b83); with the daemon stopped the cron's `tides.acquire` failed (docs/UPGRADE_REPORT.md section 51) |

Earlier rounds on the same branch: review round 1 (the code of 5b9ce29) 2,882 unit, 638
integration, 604 Vitest and 154 e2e tests passed; review round 2 (a4560d6) 2,889, 650, 614
and 154; review round 3 (1775c67) 2,894, 657, 622 and 154; review round 4 (c85474f) 2,894,
671, 628 and 155. Every test added since is counted above; none was removed or weakened.
Expectations changed on purpose: the build seed without `data/` now fails on an empty
database and skips only when spots exist, so its unit test is split into those two cases
(review r3-spec-SPEC-6); the Vercel smoke's map check with the ocean layers off
(`MAP_FIELDS_ENABLED=false`, the stack's default) expects the "Spot forecasts only" note
instead of the outage notice (review r4-spec-R4-SPEC-2); at the final gate, the fields-only
map test counts the served `version_id` as bookkeeping and asserts that it moves exactly one
per run of the stages (every publication is a version since 2.0; the test passed in full
runs only because an earlier test's Redis fail-open window left the version marker
unwritten, and the test fixture now forgets that window, 24d114e), and the Vercel smoke
expects the daily cron's `jobs` key, answered since review round 1 (0899b83).

**The final documentation pass** (2026-10-30, on top of e29141a; documentation, the two env
examples, one new and one extended unit test, no application code) re-ran: `ruff format
--check .` and `ruff check .` (425 files), `mypy app` (219 files, no issues), `pytest
tests/unit` with `LEAN_PYTHON` (2,917 passed, 0 skipped, 93.5 s: the 15 tests of the new
`tests/unit/test_readme_commands.py` and 2 new ones in `test_api_setup_doc.py` included),
the 117 doc-reading unit tests, `docker compose config -q` with `.env.example` and with
`.env.vercel.example`, `scripts/generate_spot_research_docs.py --check` (exit 0) and
`scripts/measure_refresh_egress.py` (docs/sync.md, "Measured (2.0)"). The integration
suite, Vitest, the e2e suite and the offline Vercel build were not re-run: none of them reads
a file this pass changed.

**Continuous integration** (`.github/workflows/ci.yml`, GitHub Actions) runs on every
push and pull request: a backend job (PostgreSQL 16 and Redis 7 services, Python 3.13, uv
0.11.32: `uv lock --check`, the install from `uv.lock`, `ruff format --check`, `ruff check`,
`mypy app`, shellcheck of the operations scripts, the lean-install import test and the API
bundle check, `alembic upgrade head` + `alembic check`, then `pytest tests/unit` and
`pytest tests/integration` as separate steps), a frontend job (Node 22: `tsc`, `eslint`,
`vitest`, `next build`), a `deploy-config` job (actionlint, both compose files, a Docker
build of the daemon's `Containerfile` and of `backend/Dockerfile`) and a `vercel-build` job
(`scripts/vercel-build-check.sh --offline`). The end-to-end job runs on pushes and on demand
(Actions → CI → Run workflow). These jobs were first run locally, step by step; all five
have run on GitHub since 2026-10-23 (for example run 40062509283 on 15a2754, all passed).

**Tools added in Upgrade 4.** `scripts/vercel-build-check.sh --offline` (above; section E.6),
`scripts/vercel-local.sh` (the local Vercel-style stack; `up`, `sync`, `check`, `status`,
`cron`, `smoke`, `logs`, `psql`, `bundle`, `down`) and `scripts/measure_db_egress.py`, which
counts the bytes PostgreSQL sends to the app (what Neon bills as egress) for a sync pass
(`pipeline`), the periodic jobs (`jobs`) or page views (`pages`) on a scratch copy of a
database. One demo pass on a fresh scratch database (PostgreSQL on localhost with the
`swell` role, any free Redis database number):

```bash
backend/.venv/bin/python scripts/measure_db_egress.py pipeline --redis-url redis://localhost:6379/3 --prune
```

It prints the bytes per stage and drops its scratch database (`--keep-db` keeps it); run on
2026-10-23 it reported 11.18 MB for the pass, the verifier's figure. The Neon Free budget in
docs/map-layers.md was measured the same way on a copy of a recorded live run
(`--template <database> --replay --no-acquire --searches 3`, three synthetic saved
searches): 21.28 MB per both-family sync pass. `--help` lists the options.

**Tools added in Swell 2.0** (run from the repository root with the backend's Python):

```bash
backend/.venv/bin/python scripts/measure_refresh_egress.py --redis-url redis://localhost:6379/3
backend/.venv/bin/python scripts/load_test_public.py --base-url http://localhost:3100 --visitors 1000 --concurrency 64
backend/.venv/bin/python scripts/generate_spot_research_docs.py --check
```

`measure_refresh_egress.py` creates a scratch database with the demo provider and measures,
through the refresh route, a first refresh, a check with nothing new, a wave-only and an
atmosphere-only refresh: wall time, CPU, database egress and Redis commands per call (it
empties the Redis database it is given and drops its scratch database; `--keep-db` keeps
it). `load_test_public.py` loads one spot's public reads as 1,000 visitors against a
running stack (here the local Vercel-style stack's edge on :3100) and prints p50/p95, the
errors and whether every visitor got the same body; the deployment verifier's run is in
docs/UPGRADE_REPORT.md section 50. `generate_spot_research_docs.py` writes
`docs/surf_spot_research/` from the data files; `--check` exits 1 when a document is out of
date (a unit test runs the same check).

**Regenerate frontend API types** after changing backend schemas. This writes
`frontend/types/openapi.json` and `openapi.d.ts`, including the dev routes:

```bash
./scripts/export-openapi.sh
```

## K. Debugging

| Symptom | Cause and fix |
|---------|---------------|
| **Map:** grey or black map with markers, "Map tiles couldn't load" | Only on the Streets basemap: the tile server is unreachable (network, firewall, content blocker) or the URL is wrong. Open a tile URL from the browser's Network tab. The configured tile host is allowed by the CSP automatically; add others with `MAP_TILE_HOSTS`. The default dark basemap needs no tiles |
| **Map:** "API KEY REQUIRED" tiles | CARTO raster basemaps require a key. Set `NEXT_PUBLIC_CARTO_API_KEY` and rebuild the frontend (`docker compose build frontend && docker compose up -d`), or use the default basemap |
| **Map:** ocean layers missing, "Ocean layers are unavailable right now" | Map layers need `MAP_FIELDS_ENABLED` (unset = on with a live spot provider, off with demo only; `GET /api/map/fields` says which). With `MAP_FIELDS_ENABLED=false` they are off on purpose: the map shows "Spot forecasts only" and no layer tabs instead (the index answers `enabled: false`). Run `python -m app.cli engine map-fields` once to fetch a cycle. A field whose run or valid time does not match the request is never drawn: the map re-reads the field index (at most every 30 s) and shows what the index lists |
| **Map:** layers drawn but no particles or crests | Settings → "animated flow" is off (it switches the particles and crests off). With the system's reduced-motion setting they are drawn still (arrows, still crests). A wave system the run lacks is greyed out in the chooser ("Not available for this model run"), and crests are not drawn under 0.2 m |
| **Wind direction looks wrong:** "offshore" where you expect onshore, or arrows the wrong way | Text and data say where the wind comes FROM; arrows on the charts and the map point where it travels TO (an arrow pointing east is a wind from the west). Offshore means from land to sea at that break: the spot's researched `offshore_wind_direction` (`data/surf_spots/`) decides it, never the compass alone. "Sources disagree about which wind is offshore" means the research is disputed (`python -m app.cli research lint`). "Models disagree on the wind direction" means GFS and ECMWF blow from directions more than 60° apart: the blend takes the direction of the mean wind vector and forecast confidence is at most Moderate. The map's 0.5° wind can differ from the spot's own point wind near coasts |
| **Stale forecasts:** "Forecast out of date" banner, chip or badge, "Forecast last updated N hours ago" | Two rules, shown separately. A **spot** is stale ("Forecast last updated 7 hours ago. Not a current forecast.") when its latest refresh failed while other spots updated, when it has no data, or when a component is older than its model's update interval (6 h for GFS and GFS-Wave) plus `FORECAST_STALE_THRESHOLD_MINUTES` (360; per model `FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL`), so 12 h by default (`versions.spot_freshness`, design D10). A **model run** is stale once it is older than its source's own limit, its cycle interval + publication lag + `FORECAST_REFRESH_MINUTES` + 2 h, about 17.2 h for the GFS family and 24.7 h for the ECMWF family (`FORECAST_STALE_HOURS`, 18 h, applies only to demo, Windy and the blend fallback; `docs/FORECAST_SOURCES.md`, "Staleness"): that caps the forecast confidence at Moderate, and alerts pause only at twice that age (`STALE_ALERT_FACTOR`). Map layers are stale after `MAP_FIELD_STALE_AFTER_HOURS` (21 h). Usually the provider is unreachable, or nothing calls the refresh: in Docker beat and worker; on Vercel the refresh scheduler of docs/deploy-vercel.md step 7b (QStash on Hobby, a Vercel Cron entry on Pro), with the daily cron as a once-a-day backstop. Check `/admin/freshness` (signed in as an admin: the "Refresh scheduler" row shows the last refresh check and who made it, and each stale spot with its reason), `/api/forecasts/status` and `/status` (each component's state, for example "waiting" while Open-Meteo is still publishing a run); in Docker `docker compose logs worker`, or `python -m app.cli sync check`. A spot whose own refresh failed keeps its previous forecast, says so, and is retried 5, 15 and 45 minutes later |
| **Missing wave data:** "No forecast yet", or no secondary swell / wind-sea row | Check `/status` → *forecast.acquire*; `python -m app.cli pipeline` shows each provider's result synchronously. Partitions exist only where the model has them: the ECMWF family publishes none, so the blend takes them from GFS-Wave; a system the model does not have is left out, never drawn as 0. A spot whose nearest Open-Meteo wave cell is masked reads the nearest neighbouring cell with data (recorded in the run's warnings) |
| **Broken chart synchronisation:** timeline rows out of step, or a `?t=` link opens the wrong time | Every row draws from one `/api/spots/{slug}/timeline` response on one time axis, and the selected time snaps to the nearest stored step. Check that response (`curl "http://localhost:8000/api/spots/pipeline/timeline?days=10"`) and the browser console. "The forecast timeline could not be loaded" means that request failed (the **Try again** button refetches). `?t=` takes an ISO time in UTC |
| **API authentication or quota errors** from providers | A failed run shows the provider's reason on `/status` and in `docker compose logs worker`; 401/403 and other 4xx answers are permanent errors and are not retried. Open-Meteo: with `OPEN_METEO_API_KEY` set, requests go to the `customer-*` hosts, and a 4xx answer (for example a wrong key) fails the run with Open-Meteo's own reason (mock-tested only: no paid key here); "Daily API request limit exceeded" on the free tier means the IP address used its 10,000 calls (shared addresses count other users too): wait for the next day or use a key. Flights: fix `DUFFEL_ACCESS_TOKEN` / `AMADEUS_*`, run `check-config`, then **Refresh flights** on the opportunity. Email and SMS: the notification shows `failed`/`rejected` with the provider's reason. Windy: `WEATHER_PROVIDER=windy` without `WINDY_API_KEY` and `WINDY_STORAGE_AUTHORIZED=true` is refused at start-up in production (with `auto` Open-Meteo serves instead); a Windy key, testing-key, quota or availability failure falls back to Open-Meteo and `python -m app.cli forecast provider-check` shows why (docs/WINDY_API_SETUP.md, section 10). Refused keys raise the admin problem `provider_credentials` (`/admin/freshness`) |
| **App authentication:** login refused, "Missing or invalid CSRF token", logged out | Sessions are HttpOnly cookies on the frontend's origin: use the app through http://localhost:3000, not the API port. With HTTPS set `COOKIE_SECURE=true`, without it `false`. `LOGIN_MAX_FAILURES` (8) failed logins lock an account for `LOGIN_LOCKOUT_MINUTES` (15) |
| **Excessive memory:** worker killed, or a job stays "running" until `job_stuck` | The prediction job peaks at about 0.6 GB, the map-field job at about 0.57 GB, GEFS at about 0.42 GB (measured 2026-10-16) and the GRIB fallbacks more (`docs/operations.md` §5 and §6). Give the worker more memory or lower `WORKER_CONCURRENCY`, or move downloads to the `bulk-worker` profile; `/status` shows `job_stuck` until the lock TTL expires. Redis near `REDIS_MAXMEMORY` (256 MB) shows as `redis_memory`: with `noeviction` writes then fail, so raise the cap rather than enabling eviction. In the browser the map's field cache is capped at 64 MB |
| `/status` or `/api/health/data` says *degraded* or *failing* | Each reason names the source, job or layer and its limit. Follow the incident playbook in [`docs/operations.md`](docs/operations.md); `scripts/check-health.sh` prints the same reasons from a shell. "No future tide predictions from NOAA CO-OPS" means the worker cannot reach `api.tidesandcurrents.noaa.gov` (no outbound network or DNS from the containers); EOT20 tides are computed locally and still work. A database above `DATA_HEALTH_DATABASE_MAX_GB` is an alert only (size it from the disk) |
| `/status` says `sync_heartbeat_stale` (failing) | The sync daemon has stopped checking for new model runs: on Vercel, look at the project's Daemons page and the daemon's CPU quota (5 Active CPU-hours a month on Hobby; when it runs out the daemons stop until the 1st of next month). With the hosted profile the daemon is optional and only the map layers age (degraded); the spot forecasts keep refreshing through `/api/internal/refresh`. Playbooks: [`docs/operations.md`](docs/operations.md), section 5 |
| `/status` says `sync_failed` or `licence` | `sync_failed`: the last two syncs failed (the admin email carries the error; `python -m app.cli sync status` lists them). `licence`: `COMMERCIAL_USE=true` without `OPEN_METEO_API_KEY`, so the Open-Meteo sources refuse to run |
| The map is blank or says "No forecast has been ingested yet" after an upgrade | Run `python -m app.cli migrate` (migration 0008 records one publication of the data stored before Upgrade 4; 0009 adds `forecast_versions`). Until the first Swell 2.0 refresh publishes a version, the site serves the runs of the configured sources only, so a database filled by `open_meteo,open_meteo_ecmwf` and served with `demo` shows no forecast: keep the weather provider of the stored data (`WEATHER_PROVIDER=auto` serves the stored `open_meteo_noaa` runs), then run one refresh (`POST /api/internal/refresh` or `python -m app.cli sync run`); from then on the published version decides what is served |
| **An open page does not pick up a new forecast** | The page polls `GET /api/forecasts/version` every `NEXT_PUBLIC_FORECAST_VERSION_POLL_MS` (20 s) while the tab is visible, and at once when it becomes visible again; a hidden tab does not poll. Compare `curl -s http://localhost:8000/api/forecasts/version` with the label on the page ("Forecast updated hh:mm"). The version is written to Redis after the refresh's `detect` and `match` stages, so it appears a few seconds after `published_at`; on Vercel the CDN shares the answer for `FORECAST_VERSION_CDN_SECONDS` (15). `NEXT_PUBLIC_*` values are build-time: rebuild the frontend after changing them |
| `/api/internal/refresh` answers 404, 401, `busy` or `not_due` | 404: `CRON_SECRET` is not set on that deployment (16 characters or more); 401: the bearer token does not match; `busy`: another refresh holds the lease (the next check comes anyway); `not_due`: nothing new, and the answer's `plan` says why per component (`current`, `waiting` with the time it becomes servable, `unknown` when the metadata could not be read); `disabled`: `FORECAST_REFRESH_ENABLED=false` |
| `connection refused … 5432` / `/api/health/ready` returns 503 with `"database": "unavailable"` | PostgreSQL isn't running or `DATABASE_URL` is wrong. Run `docker compose up -d postgres` and `docker compose ps` (wait for *healthy*). Outside Docker, `DATABASE_URL` must use `localhost`; inside compose it is set to `postgres` automatically |
| `extension "btree_gist" is not available` during migration | A PostgreSQL build without contrib modules. Use the `postgres:16-alpine` image or Homebrew `postgresql@16`, which include it |
| Redis `Connection refused` / readiness shows `"redis": "unavailable"` | Run `docker compose up -d redis`. Rate limiting fails open, but Celery and job locks need Redis |
| Background jobs not executing | Under `JOB_RUNNER=daemon` with the hosted profile there is no worker. On Vercel without the optional daemon the refresh endpoint runs the refresh and the pending kicks, a due email retry and the hourly data-health check at each call, and the daily cron (`GET /api/internal/cron/daily`, 07:00-07:59 UTC on Hobby) runs every periodic job whose last run is older than its interval (cleanup, matching housekeeping, tides, alerts, delivery, the staleness check; the tides fail in the function, which has no `data/`: run `python -m app.cli tides acquire` from a checkout, docs/deploy-vercel.md section 4). So check that the refresh scheduler calls (`/admin/freshness`, "Refresh scheduler") and that the daily cron ran (its answer, `jobs` key, and the job list on `/status`); the optional map daemon only refreshes the map layers. The local Vercel-style stack has no external scheduler: its daemon refreshes the spots, the ensemble and the map (`SYNC_DAEMON_KINDS=spots,ensemble,map`) and runs the periodic jobs, so there check the daemon (`scripts/vercel-local.sh status` shows its heartbeat and newest syncs). Under Celery: `docker compose ps` should show `worker` and `beat` healthy, and `docker compose logs beat` should list "Sending due task". `/status` shows each job's last run. A job stuck "running" holds its Redis lock until the lock TTL expires; restart the worker if it crashed. Outside Docker, remember the separate worker and beat processes, and that a worker started with `-Q celery` alone never runs downloads |
| `beat` shows *unhealthy* | Its schedule file has not been written for 10 minutes: beat is stuck or cannot reach Redis. `docker compose logs beat`, then `docker compose restart beat` (run exactly one beat) |
| No verification / alert email | In Docker, open Mailpit (http://localhost:8025). Console mode writes to the outbox file. The dashboard banner and `/notifications` show each email's status: `failed`/`rejected` carry the provider's reason; `accepted` means the provider took it but has not confirmed delivery (check the provider's logs, your domain's SPF/DKIM/DMARC, and spam folders) |
| No data-health alert email although the status is degraded | Alerts go to active admin accounts with a verified address only (`python -m app.cli make-admin you@example.com`). Each problem is emailed once, then every `DATA_HEALTH_ALERT_REPEAT_HOURS` (12 h); `DATA_HEALTH_ALERTS_ENABLED=false` switches them off |
| Opportunity is "surf only" with "flight provider error" | Invalid or expired flight credentials (see "API authentication" above), or the provider returned nothing |
| Opportunity says "quoted in EUR (no FX rate configured)" | Offers in another currency are never guessed into your budget. Set `FX_RATES_USD_JSON` (USD per unit of each currency, as `.env.example` shows), or use the budget currency the provider quotes |
| Backend exits with `ValidationError` on start | A setting is invalid (e.g. `APP_ENV=production` with the default `SECRET_KEY`, `demo` mixed with live providers, or an unknown provider). The message names the field |
| `Bind for 0.0.0.0:5432 failed: port is already allocated` | Another Postgres, Redis or app is using the port. Change the host port in `.env` (`POSTGRES_PORT`, `REDIS_PORT`, `BACKEND_PORT`, `FRONTEND_PORT`), or stop the local service (`brew services stop postgresql@16`) |
| Pages show "The service is temporarily unavailable" / `/api/*` returns 500 | The Next.js server can't reach the backend: check `docker compose ps backend`. If the backend is up, `BACKEND_URL` is wrong. It is baked in at build time: rebuild the frontend after changing it (`docker compose build frontend`) |
| `noaa_gfs` run fails with "no complete NOAA GFS cycle" | The newest cycles are still uploading (f240 lands about 5 h after the cycle) or NOAA changed the file layout (404 on every file). Check one URL from the error; adjust `NOAA_GFS_WAVE_PATH_TEMPLATE` / `NOAA_GFS_ATMOS_PATH_TEMPLATE` after a layout change |
| ECMWF logs `503 SlowDown` | The ECMWF bucket throttles bursts. The provider paces and retries; occasional 503s are normal. Persistent ones: lower `GRIB_DOWNLOAD_WORKERS` |
| `ImportError: eccodes` / `Cannot find the ecCodes library` | ecCodes ships as a binary wheel (`eccodeslib`) for Linux x86-64/arm64 and macOS; reinstall with `pip install -e ".[dev]"`. On other platforms install the system library (`brew install eccodes`, `apt install libeccodes0`) |
| A spot shows many "models disagree" hours | Expected near sheltered coasts, where the models' nearest sea cells differ (e.g. Rincon, Trestles); the range widens instead of trusting one model. Check the chosen cells in `wave_forecasts.extra` |

## L. Deployment

**Recommended: Vercel** (Upgrade 4, Swell 2.0). Section E.7 and
[`docs/deploy-vercel.md`](docs/deploy-vercel.md): the web and api services (the forecast
refresh runs in the api function) and the optional map-layer daemon in one Vercel project,
Neon Postgres and Upstash Redis from the Vercel Marketplace, a 5-minute refresh schedule
(Upstash QStash on Hobby, Vercel Cron on Pro) and the daily cron; test it first with the
local Vercel-style stack (section E.6).
Operations on Vercel (quotas, meters, runbooks): [`docs/operations.md`](docs/operations.md).

**Any container host.** A production deployment on your own servers needs the same five
processes:

| Component | Managed option |
|-----------|----------------|
| PostgreSQL 16 with `btree_gist` | AWS RDS / Aurora, Google Cloud SQL, Neon, Supabase, Crunchy Bridge |
| Redis 7 | ElastiCache, Memorystore, Upstash (TLS: use `rediss://` URLs), Redis Cloud |
| API (`backend` image, `api` role) | ECS/Fargate, Cloud Run (min instances ≥ 1), Fly.io, Render, Kubernetes |
| Worker + beat (same image, `worker` / `beat` roles), or instead one sync daemon (same image, `sync` role, `JOB_RUNNER=daemon`) | **Persistent** containers: ECS services, Fly machines, Render background workers. Not serverless functions. Run **exactly one** beat instance (or one daemon, never both); scale workers horizontally (both queues must be consumed) |
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
* Use commercial terms for Open-Meteo (`OPEN_METEO_API_KEY`), live Duffel or Amadeus
  Enterprise, a verified sending domain (SPF/DKIM) for email, and a registered Twilio sender.
* Size `DATA_HEALTH_DATABASE_MAX_GB` from the disk (section F.3).
* Monitoring ([`docs/operations.md`](docs/operations.md)):
  * `/api/health/ready` for load balancers (database and Redis reachable);
  * `/api/health/data` for data health: every forecast source's age against its own
    stale limit, every scheduled job's last result (failed, overdue, stuck), map-layer,
    tide and buoy freshness, Redis memory and database size, and a job heartbeat, summed
    up as `ok` / `degraded` / `failing` with reasons. Public (operational metadata only,
    rate limited); `?strict=true` answers 503 when failing. `scripts/check-health.sh URL`
    wraps it for cron (exit 0 / 1 / 2 / 3);
  * the `ops.data_health` job emails every active, verified admin account when a problem
    appears, again at most every 12 h while it lasts, and once more when it clears. A
    stopped worker cannot send that email, so also poll `/api/health/data` from outside.
* Backups: `scripts/backup-db.sh` (pg_dump custom format, checksummed, retention by count
  and age) from cron or the compose `backup` profile, `scripts/restore-db.sh` to restore,
  and `scripts/restore-drill.sh` to prove a backup restores (exact row counts per table).
  With managed PostgreSQL, keep its snapshots as well. Redis holds only transient state
  (queue, locks, counters, the small response cache).
* Capacity, bandwidth and retention for a single small server: docs/operations.md
  "Capacity".

## M. Limitations

**Fully implemented and tested here**

* Accounts, sessions, CSRF, rate limits, account deletion.
* Spot dataset and seeding.
* Scoring, detection, matching, travel windows, filtering and ranking.
* Alert generation, de-duplication and delivery.
* All Celery jobs and the sync daemon, plus the web app and its pages.
* The Docker Compose stack: built and run again on 2026-10-16 (seven services healthy, the
  first pipeline run completed, the `bulk-worker` profile splitting the queues), earlier
  including a real Celery worker delivering alerts to Mailpit.
* Data-health monitoring (`/api/health/data`, the status page, the `ops.data_health`
  admin alerts with de-duplication and recovery notices), the backup, restore and
  restore-drill scripts and the compose `backup` profile (docs/operations.md).

**Swell 2.0: verified here, and what is not verified yet**

* Verified on this development machine (2026-10-27 to 29; docs/UPGRADE_REPORT.md, "Swell
  2.0"): the refresh endpoint with live Open-Meteo data (a refresh of both components, 125
  weighted calls; a natural wave-only refresh of the 00Z GFS-Wave run, 75 weighted calls,
  the wind carried forward with its 18Z model time); demo refreshes through the local
  Vercel-style stack with open browsers updating without a reload (publication to new data
  on screen p50 13.8 s, 16 samples, no CDN; on the final code, 2026-10-30, p50 16.5 s, 12
  samples, smoke 22 of 22 and 6 of 6); a load test through the local edge (4,000
  requests, 64 at a time, 0 errors; a new version reached every route 3.2 s after
  publication);
  the daemon-less hosted path (kicks, delivery retries and overdue periodic jobs run by the
  refresh endpoint and the daily cron, `tests/integration/test_daemonless_hosted.py`).
* **Not verified** (needs a real deployment or accounts this project does not have yet):
  Vercel's CDN caching of the versioned reads and of `/api/forecasts/version` (the latency
  above was measured without a CDN); Neon itself (only PgBouncer in transaction mode was
  tested, locally; `test_pgbouncer_mode` is skipped in CI without `TEST_PGBOUNCER_URL`); the
  QStash schedule (never deployed); the Vercel daemon (the Vercel CLI 64.5.0 refuses a
  `vercel.json` with `daemons` in `vercel build`); the build-time seed on Vercel (whether the
  api service's build, rooted at `backend/`, can read the repository's `data/`: a build that
  cannot fails on an empty database, docs/deploy-vercel.md step 6); the daemon-less hosted
  path on Vercel itself (covered by tests; on the local Vercel-style stack only the daily
  cron ran with the daemon stopped, and its tide acquisition failed for want of `data/`);
  a live **atmosphere-only** refresh (never
  observed: when GFS 0.13 moved to 00Z on 2026-10-29, Open-Meteo's servers answered 00Z and
  18Z alternately for over 36 minutes, so three checks saw the run and three syncs skipped
  it safely without fetching; the refresh is covered by tests with the demo provider); Windy
  (no key: the adapter, the testing-key refusal, the fallback and the activation are tested
  with recorded and synthetic responses only, docs/WINDY_API_SETUP.md); real flight prices
  (no Duffel or Amadeus credentials: the demo fares are labelled "Mock fares").
* Hosting budgets in docs/API_SETUP_REQUIRED.md 1.4 are arithmetic on unit costs measured
  here, not measurements on Vercel, Neon or Upstash. With steady traffic and an open tab in a
  CDN region all month, the Upstash commands can pass the free 500K (pay-as-you-go then costs
  about $1 a month).

**Upgrade 4: verified here, and what only a real deployment can show**

* Verified on 2026-10-23 (docs/UPGRADE_REPORT.md, "Upgrade 4"): the offline Vercel build;
  the local Vercel-style stack with demo and with live data (smoke 19/19 and 6/6 both
  times); the classic Celery stack running the sync at beat start; "data only on update"
  (a second check made 0 SQL statements and 3,588 bytes of metadata requests; a forced
  re-sync wrote no data row); the database egress budget (2.01 GB a month in the background
  on the free-database profile); full e2e, 127 passed twice.
* Not verifiable without a Vercel account, so the **first production deploy** is the test:
  the daemon's image build and start on Vercel (daemons cannot be previewed), the CDN
  caching, Neon's compute suspend and wake and its CU-hour accounting, real Upstash command
  counts, the daily cron invocation, previews, and the GitHub Actions jobs on GitHub.
* A brand-new hosted deployment gets its NOAA CO-OPS tide predictions at the daemon's start
  (the start-up catch-up runs every periodic job that has not succeeded within its longest
  gap, `SYNC_DAEMON_CATCH_UP`), not at the next 02:20 UTC. Without the daemon (Swell 2.0)
  the daily cron runs the overdue `tides.acquire`, but on Vercel it fails there: the API
  function has no `data/` (`data/tide_stations.json`, `data/tides/`), so run
  `python -m app.cli tides acquire` from a checkout (docs/deploy-vercel.md section 4,
  "Except the tides"). Data health flags tides never acquired 3 h after the catalog was
  loaded (`tides_missing:<provider>`) and a failed acquisition (`job_failed:tides.acquire`).

**Demo-only**

* The demo forecast is synthetic. It is shaped like real swells but is not a
  forecast of anything.
* Demo fares are generated mock data.
* Both are labelled DEMO / MOCK in the UI, the API (`is_demo`, `is_mock`) and every alert.
* The `simulate-swell` tool works only with the demo forecast provider.

**Verified live**

* The recommended API-first setup on fresh databases: on 2026-10-14 12Z both Open-Meteo
  families for all 50 spots (10,200 and 4,100 records, 345.6 kB and 175.1 kB), the blend and
  24,500 predictions; on 2026-10-16 00Z every acquisition job of that setup (10,400 and 4,200
  records, 329 kB and 172 kB, 0 errors), the blend, 25,000 predictions, GEFS-Wave statistics
  (69.4 MB), the standalone map fields with partition layers (7.8-8.0 MB per map hour),
  CO-OPS and EOT20 tides (16 requests, 140 kB) and NDBC `5day2` observations; no file left on
  disk ([`docs/FORECAST_SOURCES.md`](docs/FORECAST_SOURCES.md), "API-first acquisition" and
  "Measured on 2026-10-16"). Later on 2026-10-16 the CLI steps of section F.5 were run again:
  tides, GEFS-Wave and the map fields succeeded with the same byte counts; Open-Meteo refused
  the sandbox's shared address for the rest of the day ("Daily API request limit exceeded").
* Same-cycle parity of the Open-Meteo families with direct sampling (2026-10-14): wave height
  at all 50 spots within 0.05 m of NOAA NODD and of ECMWF open data; GFS wind within 0.21 km/h
  and 2.4° of NOAA's own 13 km grid.
* On the real 2026-10-11 run rescored with algorithm 2.1.0: offshore steps at 45 km/h or more
  rated Very Good or better 0 of 3; cross-shore steps at 30 km/h or more rated Good or better
  0 of 151 (37 of 127 with 2.0.0); 1,313 of 4,050 labels changed
  ([`docs/SURF_SCORING.md`](docs/SURF_SCORING.md) 3e).
* Unattended operation in Docker Compose, 2026-10-12 and 2026-10-13, with the direct GRIB
  providers (NOAA 1,908 MB, ECMWF about 0.9 GB and GEFS-Wave 201.5 MB per 00Z cycle, all 50
  spots, 0 errors), beat recovering after two Docker daemon outages, three soaks without a
  job error and the data-health emails to Mailpit (docs/operations.md §6 and §8).
* Backups: restore drills on a copy of the real 2026-10-11 runs (42 tables, 34,800 rows
  identical), a restore into the Compose database, and the compose `backup` service.
* NOAA GFS-Wave + GFS direct, ECMWF IFS open data and GEFS-Wave statistics, on
  2026-10-11: all 50 spots, 0 errors.
* Map ocean layers from real GFS cycles (2026-10-11 12Z, all 81 steps; the standalone
  `pgrb2.0p50` cycles with partitions on 2026-10-15 and 2026-10-16), cross-checked against an
  independent ecCodes decode and NDBC buoy directions ([`docs/map-layers.md`](docs/map-layers.md)).
* Live tides: CO-OPS at the 8 US spots and EOT20 at the other 42, shown with datum, station and
  credit; EOT20 against CO-OPS over 14 days in [`docs/tides.md`](docs/tides.md).
* NOAA NDBC buoys since 2026-10-12: 11 Hawaii and California stations, a 24-month backfill
  and the realtime files ([`docs/observations.md`](docs/observations.md)).
* Email to Mailpit, end to end (verification and alert emails, including the 15-step
  scenario).

**Known limits of the confidence assessments**

* Research evidence: 468 sources (403 read directly; 65 behind bot protection remain search
  summaries). No spot is High overall: tide evidence conflicts at the two spots whose swell
  direction, size and wind are all High, and most spots have no measured bathymetry (only
  four US spots, from NOAA BlueTopo).
* Eleven offshore wind directions are disputed by the evidence and were not changed;
  forecasts at 15 spots carry "Sources disagree about which wind is offshore here." The
  score still uses the profile's direction.
* Forecast and research confidence classes are documented heuristics; they have not been
  calibrated against observed surf (no nearshore observations exist yet). With a GEFS-Wave
  spread the forecast classes are ordered by the size of the offshore error at NDBC buoys;
  without one, High and Moderate from a single model were not clearly different
  ([`docs/ml-evaluation.md`](docs/ml-evaluation.md) §12.3).

**Implemented but not verified against the real services** (no credentials here). Each
adapter is unit-tested against documented response shapes with mocked HTTP. Expect to
adjust details on first live use.

| Service | Needs |
|---------|-------|
| Open-Meteo commercial hosts | `OPEN_METEO_API_KEY` |
| Windy | `WINDY_API_KEY` (and a licence that allows storing the data) |
| Duffel | `DUFFEL_ACCESS_TOKEN` |
| Amadeus Enterprise | `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` |
| Resend | `RESEND_API_KEY` |
| SendGrid | `SENDGRID_API_KEY` |
| Twilio | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |

SMTP is verified end to end: the Docker stack delivered a verification email to Mailpit.
Admin data-health emails were verified with the console and Mailpit capture providers only.
The browser tests ran in Chromium and its Pixel 7 emulation only; Firefox, WebKit and real
phones were not available here.

**Not implemented**

* Booking or payment: the app links to the provider or a public search page. The future
  paywall (a payment provider, plans, access control for paying accounts) is not built;
  only the hosting side of it is ready as configuration.
* Surfline or other proprietary surf forecasts: no authorized API, and nothing is scraped.
* Fitted per-spot calibration of breaking heights: there are no nearshore (surf-height)
  observations to fit or verify against (the schema supports them; see SURF_SCORING.md
  §7).
* Social login, two-factor authentication, multi-language UI, push notifications, hotel or
  car search, a general admin UI (use the API docs, the status page and the accuracy
  dashboard).

**Forecast and price uncertainty**

* Wave forecasts 5–10 days ahead routinely shift in timing (± a day) and size.
  Confidence is shown separately from quality for this reason.
* Breaking heights come from a physical model with estimated spot profiles (shore normal,
  shadowing, focusing; survey depths at the forecast points of three US spots and one
  measured shore normal, at Trestles; no measured depth at any break, because the break
  coordinates are shoreline positions). They are unvalidated against surf observations and
  can be wrong by a factor of 2 at reef and point breaks with unusual bathymetry. The 80%
  range is a heuristic, not a calibrated probability.
* Quality-score weights, thresholds and labels are reasoned defaults, not fitted to
  observations. Algorithm 2.1.0 changed 32 % of the labels on a real run; the owner has not
  reviewed those changes yet.
* Fares change constantly. Every price is a quote with a timestamp, re-validated
  before alerting when the provider supports it, and may differ at booking time.
* Nothing here is travel, safety or financial advice. Check local conditions, hazards,
  visas and entry rules yourself.
