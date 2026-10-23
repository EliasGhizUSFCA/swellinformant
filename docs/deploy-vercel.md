# Deploying to Vercel

How to put Swell Travel Agent on Vercel, test it on your own machine first, and keep it
running on the free plans until a paywall arrives. It follows the design decisions D1-D16
of Upgrade 4. Quotes from Vercel, Neon, Upstash and GitHub are at the end (section 11),
with their URLs and the date they were read.

What was checked here, and how, is in section 10. In short: the configuration validates
against Vercel's published schema; an offline `vercel build` of the repository succeeds
(`RESULT: ok`, a 127.1 MiB API function; CI runs the same check on every push); the sync
daemon's image builds from the `Containerfile` Vercel uses; `vercel dev -L` serves the site
and the API; and the manual GitHub runner's steps run end to end on a development machine.
No real deployment was made while this was written: the first production deploy is the step
that proves the daemon builds and starts on Vercel (daemons cannot be previewed).

## 1. What runs where

One Vercel project, built from the repository root, with two services and one daemon
(`vercel.json`):

| Part | What | Where it comes from |
|---|---|---|
| `web` service | the Next.js site; every path except `/api` | `frontend/` (`framework: nextjs`) |
| `api` service | FastAPI as one Python function (`app.main:app`), `maxDuration` 60 s | `backend/` (`framework: fastapi`) |
| routing | `/api/(.*)` goes to `api`, everything else to `web` (vercel.json `rewrites`) | Vercel's router; Next.js adds no `/api` rewrite on Vercel (`API_ROUTING=platform`) |
| `sync` daemon | `python -m app.cli sync daemon`: watches the model runs and syncs spots, map layers and alerts together | the repo-root `Containerfile`, 1 instance in `iad1`, 2 GiB / 1 vCPU; after `SIGTERM` it finishes its current sync stage, for up to 600 s (`terminationGracePeriodSeconds`) |
| daily watchdog | Vercel Cron, `GET /api/internal/cron/daily` at 07:23 UTC with `Authorization: Bearer $CRON_SECRET`; emails admins when the daemon or the data is stale | vercel.json `crons` |
| database | Neon Postgres (Vercel Marketplace), Free plan now | `DATABASE_URL` (pooled, the API) and `DATABASE_URL_UNPOOLED` (direct, the daemon and migrations) |
| Redis | Upstash Redis (Vercel Marketplace), Free plan now | `KV_URL` (the app reads it when `REDIS_URL` is unset) |
| migrations | the api service's build command, `python -m app.cli migrate --on-build`; with `MIGRATE_ON_BUILD=production` it migrates production builds only; the daemon also runs `init-db` at every start | vercel.json `services.api.buildCommand` |

Same origin for the site and the API, so login cookies, the CSRF check and the
Content-Security-Policy work exactly as on the classic Docker stack.

## 2. Free now, paywall later

| | Now: free | Later: paywall |
|---|---|---|
| Vercel | **Hobby**: "restricted to non-commercial personal use only". Free. | **Pro** ($20/month platform fee, $20 monthly usage credit) **before** the site asks for any payment: a paywall is "Any method of requesting or processing payment from visitors of the site", and ads count too. Donations do not. |
| Open-Meteo (spot forecasts) | free API, non-commercial only | `COMMERCIAL_USE=true` plus `OPEN_METEO_API_KEY` (a paid plan). With the flag set and no key, the app refuses the free hosts. |
| Neon | Free: 100 CU-hours, 1 GB storage and 5 GB egress per project. The free-database profile keeps about 1 day of forecast history and 1 week of verification archive, and syncs only for the 06Z and 18Z map cycles: about 2.01 GB of egress a month in the background (40 %) and about 20 CU-hours (docs/map-layers.md, "Free database budget"). | Launch ($0.106 per CU-hour, $0.35 per GB-month, 500 GB egress), then restore full retention (section 4, step 5). |
| Upstash | Free: 250 MB and 500K commands a month | pay-as-you-go ($0.2 per 100K commands, with a budget cap) or a Fixed plan (from $10/month) |
| Daemon | Hobby quota: 10 Active CPU-hours a month (estimate for this app: 4.4-7.9 when every new model run is synced, docs/sync.md; less with the free-database cadence) | Pro: billed for Active CPU and memory; optional `JOB_INTERVAL_FLOOR_MINUTES=0`, `SYNC_CHECK_MINUTES=5`, a 4Gi daemon |
| Logs | runtime logs kept 1 hour | 1 day on Pro |

Also worth knowing on Hobby: Vercel's terms say "if you are on a Hobby plan or trial Pro
plan, you agree that we may use Your Content to train our artificial intelligence".

For the hosting, the paywall is a configuration change (D13), no code change. Do it in this
order, before the site asks anyone for money:

1. Upgrade the Vercel team to **Pro** (Hobby is non-commercial).
2. Buy an Open-Meteo API plan and add `OPEN_METEO_API_KEY` (Production) in Project
   Settings, Environment Variables; then set `COMMERCIAL_USE=true`. With the flag and no
   key the spot forecasts stop: the app refuses the free hosts and `/status` reports the
   `licence` problem.
3. Move Neon to **Launch**, then change the retention and the sync cadence to the
   full-retention profile: `RETENTION_FORECAST_DAYS=10`, `RETENTION_ARCHIVE_DAYS=730`,
   `RETENTION_JOB_LOG_DAYS=30`, `SYNC_PRUNE_EACH_PASS=false`, `DATA_HEALTH_DATABASE_MAX_GB=20`,
   `SYNC_TRIGGER_SOURCES=spots,map`, `MAP_FIELD_CYCLES=00,12`.
4. Move Upstash off Free (pay-as-you-go with a budget cap, or a Fixed plan).
5. Redeploy production: environment variables reach new deployments only.

The paywall's own code (accounts that pay, checkout) is not part of this repository yet.

## 3. Test on your machine first

Two levels; neither needs a Vercel account.

### 3.1 The local Vercel-style stack (Docker)

`docker-compose.vercel.yml` runs the same topology as Vercel: an edge that routes `/api`
like Vercel and sets Vercel's client-IP headers, the lean API, the Next.js build with
`API_ROUTING=platform`, PgBouncer in transaction mode like Neon's pooler, Redis, the sync
daemon built from the same `Containerfile`, Mailpit and a cron helper. Start it with
`scripts/vercel-local.sh up`; docs/local-vercel-stack.md has the steps and what it does
not emulate.

### 3.2 The Vercel build check: `scripts/vercel-build-check.sh --offline`

It runs Vercel's own build (`vercel build`, Vercel CLI 64.5.0 through `npx`) on a temporary
copy of the repository and checks the result against Vercel's limits:

```bash
scripts/vercel-build-check.sh --offline          # no login; about 1 min with a warm npm cache
scripts/vercel-build-check.sh --offline --keep   # keep the build directory to look at it
```

CI runs `scripts/vercel-build-check.sh --offline` on every push and pull request (the
`vercel-build` job of `.github/workflows/ci.yml`), and builds the daemon's image from the
`Containerfile` (the `deploy-config` job), so a change that breaks either shows up before a
deployment does.

- It copies what a deployment receives (tracked files plus new files git does not ignore,
  minus `.vercelignore`), so your checkout is never changed.
- Offline it writes a hand-written `.vercel/project.json` (`prj_local` / `team_local`,
  framework `services`) into the copy and uses an empty CLI config directory. This is not
  documented by Vercel: normally `vercel pull` writes that file after a login. It worked
  with CLI 64.5.0; a later CLI may need a login.
- CLI 64.5.0 refuses daemons: the first attempt stops after a few seconds with
  "[DAEMONS_NOT_SUPPORTED] Can't build daemons yet: sync". The script then removes the
  `daemons` block from the copy's `vercel.json`, says so ("this CLI cannot build daemons;
  building again without: sync"), builds again and checks that the `Containerfile` exists.
  Only Vercel's production build builds the daemon image; the local stack (3.1) and CI
  build the same file with Docker.
- It prints every function with its size. A Python function over 225 MiB is a warning
  (above it Vercel's Python builder leaves the dependencies out of the bundle and installs
  them at cold start, an undocumented builder detail) and over 500 MiB a failure (Vercel's
  limit for Python functions); other functions fail over 250 MiB.
- It reports whether the daily cron and the routes are in `.vercel/output/config.json`.
- It never migrates: the build runs with `MIGRATE_ON_BUILD=off`, and the api's build
  command prints `"status": "skipped"`.
- On macOS or an ARM machine it leaves the api's build command out: outside Vercel's build
  image the Python builder installs Linux x86_64 wheels, which a local Python on macOS
  cannot import.
- Linked checkouts (after `vercel link`): run it without `--offline`; it copies
  `.vercel/project.json` and runs `vercel pull --environment=preview` first. The pulled
  file holds the Preview environment variables, secrets included; it lives only in the
  temporary copy and is deleted with it (with `--keep`, the file alone is deleted).
  `MIGRATE_ON_BUILD=off` still wins over the pulled value.
- Needs git, Node.js with npx, tar, and internet access to npm and PyPI. It needs about
  1.1 GB of free space in `$TMPDIR` while it runs.

Measured results are in section 10.

### 3.3 `vercel dev -L`: the site and the API with no login

Vercel's own dev server runs both services together; it does not run the daemon or crons.
Steps that worked here (Vercel CLI 64.5.0, Linux, 2026-10-23):

1. The project must not be linked: no `.vercel/project.json` (with one, `-L` still tries
   to fetch the project and fails without a login).
2. Install the API's lean dependencies (the versions of `backend/uv.lock`, as Vercel
   installs them) into `backend/.venv`: `cd backend && uv sync --locked --no-dev`
   (132 MB; no Celery, pandas, ecCodes or scikit-learn). It needs uv; the repository pins
   0.11.32 everywhere (`pip install uv==0.11.32`).
3. Start PostgreSQL and Redis (for example `docker compose up -d postgres redis`), then
   initialise the database from `backend/`:
   `DATABASE_URL=postgresql://swell:swell@localhost:5432/swell .venv/bin/python -m app.cli init-db`
   (the lean install is enough: it migrates, seeds the 50 spots and loads the tide catalog).
4. From the repository root:
   `DATABASE_URL=postgresql+psycopg://swell:swell@localhost:5432/swell REDIS_URL=redis://localhost:6379/0 FORECAST_PROVIDERS=demo JOB_RUNNER=daemon npx vercel@64.5.0 dev -L --listen 3000`
   (`JOB_RUNNER=daemon` as on Vercel: the lean install has no Celery, so account emails go
   out inline; without it they still do, after a "Celery unavailable" warning).
5. Open http://localhost:3000/ (the site) and http://localhost:3000/api/health/ready
   (`{"status":"ok","checks":{"database":"ok","redis":"ok"}}`).

It prints "`vercel dev` doesn't run daemons: sync. Run them with Docker." For data, use
the local stack (3.1), or `python -m app.cli sync run --force` from a full install
(`pip install -e ".[dev]"`; the lean venv has no data libraries). `vercel dev` installs the
frontend's dependencies itself ("Synchronizing dependencies") and runs the web service with
`next dev`, the development server, so it is a check of the routing and the API, not of the
production build (that is 3.2).

### 3.4 Known security advisories

Before a production deploy, ask npm whether the web service's dependencies have published
advisories:

```bash
cd frontend && npm audit --omit=dev
```

It exits 0 when there are none. Fix a `high` or `critical` one first (usually a patch
release of the package, for example Next.js); a public site is exposed from the first day.
On 2026-10-23 it found three advisories in Next.js 16.4.0 (two critical); after the upgrade
to 16.4.1 it reports 0 vulnerabilities (docs/decisions.md, ADR-037).

### 3.5 Upgrading a database from before Upgrade 4

Keep `FORECAST_PROVIDERS` the same as before: the spot list and the map serve only runs of
the configured sources, so a database filled by `open_meteo,open_meteo_ecmwf` and served
with `demo` shows "No forecast has been ingested yet". Then migrate (the build step or
`python -m app.cli migrate`): migration `0008_publish_stored_runs` records one publication
of the data already stored, so `/status` and the map name a publication at once and the
first check does not re-sync everything. On a new database, or one that already has a
publication, it does nothing.

## 4. First deployment, step by step

### Step 1. Accounts

A Vercel account on the Hobby plan, signed in with GitHub, and this repository on GitHub
(it stays public: CI is free on public repositories).

### Step 2. Import the repository

In Vercel, add a new project from this GitHub repository.

- **Root Directory: leave it at the repository root.** Daemons do not work with a
  subfolder: "Projects with a root directory such as `apps/web` can't add daemons yet."
- Leave the project's build settings (framework preset, build, install and output
  commands) at their defaults. With `services` in vercel.json these belong to each service,
  and Vercel says they "are not valid at the top level".
- Environment variables can be added now or in step 5. Vercel deploys right after the
  import; that first deployment has no database yet, and that is fine: redeploy after
  step 5.

### Step 3. Neon Postgres (Vercel Marketplace)

Install Neon from the Marketplace (Storage), Free plan, region **AWS us-east-1** (next to
Vercel's `iad1`, where the API and the daemon run). Connect it to the project for
Production, Preview and Development.

- Neon injects `DATABASE_URL` (pooled, through PgBouncer) and `DATABASE_URL_UNPOOLED`
  (direct). Keep the default variable names: no custom prefix.
- Optional: Preview Branching (a database branch per preview deployment), see section 6.

### Step 4. Upstash Redis (Vercel Marketplace)

Install Upstash Redis from the Marketplace, Free plan, region us-east-1, and connect it to
the project. The app reads `KV_URL` (a `rediss://` URL; Upstash always uses TLS) when
`REDIS_URL` is not set. `KV_REST_API_URL` and `KV_REST_API_TOKEN` are not used.

Check it: in Project Settings, Environment Variables, look for `KV_URL` (or `REDIS_URL`).
The Marketplace page showed `KV_URL` in its snippets on 2026-10-18, but only the REST
variables on 2026-10-22 (section 11). If neither is there, copy the database's TLS
connection string (`rediss://...:6379`) from its page under Storage into a `REDIS_URL`
variable yourself. Without one the app falls back to `redis://localhost:6379/0`: the API's
Redis check fails (`/api/health/ready` answers 503 with `"redis": "unavailable"`), rate
limits fail open, and the daemon's heartbeat and the "match my search" kicks are lost.

### Step 5. Environment variables

Project Settings, Environment Variables. Changes apply only to new deployments: "Any change
you make to environment variables are not applied to previous deployments, they only apply
to new deployments."

**The hosted profile.** Add these to Production and Preview. They are
`profile_env("hosted", "free-database")` in `backend/app/core/profiles.py`, the same values
as `.env.vercel.example` of the local stack:

```env
JOB_RUNNER=daemon
MIGRATE_ON_BUILD=production
CLIENT_IP_HEADER=x-vercel-forwarded-for
DB_POOL_SIZE=2
DB_MAX_OVERFLOW=3
DB_POOL_RECYCLE_SECONDS=240
DB_PREPARED_STATEMENTS=false
REDIS_CONNECT_TIMEOUT_SECONDS=1.5
REDIS_SOCKET_TIMEOUT_SECONDS=2
FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf
MAP_FIELDS_ENABLED=true
PROVIDER_RESPONSE_CACHE_ENABLED=false
GEFS_ENABLED=false
JOB_INTERVAL_FLOOR_MINUTES=60
DATA_HEALTH_CHECK_MINUTES=60
SYNC_CPU_BUDGET_HOURS=10
COMMERCIAL_USE=false
RETENTION_FORECAST_DAYS=1
RETENTION_ARCHIVE_DAYS=7
RETENTION_JOB_LOG_DAYS=7
SYNC_PRUNE_EACH_PASS=true
DATA_HEALTH_DATABASE_MAX_GB=0.85
SYNC_TRIGGER_SOURCES=map
MAP_FIELD_CYCLES=06,18
```

`GEFS_ENABLED=false` protects the daemon's 10 CPU-hours; set it to `true` to show the GEFS
ensemble spread. After moving Neon to Launch, change the last seven to the full-retention
profile: `RETENTION_FORECAST_DAYS=10`, `RETENTION_ARCHIVE_DAYS=730`,
`RETENTION_JOB_LOG_DAYS=30`, `SYNC_PRUNE_EACH_PASS=false`, `DATA_HEALTH_DATABASE_MAX_GB=20`,
`SYNC_TRIGGER_SOURCES=spots,map`, `MAP_FIELD_CYCLES=00,12`. The last two keep the
database inside Neon Free's 5 GB of egress a month: a sync only for a new map cycle (06Z
and 18Z), which also brings the newest spot runs of both model families
(docs/map-layers.md, "Free database budget", has the measured budget and the freshness
this costs).

**Yours to set.** `<project>` is your `*.vercel.app` name.

| Variable | Production | Preview | Why |
|---|---|---|---|
| `APP_ENV` | `production` | `development` | production refuses a development secret key, dev endpoints, insecure cookies and email providers that only capture mail |
| `SECRET_KEY` | 64 random characters | a different random value | required in production (the development default is refused); it keys the rate-limit counters; `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `CRON_SECRET` | 16+ random characters | not needed | Vercel Cron sends it as `Authorization: Bearer ...`; without it the watchdog route answers 404 |
| `COOKIE_SECURE` | `true` | `true` | the site is HTTPS-only |
| `APP_BASE_URL` | `https://<project>.vercel.app` | leave unset | links in emails |
| `API_BASE_URL` | `https://<project>.vercel.app` | leave unset | email-provider webhook validation |
| `CORS_ORIGINS` | `https://<project>.vercel.app` | leave unset | the browser calls the same origin; kept consistent |
| `EMAIL_PROVIDER`, `EMAIL_FROM` and the provider's key (`RESEND_API_KEY`, `SENDGRID_API_KEY` or `SMTP_*`) | a real provider | `console` | docs/API_SETUP_REQUIRED.md, section 2.11 |
| `OPEN_METEO_API_KEY` | only with a paid Open-Meteo plan | | required once `COMMERCIAL_USE=true` |
| `FLIGHT_PROVIDER`, `SMS_PROVIDER` and their keys | as in docs/API_SETUP_REQUIRED.md | | optional |

Email needs a domain verified with the email provider, and this project has no custom
domain yet (it runs on `*.vercel.app`). Until then, account emails cannot reach other
people, so public sign-up is blocked in production. Previews use `EMAIL_PROVIDER=console`,
which only writes the messages to the function's log and a temporary outbox file
(`OUTBOX_DIR`); nothing is delivered.

### Step 6. Initialise the database (once, from your machine)

The daemon runs `init-db` (migrate, seed the spots, load the tide catalog; safe to repeat)
every time it starts, so this step is a head start rather than a requirement. Doing it
yourself means the site has its spots before the daemon's first sync.

1. Copy the **direct** connection string (`DATABASE_URL_UNPOOLED`) from the database's page
   in Vercel's Storage tab, or from the Neon console.
2. From `backend/` in your checkout, with the lean install of section 3.3, step 2
   (`uv sync --locked --no-dev`; it is enough for `init-db`):

   ```bash
   export DATABASE_URL='postgresql://...direct URL...' DATABASE_URL_UNPOOLED="$DATABASE_URL"
   .venv/bin/python -m app.cli init-db
   ```

   Both variables name the same database on purpose: the app uses `DATABASE_URL_UNPOOLED`
   only when it names the same database as `DATABASE_URL`.
3. Later, after you have registered on the site and verified your email address:
   `.venv/bin/python -m app.cli make-admin you@example.com` (same environment). The data-health
   alerts go to active admin accounts with a verified email address.

Do not keep the URL in a file inside the repository; `unset DATABASE_URL
DATABASE_URL_UNPOOLED` when you are done.

### Step 7. Deploy to production and check it

Redeploy the production deployment (or push to the default branch) so it picks up the
variables. The order inside one deployment:

1. Vercel builds `web` and `api`. The api's build command runs
   `python -m app.cli migrate --on-build`; on a production build with
   `MIGRATE_ON_BUILD=production` it migrates through `DATABASE_URL_UNPOOLED`, and a failed
   migration fails the build, so new code never runs against an old schema. It needs
   Vercel's system environment variables (`VERCEL_ENV`) exposed to the build, which is
   Vercel's default; without them it prints `skipped` and the daemon's `init-db` migrates.
2. Vercel builds the daemon image from the `Containerfile` (production only).
3. When the deployment becomes production, Vercel starts the daemon and stops the previous
   one: "Vercel sends `SIGTERM` to the instances from the previous production deployment,
   then stops them after the shutdown grace period" (600 s here). On `SIGTERM` the daemon
   finishes the sync stage it is in and exits; an idle daemon stops at once. The longest
   stage, the map layers of a full cycle, is estimated at 165-287 CPU seconds on one vCPU
   (docs/sync.md), more than Vercel's 30 s default; a stage cut off by the kill would leave
   the sync lease held for up to `SYNC_LEASE_MINUTES` (15 under the daemon). While the old
   daemon finishes, the new one finds the lease taken and syncs at its next check.

Then check, in this order:

| Check | Expect |
|---|---|
| `https://<project>.vercel.app/api/health/ready` | `{"status":"ok","checks":{"database":"ok","redis":"ok"}}` |
| `https://<project>.vercel.app/` | the home page |
| Vercel dashboard: Daemons, `sync` | one running instance; its logs show `init-db`, then the first metadata check |
| `https://<project>.vercel.app/status` | the sync section: the daemon's last check and, after the first sync, a published sync |
| `/api/health/data` | `sync.runner` is `daemon` |

The first sync starts as soon as the daemon sees model runs newer than the (empty)
database. In the local Vercel-style stack a full live sync (spots, map layers, blend,
predictions, publish) took 193.9 s (docs/local-vercel-stack.md); on Vercel's 1 vCPU it may
take longer.

With the CLI (section 7): `vercel daemons ls`, `vercel daemons inspect sync`,
`vercel logs --query 'source:daemon'`. Hobby keeps runtime logs for 1 hour only; the
lasting record is the `sync_runs` table, the job logs and `/status`.

### Step 8. The daily watchdog

Vercel Cron calls `GET /api/internal/cron/daily` once a day (`23 7 * * *`, UTC). On Hobby
"Vercel may invoke these cron jobs at any point within the specified hour", so expect it
between 07:00 and 07:59 UTC, and a failed call is not retried. It runs the
data-health check and emails the admins when the daemon's heartbeat or the data is stale,
so a stopped daemon (quota used up, crash) is still noticed. Try it by hand:

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/internal/cron/daily
```

404 means `CRON_SECRET` is not set on the deployment; 401 means the token does not match.
(This one runs against the deployment. On your machine, `scripts/vercel-local.sh cron` makes
the same call to the local Vercel-style stack.)

## 5. Everyday operation

- **Deploys.** A push to the default branch makes a production deployment; the daemon is
  replaced with it. For a short time both versions run ("Make sure both versions can run
  side by side ... keeping database migrations compatible with the previous version");
  the sync lease in Postgres keeps two syncs from running at once.
- **Stopping the daemon** without a deploy: Daemons, `sync`, Scale, 0 instances (a scaling
  override that stays until you clear it).
- **Quotas to watch** (Hobby and the free tiers):

  | Quota | Limit | What happens | Where to see it |
  |---|---|---|---|
  | Daemon Active CPU | 10 hours a month | "Vercel stops the team's daemons until the first of the next month (UTC) or until the team upgrades to Pro"; the site keeps working, the data stops updating | `/status` (warns at 80 % of `SYNC_CPU_BUDGET_HOURS`), the daemon's usage charts |
  | Function Active CPU | 4 hours a month | over a Hobby limit "you will have to wait until 30 days have passed before you can use the feature again" | Vercel Usage |
  | Neon | 100 CU-hours, 1 GB, 5 GB egress | out of CU-hours or egress the compute is suspended until the next billing period; over 1 GB writes fail | Vercel Storage, Neon console; `/api/health/data` (database size) |
  | Upstash | 500K commands a month | | Vercel Storage |

- **When the daemon is stopped** (quota, crash): the watchdog emails the admins; run the
  manual GitHub Actions sync (section 8) if the data must not wait.

## 6. Preview deployments

Every pull request gets a preview deployment with its own URL.

- **No daemon**: "During the beta phase, daemons only run in production environments."
  Previews show whatever data their database holds; they never sync.
- **Database**: with Neon's Preview Branching each preview gets a copy-on-write branch of
  the production database: "Environment variables for the branch connection are injected
  via webhook at deployment time". With `MIGRATE_ON_BUILD=production` previews skip the
  migrations, so a pull request that adds a migration needs `MIGRATE_ON_BUILD=all` in the
  Preview environment (it then migrates that preview's branch, never production). Without
  Preview Branching, previews share the production database: keep `MIGRATE_ON_BUILD` at
  `production` then.
- **Redis**: if Upstash is connected to Preview too, previews share the production Redis
  (rate-limit counters and the kick flags the daemon reads). To keep previews apart,
  connect Upstash to Production (and Development) only; previews then run without Redis,
  rate limits fail open, and "match my search" kicks are not delivered.
- **Access**: Vercel's standard Deployment Protection covers previews ("Protects all
  deployments **except** production domains").

## 7. Using the Vercel CLI (logged in)

Pin the CLI version the build check uses: `npx vercel@64.5.0 <command>`. These commands
need a Vercel login and a project; they were not run while this guide was written (only the
offline build and `vercel dev -L` of section 3 were).

| Task | Command |
|---|---|
| log in, link this checkout to the project | `vercel login`, then `vercel link` (writes `.vercel/project.json`; do not commit it) |
| local env file for `vercel dev` | `vercel env pull` (the Development variables) |
| build like Vercel, with the project's settings | `scripts/vercel-build-check.sh` (runs `vercel pull --environment=preview` and `vercel build` in a copy) |
| preview deployment from your checkout | `vercel deploy` |
| promote a tested deployment to production | `vercel promote <deployment-url>` |
| daemon status and logs | `vercel daemons ls`, `vercel daemons inspect sync`, `vercel logs --query 'source:daemon'` |
| scale the daemon | `vercel daemons scale sync iad1=0` (stop), `vercel daemons scale sync --reset` |

Do not deploy production with `vercel deploy --prebuilt` from a local build: a local build
has no daemon (the CLI refuses to build one), and "When the new production deployment
doesn't declare a daemon, Vercel stops all of that daemon's instances." (Our inference from
those two documented facts; not tried.) Let Vercel build production deployments.

## 8. The manual runner: GitHub Actions `data-sync.yml`

`.github/workflows/data-sync.yml` runs one sync (`python -m app.cli sync run --trigger
github`) on a GitHub-hosted runner, against the production database. It is for
emergencies (the daemon is stopped, for example its CPU quota is used up) and backfills;
the daemon stays the scheduler.

**Set up once** (repository Settings, Secrets and variables, Actions):

- Secrets: `DATABASE_URL_UNPOOLED` (Neon's direct URL), `KV_URL` or `REDIS_URL`,
  `SECRET_KEY` (any long random value; the run uses `APP_ENV=production`, which refuses the
  development default),
  and the provider keys the deployment uses: `OPEN_METEO_API_KEY`, `RESEND_API_KEY`,
  `SENDGRID_API_KEY`, `SMTP_PASSWORD`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
  `DUFFEL_ACCESS_TOKEN`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET` (only the ones you
  use). GitHub does not pass secrets to runs triggered from forks.
- Variables: `APP_BASE_URL`, `EMAIL_PROVIDER` (`smtp`, `resend` or `sendgrid`) and
  `EMAIL_FROM` (all three required), and any setting you changed on Vercel (for example
  `GEFS_ENABLED`). Variables override the hosted profile; names that are not app settings
  are ignored with a warning, and so is any setting named like a credential (`*_KEY`,
  `*_SECRET`, `*_TOKEN`, `*_PASSWORD`, the database and Redis URLs): those are read only
  from secrets. The run always uses `APP_ENV=production`, so it stops at the configuration
  step without a real email provider instead of marking alerts as sent to nowhere.
- The log of a public repository is public. The run prints the names of the variables it
  applied, never their values; GitHub masks the secrets.
- It installs the backend with the `sync` extra at the versions in `backend/uv.lock`, the
  same versions the daemon image installs.

**Run it**: Actions, "Data sync (manual)", Run workflow. Inputs: `force` (run every stage
even when no new model run is out; runs already stored are still skipped), `only` (a
subset of `spots,ensemble,map`), `migrate` (`python -m app.cli migrate` first). From
outside GitHub:

```bash
curl -X POST -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/OWNER/REPO/dispatches \
  -d '{"event_type": "data-sync", "client_payload": {"force": false, "only": "map"}}'
```

(a fine-grained token with Contents: write on the repository; this call and the workflow
run only on GitHub and were not run while this guide was written). One run at a time
(`concurrency: data-sync`); a run is never cancelled half-way. Exit 3 means the daemon
holds the sync lease right now: nothing was done, run it again later.

**Why there is no schedule.** GitHub's terms say Actions should not be used for "Any
activity that places a burden on our servers, where that burden is disproportionate to the
benefits provided to users (for example, don't use Actions as a content delivery network or
as part of a serverless application ...)". A recurring production data job is that kind of
use. Scheduled runs are also unreliable: "The schedule event can be delayed during periods
of high loads", "some queued jobs may be dropped", and "In a public repository, scheduled
workflows are automatically disabled when no repository activity has occurred in 60 days."
If you decide to add one anyway (for example a short-term stand-in while the daemon is
stopped), add under `on:`

```yaml
  schedule:
    - cron: "41 */6 * * *"   # UTC; avoid minute 0, the busiest time
```

and remove it again when the daemon is back. `backend/tests/unit/test_vercel_deploy.py`
checks that the committed workflow has no schedule, so that test must change with it.

## 9. If something does not work: fallback plans

| Problem | Fallback |
|---|---|
| Services (Beta) misbehaves | Two projects from the same repository: one with Root Directory `frontend/` (set `API_ROUTING=proxy` and `BACKEND_URL` to the API project's URL, so Next.js proxies `/api`), one with Root Directory `backend/`. Previews then need Related Projects and a Deployment Protection bypass header, and the external rewrite has a 120 s timeout. Daemons cannot be added to a project whose Root Directory is a subfolder, so the daemon would need its own project rooted at the repository root (not tried). |
| The daemon does not build or start | Check its build log (Deployments, the production deployment) and the Daemons page. Known risks: the `Containerfile`'s optional `RUN --mount=type=secret,id=ca` (remove the mount if the builder rejects it), and the daemon root `./` being the repository root (Vercel fails the build "If the project's output directory is the same directory as a daemon's `root`"). If the root is the problem, move `data/` under `backend/`, move the `Containerfile` there (adjusting its `COPY` paths) and set the daemon's `root` to `backend/`. Meanwhile run the manual GitHub Actions sync (section 8). |
| The api's build command fails | The build log shows the JSON from `migrate --on-build` (`status`, `error`). A database problem fails the build on purpose. To deploy anyway, set `MIGRATE_ON_BUILD=off` and let the daemon's `init-db` migrate. |
| The API function is too large | Run `scripts/vercel-build-check.sh --offline`: it lists the largest parts of the function. The API must install only the lean dependency set (`[project].dependencies`); the data libraries belong to the `sync` extra. |
| The daemon's CPU quota runs out | The site keeps serving the last published data; the watchdog emails admins. Use the manual runner, set `GEFS_ENABLED=false` if you turned it on, or move to Pro. |

## 10. What was checked, and how

On a development machine (Linux x86_64, 4 shared vCPUs), Vercel CLI 64.5.0, Node.js 22.22.0,
Docker 29.8.2 with Compose 5.6.0. Results of 2026-10-23 unless a date is given.

- **Schema.** `vercel.json` validates against https://openapi.vercel.sh/vercel.json (fetched
  2026-10-23, last modified 2026-10-23 02:39 UTC, JSON Schema draft-04): 0 errors. The
  published schema no longer passes `jsonschema` 4.23.0's own draft-04 meta-schema check
  (inside `functions.experimentalTriggers`), so the file was validated against it with
  that check skipped. A deliberately broken copy (an unknown daemon key, memory `3Gi`, a
  grace period of 4000 s) gave 3 errors.
- **Offline build** (`scripts/vercel-build-check.sh --offline`, section 3.2): **`RESULT: ok`**
  in 53 s with a warm npm cache. The api function: python3.13 ("Using Python 3.13 from
  backend/.python-version", "Installing required dependencies from uv.lock"), maxDuration
  60 s, 3,425 files, **127.1 MiB** (limit 500 MiB, builder threshold 225 MiB); largest
  parts numpy 25.5 MiB plus its libraries 26.2 MiB, SQLAlchemy 17.0 MiB, cryptography
  14.2 MiB, psycopg-binary 8.2 MiB plus 10.8 MiB, pydantic-core 4.6 MiB, redis 2.5 MiB, the
  app 2.0 MiB; no tests, no `.env` file, none of Celery, kombu, pandas, ecCodes, pyarrow,
  scikit-learn, scipy or joblib. Before `excludeFiles` also left out `ml_models/` (the
  lean install cannot load them: no joblib), the same build measured 3,430 files and
  130.9 MiB. The web service: Node.js 24 functions of
  4.6 MiB and less. The api's build command printed `"status": "skipped"`
  (`MIGRATE_ON_BUILD=off`); the cron and both routes are in `.vercel/output/config.json`;
  the first attempt stopped after 2 s with `DAEMONS_NOT_SUPPORTED`, and the `Containerfile`
  is present. In a coding-agent session, Next.js 16.4 detects the agent (environment
  variables such as `AI_AGENT` or `CLAUDECODE`) and its build stops once to recommend an
  upgrade when the installed version has a published advisory; the check above ran with
  those variables unset, as a terminal, CI and Vercel run it. (Historical: that was Next.js
  16.4.0. Since the upgrade to 16.4.1 no advisory applies, and the verifier's builds and
  this check ran on 2026-10-23 with those variables set and did not stop.)
- **Re-run on the final integration tree** (2026-10-23): `RESULT: ok` in 83 s (the first
  attempt refused the daemon after 12 s, the second built in 63 s); the api function 3,428
  files, **127.3 MiB**; web functions 4.6, 3.8 and 1.7 MiB on nodejs24.x, Next.js 16.4.1.
- **The daemon and classic images** (`docker build -f Containerfile .` and
  `docker build -f backend/Dockerfile .`): 72 s and 73 s from a warm base image; the sync
  image is 1.22 GB, the classic one 1.28 GB, and both builds printed `ecCodes 2.49.0`. The
  lean API image of the local stack (`docker/api-lean/Dockerfile`): 18 s, 408 MB, "lean
  install", no `/data`, no `ml_models/`, and `import app.main` works. The sync and classic
  images run `python -m app.cli --help`, and neither has a secret or a proxy setting in its
  history. (These builds reached PyPI through this machine's proxy, passed on the command
  line; nothing of that is in the repository.)
- **Compose files.** `docker compose ... config -q` passes for `docker-compose.yml` (every
  profile) and for `docker-compose.vercel.yml` (with `--env-file /dev/null` and with
  `--env-file .env.vercel.example`).
- **`vercel dev -L`** (section 3.3, steps 1-5, on a copy of the repository and a scratch
  database): the lean venv took 4 s (132 MB), `init-db` 13 s; `/api/health/ready` answered
  200 with database and Redis ok, and `/api/spots`, `/`, `/map` and `/status` 200; it
  printed "`vercel dev` doesn't run daemons: sync. Run them with Docker."
- **The build command** (2026-10-22). The Python builder runs `services.api.buildCommand`
  after installing the dependencies, inside the build venv. With `MIGRATE_ON_BUILD=all`
  and a plain `postgresql://` `DATABASE_URL` naming an empty scratch database,
  `python -m app.cli migrate --on-build` ran the seven migrations up to `0007_sync_runs`
  (46 tables), printed `"status": "migrated"`, and the build went on. (The schema has eight
  since: `0008_publish_stored_runs` is data only, below.) A command that exits
  non-zero fails the build (`[BUILD_UTILS_SPAWN_4]`).
- **Routing on Vercel** (2026-10-22). In the Vercel build, the Next.js routes manifest has
  no rewrites (`API_ROUTING` defaults to `platform` because the CLI sets `VERCEL`) and no
  `.next/standalone` is produced. `.vercel/output/config.json` routes `^/api(?:/(.*))$` to
  `api` and everything else to `web`.
- **The manual runner** (2026-10-22). actionlint 1.7.12 finds nothing in the workflows
  (re-run 2026-10-23). Its `run:` steps were run locally in order, as a runner would
  (inputs and secrets set by hand, `$GITHUB_ENV` carried between steps): the install step
  installed the `sync` extra at the locked versions; the configuration and Sync steps ran
  `migrate`, `check-config`, `sync run --trigger github --force --only spots` and
  `sync status` against a scratch database with the synthetic `demo` provider, and the
  sync finished `success` in 25.9 s. Variables named like credentials were left out with a
  warning and no variable value reached the log. Not run on GitHub itself.
- **Not checked here**: a real deployment (no Vercel account was used), the daemon image
  build and start on Vercel, Neon and Upstash themselves, the cron invocation, previews.

## 11. Sources

Read 2026-10-18 for the design (research) and re-read 2026-10-22 where marked.

| Fact | Quote | Source |
|---|---|---|
| Hobby is non-commercial | "**Hobby teams** are restricted to non-commercial personal use only. All commercial usage of the platform requires either a Pro or Enterprise plan." | https://vercel.com/docs/limits/fair-use-guidelines |
| A paywall is commercial | "Any method of requesting or processing payment from visitors of the site" | same |
| Donations are not | "Asking for Donations **does not** fall under commercial usage." | same |
| Hobby content and AI training | "if you are on a Hobby plan or trial Pro plan, you agree that we may use Your Content to train our artificial intelligence" | https://vercel.com/legal/terms |
| Pro price | "$20/month Pro platform fee"; "Every Pro plan has $20 in monthly credit." | https://vercel.com/docs/plans/pro-plan |
| Hobby limits pause a feature | "if you exceed your usage limits on the Hobby plan, you will have to wait until 30 days have passed before you can use the feature again." | https://vercel.com/docs/plans/hobby |
| Services | "a Next.js frontend and a FastAPI backend in the same repository deploy together with shared routing, environment variables, and a unique domain" | https://vercel.com/docs/services |
| Service path | "The service receives the original request path." | https://vercel.com/docs/services/routing |
| Top-level build fields | "`functions`, `installCommand`, `buildCommand`, `devCommand`, `ignoreCommand`, `outputDirectory` and `framework` keys should be moved into the relevant service." | https://vercel.com/docs/services |
| `vercel dev -L` | "To run everything locally without authenticating with the Vercel Cloud, add the `-L` flag" | https://vercel.com/docs/services |
| Daemons, all plans | "Daemons are available in Public Beta on all plans" | https://vercel.com/docs/daemons (re-read 2026-10-22) |
| Daemons, production only | "During the beta phase, daemons only run in production environments." | same |
| Daemons, not in `vercel dev` | "`vercel dev` currently doesn't run daemons." | same |
| Daemon root | "Projects with a root directory such as `apps/web` can't add daemons yet." | https://vercel.com/docs/daemons/limits (re-read 2026-10-22) |
| Hobby daemon quota | "1 GiB or 2 GiB (1 vCPU) \| 10 Active CPU-hours per month"; "When a Hobby team reaches the quota, Vercel stops the team's daemons until the first of the next month (UTC) or until the team upgrades to Pro." | same |
| Waiting is free | "Time spent waiting, such as long polling an empty queue, doesn't count" | https://vercel.com/docs/daemons |
| Daemon build args | "Vercel passes your deployment's environment variables to the build as build arguments" | https://vercel.com/docs/daemons/config-reference (re-read 2026-10-22) |
| Daemon output-directory rule | "If the project's output directory is the same directory as a daemon's `root`, the build fails." | same |
| Rollout | "Vercel sends `SIGTERM` to the instances from the previous production deployment, then stops them after the shutdown grace period."; "When the new production deployment doesn't declare a daemon, Vercel stops all of that daemon's instances." | https://vercel.com/docs/daemons/managing-daemons (re-read 2026-10-22) |
| Daemon grace period | `terminationGracePeriodSeconds`: "Kubernetes-style termination grace period in seconds. Defaults to 30; maximum 3600 (1 hour)."; memory one of `1Gi`, `2Gi`, `4Gi`, `8Gi` | https://openapi.vercel.sh/vercel.json (fetched 2026-10-23) |
| Hobby cron | "Hobby accounts are limited to daily cron jobs." Precision "Per-hour (±59 min)". | https://vercel.com/docs/cron-jobs/usage-and-pricing |
| Hobby cron timing | "Vercel may invoke these cron jobs at any point within the specified hour" | https://vercel.com/docs/cron-jobs/manage-cron-jobs (re-read 2026-10-22) |
| Cron secret | "The value of the variable will be automatically sent as an `Authorization` header when Vercel invokes your cron job." | https://vercel.com/docs/cron-jobs/manage-cron-jobs |
| Cron retries | "Vercel will not retry an invocation if a cron job fails." | same |
| Python function size | "For Python functions, the maximum uncompressed size is **500 MB**." | https://vercel.com/docs/functions/limitations |
| Env var changes | "Any change you make to environment variables are not applied to previous deployments, they only apply to new deployments." | https://vercel.com/docs/environment-variables |
| CLI ignores | "These ignored files are only relevant when using Vercel CLI." | https://vercel.com/docs/builds/build-features (re-read 2026-10-22) |
| Neon variables | A table: `DATABASE_URL`, "Pooled connection string (PgBouncer)"; `DATABASE_URL_UNPOOLED`, "Direct connection string" | https://neon.com/docs/guides/vercel-managed-integration (re-read 2026-10-22) |
| Neon preview branches | "Environment variables for the branch connection are injected via webhook at deployment time" | same |
| Neon Free | "100 CU-hours/project, 1 GB Postgres storage per project ... and 5 GB of egress per project." | https://neon.com/docs/introduction/plans |
| Neon Launch | "$0.106 per CU-hr $0.35 per GB-month" | https://neon.com/pricing |
| Upstash Free | "Free tier includes 250MB data size and 500K commands per month." | https://upstash.com/pricing/redis (re-read 2026-10-22 in its `.md` version) |
| Upstash on Vercel | "redis-cli --tls -u {{KV_URL}}" (2026-10-18). On 2026-10-22 the page's snippets showed only `KV_REST_API_URL` and `KV_REST_API_TOKEN`, so check the variables after connecting (step 4). | https://vercel.com/marketplace/upstash/upstash-kv |
| Upstash TLS | "TLS is enabled by default for all Upstash Redis databases. It's not possible to disable it." | https://upstash.com/docs/redis/howto/connect-client (re-read 2026-10-22) |
| Open-Meteo | "You may only use the free API services for non-commercial purposes." | docs/API_SETUP_REQUIRED.md (read 2026-10-16) |
| GitHub terms | "Any activity that places a burden on our servers, where that burden is disproportionate to the benefits provided to users (for example, don't use Actions as a content delivery network or as part of a serverless application, but a low benefit Action could be ok if it's also low burden)" | https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features |
| Scheduled workflows | "The schedule event can be delayed during periods of high loads of GitHub Actions workflow runs. ... some queued jobs may be dropped."; "In a public repository, scheduled workflows are automatically disabled when no repository activity has occurred in 60 days." | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows, https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows |
| Public-repo Actions | "GitHub Actions usage is free for self-hosted runners and for public repositories that use standard GitHub-hosted runners." | https://docs.github.com/en/billing/concepts/product-billing/github-actions |
| Secrets and forks | "With the exception of GITHUB_TOKEN , secrets are not passed to the runner when a workflow is triggered from a forked repository." | https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets |

Not verified: the exact labels of Vercel's dashboard screens (the steps follow the
documentation's wording); which variable names Upstash injects today (`KV_URL` on
2026-10-18; step 4 says how to check); the daemon rates on Pro (the limits page says "On
Pro and Enterprise, daemons are billed for Active CPU and Provisioned Memory" and points
to the general pricing page; read 2026-10-22).
