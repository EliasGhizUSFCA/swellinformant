# Local Vercel-style stack

`docker-compose.vercel.yml` runs the app the way the Vercel deployment runs it, on one
machine, so you can test it before you deploy. The topology is the same: an edge routes
`/api/*` to the API and everything else to the website, the API is the lean Python install
with no background-job libraries, and the database is reached through a pooler in
transaction mode. The weather data is pulled by the same sync daemon image, built from the
same `Containerfile` that Vercel builds. `scripts/vercel-local.sh` drives it.

The classic stack (`docker-compose.yml`: Celery worker and beat, Next.js proxying `/api`)
stays as it was; it can also run the same sync daemon instead of Celery (its optional
`sync` profile, described in the file). The two stacks use different ports and volumes and
can run side by side.

## Quick start

You need Docker with Compose v2.24 or newer and about 3 GB of disk for the images (plus
about 5 GB of build cache while building, which `docker builder prune -a` frees). The smoke
test also needs curl and the frontend's dependencies:
`cd frontend && npm ci && npx playwright install chromium`.

```bash
scripts/vercel-local.sh up
scripts/vercel-local.sh sync --force
scripts/vercel-local.sh smoke
```

* `up` builds the images, starts everything and waits until it is healthy. Then the app is
  at <http://localhost:3100> and every email it sends is in Mailpit at
  <http://localhost:18025>.
* `sync --force` runs one sync now (spots, then map, blend, predictions, publish, ...), in
  the sync daemon's own container. Right after `up` the daemon usually runs its own first
  sync; while that holds the lease, `sync` waits and tries again every 15 s (up to
  `VERCEL_LOCAL_SYNC_WAIT_SECONDS`, default 1200; `0` returns at once with exit 3).
* `smoke` runs the shell checks and the Playwright smoke spec (below).
* `scripts/vercel-local.sh down` stops it; `down --volumes` also deletes the database.

Without settings the stack runs **demo forecasts** (synthetic data, labelled as such on
every page) and **no map layers**, so no forecast or map provider is called (the daily tide
job still asks NOAA CO-OPS at 02:20 UTC, as in every setup). Every other setting
is the hosted free profile (`profile_env("hosted", "free-database")` in
`backend/app/core/profiles.py`).

## Live weather data

Copy the example settings, then start the stack and sync once. For a short test, first add
a 24-hour map horizon (9 map hours, about 70 MB a cycle) to `.env.vercel.local`: the lines
`MAP_FIELD_STEPS=0-24:3` and `MAP_FIELD_MAX_LEAD_HOURS=24`.

```bash
cp .env.vercel.example .env.vercel.local
scripts/vercel-local.sh up
scripts/vercel-local.sh sync --force
```

`.env.vercel.local` is git-ignored. With the example's values the sync daemon pulls the
Open-Meteo GFS and ECMWF point forecasts for the 50 surf spots and the NOAA GFS world map
layers, only when a new model run is published (docs/sync.md). The free Open-Meteo API is
for non-commercial use; `COMMERCIAL_USE=true` needs `OPEN_METEO_API_KEY`.

Right after `up` the daemon checks the providers too and may start a sync of its own.
While it holds the lease, `sync --force` prints "another sync holds the lease" and waits
for it (above); `scripts/vercel-local.sh logs sync` shows what the daemon is doing.

## Commands

| Command | What it does |
|---|---|
| `up [--no-build]` | build (unless `--no-build`), start, wait until healthy, print the URLs |
| `sync [--force] [--only spots,ensemble,map]` | `python -m app.cli sync run` in the sync container; exit 0 success/partial/skipped, 1 failed, 3 another sync still held the lease after `VERCEL_LOCAL_SYNC_WAIT_SECONDS` |
| `check` | `sync check`: the plan from provider metadata (no data download) |
| `status` | containers, image sizes, the daemon's heartbeat, the newest syncs, the Redis commands processed (Upstash bills commands) |
| `cron` | the daily watchdog, called as Vercel Cron calls it (`GET /api/internal/cron/daily` with the Bearer token, through the edge) |
| `smoke [playwright args]` | the shell checks, then `npx playwright test -c playwright.vercel.config.ts` |
| `logs [service...]` | follow the logs |
| `psql [--pooled]` | psql on the database, directly or through PgBouncer |
| `bundle [args]` | the API bundle size check, `scripts/check_api_bundle.py` |
| `down [--volumes]` | stop; `--volumes` deletes the database too |

Settings come from `.env.vercel.local`, or from the file `VERCEL_LOCAL_ENV_FILE` names.
`VERCEL_LOCAL_COMPOSE_FILES` adds compose override files (space-separated), for example
build arguments and a CA certificate for a proxy your image builds go through: every
Dockerfile accepts an optional `ca` build secret.

Run compose yourself only with an env file
(`docker compose -f docker-compose.vercel.yml --env-file .env.vercel.local ...`): without
one, compose reads the classic stack's `.env` for the `${...}` values.

## What runs

| On Vercel | Here | Notes |
|---|---|---|
| routing: the Services rewrites of vercel.json | `edge` (Caddy, docker/edge/Caddyfile) | `/api/*` to `api` with the original path, the rest to `web`. Sets `X-Vercel-Forwarded-For`, `X-Real-IP` and `X-Forwarded-For` to the TCP peer, overwriting whatever the client sent, and removes the `Via` header Caddy's proxy would add (Vercel sends none). A request body over 4.5 MB is refused with 413 (a 5 MB login body was; a 4 MB one reached the API), and a response that has not started after 300 s fails at the edge (maxDuration of vercel.json; configured, not exercised here). |
| service `web` (Next.js) | `web` | the production build with `API_ROUTING=platform`: Next.js has no `/api` rewrite |
| service `api` (one Python function) | `api` (docker/api-lean/Dockerfile) | `[project].dependencies` only, from `backend/uv.lock`: no Celery, kombu, pandas, ecCodes, pyarrow, scikit-learn, scipy or joblib. backend/ without what vercel.json's `excludeFiles` leaves out of the function (tests/, ml_models/), no data/ directory, read-only file system except /tmp, one uvicorn process, 2 GB and 1 CPU. `JOB_RUNNER=daemon`: account emails go out inline. |
| daemon `sync` (Containerfile) | `sync` | the same Containerfile: `python -m app.cli sync daemon`, one instance, 2 GB and 1 CPU; after SIGTERM it finishes its current sync stage, for up to 600 s (vercel.json `terminationGracePeriodSeconds`; compose `stop_grace_period`). Connects directly (`DB_USE_UNPOOLED=true` in the image) and runs init-db at start. |
| build step `python -m app.cli migrate --on-build` | `migrate` | one-shot `init-db` (migrate, seed the 50 spots, the tide catalog) before `api` and `sync` start |
| Vercel Cron, `23 7 * * *` | `cron` | calls the watchdog through the edge every day at 07:23 UTC; `vercel-local.sh cron` calls it now |
| Neon, pooled `DATABASE_URL` | `pgbouncer` | PgBouncer 1.25 in transaction mode, `max_prepared_statements 1000`, `query_wait_timeout 120` (Neon's published values); the API connects with `DB_PREPARED_STATEMENTS=false` |
| Neon, `DATABASE_URL_UNPOOLED` | `postgres` | PostgreSQL 16; connections are logged with their application_name (`swell-api` through PgBouncer, `swell-sync` for the daemon) |
| Upstash Redis (`KV_URL`) | `redis` | Redis 7, nothing persisted, 256 MB |
| email provider | `mailpit` | every email of the app lands here; nothing reaches a real mailbox |

Host ports, all on 127.0.0.1: edge 3100 (`EDGE_PORT`), Postgres 55432 (`VERCEL_PG_PORT`),
PgBouncer 56432 (`VERCEL_PGBOUNCER_PORT`), Redis 56379 (`VERCEL_REDIS_PORT`), Mailpit
18025 (`VERCEL_MAILPIT_PORT`). The database is `swell`, user and password `swell`.

### Fixed and changeable settings

The containers receive every variable of `.env.vercel.local`. These keep the stack's
values whatever the file says, because they belong to this machine:
`DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `REDIS_URL` (and so `KV_URL`), `APP_ENV=development`
(production refuses Mailpit and cookies over plain HTTP), `COOKIE_SECURE=false`,
`APP_BASE_URL`, `API_BASE_URL` and `CORS_ORIGINS` (`http://localhost:3100`),
`EMAIL_PROVIDER=mailpit` and `SMTP_*`, `SMS_PROVIDER=console`,
`CLIENT_IP_HEADER=x-vercel-forwarded-for`, `JOB_RUNNER=daemon` and
`ENABLE_DEV_ENDPOINTS=false`. Everything else, the hosted profile values included, can be
changed in `.env.vercel.local`.

## The smoke test

`scripts/vercel-local.sh smoke` checks the running stack. Shell checks:

* the API answers behind the edge;
* the api image cannot import pandas, eccodes, celery, kombu, sklearn, pyarrow, scipy or
  joblib, and has no `/data`;
* the api service's Vercel build command, `python -m app.cli migrate --on-build` with
  `VERCEL_ENV=production`, runs on the lean image (the database is at head, so it only
  checks the revision);
* PgBouncer's `SHOW CONFIG` says `pool_mode = transaction`, and `GET /api/health/ready`
  raises its transaction counter (the API's queries go through the pooler). A `note` line
  then shows the counter around `GET /api/spots`, which does not move once the published
  version's snapshot is in Redis (the public reads do not wake the database);
* a request with forged `X-Vercel-Forwarded-For`, `X-Forwarded-For` and `X-Real-IP` headers
  gets the rate-limit counter of the address the edge saw (the Docker network's gateway for
  a request from this machine), never one of the forged addresses. The API keys its counters
  by a keyed digest of the address (`app.core.ratelimit.limit_key`), so the check computes
  the expected keys in the api container;
* the web service itself answers 404 for `/api/health` (no Next.js rewrite);
* the daemon's heartbeat is in Redis, and a sync was published;
* the cron endpoint answers 401 without the token and 200 with it;
* not a check: a `note` line with the data-health verdict the API reports
  (`GET /api/health/data`, what the daily watchdog alerts on), as the deployed function
  would report it.

Then `frontend/e2e/vercel-smoke.spec.ts` (chromium, `frontend/playwright.vercel.config.ts`,
base URL <http://localhost:3100>):

* `/api` responses carry the API's own security headers, an unknown `/api` path is the API's
  JSON 404, and `/` is the web's HTML;
* the home page and the map load; the map index answers, with fields when the stack syncs
  map layers (`MAP_FIELDS_ENABLED=true`) and with its "unavailable" message otherwise; with
  map layers the index names its publication (`published_at`) and the map's model-run label
  carries the same time (`data-testid="map-lead-time"`, `data-published-at`);
* a spot page shows its forecast;
* register, read the verification email from Mailpit's API (the link points at the edge),
  verify, sign out, log in;
* `/status` shows a published sync: `GET /api/health/data` reports the daemon as the runner,
  its last check and a publication, and the status page's sync section
  (`data-testid="sync-status"`) shows the publication time (`sync-published`), the last
  check without the "Not checking" badge (`sync-last-check`), and one row per component of
  the report, each with its state (`sync-components`, `data-state`: current, new, waiting,
  in_progress, failed, unknown or disabled), a spot-forecast row among them (and the map's
  with map layers);
* the cron endpoint: 401 without or with a wrong token, 200 with `CRON_SECRET`.

The default `playwright.config.ts` ignores this spec (`testIgnore`), so `scripts/run-e2e.sh`
never runs it. The token never appears on a command line or in a retained Playwright trace
(curl reads the header from stdin; the spec makes the authorised call with Node's `fetch`).

## Neon development branches and Upstash

To try the stack against a real Neon database, create a development branch in Neon (never
the production branch) and put its URLs in `.env.vercel.local` (the third line is optional:
an Upstash database instead of the local Redis):

```env
VERCEL_LOCAL_DATABASE_URL=postgresql://USER:PASSWORD@ep-...-pooler.REGION.aws.neon.tech/neondb?sslmode=require
VERCEL_LOCAL_DATABASE_URL_UNPOOLED=postgresql://USER:PASSWORD@ep-....REGION.aws.neon.tech/neondb?sslmode=require
VERCEL_LOCAL_REDIS_URL=rediss://default:TOKEN@HOST:6379
```

Then the API connects to Neon's pooler and the daemon and `migrate` to the direct
endpoint, as on Vercel. `migrate` migrates that branch, and the daemon syncs into it, which
uses the branch's storage and compute hours (Neon Free: 1 GB, 100 CU-hours a month per
project). Between syncs the daemon does not touch the database, so the branch can scale to
zero. `vercel env pull .env.vercel.local` writes the project's development variables;
check that `DATABASE_URL` there is not the production database, and copy the URLs into the
`VERCEL_LOCAL_*` names (the stack ignores `DATABASE_URL` itself).

## What it emulates and what it does not

Emulated: the routing and client-IP headers of Vercel's edge, the request-body and duration
limits of the API function, the lean API install and bundle contents, the same-origin
cookies of one domain, the daemon image and its resources, a pooler in transaction mode in
front of Postgres, Redis without persistence, and the daily cron call with its Bearer token.

Not emulated:

* **The Lambda runtime.** The API runs as one long-lived uvicorn process, not as a
  function: no cold starts, no freezing between requests, no Fluid concurrency, no
  `vercel_runtime` wrapper and no 4.5 MB response limit. `vercel dev -L` runs the Python
  service locally without Docker (docs/deploy-vercel.md).
* **The CDN.** Nothing caches `Cache-Control` / `CDN-Cache-Control` responses here, so the
  map index and field responses are always fresh; on Vercel they can lag by their cache
  lifetime.
* **Neon's scale-to-zero, cold starts, limits and TLS.** The local Postgres is always on.
  Point the stack at a Neon branch to see them.
* **Upstash's TLS (`rediss://`), command pricing and limits.**
* **HTTPS, the Firewall, Deployment Protection, preview deployments** (daemons run only in
  production on Vercel), **the build machine** (the Next.js and Python builds run in Docker
  here), and Vercel Cron's timing (Hobby runs a daily job anywhere within its hour).
* **The daemon quota.** The Hobby daemon has 5 Active CPU-hours a month
  (https://vercel.com/docs/daemons/limits); here only the 1-CPU limit applies. The heartbeat and `/status` report the CPU used
  (`SYNC_CPU_BUDGET_HOURS`).
* **Build arguments.** Vercel passes every environment variable to the daemon's image build
  as a build argument; compose passes none. The Containerfile declares no `ARG`, so the
  result is the same.

`vercel build` of the CLI (64.5.0) refuses a vercel.json with `daemons`
("[DAEMONS_NOT_SUPPORTED] Can't build daemons yet"), so a local `vercel build` checks the
web and api services only; this stack is the local check of the daemon image.

## Tests against this stack

The API's PgBouncer test (`backend/tests/integration/test_pgbouncer_mode.py`) can use the
stack's pooler: create a test database and point both URLs at it. First open psql on the
stack's database with `scripts/vercel-local.sh psql` and run
`CREATE DATABASE swell_test_pgb;` there; then:

```bash
cd backend
TEST_DATABASE_URL=postgresql+psycopg://swell:swell@localhost:55432/swell_test_pgb \
TEST_PGBOUNCER_URL=postgresql+psycopg://swell:swell@localhost:56432/swell_test_pgb \
TEST_REDIS_URL=redis://localhost:56379/7 \
  .venv/bin/python -m pytest tests/integration/test_pgbouncer_mode.py
```

`backend/tests/unit/test_local_vercel_stack.py` checks the stack's files without Docker:
`.env.vercel.example` against the hosted profile and as a production configuration (with
its placeholders filled it loads under `APP_ENV=production` with no missing credential;
unfilled, it is refused or the gaps are reported), the compose defaults, the pinned base
images, the Containerfile (no `ARG`), the edge's headers, PgBouncer's mode, the script's
subcommands and smoke checks, and, when vercel.json is present, that the stack mirrors it
(cron path and time, daemon image, memory and grace period, routing, maxDuration).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `sync` exits 3, "another sync still holds the lease" | a sync ran longer than `VERCEL_LOCAL_SYNC_WAIT_SECONDS`; `status` and `logs sync` show it, then run `sync` again |
| map layers stay unavailable with live settings | `MAP_FIELDS_ENABLED=true` and a map cycle of `MAP_FIELD_CYCLES` (06Z and 18Z in the free hosted profile, 00Z and 12Z by default) must be published; `check` shows the map component's state. A "Cannot find the ecCodes library" error means the image was built without the eccodeslib wheel: the Containerfile build checks for it |
| the image build fails at `uv sync --locked` | `backend/uv.lock` does not match `backend/pyproject.toml`: run `uv lock` in backend/ |
| `up` fails with a port in use | set `EDGE_PORT` or another `VERCEL_*_PORT` in `.env.vercel.local` |
| emails do not arrive | they are in Mailpit (<http://localhost:18025>), never in a real mailbox |
| containers cannot reach the weather providers | the sync container needs outbound HTTPS; behind a proxy, add `HTTPS_PROXY` (and its CA) for the `sync` service in an override file named by `VERCEL_LOCAL_COMPOSE_FILES` |

## Measured

On this development sandbox (4 vCPUs shared with other work). Image builds and the sync
container reached the internet through the sandbox's proxy (compose overrides named by
`VERCEL_LOCAL_COMPOSE_FILES`, not part of the repository), and its Playwright used a
preinstalled Chromium (`PLAYWRIGHT_CHROMIUM_PATH`).

### 2026-10-23, the whole stack on the final integration code

Run by the integration verifier with the steps above (`cp .env.vercel.example
.env.vercel.local`, plus `MAP_FIELD_STEPS=0-24:3` and `MAP_FIELD_MAX_LEAD_HOURS=24`; this
sandbox's build and egress overrides named by `VERCEL_LOCAL_COMPOSE_FILES`):

| Step | Result |
|---|---|
| `up` | 216 s from a cold build cache, all nine services healthy (33-34 s from cached images). Images: sync 1.22 GB (ecCodes 2.49.0), api 409 MB ("lean install"), web 344 MB, pgbouncer 15.1 MB |
| `sync --force`, live (first) | `partial` in 38.6 s / 30.9 CPU s: Open-Meteo refused this sandbox's shared address ("Daily API request limit exceeded. Please try again tomorrow."); the NOAA GFS 06Z map layers were stored (135 fields, 9 of 9 hours, 73.3 MB downloaded in 63 requests, 10.6 MB stored) |
| `sync --force`, demo fallback | `FORECAST_PROVIDERS=demo` in `.env.vercel.local`, `up --no-build`, `sync --force`: `success` in 35.2 s / 17.5 CPU s, 19,200 SYNTHETIC demo records and predictions, 164 events; the map "already stored" |
| `smoke`, demo | 19 of 19 shell checks, 6 of 6 Playwright; data health ok |
| `sync --force`, live (later, 19:02 UTC, after two more refusals) | `success`: Open-Meteo GFS 10,264 records for the 50 spots, 10,264 predictions, 88 swell events, published (58.9 s / 10.5 CPU s); ECMWF "not served yet" (Open-Meteo was serving its 06Z run) |
| `smoke`, live | 19 of 19 and 6 of 6, 25-27 s; data health degraded only by `forecast_missing:open_meteo_ecmwf` |

Also checked: forged `X-Vercel-Forwarded-For`, `X-Forwarded-For` and `X-Real-IP` were
overwritten (the rate-limit key was the gateway 172.22.0.1's); `GET /api/spots` went through
PgBouncer (its transaction count rose); the cron answered 200 with the token, 401 with a
wrong one or none, and 404 with `CRON_SECRET` unset; a second `scripts/vercel-local.sh
check` moved 30,145 B in and 22,667 B out of the sync container and changed 0 rows; the
backend's PgBouncer test passed 3 of 3 against the stack's pooler with the command of
"Tests against this stack". The API's data health no longer reports
`tides_constants:harmonic_model` (round 1: the function reports the daemon's own check from
its heartbeat; finding below). Screenshots through the edge showed the real GFS-Wave 06Z
+12 h field with the crest dashes (1,494 crests at zoom 6 off California, draw p95 4.0-4.7
ms on the desktop) and no "No forecast" banner.

### 2026-10-23, checks of the configuration changes

Not a run of the whole stack (`up`, `sync` and `smoke` were last run on 2026-10-22, below).
Checked one by one, with Docker 29.8.2 and Compose 5.6.0:

* **Edge.** The repository's Caddyfile (image `caddy:2.11.6-alpine`) in front of two stub
  servers named `api` and `web`: before the change every response carried `Via: 1.0 Caddy`,
  after it none did; `/api/health` reached `api` and `/` reached `web`. `caddy validate` and
  `caddy fmt` accept the file.
* **Images.** `sync` (Containerfile): built in 72 s, 1.22 GB, "ecCodes 2.49.0". `api`
  (docker/api-lean): built in 18 s, 408 MB, "lean install", no `/data`, no `ml_models/`,
  and `import app.main` works. No secret or proxy value is in the sync image's history.
* **Compose.** `docker compose -f docker-compose.vercel.yml --env-file /dev/null config -q`
  and the same with `--env-file .env.vercel.example` pass.
* **Smoke spec.** Under the default `playwright.config.ts` the list holds no smoke test
  (127 tests in 12 files); `playwright.vercel.config.ts` lists its 6. The added status and
  map assertions type-check and lint; they have not run against a stack yet.

### 2026-10-22, all upgrade-4 packages merged

A scratch copy with every upgrade-4 package merged: sync-core, api-serverless, jobs-health,
egress-map-serving, vercel-deploy, deps-packaging (`backend/uv.lock`), wave-look, status-ui
and this one.

**With the defaults** (no `.env.vercel.local`: demo forecasts, no map layers), the base
images present and no build cache for the stack's images:

| Step | Result | Time |
|---|---|---|
| `up` | the four images built (the api build printed "lean install", the sync build "ecCodes 2.49.0"), `migrate` exited 0, the nine services up | 186 s |
| `sync --force` | the daemon's own first sync held the lease: "another sync holds the lease ... trying again in 15 s", then `success`, published | 31 s (the sync 3.2 s) |
| `smoke` | shell checks 19 of 19, Playwright 6 of 6 (the `/status` test too) | 36 s |

**With the live settings** of `.env.vercel.example` and a 24-hour map horizon
(`MAP_FIELD_STEPS=0-24:3`, `MAP_FIELD_MAX_LEAD_HOURS=24`), from an empty database:

| Step | Result | Time |
|---|---|---|
| `up` | images from the run above | 35 s |
| `sync --force` (first) | `partial`: the NOAA GFS 00Z map layers were stored (135 fields, 9 of 9 map hours, 72.2 MB downloaded, 10.5 MB stored); the Open-Meteo GFS request failed on TLS handshake timeouts through the sandbox's proxy, and ECMWF was not read (Open-Meteo was publishing its 06Z run; this source reads only 00Z and 12Z runs) | 141 s (the sync 132.1 s, 32.7 CPU s) |
| `sync --force` (second) | `success`: the GFS 06Z point forecasts for the 50 spots (10,300 records, 142.857 weighted Open-Meteo calls), 10,300 predictions, swell detection, published | 126 s (the sync 92.1 s, 10.8 CPU s) |
| `smoke` | shell checks 19 of 19, Playwright 6 of 6; the map painted the GFS 00Z wave field with the crest dashes | 35 s |

The same run also measured:

* **The daemon leaves the database alone between model runs.** With `log_statement = all`
  from 13:19:17 to 13:42:55 UTC (23.6 minutes, two checks with no new run), Postgres logged
  no connection and no statement from the sync container. At 13:42:55 Open-Meteo listed a new
  GFS run (the 0.25° atmosphere moved to 06Z) and the daemon opened one session: 76
  statements until 13:44:02 (the run was still being published, so the sync ended
  `skipped`). Every other statement in the window came through PgBouncer from the API, for
  the compose health check (`/api/health/ready` every 10 s), which Vercel does not have.
* **Redis commands** (Redis MONITOR, 13:33:43 to 13:45:12). Between checks the daemon sent
  one EVAL a minute (its poll for kicks from the API) and nothing else; the check at 13:42
  and the sync it started added 15 (GET, SET, EVAL, INCRBYFLOAT, EXPIRE). The kick poll alone
  is about 43,000 commands a month; with 144 checks a day and a few syncs the daemon stays
  well under Upstash Free's 500,000 (an estimate from this sample, not a month of
  measurement). PING, which Upstash does not bill, came only from the compose health checks.
* **SIGTERM.** `docker stop` of the sync container (PID 1 is `python -m app.cli sync daemon`)
  took 1.5 s: "signal 15, stopping after the current stage", "Sync daemon stopped", exit 0.
* **The client IP.** The forged `X-Vercel-Forwarded-For`, `X-Forwarded-For` and `X-Real-IP`
  produced only the counter of the Docker gateway the edge saw (`172.22.0.1`, stored as its
  keyed digest).
* **The cron call** (`scripts/vercel-local.sh cron`): 200,
  `{"status":"failing","problems":["tides_constants:harmonic_model","forecast_missing:open_meteo_ecmwf"],...}`
  (see the findings below; ECMWF had no run yet).

Images (`docker image ls`): sync 1.22 GB, api 416 MB, web 344 MB, pgbouncer 15.1 MB.

### 2026-10-21, before deps-packaging and vercel-deploy existed

The first runs, with stand-ins for the dependency split and `API_ROUTING` written from their
design. From a fresh clone with the defaults: `up` 168 s, `sync --force` 15 s after the
daemon's first sync (59.2 s, 31.1 CPU s), `smoke` 18 of 18 shell checks and 5 of 6
Playwright tests (`/status` waited for the data-health sync block, which jobs-health and
status-ui added). The build cache was 5.0 GB after the cold build.

**Live data through the stack.** The daemon, started with the live settings, stored the
GFS 12Z point forecasts for the 50 spots (10,200 records, 142.9 weighted Open-Meteo calls)
at its first check. `sync --force` then stored the ECMWF 12Z run (4,100 point forecasts,
124.3 weighted calls) and the NOAA GFS 12Z map layers (135 fields, 9 of 9 map hours,
70.8 MB in 63 requests, 10.4 MB stored), built the two-family blend (10,200 records),
computed 14,300 predictions, ran detection and published: `success`, 193.9 s and 67.6 CPU s
for the sync, 227 s for the command. A later live run the same evening was refused by
Open-Meteo ("Daily API request limit exceeded. Please try again tomorrow.", this sandbox's
IP is shared); the spot pages then said "No forecast has been ingested for this spot yet."
and nothing was filled in.

That first live attempt also found a packaging defect: the map stage failed with "Cannot
find the ecCodes library". `uv lock` had recorded `eccodes` 2.50.0 without its `eccodeslib`
dependency, so `uv sync --locked` installed ecCodes without its C library. The Containerfile
build now fails when the library is missing (the 2026-10-22 build printed "ecCodes 2.49.0").

**Idle database, longer window.** With `log_statement = all` from 21:29:13 to 22:12:46 UTC
(43.5 minutes, four checks, every component current) Postgres logged no connection and no
statement from the sync container. At 22:12:46, 60 minutes after the daemon started
(`JOB_INTERVAL_FLOOR_MINUTES=60`), it opened one session for its hourly batch of six
periodic jobs: 120 statements in 0.8 s, then nothing.

**PgBouncer.** `pg_stat_activity` showed the API's server connections (`swell-api`) only
from the PgBouncer container's address, and the API's own PgBouncer test,
`tests/integration/test_pgbouncer_mode.py`, passed against the stack's pooler (2 passed).
A 5 MB login body got 413 from the edge; a 4 MB one reached the API.

### Findings for the other packages, seen only in this stack

* (Fixed at integration round 1: the API now takes the daemon's own constants check from
  its heartbeat, and the 2026-10-23 run above no longer shows it.) The API image has no
  `data/` (as the Vercel function, whose root is `backend/`). The data
  health report the API computes, and with it `/status` and the daily watchdog, was
  `failing` there with `tides_constants:harmonic_model` ("The tide model's constants file
  cannot be read; model tides are unavailable."), although the daemon (which has `data/`)
  computes the tides. The smoke prints this verdict as a note.
* `vercel build` (CLI 64.5.0, offline, hand-written `.vercel/project.json`) refuses
  `daemons` ("[DAEMONS_NOT_SUPPORTED] Can't build daemons yet"), and without
  `"framework": "nextjs"` on the web service it stops at "[MULTIPLE_FRAMEWORKS_SERVICE]
  Multiple frameworks detected ... Next.js, Container" (frontend/ also holds the classic
  stack's Dockerfile). With the framework set and the daemon left out it built in 82 s: the
  Next.js service, and the api service as one `python3.13` function with maxDuration 60
  (3,637 files, 139 MB, no tests/), the two service routes and the cron entry.
