# Local Vercel-style stack

`docker-compose.vercel.yml` runs the app the way the Vercel deployment runs it, on one
machine, so you can test it before you deploy. The topology is the same: an edge routes
`/api/*` to the API and everything else to the website, the API is the lean Python install
with no background-job libraries, and the database is reached through a pooler in
transaction mode. The weather data is pulled by the same sync daemon image, built from the
same `Containerfile` that Vercel builds. `scripts/vercel-local.sh` drives it.

The classic stack (`docker-compose.yml`: Celery worker and beat, Next.js proxying `/api`)
stays as it was. The two stacks use different ports and volumes and can run side by side.

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
  the sync daemon's own container.
* `smoke` runs the shell checks and the Playwright smoke spec (below).
* `scripts/vercel-local.sh down` stops it; `down --volumes` also deletes the database.

Without settings the stack runs **demo forecasts** (synthetic data, labelled as such on
every page) and **no map layers**, so no forecast or map provider is called (the daily tide
job still asks NOAA CO-OPS at 02:20 UTC, as in every setup). Every other setting
is the hosted free profile (`profile_env("hosted", "free-database")` in
`backend/app/core/profiles.py`).

## Live weather data

```bash
cp .env.vercel.example .env.vercel.local
# optional, for a short test: a 24-hour map horizon (9 map hours, about 70 MB a cycle)
#   MAP_FIELD_STEPS=0-24:3
#   MAP_FIELD_MAX_LEAD_HOURS=24
scripts/vercel-local.sh up
scripts/vercel-local.sh sync --force
```

`.env.vercel.local` is git-ignored. With the example's values the sync daemon pulls the
Open-Meteo GFS and ECMWF point forecasts for the 50 surf spots and the NOAA GFS world map
layers, only when a new model run is published (docs/sync.md). The free Open-Meteo API is
for non-commercial use; `COMMERCIAL_USE=true` needs `OPEN_METEO_API_KEY`.

Right after `up` the daemon checks the providers too and may start a sync of its own.
While it holds the lease, `sync --force` prints "another sync holds the lease" (exit 3);
run it again when the daemon's sync is done (`scripts/vercel-local.sh logs sync`).

## Commands

| Command | What it does |
|---|---|
| `up [--no-build]` | build (unless `--no-build`), start, wait until healthy, print the URLs |
| `sync [--force] [--only spots,ensemble,map]` | `python -m app.cli sync run` in the sync container; exit 0 success/partial/skipped, 1 failed, 3 another sync holds the lease |
| `check` | `sync check`: the plan from provider metadata (no data download) |
| `status` | containers, image sizes, the daemon's heartbeat, the newest syncs |
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
| routing: the Services rewrites of vercel.json | `edge` (Caddy, docker/edge/Caddyfile) | `/api/*` to `api` with the original path, the rest to `web`. Sets `X-Vercel-Forwarded-For`, `X-Real-IP` and `X-Forwarded-For` to the TCP peer, overwriting whatever the client sent. A request body over 4.5 MB is refused with 413 (a 5 MB login body was; a 4 MB one reached the API), and a response that has not started after 60 s fails at the edge (maxDuration; configured, not exercised here). |
| service `web` (Next.js) | `web` | the production build with `API_ROUTING=platform`: Next.js has no `/api` rewrite |
| service `api` (one Python function) | `api` (docker/api-lean/Dockerfile) | `[project].dependencies` only, from `backend/uv.lock`: no Celery, kombu, pandas, ecCodes, pyarrow, scikit-learn, scipy or joblib. backend/ without tests/, no data/ directory, read-only file system except /tmp, one uvicorn process, 2 GB and 1 CPU. `JOB_RUNNER=daemon`: account emails go out inline. |
| daemon `sync` (Containerfile) | `sync` | the same Containerfile: `python -m app.cli sync daemon`, one instance, 2 GB and 1 CPU, 120 s to stop after SIGTERM. Connects directly (`DB_USE_UNPOOLED=true` in the image) and runs init-db at start. |
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
* PgBouncer's `SHOW CONFIG` says `pool_mode = transaction`, and `GET /api/spots` raises its
  transaction counter (the API's queries go through the pooler);
* a request with forged `X-Vercel-Forwarded-For`, `X-Forwarded-For` and `X-Real-IP` headers
  gets the rate-limit key of the address the edge saw (the Docker network's gateway for a
  request from this machine), never one of the forged addresses;
* the web service itself answers 404 for `/api/health` (no Next.js rewrite);
* the daemon's heartbeat is in Redis, and a sync was published;
* the cron endpoint answers 401 without the token and 200 with it.

Then `frontend/e2e/vercel-smoke.spec.ts` (chromium, `frontend/playwright.vercel.config.ts`,
base URL <http://localhost:3100>):

* `/api` responses carry the API's own security headers, an unknown `/api` path is the API's
  JSON 404, and `/` is the web's HTML;
* the home page and the map load; the map index answers, with fields when the stack syncs
  map layers (`MAP_FIELDS_ENABLED=true`) and with its "unavailable" message otherwise;
* a spot page shows its forecast;
* register, read the verification email from Mailpit's API (the link points at the edge),
  verify, sign out, log in;
* `/status` shows a published sync (needs the sync block of `GET /api/health/data` and the
  status page's sync section, `data-testid="sync-status"`);
* the cron endpoint: 401 without or with a wrong token, 200 with `CRON_SECRET`.

The default `playwright.config.ts` skips this spec.

## Neon development branches and Upstash

To try the stack against a real Neon database, create a development branch in Neon (never
the production branch) and put its URLs in `.env.vercel.local`:

```bash
VERCEL_LOCAL_DATABASE_URL=postgresql://USER:PASSWORD@ep-...-pooler.REGION.aws.neon.tech/neondb?sslmode=require
VERCEL_LOCAL_DATABASE_URL_UNPOOLED=postgresql://USER:PASSWORD@ep-....REGION.aws.neon.tech/neondb?sslmode=require
# optional: an Upstash database instead of the local Redis
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
* **The daemon quota.** The Hobby daemon has 10 Active CPU-hours a month; here only the
  1-CPU limit applies. The heartbeat and `/status` report the CPU used
  (`SYNC_CPU_BUDGET_HOURS`).
* **Build arguments.** Vercel passes every environment variable to the daemon's image build
  as a build argument; compose passes none. The Containerfile declares no `ARG`, so the
  result is the same.

`vercel build` of the CLI (64.5.0) refuses a vercel.json with `daemons`
("[DAEMONS_NOT_SUPPORTED] Can't build daemons yet"), so a local `vercel build` checks the
web and api services only; this stack is the local check of the daemon image.

## Tests against this stack

The API's PgBouncer test (`backend/tests/integration/test_pgbouncer_mode.py`) can use the
stack's pooler: create a test database and point both URLs at it.

```bash
scripts/vercel-local.sh psql   # then: CREATE DATABASE swell_test_pgb;
cd backend
TEST_DATABASE_URL=postgresql+psycopg://swell:swell@localhost:55432/swell_test_pgb \
TEST_PGBOUNCER_URL=postgresql+psycopg://swell:swell@localhost:56432/swell_test_pgb \
TEST_REDIS_URL=redis://localhost:56379/7 \
  .venv/bin/python -m pytest tests/integration/test_pgbouncer_mode.py
```

`backend/tests/unit/test_local_vercel_stack.py` checks the stack's files without Docker:
`.env.vercel.example` against the hosted profile, the compose defaults, the pinned base
images, the Containerfile (no `ARG`), the edge's headers and PgBouncer's mode.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `sync` exits 3, "another sync holds the lease" | the daemon is syncing; wait (`logs sync`) and run it again |
| map layers stay unavailable with live settings | `MAP_FIELDS_ENABLED=true` and a map cycle (00Z, 12Z) must be published; `check` shows the map component's state. A "Cannot find the ecCodes library" error means the image was built without the eccodeslib wheel: the Containerfile build checks for it |
| the image build fails at `uv sync --locked` | `backend/uv.lock` does not match `backend/pyproject.toml`: run `uv lock` in backend/ |
| `up` fails with a port in use | set `EDGE_PORT` or another `VERCEL_*_PORT` in `.env.vercel.local` |
| emails do not arrive | they are in Mailpit (<http://localhost:18025>), never in a real mailbox |
| containers cannot reach the weather providers | the sync container needs outbound HTTPS; behind a proxy, add `HTTPS_PROXY` (and its CA) for the `sync` service in an override file named by `VERCEL_LOCAL_COMPOSE_FILES` |

## Measured

On this development sandbox (4 vCPUs shared with other work), 2026-10-21, with
docker-compose.vercel.yml and the other upgrade-4 packages that existed then merged into a
scratch copy: sync-core, api-serverless and wave-look. deps-packaging (the dependency split
and `backend/uv.lock`) and vercel-deploy (`API_ROUTING` in next.config.ts, vercel.json) did
not exist yet; the scratch copy carried a minimal stand-in for each, written from their
design. Image builds and the sync container reached the internet through the sandbox's
proxy (a compose override, not part of the repository).

**From a fresh clone, with the defaults** (no `.env.vercel.local`: demo forecasts, no map
layers), no image and an empty build cache (the base images were pulled already):

| Step | Result | Time |
|---|---|---|
| `up` | the four images built, `migrate` exited 0, the nine services up (healthy where they have a health check) | 168 s |
| `sync --force` | exit 3 at first: the daemon was running its own first sync (the demo run, 59.2 s, 31.1 CPU s); once it finished, `sync --force` ended `success` and published | 11 s, then 15 s (the sync 9.4 s, 5.7 CPU s) |
| `smoke` | shell checks 18 of 18; Playwright 5 of 6 (`/status`: `GET /api/health/data` has no sync block yet, see the findings below) | 38 s |

The same from a fresh database **with the live settings** of `.env.vercel.example` and a
24-hour map horizon: `up` 150 s (the dependency layers cached), `sync --force` 84 s,
status `partial`. The NOAA map layers were stored (135 fields, 9 of 9 map hours, 70.8 MB),
but Open-Meteo answered both spot requests with "Daily API request limit exceeded. Please
try again tomorrow." (this sandbox's IP is shared), so the spot pages said "No forecast has
been ingested for this spot yet." and the spot-page test failed with the `/status` one;
nothing was filled in. The shell checks passed 18 of 18.

Images (`docker image ls`): sync 1.24 GB, api 417 MB, web 344 MB, pgbouncer 15.1 MB; with
the pulled base images about 3 GB. The sync image's virtual environment is 778 MB, the api
image's 172 MB (the lean set; its build prints "lean install"). The build cache was 5.0 GB
after the cold build.

**Live data through the stack** (an earlier run the same evening). The daemon, started with
the live settings, found the GFS 12Z spot run new at its first check and stored 10,200
point forecasts for the 50 spots (142.9 weighted Open-Meteo calls). `sync --force` then
stored the ECMWF 12Z run (4,100 point forecasts, 124.3 weighted calls) and the NOAA GFS 12Z
map layers (135 fields, 9 of 9 map hours, 70.8 MB in 63 requests, 10.4 MB stored), built
the two-family blend (10,200 records), computed 14,300 predictions, ran detection and
published: status `success`, 193.9 s and 67.6 CPU s for the sync, 227 s for the command.
Several Open-Meteo TLS handshakes through the sandbox's proxy timed out and were retried.
The map then showed the GFS 12Z wave field with the crest dashes, and the spot pages the
blended forecast.

That first live attempt also found a packaging defect: the map stage failed with "Cannot
find the ecCodes library". `uv lock` recorded `eccodes` 2.50.0 without its `eccodeslib`
dependency (the Linux wheel's metadata has `eccodeslib; platform_system != "Windows"`, the
lock's entry has no such line), so `uv sync --locked` installed ecCodes without its C
library. Declaring `eccodeslib` in the `sync` extra fixed it (ecCodes 2.50.0 loads), and
the Containerfile now fails its build when the library is missing.

**The client IP.** A request from this machine with `X-Vercel-Forwarded-For: 203.0.113.77`,
`X-Forwarded-For: 198.51.100.9` and `X-Real-IP: 192.0.2.5` produced exactly one rate-limit
key, `rl:health-data:172.22.0.1:<window>`: the Docker network's gateway, the address the
edge saw. None of the forged addresses reached the API.

**PgBouncer.** `SHOW CONFIG` reports `pool_mode = transaction`; `GET /api/spots` raised
PgBouncer's `total_xact_count` (26 to 29, and 226 to 228 in a later run);
`pg_stat_activity` showed the API's server connections (`swell-api`) only from the
PgBouncer container's address. The API's own PgBouncer test,
`tests/integration/test_pgbouncer_mode.py`, passed against the stack's pooler (2 passed).

**The daemon leaves the database alone between wakes.** With `log_statement = all` from
21:29:13 to 22:12:46 UTC (43.5 minutes, four metadata checks, every component current),
Postgres logged no connection and no statement from the sync container. At 22:12:46,
60 minutes after the daemon started (`JOB_INTERVAL_FLOOR_MINUTES=60`), it opened one
session for its hourly batch of six periodic jobs: 120 statements in 0.8 s, then nothing.

**Mailpit.** The smoke spec registered a synthetic `@example.com` account; the API sent the
verification email inline over SMTP, the spec read it from Mailpit's API (its link pointed
at `http://localhost:3100`), verified the address and logged in.

**Findings for the other packages**, seen only in this stack:

* The API image has no `data/` (as the Vercel function, whose root is `backend/`). The data
  health report the API computes, and with it the daily watchdog, is `failing` there with
  `tides_constants:harmonic_model` ("The tide model's constants file cannot be read"),
  although the daemon (which has `data/`) computes tides fine.
* `/status` cannot show a published sync yet: `GET /api/health/data` has `"sync": null`
  until the data-health sync block exists, so the spec's `/status` test fails (5 of its 6
  tests passed).
* `vercel build` (CLI 64.5.0, offline, hand-written `.vercel/project.json`) refuses
  `daemons` ("[DAEMONS_NOT_SUPPORTED] Can't build daemons yet"), and without
  `"framework": "nextjs"` on the web service it stops at "[MULTIPLE_FRAMEWORKS_SERVICE]
  Multiple frameworks detected ... Next.js, Container" (frontend/ also holds the classic
  stack's Dockerfile). With the framework set and the daemon left out it built in 82 s: the
  Next.js service, and the api service as one `python3.13` function with maxDuration 60
  (3,637 files, 139 MB, no tests/), the two service routes and the cron entry.
