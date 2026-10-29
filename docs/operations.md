# Operations: deploy, monitor, back up, recover

How to run Swell Travel Agent unattended, on Vercel (Upgrade 4) or on one server, notice
when its data goes stale, keep backups that restore, and size the machine. Numbers in
sections 2, 4, 6 and 8 were measured on 2026-10-12 or 2026-10-13 on the Docker Compose stack
with live providers (`FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs`, `GEFS_ENABLED=true`), unless
they say otherwise; the hosted figures (sections 1 and 3) come from the Upgrade 4
measurements of 2026-10-20 to 2026-10-23, cited where they appear. "Verification record" at
the end lists what was and was not run.

Since Upgrade 4 the weather data is pulled when a provider publishes a new model run, by one
coordinated sync, not on fixed timers (docs/sync.md has the rules). Deploying on Vercel step
by step: docs/deploy-vercel.md; testing that setup on your machine first:
docs/local-vercel-stack.md.

## 1. What runs

| Process | Role | Instances | Health check |
|---|---|---|---|
| `backend` | FastAPI API; applies migrations and seeds on start (`RUN_MIGRATIONS=true`) | 1+ (`API_WORKERS` uvicorn workers each) | `GET /api/health/ready` (database and Redis) |
| `worker` | Celery worker: every job in docs/ARCHITECTURE.md §2, from the queues in `WORKER_QUEUES` (default `celery,bulk`) | 1+ (`WORKER_CONCURRENCY` slots each) | `celery inspect ping` |
| `worker-bulk` | optional (`--profile bulk-worker`): a second worker for the `bulk` queue only (data downloads); then set `WORKER_QUEUES=celery` for the main worker | 0-1 | `celery inspect ping` |
| `beat` | Celery beat: the schedule (`app/jobs/schedule.py`, through `app/workers/schedule.py`): the sync check `sync.tick` and the periodic jobs below | **exactly 1** | its schedule file was written in the last 10 min |
| `sync` | optional (`--profile sync`): the sync daemon instead of worker and beat (`JOB_RUNNER=daemon`); never next to beat | 0-1 | its heartbeat in Redis (`/api/health/data`) |
| `frontend` | Next.js server; proxies `/api/*` to the backend | 1+ | `GET /` |
| `postgres` | PostgreSQL 16 (all persistent state) | 1 | `pg_isready` |
| `redis` | Celery broker and results, locks, rate limits, Open-Meteo call budgets and the small provider response cache (transient only); `--maxmemory ${REDIS_MAXMEMORY:-256mb} --maxmemory-policy noeviction` | 1 | `redis-cli ping` |
| `mailpit` | development inbox (not needed with real email) | 0-1 | built in |
| `backup` | optional (`--profile backup`): `scripts/backup-db.sh` every `BACKUP_INTERVAL_HOURS` | 0-1 | newest backup younger than the interval + 1 h |

Redis holds nothing that must survive: on a Redis restart beat re-sends the schedule, a
lost lock only allows one duplicate run (every job is idempotent), and queued alert
deliveries are rows in PostgreSQL, not Redis messages. The Open-Meteo call-budget counters
restart from zero (Open-Meteo still enforces its own limits), and cached provider responses
are fetched again.

**Queues.** Data acquisition (`forecasts.*`, `observations.*`, `verification.*`,
`tides.*`) is routed to the `bulk` queue, everything else (account emails, alert
delivery, matching, the data-health check) to the default `celery` queue
(`app/workers/schedule.py`, `task_routes`), so a verification email never waits behind a
download. The default worker consumes both. A queue that no worker consumes stalls
silently: if you run a bulk-only worker, the main worker must still list `celery`.
Every beat message expires after its own interval, so a worker that was down does not
replay a backlog of missed runs.

**Retries.** In a sync, a component whose acquisition failed is retried
`SYNC_RETRY_MINUTES` (15) after the attempt, or later when the provider asked for a longer
wait, at most `SYNC_MAX_ATTEMPTS` (6) times per model run; then it waits for the provider's
next run, and `/status` shows it `failed`. An Open-Meteo daily or monthly limit makes that
source wait until 00:00 UTC (or the next month) instead. A run that Open-Meteo is still
publishing, or whose servers disagree for a few minutes about it, is `deferred`: no failed
attempt is counted and a later check takes it. The other providers sync normally meanwhile.

### Hosted on Vercel (Upgrade 4)

| Part | Role | Instances | Health |
|---|---|---|---|
| `web` service | the Next.js site, every path except `/api` | Vercel scales it | `GET /` |
| `api` service | FastAPI as one Python function (`app.main:app`, maxDuration 60 s), the lean install (no Celery, no data libraries) | Vercel scales it | `GET /api/health/ready` |
| `sync` daemon | `python -m app.cli sync daemon` from the repo-root `Containerfile`: checks for new model runs, syncs, runs the periodic jobs | **1** in `iad1`, 2 GiB / 1 vCPU, 600 s shutdown grace | its Redis heartbeat (`sync_heartbeat_stale`) |
| daily cron | Vercel Cron: `GET /api/internal/cron/daily` at 07:23 UTC (Hobby: anywhere in that hour), the watchdog (section 3) | 1 a day | Vercel's Cron Jobs page |
| Neon Postgres | all persistent state; the API uses the pooled URL (PgBouncer), the daemon and migrations the direct one | Neon | `GET /api/health/ready` |
| Upstash Redis | rate limits, job locks, Open-Meteo call budgets, the daemon's heartbeat, known runs and kick flags, the public snapshot cache; no Celery broker | Upstash | `GET /api/health/ready` |

The same image runs anywhere: the local Vercel-style stack (docs/local-vercel-stack.md)
runs this topology in Docker, with Caddy for Vercel's router, PgBouncer for Neon's pooler
and plain Redis for Upstash.

### The synced pipeline replaces the acquisition timers

Before Upgrade 4, beat fetched the spot forecasts and the map layers every
`FORECAST_REFRESH_MINUTES` (180), GEFS-Wave at fixed times, and ran a prediction safety net
and an hourly detection. Those five entries are gone. Now every `SYNC_CHECK_MINUTES` (10)
the runner reads the providers' metadata only (Open-Meteo's `meta.json`, NOAA `.idx` HEAD
requests) and, when a run is newer than the stored one, runs one sync of 12 stages under a
database lease (one sync at a time): spots → ensemble → map → blend → predict → publish →
detect → match → flights → alerts → deliver → prune. A run already stored is never fetched
again, so duplicate triggers are harmless. The daemon's idle check makes no database query
(measured: 0 SQL statements and 0 pool checkouts after a sync), so a scale-to-zero database
stays asleep between syncs.

Still on timers, and why:

* **Tides** (`tides.acquire`, daily 02:20 UTC): tide predictions change slowly; one request
  (or local computation) per station a day. At start the daemon runs, once, every periodic
  job that has not succeeded within its longest gap (24 h for a daily job) or never has
  (`SYNC_DAEMON_CATCH_UP`, default true), after init-db and before the first check, so a
  brand-new deployment gets its tide predictions within minutes, not at the next 02:20 UTC.
  A job that started in the last hour (or its own interval, when shorter) is skipped, so a
  restart loop does not ask NOAA CO-OPS again at every start. Celery's beat runs the job
  once at start (`BEAT_RUN_ON_START`). Data health flags a tide provider none of whose
  linked stations was ever tried 3 h after the tide catalog was loaded
  (`tides_missing:<provider>`).
* **Observations and verification**, when `OBSERVATIONS_ENABLED` / `VERIFICATION_ENABLED`:
  buoys report all the time (hourly ingest); the verification jobs pair forecasts with
  them (00:50/06:50/12:50/18:50 and 04:10 UTC).
* **Maintenance** (`maintenance.cleanup`, daily 03:30 UTC): retention.
* **The per-user jobs and checks** between syncs (a sync runs them too): matching, flights,
  alerts, delivery, the staleness check and the data-health check, at least
  `JOB_INTERVAL_FLOOR_MINUTES` apart (60 on Vercel, so the database is woken at most hourly).

### Runners: the sync daemon or Celery

`JOB_RUNNER` picks who runs the jobs; both run the same sync code and the same schedule
(`app/jobs/schedule.py`).

| | `daemon` (Vercel; the local Vercel-style stack; compose profile `sync`) | `celery` (the classic stack, the default) |
|---|---|---|
| Process | one `python -m app.cli sync daemon` | a worker (`celery`, `bulk` queues) and exactly one beat |
| Model-run check | its own, every `SYNC_CHECK_MINUTES`, from Redis and the providers only | beat's `sync.tick` (bulk queue, 60 min time limit), which plans with the database |
| Periodic jobs | in-memory timers starting one interval after the daemon, batched hourly when floored; daily jobs by the UTC clock; at start, once, the jobs without a success within their longest gap (`SYNC_DAEMON_CATCH_UP`) | beat entries, also once at beat start |
| "Match my search", "refresh flights" | the API writes a Redis kick flag; the daemon picks it up within `SYNC_DAEMON_POLL_SECONDS` (60) | queued Celery tasks |
| Account emails, SMS | sent inline by the API (3 attempts, 0.5 s and 1.5 s apart) | queued; inline when the broker does not answer within 2 s |
| Liveness | Redis heartbeat at every check (`sync_heartbeat_stale`) | the newest finished job (`jobs_not_running`) |
| Lease when unset | `SYNC_LEASE_MINUTES` 15 | 45 |
| At start | `init-db` (migrate, seed, tide catalog), the known runs, then the overdue periodic jobs (the start-up catch-up) | beat queues `sync.tick` and `tides.acquire` |

Run either the daemon or worker and beat, never both against one database.

### The schedule

`build_schedule` (`app/jobs/schedule.py`); every interval is raised to at least
`JOB_INTERVAL_FLOOR_MINUTES` (0 in the classic stack, 60 on Vercel). The daemon runs every
entry but `sync-tick` (it checks on its own).

| Entry | Task | When (classic) | Hosted (floor 60) |
|---|---|---|---|
| sync-tick (beat only) | `sync.tick` | every `SYNC_CHECK_MINUTES` (10) | the daemon checks every 10 min itself |
| matching-housekeeping | `matching.match` | daily 03:45 UTC (matching also runs after every publication, as a sync stage) | the same |
| alert-generation | `alerts.generate` | every 15 min | hourly |
| notification-delivery | `notifications.deliver` | every 2 min | hourly |
| forecast-staleness-check | `forecasts.check_staleness` | hourly | hourly |
| data-health | `ops.data_health` | every `DATA_HEALTH_CHECK_MINUTES` (15) | every 60 min (`DATA_HEALTH_CHECK_MINUTES=60`) |
| maintenance-cleanup | `maintenance.cleanup` | daily 03:30 UTC | the same |
| tide-acquisition (with `TIDE_PROVIDERS`) | `tides.acquire` | daily 02:20 UTC | the same |
| observation-ingest (with `OBSERVATIONS_ENABLED`) | `observations.ingest` | hourly | hourly |
| point-forecast-archive, verification (with `VERIFICATION_ENABLED`) | `verification.collect_point_forecasts`, `verification.run` | 00:50, 06:50, 12:50, 18:50; 04:10 UTC | the same |
| (Vercel Cron) | `ops.data_health` through `GET /api/internal/cron/daily` | | daily 07:23 UTC, `vercel.json` |

The old acquisition tasks (`forecasts.acquire`, `forecasts.map_fields`,
`forecasts.acquire_ensemble`) can still be run by hand; a hand-run acquisition does not
publish, and the next sync (or `python -m app.cli sync publish`) does.

## 2. Deploy on one server (Docker Compose)

1. Install Docker Engine with Compose v2, clone the repository, `cp .env.example .env`.
2. Production settings in `.env` (the backend refuses to start without the first four):

   ```env
   APP_ENV=production
   SECRET_KEY=<64 random characters>          # python -c "import secrets; print(secrets.token_urlsafe(48))"
   COOKIE_SECURE=true
   ENABLE_DEV_ENDPOINTS=false
   EMAIL_PROVIDER=resend                      # or smtp / sendgrid; mailpit/console only capture
   RESEND_API_KEY=...
   EMAIL_FROM=Swell Travel Agent <alerts@your-domain>
   APP_BASE_URL=https://your-domain
   API_BASE_URL=https://your-domain
   CORS_ORIGINS=https://your-domain
   POSTGRES_PASSWORD=<random>
   FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf   # docs/API_SETUP_REQUIRED.md
   GEFS_ENABLED=true
   # Publish only on the loopback interface; a reverse proxy (Caddy, nginx) terminates TLS.
   POSTGRES_PORT=127.0.0.1:5432
   REDIS_PORT=127.0.0.1:6379
   BACKEND_PORT=127.0.0.1:8000
   FRONTEND_PORT=127.0.0.1:3000
   MAILPIT_UI_PORT=127.0.0.1:8025
   MAILPIT_SMTP_PORT=127.0.0.1:1025
   ```

   `*_PORT` values become the host side of the port mapping, so `127.0.0.1:5432` keeps
   PostgreSQL and Redis off the public interface. Set `APP_BASE_URL`, `API_BASE_URL` and
   `CORS_ORIGINS` explicitly when you do this (their defaults are derived from the ports).
3. `docker compose up -d --build`, then `docker compose ps` until every service is
   *healthy* (about 30 s after the images are built). With this recommended setup the first
   live acquisition takes about 1-3 min: the two spot-forecast families under 2 min, GEFS-Wave
   about 12 s and a full map cycle about 1-2.5 min (§6); with the direct GRIB fallback
   (`noaa_gfs,ecmwf_ifs`) about 8.5 min.
4. Create your account in the web app, verify the email, then make it an admin (admins
   get the data-health alerts and the accuracy dashboard):
   `docker compose exec backend python -m app.cli make-admin you@your-domain`.
5. Backups: `docker compose --profile backup up -d backup` (§4), and copy the backup
   directory off the server.
6. An external monitor on `https://your-domain/api/health/data?strict=true` (§3).

Releases: back up (`scripts/backup-db.sh --compose`), `git pull`,
`docker compose up -d --build`. The backend applies new migrations on start; migrations
never delete data. To run pre-built images instead of building on the server, set
`BACKEND_IMAGE` / `FRONTEND_IMAGE` to your registry tags.

Upgrading a Celery deployment from a build older than 2026-10-10 (before commit b83c454):
those builds queued verification and password-reset emails as the Celery task
`notifications.send_email`, which current builds no longer register, so a current worker
logs "Received unregistered task" and discards such a message. After stopping the old
worker, check that the broker holds none:
`docker compose exec redis redis-cli -n 1 LRANGE celery 0 -1 | grep -c notifications.send_email`
should print 0 (`-n` is the database of `CELERY_BROKER_URL`, 1 in docker-compose.yml). If
it does not, start the old worker again until the queue is empty, then upgrade.

Images (measured): backend 1.29 GB (Python 3.13, ecCodes, pandas, pyarrow,
scikit-learn), frontend 343 MB, `postgres:16-alpine` 420 MB, `redis:7-alpine` 58 MB,
`axllent/mailpit:v1.27` 51 MB. Building both app images from scratch (base images already
pulled, no layer cache: pip and npm install included) took 2 min 54 s on 4 vCPUs; a backend
rebuild after a code-only change took 1.6 s (the dependency layer is cached).

## 3. Monitor

### Data health: `GET /api/health/data`

`app/services/health.py` builds one report, served by the API and used by the
`ops.data_health` job:

| Check | Problem when | Severity |
|---|---|---|
| Each `FORECAST_PROVIDERS` source | newest successful run older than the source's own stale limit (`queries.stale_after_hours`: Open-Meteo GFS family 17.2 h, Open-Meteo ECMWF family 24.7 h, NOAA direct 16 h, ECMWF direct 25 h, others `FORECAST_STALE_HOURS`), or no successful run | degraded |
| All forecast sources at once | none of them is fresh: forecasts, events and alerts run on stale data | **failing** |
| Blend (two or more independent families) | stale or missing although every member is fresh and no `forecasts.acquire` / `forecasts.blend` run is in progress (`forecasts.blend` broken; during an acquisition the blend trails its first refreshed member until the chained blend stage runs) | degraded |
| GEFS-Wave (`GEFS_ENABLED`) | older than 18.5 h or missing (uncertainty only) | degraded |
| Every scheduled job | last real run failed (with the number in a row); no run started for 2 × its longest interval + 30 min (overdue; a run in progress counts, also when a duplicate was logged "skipped" after it); still "running" 15 min past the 30-min hard time limit (stuck: worker killed, e.g. out of memory) | degraded |
| Heartbeat | no job of any kind finished for `DATA_HEALTH_HEARTBEAT_MINUTES` (30; `notifications.deliver` alone runs every 2 min): worker or beat down | **failing** |
| Map ocean layers (unless off) | served GFS cycle older than `MAP_FIELD_STALE_AFTER_HOURS` (21 h, the map index's own "out of date" limit), or no complete field set | degraded |
| Buoy observations (`OBSERVATIONS_ENABLED`) | newest observation of a provider older than `DATA_HEALTH_OBSERVATION_STALE_HOURS` (default: the 48 h ingestion look-back, beyond which the hourly job cannot fill the gap) | degraded |
| Storage | database larger than `DATA_HEALTH_DATABASE_MAX_GB` (default 20 GB; an alert only, nothing is deleted) | degraded |
| Redis | used memory above `DATA_HEALTH_REDIS_MAX_FRACTION` (0.8) of its cap (Redis's `maxmemory`, or `REDIS_MAXMEMORY` when Redis runs without one); an evicting `maxmemory-policy` (not checked under `JOB_RUNNER=daemon`, which has no broker); Redis not answering | degraded |
| Sync daemon heartbeat (`JOB_RUNNER=daemon`) | the daemon's heartbeat in Redis is missing or older than max(3 × `SYNC_CHECK_MINUTES`, `DATA_HEALTH_HEARTBEAT_MINUTES`), 30 min by default, and no sync with a valid lease is running | **failing** |
| Syncs | the two newest finished syncs both failed or were abandoned | degraded |
| CPU budget (`SYNC_CPU_BUDGET_HOURS`, 5 on Vercel) | this month's CPU of the background jobs (job logs, a Redis counter, the daemon's own) above 80 % of the budget | degraded |
| Licence | `COMMERCIAL_USE=true` with an Open-Meteo source and no `OPEN_METEO_API_KEY` | **failing** |
| Tides | no future predictions from a provider, or the tide model's constants file unreadable where it is deployed | degraded |

The overall status is `failing` if any problem is failing, `degraded` if any is degraded,
else `ok`, and `reasons` lists one plain sentence per problem, the most severe first. The
limits are operational heuristics (how late data may be before someone should look),
not statements about forecast accuracy.

**Public on purpose.** The report holds operational metadata only: run keys, ages, limits,
job states and sentences written by the service. No personal data, no settings values beyond
the limits, and no raw error text (a failed job's redacted error appears in the admin email
only). It must be readable without credentials because the most important failure, a dead
worker, cannot report itself. It is rate limited (60 requests per minute per IP). Since
Upgrade 4 the plain report is shared by the CDN for 60 s (`Cache-Control: public,
max-age=0`, `CDN-Cache-Control: public, s-maxage=60`), so every visitor of `/status` reads
at most one report a minute; with `?strict=true` it is never cached (`no-store`) and the API
answers 503 instead of 200 when the status is `failing`, for monitors that only look at
status codes. The status page (`/status`) shows the same report (an open tab re-reads it
when it regains focus, and every minute only while a sync runs or under Celery: before the
final review it re-read every 5 minutes under the daemon, which kept Neon awake as long as a
`/status` tab stayed visible), with a sync section: the runner, the daemon's last check, the
newest and the newest published sync with their stages, each component's state (current,
new, waiting, in_progress, failed, unknown, disabled) and the CPU meter.

**Problem keys** (the `problems` of the report and the subject of the admin emails), and
what to do:

| Key | Severity | Means | Do |
|---|---|---|---|
| `forecast_stale:<source>`, `forecast_missing:<source>` | degraded | that source's newest run is past its own stale limit, or none succeeded | section 5, "Data is stale" |
| `forecasts_unavailable` | failing | no forecast source is fresh | the same, first |
| `blend_stale`, `blend_missing`; `ensemble_stale`, `ensemble_missing` | degraded | the blend is behind fresh members; GEFS-Wave is old | `python -m app.cli sync run --force` |
| `map_fields_missing`, `map_fields_stale` | degraded | no servable map run, or the served cycle is older than `MAP_FIELD_STALE_AFTER_HOURS` (21 h) | "Data is stale" (the map comes from NOAA NODD) |
| `job_failed:<job>`, `job_overdue:<job>`, `job_stuck:<job>`, `jobs_never_ran` | degraded | a periodic job failed, is late or was killed mid-run | section 5, "Worker or beat down" |
| `jobs_not_running` | failing | Celery: no job finished for `DATA_HEALTH_HEARTBEAT_MINUTES` | the same |
| `sync_heartbeat_stale` | failing | the sync daemon has stopped checking | "Daemon quota reached" (on Vercel), or restart the daemon |
| `sync_failed` | degraded | the last two syncs failed; the email has the error | `python -m app.cli sync status`, then the provider's runbook |
| `sync_cpu_budget` | degraded | above 80 % of `SYNC_CPU_BUDGET_HOURS` this month | "Daemon quota reached" |
| `licence` | failing | commercial use with the free Open-Meteo API | set `OPEN_METEO_API_KEY`, or `COMMERCIAL_USE=false` if the site is not commercial |
| `tides_missing:<provider>` | degraded | none of the provider's linked stations was tried 3 h after the tide catalog was loaded | check outbound access to `api.tidesandcurrents.noaa.gov`; run `python -m app.cli tides acquire` |
| `tides_stale:<provider>` | degraded, or failing when no linked station has a future prediction | predictions run out within 2 days | check outbound access to `api.tidesandcurrents.noaa.gov`; run `python -m app.cli tides acquire` |
| `tides_constants:harmonic_model` | failing | the tide model's constants file cannot be read (model tides unavailable) | check `data/tides/` (`TIDE_CONSTANTS_PATH`) in the daemon or worker image |
| `observations_missing:<provider>`, `observations_stale:<provider>` | degraded | a buoy provider has no recent observation | the provider's status; `python -m app.cli obs ingest` |
| `database_size` | degraded | above `DATA_HEALTH_DATABASE_MAX_GB` (0.85 on Neon Free) | "Database or disk full"; on Neon Free, "Neon suspended" |
| `redis_unavailable`, `redis_memory`, `redis_eviction_policy` | degraded | Redis not answering, near its cap, or evicting | "Redis memory high" |
| `new:database_unreachable` (watchdog only, in `alerts_sent`) | | the cron watchdog could not reach the database; `OPS_ALERT_EMAILS` were emailed | "Neon suspended" |

From a shell or cron, `scripts/check-health.sh https://your-domain` prints the reasons and
exits 0 ok, 1 degraded, 2 failing, 3 unreachable:

```cron
*/5 * * * * /srv/swell/scripts/check-health.sh --quiet https://your-domain || [ $? -eq 1 ] || echo "Swell data health FAILING" | mail -s "swell" you@your-domain
```

The same command works locally: `python -m app.services.health` prints the JSON report.

### Admin freshness dashboard: `/admin/freshness` (Swell 2.0)

The public report above stays minimal on purpose. Everything section 16 of the spec asks an
admin to see is on `/admin/freshness` (linked from `/status` for admin accounts), fed by
`GET /api/admin/freshness` (`app/api/admin.py`, `app/services/admin_freshness.py`):

* **Who can read it.** Signed-in admin accounts only (`python -m app.cli make-admin EMAIL`):
  401 signed out, 403 for other users; signed-out visitors of the page are sent to sign in.
  Always `Cache-Control: private, no-store`, never shared by the CDN: the body carries
  redacted error text, quota counters and per-spot failure reasons.
* **What it costs.** Bounded reads only, never a provider call: the newest 30 forecast
  versions by id (without their spot maps) and the published one, the newest 20 syncs, the
  newest 200 forecast runs by id, today's flight searches through their `searched_at` index,
  today's account and alert emails (`email_messages` and `notifications` have no index on
  `created_at` alone: these two counts scan the rows retention keeps), plus a few Redis reads
  (the call counters, the latency list, the first-served marks of the newest 5 versions, the
  provider health and the refresh check), and the data-health report. Nothing here runs for a
  public request.
* **When the page reads it.** On opening, when the tab regains focus (a report older than
  15 s), on "Reload", and when the published forecast version changes: the page polls the
  Redis-only `GET /api/forecasts/version` every `NEXT_PUBLIC_FORECAST_VERSION_POLL_MS` (20 s)
  while the tab is visible, never the report itself. Without that endpoint (a deployment
  without the live-updates API answers 404) the poll stops and the report is still read on
  focus.

| Group | What it shows | Where it comes from |
|---|---|---|
| Problems | the admin-only problems (next section) and the public data-health problems, the most severe first, with the redacted last error | `app/services/health.py` |
| Provider and models | the active provider, why it was chosen (`WEATHER_PROVIDER` / `FORECAST_PROVIDERS`), whether a fallback stands in (Windy -> Open-Meteo) and until when, the recorded provider health; per component (atmosphere, wave): the model, the served run, its upstream availability, when it was detected and acquired, whether it was carried forward from an older refresh, its typical lag and when the next run is expected and counts as late | `forecast_versions.components`, the provider manager |
| Detection to publication | the last detected model update, the last successful acquisition and scoring (from the forecast runs before the first version exists), the published version with its spot counts (updated, stale, failed) and the refresh scheduler's last check | `forecast_versions`, `forecast_runs`, Redis |
| Freshness timeline | the newest versions: status, publication time, wind and wave run, which components changed, spot counts, availability -> publication | `forecast_versions` |
| Stale spots | the published version's stale active spots with their last update, the reason (latest refresh failed, no data, a component older than its model's interval + threshold) and the failed refresh's error | `versions.spot_freshness` |
| Failures | non-2xx upstream responses of the last 24 h by source and HTTP status, failed flight searches by provider | `forecast_runs.details.stats`, `flight_searches` |
| Refresh jobs | the newest 20 syncs: trigger, status, kinds, start, duration, every stage's duration, the versions they published, the redacted error | `sync_runs` |
| API usage against the limits | see "API usage reporting" below | Redis counters, `flight_searches`, emails |
| Processing times and latencies | per stage of the finished refresh syncs: runs, average, p95, maximum, average CPU; the whole refresh; detection -> acquisition -> scoring -> publication; upstream availability -> publication (versions that fetched new data; a retry of failed spots is left out); publication -> browsers (p50/p95 of the open tabs' reports after a version change, newest 200 in Redis); publication -> first served by the version endpoint | `sync_runs.stages`, `forecast_versions`, `versions.seen_latencies`, `versions.first_served_at` |

Before the first versioned refresh (a database upgraded from Upgrade 4) the model, spot and
latency figures stay empty and the page says so; acquisitions and requests still show from
the stored forecast runs. Verified on a clone of the real data of 2026-10-16: the requests of
the last 24 h read the runs' own records (Open-Meteo GFS 11 requests and 142.86 weighted
calls, ECMWF 7 and 124.29, the NOAA GRIB layers 63 requests and 71 MB), and after one version
was published from the stored run the typical lags came out as the run's own availability
times (GFS-Wave 318 min after the cycle, GFS atmosphere 374 min: its 0.25 deg domain is the
later one).

### API usage reporting

`app/services/usage.py` reports what the app spent against each upstream allowance; the
dashboard's "API usage" group and the `quota_near` problem read it:

| Counter | Window | Limit | Notes |
|---|---|---|---|
| Open-Meteo weighted calls | current UTC minute, hour, day, month | `OPEN_METEO_CALLS_PER_MINUTE` / `_HOUR` / `_DAY` / `_MONTH` (600 / 5,000 / 10,000 / 300,000, the free tier) | Open-Meteo bills weighted calls (more than 10 variables or 14 days per location count extra), not HTTP requests; its metadata (`meta.json`) calls are free and not counted. Reserved before each request (`app/services/forecasts/quota.py`) |
| Windy requests | current UTC day | `WINDY_DAILY_REQUEST_LIMIT` (10,000) | counted by the providers package (`quota.usage_snapshot`); a build without it shows "not counted" |
| Flight provider requests | current UTC day | `FLIGHT_DAILY_QUOTA` (300) | every attempt counts (`app.core.redis.consume_quota`, key `quota:<provider>:<YYYYMMDD>`); the demo provider has no quota. Also shown: searches stored today per provider, failed ones, and `FLIGHT_USER_DAILY_QUOTA` per user |
| Emails | current UTC day | none enforced here | account emails (verification, password reset, tests) and alert emails created today, and how many failed, bounced, were rejected or complained; the provider's own quota (e.g. Resend's daily limit) is in its console |
| Forecast requests | last 24 h | none | per source: runs started, failed runs, HTTP requests, weighted calls, bytes and responses by status, from each run's `details.stats` |

Every counter lives in Redis and restarts from zero when Redis loses its data; a counter
that cannot be read is shown as unknown, never as 0. The console figures (Neon, Upstash,
Vercel) are not visible to the app: see "Budgets and meters" below.

### Admin alerts (`ops.data_health`, every `DATA_HEALTH_CHECK_MINUTES`: 15, hourly on Vercel)

The job emails every **active admin account with a verified address** through the
configured email provider (users never get these emails, whatever their alert settings):

* when a problem appears (new) or its severity rises;
* again at most every `DATA_HEALTH_ALERT_REPEAT_HOURS` (12 h) while it lasts;
* once more when it clears (recovery notice, only for problems that were emailed).

A problem that clears and comes back (flaps) is emailed again only once 12 h have passed
since its last alert: in between, the recurrence is listed under "already reported" in any
other email and clears without a second recovery notice. So each problem costs at most one
alert and one recovery notice per 12 h, however often it flaps; only a rise in severity is
sent at once.

All due items of one run go into one email per admin; the email lists the other current
problems as "already reported", the last error of a failed job, and links to `/status`,
`/admin/freshness` and `/api/health/data`. A problem is only marked as sent when the provider accepted (or
the development inbox captured) at least one email; otherwise the next run tries again.
The de-duplication state lives in the job's own `background_job_logs.details`, so a
restart neither loses it nor re-sends old alerts. Logs carry counts, never addresses.
Settings: `DATA_HEALTH_CHECK_MINUTES` (15), `DATA_HEALTH_ALERTS_ENABLED` (true),
`DATA_HEALTH_ALERT_REPEAT_HOURS` (12), `DATA_HEALTH_HEARTBEAT_MINUTES` (30),
`DATA_HEALTH_OBSERVATION_STALE_HOURS`, `DATA_HEALTH_DATABASE_MAX_GB`.

**Who runs it.** Celery beat in Docker; the daemon's own timer under `JOB_RUNNER=daemon`;
on Vercel without the daemon (the hosted profile, no daemon heartbeat in Redis) the refresh
endpoint runs it at the first call after `DATA_HEALTH_CHECK_MINUTES` (60 there) have passed,
and sooner after a refresh that failed or was partial, or when a component's metadata
becomes unreadable (one Redis timestamp, `sync:data-health-at`: a call with nothing due
costs two Redis commands and no database read), and the daily cron runs it as well. Sooner
is never within `SYNC_RETRY_MINUTES` (15) of the last start, and a lasting unreadable
component (a metadata outage, a refused provider setting) runs it only as it starts and once
`PROVIDER_UNAVAILABLE_AFTER` (30 min) later, then hourly (`sync:data-health-signal`, two more
Redis commands per check while it lasts; review r3-backend-R3-2): before, it woke the
database at every 5-minute check.
**Without the daemon set `MAP_FIELDS_ENABLED=false`.** The hosted profile keeps the map
layers on for the daemon; with no daemon they are never synced, so data health stays
degraded (`map_fields_missing`, `sync_heartbeat_stale`) and the admins get "Data health
DEGRADED" again every `DATA_HEALTH_ALERT_REPEAT_HOURS` (12) among the real alerts. With
`MAP_FIELDS_ENABLED=false` the map shows no overlay and data health stays `ok` (review
r3-spec-SPEC-1, `tests/integration/test_daemonless_hosted.py`). So without the daemon a scheduler
that stops calling the endpoint stops the hourly check with it: `refresh_scheduler_silent`
is then raised by the daily cron, up to about 25 hours later on Hobby (docs/deploy-vercel.md
step 7b names an external check for faster notice).

**When the email provider itself refuses its key** (`provider_credentials:email_<provider>`),
no alert email can go out through it. The admins whose phone number is verified and opted
in to SMS (Settings) then get one SMS about it per UTC day when `SMS_PROVIDER` is configured
(Twilio, `app/services/notifications/ops_alerts.py`, `alert_email_refused_by_sms`). Without
such a number the problem shows only on `/admin/freshness` and in `GET /api/health/data`:
watch that endpoint with an external monitor (hourly or less often; each call wakes the
database), or check the dashboard after any account email fails to arrive.

**Admin-only problems (Swell 2.0, design D12).** The job also checks the problems below
(`app/services/health.py`, `data_health(admin=True)`). They follow exactly the rules above
(new, rise in severity, reminder every 12 h, recovery notice, flapping) and go into the same
email, only to the same verified admins. They are never part of the public report
(`/api/health/data` and `/status` keep only the checks of the table further up); the admin
dashboard lists them. If these checks themselves fail (a bug, a database error) the job
reports `admin_checks_failed` and still sends the public problems' alerts; the admin problems
already open stay open meanwhile (no recovery notice is sent for a check that did not run).

| Key | Severity | Means (threshold) | Do |
|---|---|---|---|
| `model_run_late:<atmosphere\|wave>` | degraded | no newer run of that component is known (published, staged, attempted by a refresh, or seen by the daemon's last check) although its expected availability has passed: the newest run's init time + the model's update interval (`update_interval_seconds` from Open-Meteo's meta.json where the version recorded it, else 6 h, said "assumed") + the typical lag measured on the recent versions (median of availability minus init time, over the runs whose domains share one cycle: a run that pairs GFS 0.13 of 06Z with GFS 0.25 of 00Z would add a whole cycle) + a grace of max(90 min, the measured spread of that lag). Without a measured lag (no version with an availability time yet) nothing is judged: never a fixed wall-clock guess. The 90 min cover Open-Meteo's servers disagreeing about the same run (62 min apart, measured 2026-10-26), the 10-minute servable wait and one check interval | the dashboard's model block shows the expected and late times. Check the provider's own update page (Open-Meteo: open-meteo.com/en/docs/model-updates); if the provider has the run, look at `provider_unavailable`, `refresh_failures` and `refresh_scheduler_silent`, then `python -m app.cli sync check` |
| `forecast_stale_spots` | degraded | active spots of the published version serve a stale forecast: their latest refresh failed while others updated, they have no data, or a component is older than its model's interval + `FORECAST_STALE_THRESHOLD_MINUTES` (360; per model `FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL`). The email names up to 8 spots and the failed refreshes' errors | the site already labels them "Forecast last updated N hours ago". A retry of the failed spots follows automatically (5, 15, 45 min); if they stay stale, read the errors on the dashboard (stale spots, refresh jobs) |
| `refresh_failures` | degraded | the newest 3 or more finished spot-refresh syncs in a row failed or were abandoned (skipped checks and map-only syncs do not count; `sync_failed` above already fires at 2 of any kind). The newest error goes to the email only | `python -m app.cli sync status`; then the provider's runbook below ("A forecast provider is down or late", "Open-Meteo refuses requests") or "A deploy killed a sync" |
| `provider_credentials:<provider>` | failing; degraded while a fallback stands in for that provider | the provider refused the key (HTTP 401/403, Windy's HTTP 400 "Invalid API key") or answered with a Windy TESTING key, in the newest attempt of its source or in the provider health the providers package records; no later success | fix the key in the deployment's environment (`OPEN_METEO_API_KEY`, `WINDY_API_KEY`, the flight keys, `RESEND_API_KEY` / `SENDGRID_API_KEY`, or `SMTP_USERNAME` / `SMTP_PASSWORD` for `email_smtp` when the SMTP server answers 535; docs/API_SETUP_REQUIRED.md, docs/WINDY_API_SETUP.md). A testing key is never stored or published: replace it with the paid Point Forecast key. For `email_*` the alert cannot be emailed through the refusing provider: see "When the email provider itself refuses its key" above (SMS to admins with a verified phone, else only `/admin/freshness` and `/api/health/data`) |
| `provider_unavailable:<source>` | degraded | the provider's run metadata could not be read for 30 min (`unknown` components in the daemon's or the refresh endpoint's check, or Windy's consecutive transient failures). When it was first seen unknown is kept in Redis (`ops:metadata-unknown:v1`), so the duration counts across checks; the resolution is the alert job's own interval | the provider's status page; from the server `python -m app.cli forecast provider-check` (one free metadata request) once the providers package is merged; outbound access and DNS of the host |
| `provider_fallback:<provider>` | degraded | Open-Meteo stands in for Windy: the provider manager holds a fallback after a credential, testing-key, quota or availability failure, or the published version says `fallback_from` | the site keeps updating from Open-Meteo. Fix the cause shown (often `provider_credentials` or `quota_near:windy_day`); Windy is tried again automatically when the hold ends, and a successful Windy refresh ends it at once |
| `quota_near:<counter>` | degraded | `QUOTA_ALERT_FRACTION` (0.8) of a provider allowance is used: `open_meteo_day`, `open_meteo_month` (weighted calls), `windy_day` (requests), `flights_<provider>_day` (`FLIGHT_DAILY_QUOTA`). Clears when the window resets or the limit is raised | the dashboard's usage group; "Open-Meteo refuses requests" below. A paid plan raises the limits (`OPEN_METEO_CALLS_PER_*`, `WINDY_DAILY_REQUEST_LIMIT`, `FLIGHT_DAILY_QUOTA`) |
| `refresh_scheduler_silent` | degraded | no refresh check for `REFRESH_SCHEDULER_EXPECTED_MINUTES` (30; at least 3 × `SYNC_CHECK_MINUTES` when Celery or a daemon whose `SYNC_DAEMON_KINDS` include `spots` is the runner, as for the daemon's heartbeat; never while a sync one of them started runs under a valid lease): neither the refresh endpoint `/api/internal/refresh` nor the daily backstop recorded one, no daemon that checks spots (`SYNC_DAEMON_KINDS` with `spots`) wrote a heartbeat, Celery's `sync.tick` did not run, and no sync they started began. Before any check was ever recorded it fires once background jobs have been logged for that long. Off at `REFRESH_SCHEDULER_EXPECTED_MINUTES=0`, and while `FORECAST_REFRESH_ENABLED=false` | check the scheduler that calls `/api/internal/refresh` (QStash or cron-job.org, docs/sync.md "Scheduling options", docs/deploy-vercel.md step 7b; the route needs `CRON_SECRET`, at least 16 characters), the daemon or Celery beat. Do not silence it with 0 on a hosted deployment: without a scheduler the spot forecasts refresh only once a day (the daily cron's backstop), which breaks the freshness goal (spec section 9). Only a deployment that knowingly runs on the daily cron alone, accepting that trade-off, sets 0 (the largest accepted value, 1440, is shorter than a Hobby cron's day plus its ±59 min window) |

The alert path has a blind spot by construction: if the worker, Redis or the email
provider is down, no email goes out. That is why the external monitor in §2 step 6
matters.

### The daily cron watchdog (Vercel)

Vercel Cron calls `GET /api/internal/cron/daily` once a day (`23 7 * * *` UTC; on Hobby
"at any point within the specified hour", and a failed call is not retried) with
`Authorization: Bearer $CRON_SECRET`. It runs `ops.data_health` inside the API function,
independently of the daemon and the refresh scheduler, so a silent scheduler
(`refresh_scheduler_silent`) or, where the daemon is meant to refresh the map layers, a
stopped daemon (`sync_heartbeat_stale`) is still noticed. Then, under `JOB_RUNNER=daemon`, it
runs every periodic job whose newest run is older than its own interval (maintenance cleanup,
the matching housekeeping, tide acquisition, alerts, delivery, the staleness check; at most
150 s, `app/services/sync/standin.py`), so these run at least daily without a daemon, and
last one refresh check as a backstop. It answers 404 when `CRON_SECRET` is not set, 401 on a
wrong or missing token, and 200 `{status, problems, alerts_sent, heartbeat_age_minutes,
jobs, refresh}` otherwise. When the database itself
cannot be reached (for example Neon's compute suspended after its quota) the admins cannot
be read from it: the watchdog then answers 503 and emails `OPS_ALERT_EMAILS` instead, at most
once per UTC day (`alerts_sent` lists `new:database_unreachable`). Measured in the local
Vercel-style stack on 2026-10-23: the right token 200, a wrong token 401, no token 401,
`CRON_SECRET` unset 404; with the database port closed (round 1, 2026-10-22) it answered
503, emailed `OPS_ALERT_EMAILS` once and sent nothing on the second call.

Try it by hand (runs only against a deployment; locally `scripts/vercel-local.sh cron`
makes the same call to the local stack):

```bash
curl -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/internal/cron/daily
```

Point an uptime monitor at `/api/health` (the API without the database) or at `/`, at any
rate. `/api/health/ready` and `/api/health/data?strict=true` wake Neon on every hit: every 15
minutes costs about 45-60 CU-hours a month (on top of about 20 in the background); hourly
costs at most about 15. On Neon Free, rely on the daily watchdog for the database (section
"Budgets and meters").

### Budgets and meters (hosted, free plans)

What the free plans allow, what this app was measured or computed to use, and where to watch
it. "Measured" figures come from replayed real data on this sandbox
(`scripts/measure_db_egress.py`, docs/map-layers.md "Free database budget") and the live
runs in docs/sync.md; monthly figures are arithmetic on them; none was measured on Vercel.

| Meter | Free allowance | This app | Watch it |
|---|---|---|---|
| Daemon Active CPU (Vercel Hobby; the daemon is optional in 2.0 and refreshes only the map layers) | 5 CPU-hours a month (https://vercel.com/docs/daemons/limits, re-read 2026-10-27); when used up "Vercel stops the team's daemons until the first of the next month (UTC) or until the team upgrades to Pro" | not measured for the map-only daemon. Upgrade 4 measurements in the local stack (2026-10-23): a sync of the 9-hour map layers 38.6 s and 30.9 CPU s; an idle daemon used 1.35-2.1 CPU s an hour (docs/sync.md) | `/status` CPU meter and `sync_cpu_budget` at 80 % of `SYNC_CPU_BUDGET_HOURS` (5); the daemon's usage charts on Vercel |
| Function Active CPU (Vercel Hobby) | 4 CPU-hours a month | the spot refreshes: 8.5-11.3 CPU s each (measured on this machine, docs/sync.md "Measured (2.0)"), about 0.6-0.8 CPU-hours a month at 8 a day (arithmetic); a check with nothing new under 0.02 CPU s | Vercel Usage |
| Neon egress (bytes the database sends) | 5 GB a month | Swell 2.0: 6.2-9.5 MB per spot refresh (measured on demo data through the refresh endpoint, docs/sync.md "Measured (2.0)"); at 8 component refreshes a day about 1.5-2.3 GB a month (30-46 %), up to 3.4 GB with a third refresh per GFS cycle (arithmetic); a check with nothing new reads nothing. A quiet site added about 0.40 GB a month in the Upgrade 4 measurement (not re-measured) | Neon console (usage); `scripts/measure_refresh_egress.py` and `scripts/measure_db_egress.py` to re-measure a change |
| Neon compute | 100 CU-hours a month; scale to zero after 5 idle minutes | Swell 2.0 without the daemon: about 5-8 CU-hours a month for 8-12 wakes a day (the refreshes and the daily cron; assumptions: 0.25 CU, active until 5 min after a wake's last statement; arithmetic). The optional daemon's hourly jobs add about 15 (Upgrade 4). Signed-in visitors are not in that figure: every signed-in page view reads the database (measured below). Anything that reaches the database at least every 5 minutes keeps it awake all month: 720 h × 0.25 CU = 180 CU-hours | Neon console |
| Neon storage | 1 GB | the map layers of the two retained runs are about 142 MB with partitions (14 %); the free-database retention keeps about 1 day of forecasts and 7 days of archive | `database_size` at `DATA_HEALTH_DATABASE_MAX_GB` (0.85) |
| Upstash commands | 500K a month | Swell 2.0 without the daemon: refresh checks about 60,500 a month (8,640 checks × 7 commands, the hourly data-health gate included); version polling up to about 172,800 a month per CDN region with a tab open all month (one origin GET per `FORECAST_VERSION_CDN_SECONDS`, 15), about 21,600 for 3 hours of open tabs a day; public pages about 32,000 (quiet site) to 324,000 (steady visitors, with the miss limit; Upgrade 4 estimate). About 114,000 (23 %) for a quiet site; about 557,000 with steady visitors and one region polled all month, over Free (docs/API_SETUP_REQUIRED.md 1.4: pay-as-you-go, or `FORECAST_VERSION_CDN_SECONDS=30`). The optional daemon adds about 76,000 (kick polls 43,200, checks about 13,000, the hourly batch 19,440). All arithmetic on measured unit costs | Vercel Storage, Upstash console; `scripts/vercel-local.sh status` counts the local stack's commands |
| Upstash data | 250 MB | about 265 kB of cached public payloads an hour, kept 2 hours; no provider response cache (`PROVIDER_RESPONSE_CACHE_ENABLED=false`) | `redis_memory` |
| Open-Meteo calls | 10,000 a day per IP address | 125 weighted calls per refresh of both components, 75 per wave-only refresh (measured live 2026-10-28/29); about 500 a day at 4 GFS cycles, about 500-650 with the horizon refetch of the other component on up to two refreshes a day (arithmetic; docs/sync.md, "One refresh"); checks read only the free `meta.json` | `python -m app.cli engine budget`; `/admin/freshness` usage |
| QStash messages (the refresh scheduler on Hobby) | 1,000 a day | 288 a day for a 5-minute schedule, plus retries of failed deliveries | Upstash console |

Signed-in visitors, measured at the final review (a clone of the live run, one process):
every signed-in page view runs `/api/auth/me` (5 SELECTs, 3.2 kB); a dashboard view about
27 statements (23-53 kB), a spot page 13. Every such view wakes Neon, so signed-in traffic
costs compute in proportion to the hours it spans, not covered by the 37 CU-hours above. Open
tabs no longer poll the database: the dashboard reads the opportunities on mount and on
focus, and `/status` (`/api/health/data`, 37 statements, shared by the CDN for 60 s) on
focus and while a sync runs. Visitor-chosen URLs the CDN has not cached still reach the
database once per key and hour: every map time is answered from its UTC hour within the
cached window, and a client IP gets at most `PUBLIC_CACHE_MISS_LIMIT_PER_HOUR` (120) misses
an hour, then 429, so one client cannot use up Neon Free's 5 GB of egress (Neon then
suspends the compute for the rest of the month). Many clients together still can.

### Logs and job history

* On Vercel, runtime logs are kept 1 hour on Hobby (1 day on Pro): the lasting record is
  the `sync_runs` table (one row per sync with its stages, durations and `cpu_s`), the job
  logs and `/status`. `python -m app.cli sync status` lists the newest syncs.
* `docker compose logs -f worker beat` (Celery), `docker compose logs backend` (API).
  Logs are redacted (keys, tokens and passwords are masked; addresses are not logged).
* `background_job_logs`: one row per job run with status, duration and a result summary
  (kept `RETENTION_JOB_LOG_DAYS`, 30). Latest per job:
  `docker compose exec postgres psql -U swell -c "SELECT DISTINCT ON (job_name) job_name, status, started_at, duration_ms FROM background_job_logs ORDER BY job_name, started_at DESC"`.
* Run a job now through the worker:
  `docker compose exec worker celery -A app.workers.celery_app call forecasts.acquire`
  (or `forecasts.acquire_ensemble`, `quality.predict`, `maintenance.cleanup`,
  `ops.data_health`, ...). The job lock makes a duplicate harmless.
* The beat schedule with next run times: `docker compose logs beat | grep "Sending due"`,
  or the schedule entries listed in docs/ARCHITECTURE.md §2.

## 4. Back up and restore

### Backups

`scripts/backup-db.sh` writes `PREFIX-DB-YYYYMMDDTHHMMSSZ.dump` (pg_dump custom format)
plus a `.sha256`, readable by its owner only (backups contain every account). The dump is
written to a `.partial` file and only kept once `pg_restore --list` can read it back.
Retention then keeps the newest `BACKUP_KEEP` (7) and deletes those older than
`BACKUP_MAX_AGE_DAYS` (30), never the backup just made.

```bash
scripts/backup-db.sh --compose                       # the compose database (no local pg_dump needed)
DATABASE_URL=postgresql://user:pass@db:5432/swell BACKUP_DIR=/srv/swell-backups scripts/backup-db.sh
docker compose --profile backup up -d backup         # scheduled: on start, then every BACKUP_INTERVAL_HOURS (24)
```

The `backup` service writes to the `db-backups` volume, or to a host directory with
`BACKUP_HOST_DIR=/srv/swell-backups`. Before each backup it waits up to 10 min for
PostgreSQL, and it retries a failed backup after `BACKUP_RETRY_MINUTES` (30) rather than a
whole interval later: after a reboot or a Docker restart, restart policies start every
container at once and ignore `depends_on`, which made the first backup after both
restarts of the measured stack fail with "Connection refused" before this wait existed.
Its health check turns *unhealthy* once the newest backup is older than the interval plus
an hour. Alternatively, cron on the host:

```cron
30 2 * * * cd /srv/swell && BACKUP_DIR=/srv/swell-backups scripts/backup-db.sh --compose >> /var/log/swell-backup.log 2>&1
```

Copy the directory off the server (rclone, restic, `aws s3 sync`): a backup on the same
disk does not survive the disk. Measured: the 185 MB database (the 2026-10-11 runs plus
the 2026-10-12 00Z runs) dumped to 95.6 MB in 10 s from the host and in 26 s from the
`backup` container while the stack was starting; the map-field bytes are already
compressed, everything else compressed about 5:1.

### Restore

```bash
docker compose stop backend worker beat frontend backup   # nothing may write during a restore
scripts/restore-db.sh --compose backups/swell-swell-20261012T105652Z.dump
docker compose up -d
```

The script checks the `.sha256`, reads the archive, shows the target and asks you to type
its name (`--yes` skips this; without a terminal it refuses). It drops and re-creates every
object in the archive in one transaction: on any error nothing changes. `--create
--target-db NAME` restores into a new database instead (for inspection or a drill), and
`DATABASE_URL=...` targets any server.

**A backup from before a migration** (e.g. rolling back a release that added tables): an
in-place restore only replaces what the archive holds, so the tables a later migration
added would stay behind, block the drop of the tables they reference and make the next
`alembic upgrade` fail. The script therefore compares the Alembic revision of the backup
with the target's and refuses, changing nothing, when they differ. Restore into a new
database and swap it in; the backend then migrates it to the current schema on start:

```bash
docker compose stop backend worker beat frontend backup
scripts/restore-db.sh --compose --create --target-db swell_restored backups/swell-swell-20261012T105652Z.dump
docker compose exec postgres psql -U swell -d postgres \
  -c 'ALTER DATABASE swell RENAME TO swell_before_restore' \
  -c 'ALTER DATABASE swell_restored RENAME TO swell'
docker compose up -d     # RUN_MIGRATIONS=true: alembic upgrade head on the restored database
# once everything checks out: docker compose exec postgres dropdb -U swell swell_before_restore
```

(Use your `POSTGRES_USER` / `POSTGRES_DB` if they are not `swell`.) Checked on
2026-10-13: a backup at revision `0003_forecast_engine` restored in place over a database at
`0005_research_retrieval` failed before this check existed ("cannot drop constraint
pk_surf_spots ... other objects depend on it", rolled back), and is now refused up front;
restored with `--create`, renamed in and migrated, it reached `0005_research_retrieval` and
`alembic check` found no drift. The renames were run in the Compose PostgreSQL on scratch
databases, not on the live one.

### Restore drill

`scripts/restore-drill.sh` backs up a database, restores the backup into a new scratch
database on the same server, compares exact row counts of every table and drops the
scratch database (`--keep` keeps it). Run it monthly, and after upgrades.

Drills run on 2026-10-12 and 2026-10-13 against a copy of the real 2026-10-11 runs (a
database created from `swell_live2`, with identical row counts), four of them logged:
backup 47.9 MB in 7-11 s (one drill restored an existing archive instead), restore in
3-4 s, each time **42 tables and 34,800 rows identical** (e.g. `wave_forecasts` 11,350,
`surf_quality_predictions` 11,350, `prediction_archive` 3,950, `map_fields` 486,
`spot_research_claims` 805), restored database 96 MB. A schema dump of the source and the
restored database differed only in how PostgreSQL prints 44 CHECK constraints (identical
expressions), and `alembic check` on a restored database found no drift ("No new upgrade
operations detected"). The same archive restored into the Compose database with
`restore-db.sh --compose` in 7 s, and into a new database of the Compose server
(`--compose --create --target-db`) in 8 s.

On a database that is written to while the drill runs, tables written meanwhile (job
logs) can differ by those rows; drill a quiet copy, or stop the worker and beat, for an
exact comparison.

## 5. Incident playbook

Start from `/status` or `scripts/check-health.sh`: every reason names what is late and
by how much. The first six runbooks are for the hosted setup (Upgrade 4); the rest apply
everywhere. Commands marked "(Vercel)" run only on Vercel's website or against the
deployment.

### Data is stale

**What you see.** `forecast_stale:<source>`, `map_fields_stale` or "Forecast out of date" on
the site; `/status` shows when the runner last checked and the state of each component.

**Read the components first.** `python -m app.cli sync check` (from `backend/`, against the
same database and Redis) prints the plan: `current` (stored), `new` (a sync is due),
`waiting` (coalescing, a provider still publishing, or a retry not due yet), `in_progress`,
`failed` (out of attempts for that run), `unknown` (metadata unreachable), `disabled`.

1. No recent check at all. On Vercel with the hosted profile the spot forecasts are
   refreshed by the refresh endpoint, so look for `refresh_scheduler_silent` and the "Refresh
   scheduler" row of `/admin/freshness`: the schedule that calls `/api/internal/refresh`
   (QStash on Hobby, a Vercel Cron entry on Pro, docs/deploy-vercel.md step 7b) is paused,
   deleted, out of messages or sends an old `CRON_SECRET`; the daily cron still refreshes
   once a day meanwhile. There `sync_heartbeat_stale` (degraded) concerns only the optional
   map daemon: the map layers age, the spot data does not stop ("Daemon quota reached").
   Under Celery, or a daemon whose `SYNC_DAEMON_KINDS` include `spots`,
   `sync_heartbeat_stale` (failing) means that runner stopped: restart it (or worker and
   beat).
2. `waiting` with "not served yet" for `open_meteo_ecmwf`: normal between ECMWF runs
   (Open-Meteo serves its 06Z/18Z run then; this source reads only 00Z and 12Z).
3. `failed` or repeated `sync_failed`: `python -m app.cli sync status` shows the stages and
   the error of each sync; then the provider's runbook below.
4. With the hosted and free-database profiles (`SYNC_TRIGGER_SOURCES=spots`) every new GFS
   or GFS-Wave run starts a spot refresh at the next scheduler call once Open-Meteo serves
   it (a component still being published shows `waiting`); the wave and the wind component
   are refreshed separately when they arrive at different times. The free database keeps
   only the 06Z and 18Z map cycles (`MAP_FIELD_CYCLES=06,18`), which concerns the optional
   map overlay, not the spot forecasts. (The Upgrade 4 cadence, `SYNC_TRIGGER_SOURCES=map`,
   synced only for a new map cycle and let a GFS run read stale for hours; it is not a 2.0
   profile.)
5. To refresh the spot forecasts now on Vercel, call the refresh endpoint yourself:
   `curl -X POST -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/internal/refresh`
   (its answer says `success`, `not_due` with the plan, `busy` or `failed`). For the map
   layers, use the manual GitHub workflow "Data sync (manual)"
   (docs/deploy-vercel.md section 8): it applies the hosted profile, `APP_ENV=production`
   and `DB_USE_UNPOOLED=true` itself. `force` runs every stage (runs already stored are
   still skipped); a run that reports the lease busy means the daemon is syncing right now:
   wait and run it again. A run from your own machine (from `backend/` with the dev venv of
   README E.3, which has the `sync` extra the map layers need: `python -m app.cli sync run`,
   or `sync run --force`) only with the Production environment loaded: the hosted-profile
   block of docs/deploy-vercel.md step 5, your Production variables (`APP_ENV=production`,
   `SECRET_KEY`, a real `EMAIL_PROVIDER`, `APP_BASE_URL`) and `DB_USE_UNPOOLED=true`. Never
   with only `DATABASE_URL`: the defaults are synthetic demo forecasts and console email,
   and the sync would publish them to the live site. First run `python -m app.cli sync
   check` and confirm the components name `open_meteo` / `open_meteo_ecmwf`, not `demo`.
   Exit 3 means the daemon holds the lease: wait and try again.

### Daemon quota reached (Vercel Hobby)

**What you see.** `sync_cpu_budget` (degraded) first, at 80 % of `SYNC_CPU_BUDGET_HOURS`
(5); then, with the hosted profile (the daemon optional, `SYNC_DAEMON_KINDS=map`),
`sync_heartbeat_stale` as **degraded**: "When a Hobby team reaches the quota, Vercel stops
the team's daemons until the first of the next month (UTC) or until the team upgrades to
Pro." Only the map layers stop updating (the overlay is labelled out of date); the spot
forecasts keep refreshing through `/api/internal/refresh`, and the daily cron runs the
periodic jobs. (A deployment whose daemon also syncs the spots, `SYNC_DAEMON_KINDS`
with `spots`, reports it as failing: there the data stops.) The daily watchdog emails the
admins.

**What to do.** (Vercel) Check the Daemons page and the daemon's usage. Until the 1st: run
syncs by hand when the data must not wait, with the manual GitHub workflow "Data sync
(manual)" (docs/deploy-vercel.md section 8: it applies the hosted profile,
`APP_ENV=production` and `DB_USE_UNPOOLED=true`). From your own machine (`python -m app.cli
sync run`) only with the Production environment loaded: the hosted-profile block of
docs/deploy-vercel.md step 5, your Production variables (`APP_ENV=production`, `SECRET_KEY`,
a real `EMAIL_PROVIDER`, `APP_BASE_URL`) and `DB_USE_UNPOOLED=true`. Never with only
`DATABASE_URL`: the defaults are synthetic demo forecasts and console email, and the sync
would publish them to the live site. First run `python -m app.cli sync check` and confirm
the components name `open_meteo` / `open_meteo_ecmwf`, not `demo`. Then reduce the cost for
the rest of the month: `GEFS_ENABLED=false` if you turned it on, `MAP_FIELD_PARTITIONS=off`,
or a shorter map horizon (`MAP_FIELD_STEPS`, `MAP_FIELD_MAX_LEAD_HOURS`); the map layers are
more than half of the estimate. A permanent fix is Vercel Pro. Environment changes reach new
deployments only: redeploy.

### Neon suspended (free quota used up)

**What you see.** Every page fails to load data; `/api/health/ready` answers 503 with
`"database": "unavailable"`; the daily watchdog answers 503 and emails `OPS_ALERT_EMAILS`
(`new:database_unreachable`), the only alert that can go out, since the admin accounts are
in the database. On Neon Free the compute is suspended until the next billing period once
its 100 CU-hours or 5 GB of egress are used up; writes fail above 1 GB of storage.

**What to do.** (Vercel, Neon console) Read the usage: compute hours, egress or storage.
Then either wait for the next billing period or move to Neon Launch (usage-based) and switch
to the full-retention profile (docs/API_SETUP_REQUIRED.md section 5). To stay on Free: keep
the free-database profile (`SYNC_TRIGGER_SOURCES=spots`, `MAP_FIELD_CYCLES=06,18`, the 1-day
retention) and the hosted profile's hourly data-health check (`DATA_HEALTH_CHECK_MINUTES=60`:
each check wakes the compute once), point an uptime monitor at `/api/health` or `/` only, never at
`/api/health/ready` or `/api/health/data?strict=true` (every hit wakes the compute: every 15
minutes costs about 45-60 CU-hours a month), and expect steady traffic to exceed Free (5.37
GB of egress a month at 1,000 views a day, computed in docs/map-layers.md). Storage: lower
`RETENTION_ARCHIVE_DAYS` or `RETENTION_FORECAST_DAYS`; the daily `maintenance.cleanup`
(03:30 UTC) applies them, and `SYNC_PRUNE_EACH_PASS=true` prunes superseded predictions and
map fields after every sync.

### Open-Meteo daily limit ("Daily API request limit exceeded")

The free tier counts calls per IP address, and on Vercel the daemon's outbound address may
be shared with other projects. The source then waits until 00:00 UTC (the sync plan says
"next attempt after ... 00:00 UTC"); the other sources sync normally and the previous run
stays in use until it is stale. The lasting fix is `OPEN_METEO_API_KEY` (a paid plan,
counted per key); details in the runbook "Open-Meteo refuses requests" below.

### A deploy killed a sync

A production deploy replaces the daemon: Vercel sends SIGTERM to the old one and stops it
after the grace period (600 s here, `terminationGracePeriodSeconds`). The daemon finishes the
stage it is in and exits, so a normal deploy loses nothing. If a stage outlives the grace
period (a full map cycle is estimated at 165-287 CPU s on one vCPU), the old sync's lease
stays `running`: the new daemon waits until it lapses (`SYNC_LEASE_MINUTES`, 15 under the
daemon), marks it `abandoned` and does the work, and a map or spot run the killed stage left
`running` counts as in progress until it is an hour old. Nothing to do but wait about
15-60 minutes, or, after the lease has lapsed, run the manual GitHub workflow with `force`
("Data is stale" step 5; from your machine only with the Production environment loaded).
`/status` and `python -m app.cli sync status` show the abandoned sync. Avoid deploying while
`/status` shows a sync in progress.

### Rotate SECRET_KEY or CRON_SECRET

* `SECRET_KEY`: generate a new value with
  `python3.13 -c "import secrets; print(secrets.token_urlsafe(48))"` or
  `openssl rand -base64 48`, set it in Vercel's Production environment (and in the GitHub
  `SECRET_KEY` secret if you use the manual workflow), redeploy. It keys the rate-limit
  counters (the HMAC of each client's counter), so those start from zero; sessions and email
  links are random tokens stored hashed in the database and are not affected. Production
  refuses the development default.
* `CRON_SECRET`: set a new value of 16 or more characters, redeploy (Vercel Cron sends the
  deployment's value as the Bearer token). Check it with the `curl` of section 3: 200 with
  the new token, 401 with the old one.
* After a suspected leak, also rotate the provider keys (`RESEND_API_KEY`,
  `OPEN_METEO_API_KEY`, ...) at each provider, and the database and Redis credentials in the
  Neon and Upstash consoles (the Marketplace updates the project's variables; redeploy).

### A forecast provider is down or late (NOAA, ECMWF, GEFS)

**What you see.** `forecast_stale:<source>` (degraded) once the newest run is past the
source's limit (NOAA 16 h, ECMWF 25 h, GEFS 18.5 h), often with "the latest attempt
failed". Worker logs show the provider's errors (`noaa_gfs request failed`,
`ecmwf_open_data returned 503`).

**What the system already does.** Downloads retry with backoff (ECMWF throttles bursts
with 503 SlowDown; 173 of them during the measured run, all absorbed). Fresh runs always
win over stale ones; the blend is only built from fresh members, so with one family left
the remaining fresh source is used directly, and forecast confidence states the
staleness. Alerts to users pause for spots whose data is older than twice the limit. If
every source is stale the report says `failing` (`forecasts_unavailable`).

**What to do.**

1. Is it upstream? Look at one file the ingester reads, e.g. for NOAA
   `curl -sI https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.$(date -u +%Y%m%d)/00/wave/gridded/gfswave.t00z.global.0p25.f000.grib2`
   (200 = published; 403/404 for every recent cycle = not published yet, or moved), and
   the provider's own service notices (NCEP Central Operations, ECMWF open data).
2. A 404 on every file of every cycle after a NOAA upgrade means the file layout
   changed (GFS v17): set `NOAA_GFS_WAVE_PATH_TEMPLATE` / `NOAA_GFS_ATMOS_PATH_TEMPLATE`
   (README "Debugging").
3. Persistent ECMWF 503s: lower `GRIB_DOWNLOAD_WORKERS` (default 6).
4. Our side: DNS, TLS or proxy errors in the worker log mean the server's outbound HTTPS
   is broken, not the provider.
5. Once fixed, sync now: `python -m app.cli sync run` (from `backend/`, or inside the
   backend container; on Vercel the manual GitHub workflow, "Data is stale" step 5); the
   recovery email follows at the next data-health check.
6. A provider gone for days: leave it configured (the system works on the other family
   and tells you every 12 h). Removing it from `FORECAST_PROVIDERS` silences the alert
   but also turns the blend off.

### Open-Meteo refuses requests (quota or rate limit)

**What you see.** A FAILED `open_meteo_noaa` or `open_meteo_ecmwf` run whose error is
Open-Meteo's own reason ("Daily API request limit exceeded. Please try again tomorrow.",
"Minutely API request limit exceeded ...") or the app's budget ("Open-Meteo day budget of
10000 weighted calls (OPEN_METEO_CALLS_PER_DAY) would be exceeded ..."), and later
`forecast_stale:<source>`. `python -m app.cli engine budget` prints the weighted calls the
app has used per window.

**What the system already does.** A minute or hour limit is transient: the sync retries
`SYNC_RETRY_MINUTES` later, and with the provider cache on (classic stack) the responses
already received are read from the cache. A daily or monthly limit ends the run FAILED and
that source waits until 00:00 UTC (the 1st of the next month for a monthly limit); the
previous run stays in use until it goes stale. The two Open-Meteo families share one
budget.

**What to do.** The free tier counts calls per IP address: on shared hosting other users
of the address use the same allowance (this happened in the sandbox on 2026-10-14, after
the app itself had used 143 weighted calls; the next acquisition re-read the waves it had
already received from the cache and paid only for the refused wind request). Use a paid
plan (`OPEN_METEO_API_KEY`, then raise `OPEN_METEO_CALLS_PER_DAY` /
`OPEN_METEO_CALLS_PER_MONTH`), or wait for the reset. Never lower
`OPEN_METEO_AVAILABILITY_WAIT_MINUTES` to read runs sooner.

### Redis memory high

`redis_memory` (degraded) means Redis uses more than 80 % of its cap. With `noeviction` a
full Redis refuses writes, so new jobs fail to queue. Look at the queue lengths
(`docker compose exec redis redis-cli llen celery`, `... llen bulk`: a stopped worker lets
them grow until the beat messages expire), then at the provider cache
(`python -m app.cli engine budget` shows its entry count; it is bounded by
`PROVIDER_RESPONSE_CACHE_MAX_KEYS` and `PROVIDER_RESPONSE_CACHE_MAX_BYTES`). Raise
`REDIS_MAXMEMORY` (and keep the setting and the compose value equal) only after that.

### Worker or beat down, jobs overdue or stuck

* `jobs_not_running` (failing) or many `job_overdue:*`: `docker compose ps`; restart the
  unhealthy service (`docker compose restart worker beat`). Exactly one beat.
* `job_stuck:<job>`: the worker process was killed mid-job (the hard limit is 30 min;
  out of memory is the usual cause: see `docker compose logs worker` and `dmesg`). The
  lock expires by itself (at most 1 h); give the worker more memory or lower
  `WORKER_CONCURRENCY`.
* `job_failed:<job>`: the email carries the redacted error; the job log row has it too.

### Database or disk full

`database_size` (above `DATA_HEALTH_DATABASE_MAX_GB`, default 20 GB), failed jobs with "No space
left on device", or PostgreSQL refusing writes. Free space first (old local backups,
Docker build cache: `docker builder prune`), then lower the retention settings (§6) and
run `maintenance.cleanup`. PostgreSQL reuses the space of deleted rows; it only returns
it to the operating system after `VACUUM FULL` (locks the table; do it in a quiet hour).

### Email provider failing

User alerts retry with backoff and record the provider's reason. Admin data-health
emails retry every 15 min until accepted; meanwhile only the external monitor can tell
you, which is one more reason to have it.

### Restore after data loss

§4 "Restore", then run `python -m app.cli sync run --force`: forecasts are re-fetched from the providers;
accounts, searches, alerts and the prediction archive come only from the backup.

## 6. Capacity

### API-first setup (`FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf`, 2026-10-14)

Measured live on a fresh scratch database (50 spots, GFS and ECMWF 12Z runs of
2026-10-14, every response fetched, nothing from the cache; `forecast_runs.details.stats`):

| Item | Per run | Per day (default schedule) | Basis |
|---|---|---|---|
| Open-Meteo GFS family, 50 spots, 16 days | 142.9 weighted calls (waves 85.7: 50 locations x 15 variables / 10 x 16 / 14 days; wind 57.1), 3 data requests, 11 HTTP responses with the metadata, 345.6 kB, 6.3 s; 10,200 rows | 4 runs: 571 weighted calls, 1.4 MB | measured |
| Open-Meteo ECMWF family, 50 spots, 15 days | 124.3 weighted calls (waves 53.6, wind 53.6, 17.1 for the neighbouring cells of the 2 spots whose `ecmwf_wam025` cell has no data), 7 HTTP responses, 175.1 kB, 4.7 s; 4,100 rows | 2 runs (00Z, 12Z): 249 weighted calls, 0.35 MB | measured |
| Blend and predictions | blend 9.9 s (10,200 rows + 10,200 comparisons); `quality.predict` 19.7 s for the 3 runs (24,500 predictions, 4,050 archive rows) | | measured |
| Both families | 267 weighted calls | about 820 weighted calls a day, about 24,600 a month: 8 % of the free tier's 10,000 a day and 300,000 a month | per run measured; per day computed |
| GEFS-Wave, HTSGW and PERPW only (41 steps) | 69.4 MB, 165 requests, 11.8 s (2026-10-16 00Z) | 4 cycles: about 0.28 GB | measured 2026-10-16 (65 MB computed from record sizes before) |
| Map fields, standalone (61 map hours, `pgrb2.0p50` atmosphere) | 7.85-8.10 MB per map hour with the partition layers (about 484 MB per cycle), 2.69-2.72 MB without (about 165 MB) | 2 cycles (00Z, 12Z): about 0.97 GB with the partitions, 0.33 GB without | per hour measured (2026-10-14 12Z, map-layers.md); per cycle and day extrapolated |

The whole acquisition took 47.6 s of wall time; the two providers' own runs took 6.3 s and
4.7 s, the rest went to the metadata reads before each run (`current_run`,
`ensure_servable`). One cycle of both families added 89 MB to the database (12.2 MB after
`init-db`, 101.3 MB after): `surf_quality_predictions` 63.6 MB (24,500 rows, 2.29 kB
each), `wave_forecasts` 14.6 MB (24,500 rows: GFS family 407 B, ECMWF family 597 B, blend
535 B per row on average), `forecast_comparisons` 7.7 MB (10,200 rows), `prediction_archive`
3.0 MB (4,050 rows, 555 B each with the confidence digest). The nightly cleanup removes the
predictions of superseded member runs, so only the newest runs keep theirs. Redis grew from
1.98 MB to 2.53 MB (6 cached responses and the budget counters).

Acquisitions run every `FORECAST_REFRESH_MINUTES` (180), but a run already stored costs
only the free metadata requests, and a retry reads the responses it already received from
the cache (seen live: a run whose ECMWF wind request was refused re-read its waves from the
cache and paid only for the wind). Nothing is written to disk: a traced acquisition
(`strace -f -e openat,creat,rename,unlink,mkdir`) opened no file for writing other than
the test driver's own report and a joblib semaphore in `/dev/shm` (shared memory). Before
the change, the direct GRIB setup downloaded about 10 GB a day (next section).

### Measured run, direct GRIB setup (Docker, 2026-10-12, NOAA 00Z / ECMWF 00Z / GEFS 00Z, 50 spots)

| Step | Duration | Downloaded | Rows written |
|---|---|---|---|
| NOAA GFS-Wave + GFS (81 steps to 240 h, 811 requests, incl. map fields) | 2.5 min | 1,908 MB | 4,050 forecast rows, 486 map-field rows |
| ECMWF IFS (65 steps, 438 requests, 173 of 611 HTTP attempts answered 503 SlowDown and retried) | about 6 min (its run recorded 8.5 min because both runs were stamped with the job's start time, audit PLAT-1; fixed since) | ≈ 0.9 GB (see below) | 3,250 |
| `forecasts.acquire` (both of the above, one after the other) | 8.5 min (511 s) | 2.82 GB received by the worker | |
| Blend | 3.2 s | | 4,050 forecast + 4,050 comparison rows |
| `quality.predict` (3 runs) | 13 s | | 11,350 predictions, 4,050 archive rows |
| Detection, matching, flights, alerts, delivery | 2 s | | |
| GEFS-Wave (41 steps, 409 requests) | 41 s | 201.5 MB | 2,050 ensemble rows |
| `maintenance.cleanup` / `ops.data_health` | 88 ms / 111 ms | | |

Durations and row counts come from `background_job_logs` and `forecast_runs`. The NOAA and
GEFS runs report their download volume (1,908.4 MB and 201.5 MB); the ECMWF run does not,
so its volume is the worker's received bytes over the acquisition (2.82 GB, docker stats)
minus NOAA's: about 0.9 GB including HTTP overhead and the retries (847 MB for the
2026-10-11 run, docs/UPGRADE_REPORT.md). The first attempt in the containers failed for
both providers with `CERTIFICATE_VERIFY_FAILED` until the sandbox's TLS-proxy CA was
mounted (a sandbox-only override; a normal server needs nothing).

A second acquisition, of the 12Z cycles at 20:37 UTC with every lead time limited to 24 h
(`NOAA_MAX_LEAD_HOURS`, `ECMWF_MAX_LEAD_HOURS`, `MAP_FIELD_MAX_LEAD_HOURS`: a sandbox-only
setting to stay within its download budget, not a recommendation), gives the cost per
step: NOAA 9 steps, 91 requests, 213.4 MB in 38 s (23.7 MB a step, as in the full run:
1,908 MB / 81); ECMWF 9 steps, 64 requests, about 133 MB (worker bytes) in 5.5 min, with
80 attempts answered 503 SlowDown and retried; `forecasts.acquire` 330 s in all, the
blend 2 s, `quality.predict` 3 s. The GEFS-Wave 12Z run before it (full length, started
by beat) took 34 s for 201.4 MB.

Memory (docker stats, sampled every minute): worker up to about 1.0 GiB while decoding
GRIB (2 slots), API 400 MiB (2 uvicorn workers), PostgreSQL 200 MiB, beat 77 MiB,
frontend 38 MiB, backup 48 MiB, Mailpit 19 MiB, Redis 7 MiB: about 1.8 GiB in all. The
worker used 3-3.6 of 4 vCPUs while decoding.

### Size per row and per day

Bytes per row are the measured growth of each table (heap, TOAST and indexes) over one
full run. Rows per day assume the default schedule: 4 NOAA cycles, 2 ECMWF cycles,
4 GEFS cycles and 6 blends (one per new member run) a day.

| Table | Bytes per row | Rows per day | Per day | Kept | Steady state |
|---|---|---|---|---|---|
| `surf_quality_predictions` | 2,529 | 47,000 | 119 MB | blend runs: `RETENTION_FORECAST_DAYS` (10); member runs: until superseded (pruned nightly, the newest run per source kept) | ~1.2 GB before the pruning; the member share (about half the rows) now stays at one run per source |
| `wave_forecasts` | 631 | 47,000 | 30 MB | 10 days | ~0.3 GB |
| `forecast_comparisons` | 771 | 24,300 | 19 MB | 10 days | ~0.19 GB |
| `forecast_ensemble_stats` | 156-352 | 8,200 | 1.3-2.9 MB | 10 days | ≤ 29 MB |
| `map_fields` | 84,800 (41 MB per run) | 4 runs | 165 MB written | the 2 best runs | ~82 MB |
| `prediction_archive` | 1,706 before; 746 with the confidence digest (`ARCHIVE_CONFIDENCE_DIGEST`; heap, TOAST and indexes, 555 B of it the row itself, measured on the 4,050 rows of the 2026-10-14 live run) | 24,300 | 41.5 MB before; about 18 MB with the digest (computed) | `RETENTION_ARCHIVE_DAYS` (730) | 30 GB after 2 years before; about 13 GB with the digest (computed) |
| `background_job_logs` | ~330 (+ index) | ~1,150 | ~0.5 MB | `RETENTION_JOB_LOG_DAYS` (30) | ~15 MB |

The second session's 24-hour run (1,350 forecast rows and predictions, 450 comparison and
archive rows) grew the same tables by 2,555 (predictions), 655 (`wave_forecasts`), 728
(`forecast_comparisons`) and 1,839 (`prediction_archive`) bytes per row: within 8 % of the
figures above (small deltas are rounded to whole 8 kB pages).

So with the direct GRIB setup and the full archive rows the database settled at about
**1.8 GB plus the archive**, which grew by about **42 MB a day** (15 GB a year) until it
reached its 730-day retention (about 30 GB). With the confidence digest (default) the
archive grows by about 18 MB a day (about 13 GB at 730 days, computed), and pruning the
superseded member runs' predictions removes about half of the 1.2 GB of predictions
(computed from the run mix above). Forecast runs and their rows are deleted by
`maintenance.cleanup` (daily 03:30 UTC), always keeping the two newest usable runs per
source; map fields are pruned to two runs after every acquisition. Buoy observations,
point forecasts and verification pairs (when enabled) follow `RETENTION_ARCHIVE_DAYS`
(quality-controlled buoy observations `OBSERVATION_RETENTION_DAYS`); they were not
enabled in this run.

**API-first setup, steady state (computed** from the per-run sizes measured on
2026-10-14, heap, TOAST and indexes per row: predictions 2,596 B, `wave_forecasts`
510 B GFS family / 700 B ECMWF family / 638 B blend, comparisons 759 B, archive 746 B).
Four GFS-family runs, two ECMWF-family runs and six blends a day write about 66 MB of
forecasts, 47 MB of comparisons and 159 MB of blend predictions, about 0.27 GB a day, so
the 10 days of `RETENTION_FORECAST_DAYS` hold about 2.7 GB; the member runs' predictions
stay at one run per source (about 37 MB) after the nightly pruning. A GFS-family cycle can
be stored twice when Open-Meteo publishes `ncep_gfs025` well after `ncep_gfs013` (2.2 h
apart for the 06Z run of 2026-10-15) and an acquisition falls in between; each such run
adds about 0.05 GB of rows. The archive adds about 18 MB a day.

Re-measured on 2026-10-16 with algorithm 2.1.0 (docs/FORECAST_SOURCES.md, "Measured on
2026-10-16"): a prediction row is 3,403 B (wind detail, contributions, direction partitions
and tide detail; +31 %), `wave_forecasts` 517 B GFS family / 763 B ECMWF family / 614 B
blend, comparisons 759 B, archive 760 B, and the GFS family stores 10,400 steps per run.
Six blends a day then write about 0.30 GB a day, the 10 days hold about 3.3 GB, and the
archive adds about 18.5 MB a day. With the defaults the database therefore reaches about
17 GB at the 730-day archive retention (computed; fresh tables, so a lower bound), below the
default `DATA_HEALTH_DATABASE_MAX_GB` of 20 (GiB): lower the limit to what the disk allows,
or shorten `RETENTION_ARCHIVE_DAYS` (the alert only reports; nothing is deleted).

**Bandwidth:** the direct GRIB setup downloaded about **10 GB per day** (NOAA 4 × 1.91 GB,
ECMWF 2 × 0.85-0.9 GB, GEFS 4 × 0.20 GB), about 300 GB a month. The API-first setup
needs about 1.7 MB a day for the spot forecasts (4 GFS-family runs of 345.6 kB and 2
ECMWF-family runs of 175.1 kB, measured) plus about 0.28 GB for GEFS-Wave and 0.33-0.97 GB
for the map layers (without / with the partition layers; per map hour measured, per day
extrapolated, see "API-first setup" above). Uploads are
negligible (requests only).

**Backups:** the measured dump was 52 % of the database size, dominated by the
incompressible map fields (about 74 MB); the other tables compressed about 5:1. From
this one measurement, expect roughly 0.2 × (database − map fields) + 75 MB per dump:
with the API-first setup and the archive digest about 2 GB per dump once the archive
holds a year and 3.3 GB at two years (computed; 3.4 and 6.5 GB with the full archive
rows), times the number of local copies (`BACKUP_KEEP`, default 7). Keep the history off
the server.

### A single small server

| | Minimum | Comfortable |
|---|---|---|
| CPU | 2 vCPUs (acquisitions take longer) | 4 vCPUs |
| Memory | 4 GB (about 1.8 GiB measured peak plus the OS) | 8 GB |
| Disk | 30 GB: `RETENTION_ARCHIVE_DAYS=365` (database ≤ ~10 GB) and `BACKUP_KEEP=3` (~6 GB) | 60 GB with the defaults (database ~17 GB, 7 local dumps ~23 GB at two years) |
| Network | about 0.6-1.25 GB/day inbound with the API-first setup (GEFS-Wave and the map layers; about 1.25 GB measured and extrapolated on 2026-10-16; 10 GB/day with the direct GRIB providers) | |

Disk and database sizes are computed for the API-first setup with the archive digest
(above); the direct GRIB setup with full archive rows needed about twice as much. Plus
about 2.2 GB for the images. Recommended settings for the 30 GB case, in `.env`:

```env
RETENTION_ARCHIVE_DAYS=365        # archive ~6.6 GB instead of ~13 GB; it is the record of
                                  # past forecasts used for verification, so keep what you can
BACKUP_KEEP=3                     # local copies; ship every backup off the server
DATA_HEALTH_DATABASE_MAX_GB=12    # degraded alert well before the disk fills
```

Each day of `RETENTION_FORECAST_DAYS` holds about 0.33 GB with the API-first setup and
algorithm 2.1.0 (0.27 GB with 2.0.0; about 0.17 GB with the direct GRIB setup): lowering it
from 10 to 5 days saves about 1.6 GB, and the two newest runs per source are always kept.
`GEFS_ENABLED=false` saves about 0.28 GB/day of bandwidth (0.8 GB/day before
`GEFS_STAT_FIELDS` trimmed it) at the cost of ensemble-based uncertainty. `NOAA_MAX_LEAD_HOURS=168` /
`ECMWF_MAX_LEAD_HOURS=168` read about 30 % fewer NOAA steps (57 of 81) and about 18 %
fewer ECMWF steps (53 of 65), and shorten the forecast to 7 days.

## 7. Settings reference (operations)

Every setting with its default and hosted value: docs/API_SETUP_REQUIRED.md, section 5 (the
hosting, runner and sync settings of Upgrade 4) and `.env.example`.

| Setting | Default | Meaning |
|---|---|---|
| `DATA_HEALTH_CHECK_MINUTES` | 15 | interval of the `ops.data_health` job |
| `DATA_HEALTH_ALERTS_ENABLED` | true | email admins about problems and recoveries |
| `DATA_HEALTH_ALERT_REPEAT_HOURS` | 12 | at most one email per problem in this interval |
| `DATA_HEALTH_HEARTBEAT_MINUTES` | 30 | Celery: no finished job for this long = worker or beat down (failing); daemon: its heartbeat is stale after max(3 × `SYNC_CHECK_MINUTES`, this) |
| `DATA_HEALTH_OBSERVATION_STALE_HOURS` | `OBSERVATION_LOOKBACK_HOURS` (48) | buoy provider stale after this |
| `DATA_HEALTH_DATABASE_MAX_GB` | 20 (0.85 on Neon Free) | degraded above this database size (alert only) |
| `OPS_ALERT_EMAILS` | none | addresses the daily watchdog emails when the database cannot be reached (at most once per UTC day) |
| `JOB_RUNNER` | celery | `daemon` = one `sync daemon` process runs every job (Vercel) |
| `SYNC_CHECK_MINUTES` | 10 | model-run checks (metadata only) |
| `SYNC_TRIGGER_SOURCES`, `MAP_FIELD_CYCLES` | `spots,map`, `00,12` (free database: `spots`, `06,18`) | which new runs start a sync, and the map cycles read |
| `SYNC_LEASE_MINUTES` | 45 (15 under the daemon) | the one-sync-at-a-time lease |
| `SYNC_RETRY_MINUTES`, `SYNC_MAX_ATTEMPTS` | 15, 6 | retries of a failed acquisition per model run |
| `SYNC_CPU_BUDGET_HOURS` | none (5 on Vercel) | `sync_cpu_budget` above 80 % of it |
| `JOB_INTERVAL_FLOOR_MINUTES` | 0 (60 on Vercel) | every periodic job at most this often |
| `CRON_SECRET` | none (route answers 404) | Bearer token of the daily watchdog |
| `SYNC_PRUNE_EACH_PASS` | false (true on Neon Free) | prune superseded predictions and map fields after every sync |
| `DATA_HEALTH_REDIS_MAX_FRACTION` | 0.8 | degraded above this fraction of the Redis memory cap |
| `REDIS_MAXMEMORY` | 256mb | Redis `--maxmemory` in docker-compose; also what the health check measures against when Redis runs without a cap |
| `WORKER_QUEUES` | celery,bulk | queues the worker consumes |
| `OPEN_METEO_CALLS_PER_MINUTE` / `_HOUR` / `_DAY` / `_MONTH` | 600 / 5,000 / 10,000 / 300,000 | the app's Open-Meteo weighted-call budget (free tier); raise for a paid plan |
| `PROVIDER_RESPONSE_CACHE_ENABLED`, `_MAX_BYTES`, `_MAX_KEYS` | true, 2,000,000, 200 | bounded Redis cache of provider responses (one model run) |
| `ARCHIVE_CONFIDENCE_DIGEST` | true | archive rows keep a digest of the confidence inputs |
| `OBSERVATION_RETENTION_DAYS` | 3650 | quality-controlled buoy observations (others follow `RETENTION_ARCHIVE_DAYS`) |
| `RETENTION_FORECAST_DAYS` | 10 (1 on Neon Free) | forecast runs and their rows (two newest per source always kept) |
| `RETENTION_ARCHIVE_DAYS` | 730 (7 on Neon Free) | prediction archive, unchecked buoy observations, point forecasts, verification pairs |
| `RETENTION_JOB_LOG_DAYS` | 30 (7 on Neon Free) | job logs (also the alert de-duplication state) |
| `RETENTION_FLIGHT_DAYS` / `RETENTION_NOTIFICATION_DAYS` | 30 / 365 | flight searches / notifications |
| `BACKUP_DIR`, `BACKUP_KEEP`, `BACKUP_MAX_AGE_DAYS`, `BACKUP_PREFIX` | ./backups, 7, 30, swell | `scripts/backup-db.sh` |
| `BACKUP_INTERVAL_HOURS`, `BACKUP_RETRY_MINUTES`, `BACKUP_HOST_DIR` | 24, 30, volume `db-backups` | compose `backup` service |
| `BACKEND_IMAGE`, `FRONTEND_IMAGE` | locally built tags | run pre-built images |
| `TRUSTED_PROXY_COUNT` | 1 | reverse proxies in front of the API (client IPs for rate limits) |

## 8. Verification record (2026-10-12 and 2026-10-13)

Environment: one 4-vCPU, 16 GB sandbox VM, Docker Engine 29.8 with Compose 5.6, the
Compose stack of this repository (`--profile backup`) with
`FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs`, `GEFS_ENABLED=true`, `WORKER_CONCURRENCY=2`,
`BEAT_RUN_ON_START=false` and `DATA_HEALTH_HEARTBEAT_MINUTES=10`, in three sessions:
2026-10-12 10:00-16:44 UTC (with the Docker daemon stopped by the sandbox from 11:12 to
about 15:35), 20:33-21:50 UTC, and 2026-10-13 01:33-02:07 UTC. The sandbox also stopped
the daemon between the sessions; after those two stops the containers came back by their
restart policy, with beat's persisted schedule. The only sandbox-specific overrides (not committed) mounted the sandbox's
TLS-intercepting proxy CA into the app containers (`SSL_CERT_FILE`), gave the image builds
the build proxy, moved the host ports, and, from the second session on, limited every lead
time to 24 h to stay within the sandbox's download budget (about 3.8 GB were downloaded in
all). In the third session the running containers' `app/` directory was byte-identical to
`backend/app` of the final code (compared with `diff -r`).

**Verified live**

* Images: `docker compose build` of both app images (sizes and times in §2).
* Downloads from inside the Docker network, session 1, 10:40-10:51 UTC: `forecasts.acquire`
  (NOAA 00Z and ECMWF 00Z, 0 spot errors), the chained blend, predictions, archive,
  detection, matching, flights and alerts, then `forecasts.acquire_ensemble` (GEFS-Wave
  00Z), `forecasts.map_fields` (a no-op in the default mode: the NOAA run stored the 486 map
  fields), `maintenance.cleanup` and `ops.data_health`, each started by hand and run by the
  worker container; results in `forecast_runs` and `background_job_logs` (§6). The worker
  container received 3.05 GB in that window (docker stats). The first attempt failed for
  both providers on the sandbox's TLS proxy until its CA was mounted.
* Jobs started by beat itself, session 2: after the daemon restart (20:33 UTC, 3.8 h after
  it stopped) beat sent every entry that had fallen due from its persisted schedule,
  including `forecast-acquisition` and the missed 19:40 `ensemble-acquisition`. The worker
  ran the GEFS-Wave 12Z acquisition (41/41 steps, 409 requests, 201.4 MB, 34 s, 0 errors).
  It was then stopped before it reached `forecasts.acquire` and recreated with the 24 h lead
  limit, and ran that queued task: NOAA 12Z (9 steps, 213.4 MB, 38 s, 54 map fields) and
  ECMWF 12Z (9 steps, about 133 MB, 5.5 min, 80 SlowDown retries), 0 spot errors, then the
  chained blend, 1,350 predictions, 450 archive rows, detection, matching, flights, alerts
  and delivery. Detection downgraded 18 events: a 24-hour run does not cover their days 5-10
  (an effect of the sandbox's lead limit, not of the code).
* Session 3, on the final code: after a 3.7 h outage (last job 21:49:43, containers back at
  01:33 UTC) beat sent the entries that had fallen due. `forecasts.acquire` (01:35:16, 65 s)
  stored NOAA 18Z (9 steps, 91 requests, 213.1 MB, 0 spot errors) and skipped ECMWF ("run
  already ingested": the newest ECMWF run it found was still 12Z), then the blend (1.1 s), the
  predictions and the rest of the chain ran. At 01:40:00 beat fired `ensemble-acquisition`
  at its own cron time, not as a catch-up: GEFS-Wave 18Z, 5 steps, 23.9 MB in 9 s, 250
  ensemble rows. The worker container received 252 MB in this session.
* Restart after an outage: right after each restart the report named the outage. Session 1
  (4.4 h down): `failing`, "No background job has finished for 4.4 h", with eight overdue
  jobs. Session 2 (3.8 h down): the first `ops.data_health` run finished after a quick job
  had already completed, so the heartbeat was fresh: `degraded` with eight overdue jobs and
  the GEFS run 20.6 h old (stale after 18.5 h), one email "Data health DEGRADED: 8
  problems"; the NOAA run and the map layers, emailed at 16:10, were listed as "already
  reported" and not sent again, because the alert state survived in the job log. Session 3
  (3.7 h down): `degraded` with three overdue jobs and no email, as designed: those keys had
  been emailed at 20:45 and recovered at 21:00, less than the 12 h repeat interval before;
  the next check (01:50) was `ok` and sent nothing either.
* Recovery, session 2: the first scheduled check of the rebuilt stack (21:00:43 UTC) found
  everything fresh (`ok`) and sent one notice, "Data health recovered: all checks ok",
  listing all ten problems as resolved: the eight overdue jobs, the GEFS run, and the NOAA
  run and map layers alerted at 16:10. The checks after it sent nothing.
* A defect found live and fixed: while that acquisition was still downloading ECMWF, the
  report called the blend stale "although every member source is fresh" (NOAA was new; the
  blend is rebuilt only after the whole acquisition). A stale blend is no longer a problem
  of its own while `forecasts.acquire` or `forecasts.blend` is running; the stack was rebuilt
  with this fix at 20:45 and the second soak ran on it.
* A second defect found live and fixed: after the restarts of sessions 2 and 3 the `backup`
  container started before PostgreSQL accepted connections ("Connection refused") and,
  as written then, waited a whole 24 h interval for its next attempt. It now waits for
  PostgreSQL and retries after `BACKUP_RETRY_MINUTES`; after a deliberate daemon
  restart at 02:06 UTC (session 3) it waited for PostgreSQL (one 10 s wait) and then
  backed up the database (17.8 MB in 4 s); every service was healthy again within 25 s
  and `scripts/check-health.sh` answered OK.
* Beat schedule: each time the running beat's schedule file held all 11 entries expected
  for these settings, with their next run times (e.g. `notification-delivery` +2 min,
  `data-health` and `alert-generation` +15 min, `quality-prediction-safety-net` and
  `user-matching` +30 min, `swell-detection`, `flight-discovery`,
  `forecast-staleness-check` +1 h, `forecast-acquisition` +3 h, `ensemble-acquisition` at the
  next 01:40 / 07:40 / 13:40 / 19:40 UTC, `maintenance-cleanup` 03:30 UTC); none missing.
* Soaks, each with every job due run by the worker: session 1, 15:40-16:44 UTC: 81 job runs
  of 11 jobs (42 deliveries, 10 alert generations, 6 data-health checks, ...), 0 failed, 1
  skipped by its lock; session 2, 20:45-21:50 UTC (on the rebuilt stack): 73 runs of 8 jobs
  (42 deliveries, 10 alert generations, 4 data-health checks, ...), 0 failed, 0 skipped;
  session 3, 01:35-02:05 UTC (on the final code): 63 runs of 11 jobs (26 deliveries, 10
  alert generations, 3 data-health checks, the acquisitions above, ...), 0 failed, 0
  skipped. No worker, beat, API or frontend log line of session 3 reported an error; the
  one warning was the post-outage data-health report. Peak memory sampled once a minute
  in session 3: worker 849 MiB, API 471 MiB, PostgreSQL 187 MiB, beat 96 MiB.
* Data-health alerts, end to end through the worker and Mailpit (the stack's capture
  inbox), to the one verified admin account, in session 1:
  * 16:10:33, scheduled run: the NOAA 00Z run passed its 16 h limit (and with it the map
    layers): status `degraded`, one email "Data health DEGRADED: 2 problems";
  * 16:11:13, a one-off check in the worker container with `DATA_HEALTH_DATABASE_MAX_GB=0.1`:
    one email for the new `database_size` problem only, listing the NOAA and map problems
    as already reported;
  * 16:25:33, scheduled run with the normal settings: recovery notice "1 problem resolved,
    still degraded"; the later runs of that session sent nothing (de-duplicated).
* `GET /api/health/data` directly and through the frontend proxy, `?strict=true` (200 while
  degraded), `Cache-Control: no-store`; `scripts/check-health.sh` exit 0 (ok), 1 (degraded,
  with the reasons) and 3 (unreachable); the status page rendering the degraded report.
* Backups: the `backup` service (a dump on start, 95.6 MB in 26 s, healthy; with the final
  compose file 17.8 MB in 8 s of the 204 MB session-3 database, most of whose map-field
  space was free space left by pruning), restore drills (§4; the last one on 2026-10-13
  with the final scripts), a restore into the Compose database and one into a new database
  on the Compose server (§4).

**Review re-run (2026-10-13, 16:12-16:45 UTC).** The same images (their `app/` and
`alembic/` byte-identical to the code before the review's fixes, `diff -r`), a separate
Compose project with the default `BEAT_RUN_ON_START=true`, every lead time limited to 24 h
(sandbox budget), after a 13.5 h stop of the Docker daemon (02:46-16:12):

* Restart policies brought all eight containers back. PostgreSQL ran crash recovery and
  accepted connections at 16:13:06; the `backup` container (up at 16:12:42) waited for it
  and backed up at 16:13:13: 53,115,353 bytes in 27 s.
* Beat sent every entry that had fallen due at 16:13:15. `forecasts.acquire` (75.7 s):
  NOAA 06Z, 9 steps, 91 requests, 213.3 MB, 0 spot errors; ECMWF 00Z, 9 steps, 64
  requests, 122.0 MB in 17.8 s, no 503 SlowDown; then the blend (0.4 s) and 3 × 450
  predictions. GEFS-Wave 06Z: 5 steps, 49 requests, 23.9 MB, 19.7 s. The worker received
  375 MB in all (docker stats).
* **A defect found live and fixed in the review:** the first `ops.data_health` run
  (16:13:48) emailed "Job forecasts.acquire has not run for 13.6 h" while that job had
  been running since 16:13:18: the start-up kick-off had queued a second copy, logged
  "skipped" (lock held), and the overdue check read only the newest row. The run in
  progress now counts whatever was skipped after it (and a killed run followed by skipped
  copies is still reported as stuck). The other four problems of that email were real
  (both forecast sources and the map layers past their limits, hence `failing`). At
  16:28:15 one recovery notice, "Data health recovered: all checks ok", followed;
  `GET /api/health/data` and `scripts/check-health.sh` then said ok (exit 0).
* The beat schedule file held all 11 entries, with next runs `notification-delivery`
  every 2 min, `data-health` and `alert-generation` 16:28:15, `user-matching` and
  `quality-prediction-safety-net` 16:43:15, the hourly jobs 17:13:15,
  `forecast-acquisition` 19:13:15, `ensemble-acquisition` 19:39:59, `maintenance-cleanup`
  03:29:59. `data-health` ran at 16:13:15, 16:28:15 and 16:43:15.
* 31-minute soak (16:12:30-16:44): 65 job runs of 12 jobs, 0 failed, 1 skipped (the
  duplicate acquisition), none left running; no error line in any app container's log;
  every container healthy. Peak memory (sampled every minute): worker 839 MiB, API
  287 MiB, PostgreSQL 190 MiB, beat 97 MiB, backup 53 MiB, frontend 45 MiB.
* `GET /api/health/data` (5,694 bytes): no address, credential, URL or error text in it;
  `Cache-Control: no-store`; 64 requests in a burst gave 58 × 200 and 6 × 429 (60 per
  minute; two requests had been made just before). The database grew from 110.2 MB (02:43) to
  120.6 MB with this one set of 24-hour runs.
* On a separate test database, a forced stale source and a failing job (synthetic rows)
  run through the real `ops.data_health` task produced exactly one problem email, nothing
  on the next run, one recovery notice once fixed, and nothing after it; the error text
  appeared in the email only.
* A restore drill of a fresh copy of `swell_live2` (row counts checked identical first):
  backup 47,858,041 bytes in 7 s, restore 3 s, 42 tables and 34,800 rows identical,
  restored database 96 MB.

The fixed code was not rebuilt into images (the sandbox disk was full); it ran from the
host against the stack's live database at 16:44 (`python -m app.services.health`: ok, the
same as the API) and in the tests. The stack was stopped at 16:45.

**Run locally, not on GitHub:** every command of `.github/workflows/ci.yml` except the
Playwright browser installation: actionlint 1.7.12 (with shellcheck 0.11.0) on the
workflow; a fresh Python 3.13 virtualenv with `pip install -e ".[dev]"` (1 min 21 s), then
`ruff format --check`, `ruff check`, `mypy app` and `pytest tests/unit` in it;
`alembic upgrade head` and `alembic check` on an empty database; `pytest tests/integration`
in the development virtualenv; `npm ci`, `tsc`, `eslint`, `vitest` and `next build`; and
`scripts/run-e2e.sh` against local PostgreSQL 16 and Redis 7. Results: 51 of 51 passed at
10:54 UTC on 2026-10-12. Later full runs (2026-10-12 15:58, 16:10 and 20:49, 2026-10-13
02:19 UTC) each had one or two failures, all in `e2e/map.spec.ts`, which this change does
not touch: twice "in Los Angeles time" (the synthetic five-day timeline draws the last day's
label only when that day is wide enough, so the test depends on the time of day), twice
"signed-in users see their own opportunities" (the development `simulate-swell` call took
more than 30 s and the frontend proxy hung up), and once "dragging the timeline downloads
only the step it settles on" (16 field requests instead of at most 4, a timing-dependent
check). `e2e/map.spec.ts` alone then passed 26 of 26 both times it was rerun (2026-10-12
21:00 and 2026-10-13 02:25 UTC). The last full run used Chromium from
`PLAYWRIGHT_CHROMIUM_PATH`, since the sandbox's Playwright browser cache is older than the
installed Playwright. The review's full run (2026-10-13, about 16:26-16:33 UTC) passed 50
of 51; the failure was again "in Los Angeles time" (the last day's label was not drawn).
On 2026-10-13, `tests/integration` had one failure that does not come from this change:
`test_pipeline.py::test_paused_search_does_not_generate_notifications` failed in every run
(three of three at the base commit `d2b7e42` as well). It depends on the date: the demo
provider's natural Jeffreys Bay swell (from 2026-10-17 16:00 UTC that day) overlaps the
swell the test simulates seven days ahead, detection merges the two into one event
starting four days ahead, and the unpaused search does not match it. Both have since been
fixed: the pipeline tests turn the natural demo swells off (fixture
`only_simulated_swells`), and the Los Angeles timeline test checks a whole day in the
middle of the range.

**Not verified**

* The workflow on GitHub's runners, and `npx playwright install --with-deps chromium`.
* A full-length (240 h) acquisition started by beat: the acquisitions beat started in
  sessions 2 and 3 ran with the sandbox's 24 h lead limit; the full-length acquisitions of
  session 1 were started by hand. Beat's own 3-hourly `forecast-acquisition` interval did not
  come round during any soak (each time it ran as a catch-up after a restart).
* Admin alerts through a real email provider (Resend, SendGrid or an SMTP relay): only
  Mailpit captured them.
* Buoy-observation freshness (`OBSERVATIONS_ENABLED` was off) and the standalone map-fields
  mode.
* A long soak: the stack was up for about four hours in all, not days; disk growth over days
  is extrapolated from one full run (§6).
