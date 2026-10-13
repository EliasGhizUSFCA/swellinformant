# Operations: deploy, monitor, back up, recover

How to run Swell Travel Agent unattended on one server, notice when its data goes stale,
keep backups that restore, and size the machine. Every number below was measured on
2026-10-12 or 2026-10-13 on the Docker Compose stack with live providers
(`FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs`, `GEFS_ENABLED=true`), unless it says otherwise;
"Verification record" at the end lists what was and was not run.

## 1. What runs

| Process | Role | Instances | Health check |
|---|---|---|---|
| `backend` | FastAPI API; applies migrations and seeds on start (`RUN_MIGRATIONS=true`) | 1+ (`API_WORKERS` uvicorn workers each) | `GET /api/health/ready` (database and Redis) |
| `worker` | Celery worker: every job in docs/ARCHITECTURE.md §2 | 1+ (`WORKER_CONCURRENCY` slots each) | `celery inspect ping` |
| `beat` | Celery beat: the schedule (`app/workers/schedule.py`) | **exactly 1** | its schedule file was written in the last 10 min |
| `frontend` | Next.js server; proxies `/api/*` to the backend | 1+ | `GET /` |
| `postgres` | PostgreSQL 16 (all persistent state) | 1 | `pg_isready` |
| `redis` | Celery broker and results, locks, rate limits (transient only) | 1 | `redis-cli ping` |
| `mailpit` | development inbox (not needed with real email) | 0-1 | built in |
| `backup` | optional (`--profile backup`): `scripts/backup-db.sh` every `BACKUP_INTERVAL_HOURS` | 0-1 | newest backup younger than the interval + 1 h |

Redis holds nothing that must survive: on a Redis restart beat re-sends the schedule, a
lost lock only allows one duplicate run (every job is idempotent), and queued alert
deliveries are rows in PostgreSQL, not Redis messages.

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
   FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs
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
   *healthy* (about 30 s after the images are built; the first live acquisition takes
   about 8.5 min, see §6).
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
| Each `FORECAST_PROVIDERS` source | newest successful run older than the source's own stale limit (`queries.stale_after_hours`: NOAA 16 h, ECMWF 25 h, others `FORECAST_STALE_HOURS`), or no successful run | degraded |
| All forecast sources at once | none of them is fresh: forecasts, events and alerts run on stale data | **failing** |
| Blend (two or more independent families) | stale or missing although every member is fresh and no `forecasts.acquire` / `forecasts.blend` run is in progress (`forecasts.blend` broken; during an acquisition the blend trails its first refreshed member until the chained blend stage runs) | degraded |
| GEFS-Wave (`GEFS_ENABLED`) | older than 18.5 h or missing (uncertainty only) | degraded |
| Every scheduled job | last real run failed (with the number in a row); no run started for 2 × its longest interval + 30 min (overdue; a run in progress counts, also when a duplicate was logged "skipped" after it); still "running" 15 min past the 30-min hard time limit (stuck: worker killed, e.g. out of memory) | degraded |
| Heartbeat | no job of any kind finished for `DATA_HEALTH_HEARTBEAT_MINUTES` (30; `notifications.deliver` alone runs every 2 min): worker or beat down | **failing** |
| Map ocean layers (unless off) | served GFS cycle older than the NOAA limit (16 h), or no complete field set | degraded |
| Buoy observations (`OBSERVATIONS_ENABLED`) | newest observation of a provider older than `DATA_HEALTH_OBSERVATION_STALE_HOURS` (default: the 48 h ingestion look-back, beyond which the hourly job cannot fill the gap) | degraded |
| Storage | database larger than `DATA_HEALTH_DATABASE_MAX_GB` (unset: reported, no limit) | degraded |

The overall status is `failing` if any problem is failing, `degraded` if any is degraded,
else `ok`, and `reasons` lists one plain sentence per problem, the most severe first. The
limits are operational heuristics (how late data may be before someone should look),
not statements about forecast accuracy.

**Public on purpose.** The report holds operational metadata only: run keys, ages,
limits, job states and sentences written by the service. No personal data, no settings
values beyond the limits, and no raw error text (a failed job's redacted error appears
in the admin email only). It must be readable without credentials because the most
important failure, a dead worker, cannot report itself. It is rate limited (60 requests
per minute per IP) and never cached (`Cache-Control: no-store`). With `?strict=true` the
API answers 503 instead of 200 when the status is `failing`, for monitors that only look
at status codes. The status page (`/status`) shows the same report.

From a shell or cron, `scripts/check-health.sh https://your-domain` prints the reasons and
exits 0 ok, 1 degraded, 2 failing, 3 unreachable:

```cron
*/5 * * * * /srv/swell/scripts/check-health.sh --quiet https://your-domain || [ $? -eq 1 ] || echo "Swell data health FAILING" | mail -s "swell" you@your-domain
```

The same command works locally: `python -m app.services.health` prints the JSON report.

### Admin alerts (`ops.data_health`, every 15 min)

The job emails every **active admin account with a verified address** through the
configured email provider:

* when a problem appears (new) or its severity rises;
* again at most every `DATA_HEALTH_ALERT_REPEAT_HOURS` (12 h) while it lasts;
* once more when it clears (recovery notice, only for problems that were emailed).

A problem that clears and comes back (flaps) is emailed again only once 12 h have passed
since its last alert: in between, the recurrence is listed under "already reported" in any
other email and clears without a second recovery notice. So each problem costs at most one
alert and one recovery notice per 12 h, however often it flaps; only a rise in severity is
sent at once.

All due items of one run go into one email per admin; the email lists the other current
problems as "already reported", the last error of a failed job, and links to `/status`
and `/api/health/data`. A problem is only marked as sent when the provider accepted (or
the development inbox captured) at least one email; otherwise the next run tries again.
The de-duplication state lives in the job's own `background_job_logs.details`, so a
restart neither loses it nor re-sends old alerts. Logs carry counts, never addresses.
Settings: `DATA_HEALTH_CHECK_MINUTES` (15), `DATA_HEALTH_ALERTS_ENABLED` (true),
`DATA_HEALTH_ALERT_REPEAT_HOURS` (12), `DATA_HEALTH_HEARTBEAT_MINUTES` (30),
`DATA_HEALTH_OBSERVATION_STALE_HOURS`, `DATA_HEALTH_DATABASE_MAX_GB`.

The alert path has a blind spot by construction: if the worker, Redis or the email
provider is down, no email goes out. That is why the external monitor in §2 step 6
matters.

### Logs and job history

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
by how much.

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
5. Once fixed, fetch now: `docker compose exec worker celery -A app.workers.celery_app call forecasts.acquire`
   (or `forecasts.acquire_ensemble`); the recovery email follows within 15 min.
6. A provider gone for days: leave it configured (the system works on the other family
   and tells you every 12 h). Removing it from `FORECAST_PROVIDERS` silences the alert
   but also turns the blend off.

### Worker or beat down, jobs overdue or stuck

* `jobs_not_running` (failing) or many `job_overdue:*`: `docker compose ps`; restart the
  unhealthy service (`docker compose restart worker beat`). Exactly one beat.
* `job_stuck:<job>`: the worker process was killed mid-job (the hard limit is 30 min;
  out of memory is the usual cause: see `docker compose logs worker` and `dmesg`). The
  lock expires by itself (at most 1 h); give the worker more memory or lower
  `WORKER_CONCURRENCY`.
* `job_failed:<job>`: the email carries the redacted error; the job log row has it too.

### Database or disk full

`database_size` (when `DATA_HEALTH_DATABASE_MAX_GB` is set), failed jobs with "No space
left on device", or PostgreSQL refusing writes. Free space first (old local backups,
Docker build cache: `docker builder prune`), then lower the retention settings (§6) and
run `maintenance.cleanup`. PostgreSQL reuses the space of deleted rows; it only returns
it to the operating system after `VACUUM FULL` (locks the table; do it in a quiet hour).

### Email provider failing

User alerts retry with backoff and record the provider's reason. Admin data-health
emails retry every 15 min until accepted; meanwhile only the external monitor can tell
you, which is one more reason to have it.

### Restore after data loss

§4 "Restore", then run `forecasts.acquire`: forecasts are re-fetched from the providers;
accounts, searches, alerts and the prediction archive come only from the backup.

## 6. Capacity

### Measured run (Docker, 2026-10-12, NOAA 00Z / ECMWF 00Z / GEFS 00Z, 50 spots)

| Step | Duration | Downloaded | Rows written |
|---|---|---|---|
| NOAA GFS-Wave + GFS (81 steps to 240 h, 811 requests, incl. map fields) | 2.5 min | 1,908 MB | 4,050 forecast rows, 486 map-field rows |
| ECMWF IFS (65 steps, 438 requests, 173 of 611 HTTP attempts answered 503 SlowDown and retried) | 8.5 min (start to stored, alongside NOAA) | ≈ 0.9 GB (see below) | 3,250 |
| `forecasts.acquire` (both of the above, in parallel) | 8.5 min (511 s) | 2.82 GB received by the worker | |
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
| `surf_quality_predictions` | 2,529 | 47,000 | 119 MB | with its forecast run, `RETENTION_FORECAST_DAYS` (10) | ~1.2 GB |
| `wave_forecasts` | 631 | 47,000 | 30 MB | 10 days | ~0.3 GB |
| `forecast_comparisons` | 771 | 24,300 | 19 MB | 10 days | ~0.19 GB |
| `forecast_ensemble_stats` | 156-352 | 8,200 | 1.3-2.9 MB | 10 days | ≤ 29 MB |
| `map_fields` | 84,800 (41 MB per run) | 4 runs | 165 MB written | the 2 best runs | ~82 MB |
| `prediction_archive` | 1,706 | 24,300 | 41.5 MB | `RETENTION_ARCHIVE_DAYS` (730) | 15 GB after 1 year, 30 GB after 2 |
| `background_job_logs` | ~330 (+ index) | ~1,150 | ~0.5 MB | `RETENTION_JOB_LOG_DAYS` (30) | ~15 MB |

The second session's 24-hour run (1,350 forecast rows and predictions, 450 comparison and
archive rows) grew the same tables by 2,555 (predictions), 655 (`wave_forecasts`), 728
(`forecast_comparisons`) and 1,839 (`prediction_archive`) bytes per row: within 8 % of the
figures above (small deltas are rounded to whole 8 kB pages).

So with the defaults the database settles at about **1.8 GB plus the archive**, which
grows by about **42 MB a day** (15 GB a year) until it reaches its 730-day retention
(about 30 GB). Forecast runs and their rows are deleted by `maintenance.cleanup` (daily
03:30 UTC), always keeping the two newest usable runs per source; map fields are pruned
to two runs after every acquisition. Buoy observations, point forecasts and verification
pairs (when enabled) also follow `RETENTION_ARCHIVE_DAYS`; they were not enabled in this
run.

**Bandwidth:** about **10 GB downloaded per day** (NOAA 4 × 1.91 GB, ECMWF 2 × 0.85-0.9
GB, GEFS 4 × 0.20 GB), about 300 GB a month, all from NOAA's and ECMWF's open-data
buckets. Uploads are negligible (requests only).

**Backups:** the measured dump was 52 % of the database size, dominated by the
incompressible map fields (about 74 MB); the other tables compressed about 5:1. From
this one measurement, expect roughly 0.2 × (database − map fields) + 75 MB per dump:
about 3.4 GB per dump once the archive holds a year, 6.5 GB at two years, times the
number of local copies (`BACKUP_KEEP`, default 7). Keep the history off the server.

### A single small server

| | Minimum | Comfortable |
|---|---|---|
| CPU | 2 vCPUs (acquisitions take longer) | 4 vCPUs |
| Memory | 4 GB (about 1.8 GiB measured peak plus the OS) | 8 GB |
| Disk | 40 GB: `RETENTION_ARCHIVE_DAYS=365` (database ≤ ~17 GB) and `BACKUP_KEEP=3` (~10 GB) | 120 GB with the defaults (database ~32 GB, 7 local dumps ~45 GB at two years) |
| Network | 10 GB/day inbound | |

Plus about 2.2 GB for the images. Recommended settings for the 40 GB case, in `.env`:

```env
RETENTION_ARCHIVE_DAYS=365        # archive ~15 GB instead of ~30 GB; it is the record of
                                  # past forecasts used for verification, so keep what you can
BACKUP_KEEP=3                     # local copies; ship every backup off the server
DATA_HEALTH_DATABASE_MAX_GB=20    # degraded alert well before the disk fills
```

Lowering `RETENTION_FORECAST_DAYS` below 10 saves little (each day is ~170 MB) and the
two newest runs per source are always kept. `GEFS_ENABLED=false` saves 0.8 GB/day of
bandwidth at the cost of ensemble-based uncertainty. `NOAA_MAX_LEAD_HOURS=168` /
`ECMWF_MAX_LEAD_HOURS=168` read about 30 % fewer NOAA steps (57 of 81) and about 18 %
fewer ECMWF steps (53 of 65), and shorten the forecast to 7 days.

## 7. Settings reference (operations)

| Setting | Default | Meaning |
|---|---|---|
| `DATA_HEALTH_CHECK_MINUTES` | 15 | interval of the `ops.data_health` job |
| `DATA_HEALTH_ALERTS_ENABLED` | true | email admins about problems and recoveries |
| `DATA_HEALTH_ALERT_REPEAT_HOURS` | 12 | at most one email per problem in this interval |
| `DATA_HEALTH_HEARTBEAT_MINUTES` | 30 | no finished job for this long = worker or beat down (failing) |
| `DATA_HEALTH_OBSERVATION_STALE_HOURS` | `OBSERVATION_LOOKBACK_HOURS` (48) | buoy provider stale after this |
| `DATA_HEALTH_DATABASE_MAX_GB` | unset | degraded above this database size |
| `RETENTION_FORECAST_DAYS` | 10 | forecast runs and their rows (two newest per source always kept) |
| `RETENTION_ARCHIVE_DAYS` | 730 | prediction archive, buoy observations, point forecasts, verification pairs |
| `RETENTION_JOB_LOG_DAYS` | 30 | job logs (also the alert de-duplication state) |
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
starting four days ahead, and the unpaused search does not match it.

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
