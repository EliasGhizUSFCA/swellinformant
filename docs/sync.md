# Synced data pipeline

"All of the weather data should be pulled when the forecast updates." The background
pipeline does not run on fixed timers. It checks the providers' **metadata** for a new
model run, and when one appears it runs **one coordinated sync**: the spot forecasts, the
ensemble and the world map layers of that run are acquired, blended, scored and
**published together**. The code is `backend/app/services/sync/`; the same sync runs in
every runner (the sync daemon on Vercel and in the local Vercel-style stack, Celery's
`sync.tick` in the classic compose stack, the command line, and the manual GitHub Actions
workflow).

## What "synced" guarantees

* **One sync per new model run.** A run key already stored is never fetched again; a
  duplicate trigger (two runners, a retry, `--force`) finds every stage idempotent.
* **The map and the spots switch within one job.** Spot pages switch when the new
  predictions commit (stage `predict`); the map index switches when the same job commits
  its publication (stage `publish`, a few seconds later). The map index serves the map run
  of the newest publication (`SYNC_PUBLISH_GATE`, app.services.map_fields), so a map run
  stored by any other path is not shown before a sync publishes it. Responses cached at
  the edge can lag by their cache lifetime (docs/map-layers.md).
* **The GFS spot run and the map layers of the same cycle appear together.** When one of
  them is new for a map cycle (`MAP_FIELD_CYCLES`: 00Z and 12Z by default, 06Z and 18Z in
  the free-database profile) and the other is not yet, the
  sync waits up to `SYNC_COALESCE_MINUTES` (90) from the first sighting of either, then
  syncs whatever is available.
* **Point forecasts only for the surf spots.** The spot stage requests the active seeded
  spots only (`surf_spots.is_active`), one Open-Meteo request per model for up to 50 spots.
  Global grids are downloaded only for the world map layers. `init-db`, which the daemon
  runs at every start, sets `is_active` from `data/surf_spots`, so a spot is switched off
  there, not in the database.
* **Nothing is published half-way.** A sync that stops before `publish` (SIGTERM, a crash)
  leaves its data unpublished; the next check treats it as new and the next sync publishes
  it (below, "stored" means "stored and published"). A sync whose `predict` stage failed,
  or could not run because another holder had its job lock (a daemon killed mid-stage: the
  lock outlives it by its 30-minute TTL), does not publish either, unless it was forced:
  the map and the spot pages never switch to runs that were not scored. The components it
  stored or found stored count as failed attempts, so the next check retries after
  SYNC_RETRY_MINUTES, at most SYNC_MAX_ATTEMPTS times per run.
* **The check is free.** It reads Open-Meteo `meta.json` (not charged to the call budget)
  and NOAA NODD `.idx` HEAD requests, and in the daemon it makes **no database query**, so
  a scale-to-zero database (Neon) stays asleep between syncs.

It does not guarantee that every provider has a run of the same cycle: ECMWF runs 00Z and
12Z only and is synced on its own as soon as it is new (it never waits for GFS), and a
provider that fails is retried on its own schedule (below) while the others publish.

## Components

`app.services.sync.detect.build_probes` lists the components of a plan:

| Component | Source code | What tells a new run | Present when |
|---|---|---|---|
| `spots` per FORECAST_PROVIDERS token (e.g. `open_meteo`, `open_meteo_ecmwf`) | `open_meteo_noaa`, `open_meteo_ecmwf`, ... | the provider's `current_run(now)`: Open-Meteo `meta.json` of every model domain, used OPEN_METEO_AVAILABILITY_WAIT_MINUTES (10) after its availability time; `.idx` HEADs for the direct GRIB ingesters | always |
| `ensemble` | `noaa_gefs_wave` | `.idx` of the last GEFS-Wave mean step | `GEFS_ENABLED=true` |
| `map` | `noaa_gfs_fields` | `.idx` of the last map hour (MAP_FIELD_STEPS, MAP_FIELD_MAX_LEAD_HOURS) of the map atmosphere file and `global.0p25`, map cycles only | the standalone map-fields mode (`MAP_FIELDS_ENABLED`, docs/map-layers.md) |

A component that is not configured is `disabled`. With `noaa_gfs` as a spot provider
(the "with_noaa" mode) the map layers come with that spot run and the `map` component is
disabled.

For the Open-Meteo families the check also asks `ensure_servable` (on the same metadata
read): between the 00Z and 12Z runs the ECMWF family reads, Open-Meteo serves the ECMWF
06Z/18Z run and fills its later hours from an older run, so the labelled 00Z/12Z run can
no longer be read unmixed. Such a component is `waiting` ("not served yet") and never
makes a sync due. Without this check, every such window cost up to SYNC_MAX_ATTEMPTS
failing syncs. That was seen live on 2026-10-19, before the check existed: ECMWF 00Z was
planned as new, Open-Meteo refused its acquisition, and the refusal counted as a failed
attempt.

## The rules (a plan, `detect.plan_sync`)

Per component the plan compares the provider's newest servable run with the **stored**
run key: the newest usable (success or partial) run of that source that completed before
the newest publication (`detect.load_known`; simulated demo runs are ignored).

1. **New.** The available run key differs from the stored one. A run older than the stored
   one is never new: its newest `YYYYMMDDHH` cycle is earlier (e.g. a metadata cache
   serving the previous run), or it has the stored run's model domains with none newer and
   one older (a lagging Open-Meteo server offering part of the previous run). A map cycle
   whose layers are already published is current.
2. **Coalescing** (state `waiting`). A run whose model domains are on different cycles
   (Open-Meteo published GFS-Wave, GFS 0.13 and GFS 0.25 5.3, 6.0 and up to 8.2 h after the
   cycle on 2026-10-14/15, docs/FORECAST_SOURCES.md) waits for the late domains; the GFS
   spot run of a map cycle and the map layers of that cycle wait for each other. Both waits
   end SYNC_COALESCE_MINUTES (90) after the first sighting of anything new for that source
   or cycle; then whatever is available is synced. The first sighting is kept when the
   available run key changes again before a sync, so a domain that keeps changing never
   starves the wait. `SYNC_COALESCE_MINUTES=0` turns coalescing off.
3. **Triggers.** A sync is **due** when a component of a kind in SYNC_TRIGGER_SOURCES
   (default `spots,map`; `map` in the free-database profile) is `new`. A new ensemble run
   (not a trigger by default) is acquired by the next sync anything else triggers, and so
   are the newest spot runs when only `map` triggers. When none of the configured kinds
   has a component in this setup (`map` with `MAP_FIELDS_ENABLED=false`, or in the
   with_noaa mode), new spot runs trigger instead, and the plan's reasons say so.
4. **Retries.** A component whose acquisition failed is retried SYNC_RETRY_MINUTES (15)
   after the attempt, or later when the provider asked for a longer wait (`retry_after`);
   state `waiting` meanwhile. After SYNC_MAX_ATTEMPTS (6) failed attempts for one run key
   it is `failed` until the provider offers the next run (health shows it).
5. **Provider errors** (metadata unreachable, no complete cycle yet, a bug) make the
   component `unknown`, with a redacted detail; never due, never raised.
6. **One sync at a time.** With the database (Celery's `sync.tick`, `sync run`), a sync that
   holds a valid lease makes new components `in_progress` and the plan not due.
7. **Force** (`sync run --force`) is always due and acquires every configured component;
   the acquisitions themselves still skip runs already stored.

States: `current`, `new`, `waiting`, `in_progress`, `failed`, `unknown`, `disabled`
(app.services.sync.types). `plan.anchor_cycle` is the GFS cycle of the map (or GFS spot)
run a due plan acquires.

The daemon keeps the stored keys, the first sightings and the attempts in memory and in
Redis (`sync:known`, 7 days), so a check needs only Redis and the providers; the stored
keys are reloaded from the database at start and after every sync.

## One sync (`orchestrator.run_sync`)

The lease is a `sync_runs` row with status `running` (unique partial index
`uq_sync_runs_running`). Its `lease_expires_at` is SYNC_LEASE_MINUTES (45) ahead and
renewed after every stage. A second sync meanwhile gets `SyncBusy` (`sync run` exits 3);
an expired lease (a crashed runner) is marked `abandoned` by the next sync, which then
does the work. A sync that fails before its first stage (its `sync.run` job log cannot be
written) releases the lease at once, as `failed`.

| Stage | Job log name | Runs when |
|---|---|---|
| `spots` | `forecasts.acquire` | a spot component is new (only those providers: `acquire_all(only=)`) |
| `ensemble` | `forecasts.acquire_ensemble` | the ensemble is new |
| `map` | `forecasts.map_fields` | the map is new |
| `blend` | `forecasts.blend` | new spot or ensemble data (stored now, or stored but unpublished) |
| `predict` | `quality.predict` | always (pending runs only); failed, not skipped, when another holder has its lock (so does `blend`) |
| `publish` | `sync.publish` | anything new was stored, or stored data is unpublished; never after a failed `predict` unless forced |
| `detect`, `match`, `flights`, `alerts`, `deliver` | `detection.detect`, `matching.match`, `flights.discover`, `alerts.generate`, `notifications.deliver` | after a publish |
| `prune` | `maintenance.prune_pass` | after a publish, with SYNC_PRUNE_EACH_PASS=true |

With `--force` every stage runs except two: the acquisition of a `disabled` component,
and `prune` without SYNC_PRUNE_EACH_PASS. All 12 stages are recorded either way.

Each stage runs through `app.jobs.runner.run_job`: one `background_job_logs` row under the
job name above (with `cpu_s`), the job's Redis lock, and no chaining. `sync_runs.stages`
holds one entry per stage: `{name, status, started_at, duration_ms, cpu_s, detail}`. For
an acquisition, `detail.components` lists each component's outcome:

* `stored`;
* `current`: stored already;
* `deferred`: the provider did not serve the run unmixed at acquisition time, although the
  check saw it as servable. Examples: Open-Meteo switching to its next run, or Open-Meteo
  servers that disagree for a few minutes about a run being published (seen live, below).
  This is no failure, and no attempt is counted;
* `failed`. "In progress" (another runner is storing the same run) also counts as a
  failed attempt.

One `sync.run` job log per sync holds the status and the sync's own CPU share (`cpu_s`).
Its `cpu_s_total` includes the stages, so summing `cpu_s` over all job logs counts each
CPU second once.

The publish stage (`publish.publish`) commits `published_at`, `map_run_id` (the best
servable map run) and `blend_run_id` (the newest blend run with predictions).
`python -m app.cli pipeline` and the developer "run pipeline" endpoint publish too (a row
with trigger `pipeline`).

The status of a sync is one of:

* `success`, even with a deferred component;
* `partial`: a component or a stage failed, or the sync was stopped;
* `failed`: nothing was published and something failed, or the publication failed;
* `skipped`: nothing was new after all;
* `abandoned`: the lease expired and was taken over.

`stop` (SIGTERM in the daemon) is checked between stages: the current stage finishes,
the other stages are recorded as skipped ("stopped"), the lease is released. A stage is
never interrupted, so a process killed before its stage ends (for example one that
outlives its grace period after SIGTERM: vercel.json sets the daemon's
terminationGracePeriodSeconds to 600, Vercel's default being 30 and its maximum 3600, so a
full map stage, an estimated 165-287 CPU s on one vCPU, normally finishes) leaves the
lease running. The next sync then waits for the lease to expire
(SYNC_LEASE_MINUTES after its last renewal), marks it `abandoned` and does the work; a map
or spot run the killed stage left `running` counts as "in progress" (a failed attempt)
until it is an hour old, and is then acquired again.

## The daemon (`python -m app.cli sync daemon`)

At start: `init-db` (migrate, seed, tide catalog; idempotent) when SYNC_DAEMON_INIT_DB is
true (the default; `--no-init-db` skips it), then the known runs from the database, then
the start-up catch-up (SYNC_DAEMON_CATCH_UP, default true): every periodic job that has not
succeeded within its longest gap (its interval; 24 h for a job once a day) or never has
runs once, back to back, before the first check (one statement over the job logs decides).
A job that started within an hour (or its own interval, when shorter) is skipped, so a
restart loop does not ask a provider again at every start. Without it a new deployment had
no tide predictions until the next 02:20 UTC, and frequent restarts could keep restarting
the hourly timers before they fired. A start-up failure (e.g. the database still waking) is
retried every minute. Then, until SIGTERM or SIGINT:

* every SYNC_DAEMON_POLL_SECONDS (60): the API's kicks from Redis (`sync:kick:*`, one Lua
  call). "Match my search" runs `matching.match` and the stages after it (flights, alerts,
  delivery), as Celery chains them; "refresh flights" runs `flights.discover` for the
  matches asked for and the stages after it;
* every SYNC_CHECK_MINUTES (10): the plan from the known runs and the heartbeat in Redis
  (`sync:heartbeat`); when due, one sync, then the known runs are reloaded;
* the periodic jobs of `app.jobs.schedule.build_schedule` except the sync's own (the
  acquisitions, predictions and detection, and `sync.tick`): `Every` jobs on in-memory
  timers that start one interval after the daemon, at least JOB_INTERVAL_FLOOR_MINUTES
  apart (60 when hosted), so they fall due together and run back to back in one batch,
  one database wake an hour; `DailyAt` jobs (tides, cleanup) by the UTC wall clock.

An idle iteration reads only Redis and the providers' metadata
(`tests/integration/test_sync_daemon.py` counts zero SQL statements and zero pool
checkouts), and no database connection stays open between wakes: the daemon empties its
connection pool after every wake, so a scale-to-zero database can suspend. The metadata
requests of a check use a 10 s HTTP timeout (`detect.PROBE_TIMEOUT_SECONDS`); a provider
that does not answer is `unknown` until the next check. The loop is single-threaded:
kicks wait while a sync runs (about a minute without map layers, a few minutes with them).

The heartbeat's `cpu_s_month` is this month's daemon CPU outside job runs (checks, polls,
start-up), carried over restarts; job logs plus the heartbeat count every CPU second once
(the 10 Active CPU-hour Hobby quota, SYNC_CPU_BUDGET_HOURS). Do not add `sync_runs.stages`
CPU to the job logs: every stage is also a job log.

## Other runners

* **Celery** (classic compose, JOB_RUNNER=celery): beat runs `sync.tick` every
  SYNC_CHECK_MINUTES; it plans with the database and syncs when due (`orchestrator.tick`).
* **Command line / GitHub Actions**: `sync run --trigger github` in the manual
  `data-sync.yml` workflow.

## Commands

From `backend/` (`cd backend`):

| Command | What it does |
|---|---|
| `python -m app.cli sync check`, with `--json` for JSON | the plan (Redis and provider metadata; the database only the first time) |
| `python -m app.cli sync run` | sync every component with a newer run, no waiting |
| `python -m app.cli sync run --if-due` | only when the rules say a sync is due (like the daemon) |
| `python -m app.cli sync run --force` | every stage now; stored runs are still skipped |
| `python -m app.cli sync run --only map` | limit the acquisitions (spots, ensemble, map) |
| `python -m app.cli sync daemon`, with `--once` and `--no-init-db` | the daemon (one iteration with `--once`; no init-db with `--no-init-db`) |
| `python -m app.cli sync publish` | publish what is stored now |
| `python -m app.cli sync status`, with `--limit 10` | the newest syncs and their stages |
| `python -m app.cli migrate --on-build` | migrate a production build (MIGRATE_ON_BUILD) |

Against a production database only with the Production environment loaded
(docs/operations.md, "Data is stale" step 5): with only `DATABASE_URL` set, a sync stores
synthetic demo forecasts and publishes them.

`sync run` exits 0 for success, partial or skipped, 1 for failed (or abandoned), 3 when
another sync holds the lease. To re-download a run that is already stored use
`python -m app.cli pipeline --force-acquire` or `python -m app.cli engine map-fields --force`.
`engine map-fields` stores map layers without publishing them: with SYNC_PUBLISH_GATE the
map keeps showing the published run until the next sync or `sync publish` (`pipeline`
publishes on its own).

`migrate --on-build` reads MIGRATE_ON_BUILD: `off` (default) never migrates; `production`
migrates only when VERCEL_ENV=production, so preview builds and Neon preview branches are
left alone; `all` migrates every build. It prints a JSON line with `status` `skipped`,
`migrated` or `failed` (exit 1, the build fails rather than deploying against an old
schema).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| SYNC_CHECK_MINUTES | 10 | metadata checks of the daemon and of Celery's `sync.tick` |
| SYNC_COALESCE_MINUTES | 90 | how long a GFS spot run and the map layers (or late model domains) wait for each other |
| SYNC_TRIGGER_SOURCES | `spots,map` (free-database profile: `map`) | component kinds whose new run makes a sync due |
| MAP_FIELD_CYCLES | `00,12` (free-database profile: `06,18`) | the map cycles read; with `map` triggers, also when syncs run (docs/map-layers.md) |
| SYNC_RETRY_MINUTES | 15 | wait after a failed acquisition |
| SYNC_MAX_ATTEMPTS | 6 | failed attempts per run key before waiting for the next run |
| SYNC_LEASE_MINUTES | 45 (15 under JOB_RUNNER=daemon) | lease lifetime, renewed after every stage and extended every third of it while a stage runs |
| SYNC_PUBLISH_GATE | true | the map index serves the published map run |
| SYNC_PRUNE_EACH_PASS | false | prune superseded predictions and map fields after every sync |
| SYNC_PREWARM_PUBLIC_CACHE | false | after a publishing sync, compute the public spot list and conditions at once (about 1.4 MB of reads per publication) |
| SYNC_DAEMON_POLL_SECONDS | 60 | kick polling |
| SYNC_DAEMON_INIT_DB | true | init-db at daemon start |
| SYNC_DAEMON_CATCH_UP | true | run the overdue periodic jobs once at daemon start |
| SYNC_CPU_BUDGET_HOURS | none | monthly CPU budget health warns about (10 when hosted) |
| JOB_INTERVAL_FLOOR_MINUTES | 0 | minimum interval of every periodic job (60 when hosted) |

## Redis keys

`sync:known` (stored keys, first sightings, attempts; 7 days), `sync:heartbeat` (the last
check; 6 h), `sync:kick:matching.match` and `sync:kick:flights.discover` (1 day). Reading
and writing them is best effort: with Redis down the daemon keeps the known runs in
memory, checks on, and the kicks wait for Redis.

A sync itself needs Redis, though: every stage runs through `run_job`, which takes the
job's Redis lock first. Measured on 2026-10-22 with Redis refusing connections (demo
provider, `sync run --force`): the spots, blend, predict and publish stages failed on the
lock (`ConnectionError`) in 0.4 s, nothing was published, and the command exited 1. Each
such sync counts a failed attempt per due component, so an outage of more than about an
hour and a half (SYNC_MAX_ATTEMPTS attempts SYNC_RETRY_MINUTES apart) leaves the run
`failed` until the provider offers the next one (once Redis is back, `sync run` acquires
it at once).

## Measured

On this development sandbox (4 vCPUs shared with other work, local PostgreSQL 16 and
Redis), with the settings `profile_env("hosted", "free-database")`
(FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf; MAP_FIELDS_ENABLED=true with partitions;
GEFS off; SYNC_PRUNE_EACH_PASS=true). The map was capped to 9 map hours
(`MAP_FIELD_STEPS=0-24:3`, `MAP_FIELD_MAX_LEAD_HOURS=24`; the default is 61). Each run
used a fresh database after `init-db` and the real providers, on 2026-10-20 (03:34-03:47
UTC) and 2026-10-21 (14:49-14:55 UTC).

CPU is the process's user plus system time (`getrusage`, all threads). Stage figures are
the `cpu_s` each job log records. The counters were SQLAlchemy `before_cursor_execute` and
pool `checkout` events and every httpx response (requests and bytes by kind). On 10-21,
Celery and kombu imports were blocked, and none was attempted. Open-Meteo TLS handshakes
through the sandbox's proxy sometimes timed out and were retried, so wall times are long
and vary. CPU varied by up to 2x between the two days with the machine's load. Where a
cell has two values, they are the two days.

### Checks

| Check | CPU s | Database | Downloaded |
|---|---|---|---|
| First `sync check` of a process, Redis empty (the stored keys are loaded once) | 1.82, 2.86 (provider imports included) | 1 statement | 6 `meta.json` (2.2 kB) and 2 `.idx` HEAD |
| `sync check` after a sync | 0.18, then 0.40 and 0.33 | **0 statements, 0 checkouts** | the same metadata, **0 data requests** |
| Daemon idle check (3 in a row) | 0.17-0.18, 0.29-0.32 | **0** | the same, 0 data requests |
| Daemon kick poll without a kick (10 in a row) | < 0.01 in total | 0 | none |
| Hourly periodic batch (6 jobs, no users) | 0.13, 0.22 | 62-64 statements | none |
| Daily `tides.acquire` | 13.6, 23.3 | 283 statements | 140 kB (16 GRIB ranges) |
| Daily `maintenance.cleanup` | 0.04, 0.05 | 21-22 statements | none |

### Syncs

| Sync | Plan | CPU s | Wall s | Downloaded | Rows written |
|---|---|---|---|---|---|
| 10-20 03:35 `sync run` | GFS 18Z spots and the map of 12Z new; ECMWF waiting (not served) | 36.3 | 82.0 | 69.8 MB | 10,150 wave_forecasts, 135 map_fields (10.3 MB), 10,150 predictions, 3,950 archive rows, 98 swell events |
| 10-21 14:50 `sync run` | GFS 06Z spots and the map of 00Z new; ECMWF waiting | 43.4 | 29.5 | 69.1 MB (63 data requests) | 135 map_fields (10.3 MB); GFS 06Z `deferred` (below) |
| 10-21 14:54 `sync run` | GFS 06Z spots new | 24.9 | 42.7 | 333 kB (3 data requests) | 10,300 wave_forecasts, 10,300 predictions, 4,050 archive rows, 83 swell events |

All three ended `success` and published; the first two set `map_run_id`.

| Stage | CPU s, 10-20 | CPU s, 10-21 | Downloaded |
|---|---|---|---|
| spots (50 active spots, the GFS family) | 1.69 | 3.11 | 325-328 kB: 3 data requests and 16 `meta.json` |
| map (9 map hours) | 24.36 | 42.16 | 69.1-69.5 MB: 2 HEAD, 18 `.idx`, 45 GRIB ranges |
| blend | 0.02 | 0.03 | none (one family, nothing to blend) |
| predict (about 10,300 predictions) | 7.84 | 16.70 | none |
| publish | 0.01 | 0.02 | none |
| detect | 1.92 | 4.05 | none |
| match, flights, alerts, deliver, prune | 0.11 | 0.25 | none (no users) |

**Two model families.** Neither live window served an ECMWF 00Z/12Z run unmixed, so blend
was never live with two families. It was measured on a clone of a recorded live database
instead: the real Open-Meteo GFS (10,400 point forecasts) and ECMWF (4,200) runs of
2026-10-16 00Z, plus GEFS, with the blend and the predictions reset. Results: blend
6.7-7.6 CPU s (10,400 blended records), predict 28.5-29.9 CPU s for 3 runs (25,000
predictions), detect 4.4-4.5 CPU s. Importing pandas, scikit-learn and the model costs
3.5 CPU s once per process.

**Idempotency.** A forced sync (`sync run --force`) right after each sync:

* made **0 data requests** (metadata only, 4.8-6.3 kB);
* wrote no forecast_runs, wave_forecasts, map_fields or prediction rows (12 job logs and 1
  sync_runs row each);
* used 0.8-5.1 CPU s, mostly detect.

The acquisitions reported `current` ("run already ingested", "already stored"), and ECMWF
`deferred`.

On 10-21 a forced re-sync also added one cancelled swell event at 1 of the 50 spots
(detection matched clusters to events greedily). Commit 9eb96d5 fixed this: match_clusters
pairs clusters and events one to one, and a forced re-sync now creates, merges and cancels
no swell events (integration round 2, step 7: the swell-event digest was identical).

**Deferred, live.** On 10-21 at 14:49 the check saw the GFS 06Z run as servable. Four
seconds later, Open-Meteo told the acquisition that ncep_gfs025 06Z was still being
published (available 14:42, readable from 14:52). Open-Meteo's servers can disagree for a
few minutes. The component was recorded `deferred`, with no failed attempt. The sync
published the map layers and ended `success`, and the next check took the GFS run.

Peak RSS of the measuring process was 849 MB on 10-20 and 737 MB on 10-21 (map decoding,
GRIB_DOWNLOAD_WORKERS=6).

A demo sync on a fresh database (`sync run --force` on 2026-10-20, FORECAST_PROVIDERS=demo:
synthetic data, 50 spots, 384 hourly steps over 16 days) ended `success` and published. All
12 stages were recorded; ensemble, map and prune were skipped by configuration. Stage CPU
was 21.3 s: spots 2.77, predict 14.50 for 19,200 predictions, detect 3.92, the rest 0.13.

### The daemon, live

`python -m app.cli sync daemon` ran with the settings above on 2026-10-20 from 03:43 to
06:29 UTC, against the database of the first sync. A sampler read `/proc/<pid>/stat`
(process CPU) and `pg_stat_database.sessions` (client sessions started) once a minute.
Results:

* **Start-up.** `init-db` and the first check took 3.75 CPU s, with an RSS of 263 MB.
* **Idle.** No sync was due in that window. The map layers of 00Z were first seen at
  05:03, and the GFS spot run of 00Z at 05:33, with its GFS 0.25 domain still on 18Z. Both
  were waiting for each other (coalescing) until 06:33, after the sample ended; the first
  sightings are in `sync:known`. ECMWF was not served. Over 2 h 41 min the daemon used
  3.62 CPU s, or 1.35 CPU s an hour: 16 checks and 2 hourly batches.
* **Database.** It opened exactly one database session per hourly batch (04:43 and 05:43
  UTC) and none between them. No connection stayed open between wakes.

On 2026-10-21 from 14:56 to 16:43 UTC, the daemon ran again with the same settings and
real providers, against the database of the 10-21 syncs, with Celery and kombu imports
blocked:

* **Start-up.** `init-db`, the imports and the first check took 6.19 CPU s.
* **Idle.** No sync was due. GFS 06Z and the map of 00Z were current. ECMWF was waiting,
  because Open-Meteo served its 06Z run. GFS 12Z was not published yet: GFS 0.25 06Z had
  become available on Open-Meteo only at 14:42, 8.7 h after its cycle. Over 1 h 45 min
  the daemon used 3.77 CPU s, or 2.1 CPU s an hour: 10 checks and 1 hourly batch.
* **Database.** It opened one database session, for the hourly batch at 15:56, and none
  otherwise.
* **SIGTERM.** It stopped at once.

With the demo provider (synthetic data) on a fresh database, on 2026-10-21 from 15:35 to
15:45 UTC, with SYNC_CHECK_MINUTES=5 and Celery and kombu imports blocked:

* **The sync at start.** `init-db` ran, and the first check found the demo run new. The
  daemon ran one sync (`trigger` daemon) of 52.4 s and 41.1 CPU s. All 12 stages were
  recorded, and the data was published.
* **Idle.** The daemon then idled: 0.02 CPU s in 7 minutes. No database session was
  started after the sync (3 in all, from `init-db` and the sync).
* **SIGTERM.** The daemon stopped within a second ("stopping after the current stage"),
  and no lease was left behind.

### Review re-check, 2026-10-22

An independent review re-ran the checks on the same sandbox, partly while other test
suites ran (CPU figures are therefore upper bounds).

* **Demo, fresh database** (`init-db`, then `sync run --force`, FORECAST_PROVIDERS=demo,
  synthetic data): `success`, published, 36 s wall. All 12 stages were recorded:
  spots 2.67 CPU s, predict 16.52, detect 3.90, alerts 0.20, the rest 0.05 or less;
  ensemble and map skipped as `disabled`, prune by SYNC_PRUNE_EACH_PASS=false. The next
  `sync check` was not due, `sync run` returned `skipped` without a row, and
  `sync run --if-due` `not_due`.
* **The daemon synced a real model run by itself.** On a copy of the 10-21 database
  (stored: GFS 06Z and the map of 00Z of 10-21), with the settings above but the map
  capped to 3 map hours (`MAP_FIELD_STEPS=0-6:3`), real Open-Meteo and NOAA NODD, Celery
  and kombu imports blocked, `python -m app.cli sync daemon` started at 07:47 UTC. Its
  first check saw the GFS family's 00Z run with GFS-Wave 0.16 still on 18Z, and the map
  of 00Z waiting for that spot run; ECMWF 00Z was not served (Open-Meteo still on
  ecmwf_ifs025 18Z). The first sightings held through a check whose Open-Meteo requests
  timed out (`unknown`). At 09:18, 90 minutes after the first sighting, the daemon's
  check made the sync due and the daemon ran it: `success`, published (the map run of
  00Z), anchor cycle 00Z, 26.6 s wall and 20.6 CPU s (spots 1.79 for 10,240 point
  forecasts and 327 kB, map 8.49 for 3 map hours and 23.9 MB, predict 8.14 for 10,240
  predictions, detect 2.07, the rest 0.1). Peak RSS 559 MB.
* **Idle.** From the end of start-up (3.74 CPU s with `init-db` and the first check) to
  the sync, 89 minutes with 9 checks and 1 hourly batch cost 2.07 CPU s. The process
  opened one database session in that time, for the hourly batch, and held no
  connection between wakes.
* **Redis and HTTP per idle iteration** (counted in-process on the same database): a
  check is 3 Redis commands (GET `sync:known`, SET `sync:known`, SET `sync:heartbeat`), no
  SQL statement and 7-8 `meta.json` plus 2 `.idx` HEAD requests, 0.14-0.20 CPU s; a kick
  poll without a kick is one EVAL. At the default cadence that is about 13,000 commands a
  month for checks and 43,200 for kick polls, plus the job locks.
* **After the sync**, `sync check` made 0 SQL statements and 0 pool checkouts and only
  metadata requests (6 `meta.json`, 2 HEAD, 3.6 kB), and was not due. A forced sync
  (`sync run --force`) made no data request (14 `meta.json` and 4 HEAD, 8.4 kB), added no
  forecast run, point forecast, map field, prediction or swell event (1 `sync_runs` row
  and 12 job logs), and reported GFS and the map `current` and ECMWF `deferred`.
* **ECMWF on its own, and a refused request.** At 09:38 the daemon's check found ECMWF
  00Z servable and new while GFS and the map were current, and synced it at once.
  Open-Meteo refused the data request ("Daily API request limit exceeded", the sandbox's
  shared IP), so the sync ended `failed` in 3.1 s and 0.2 CPU s, published nothing, and
  counted one attempt: the next check showed ECMWF `waiting` until 15 minutes after it.
  A two-family blend was therefore again not measured live.
* **SIGTERM** at 09:48 (idle): "stopping after the current stage", the process ended
  2.4 s later and no lease was left running. Over its 2 hours the daemon used 27.2 CPU s:
  3.7 at start-up, 20.7 for the GFS and map sync, 0.2 for the refused ECMWF sync and
  about 2.6 for 13 checks, 2 hourly batches and the kick polls.

### Monthly CPU estimate

The Hobby daemon quota is 10 Active CPU-hours a month. This is an extrapolation, not a
measurement. It describes the full-retention cadence (a sync for every new model run); the
free-database profile (`SYNC_TRIGGER_SOURCES=map`, `MAP_FIELD_CYCLES=06,18`) runs about 60
syncs a month, each with the map layers and both spot families, instead of about 180, so its
spot-sync rows are smaller (not measured separately). It rests on these assumptions:

* the default cadence: SYNC_CHECK_MINUTES=10 and JOB_INTERVAL_FLOOR_MINUTES=60;
* 4 GFS runs a day, 2 of them map cycles with the full 61 map hours, and 2 ECMWF runs a
  day;
* one sync per new run (coalescing joins the GFS spot run and the map of the same cycle);
* GEFS off, and about 30 daemon starts a month (deployments);
* CPU per unit as measured above, using the two days' range for each value (predict
  0.77-1.62 ms per prediction);
* Vercel's 1-vCPU daemon uses as many CPU seconds as this sandbox (unverified).

| Item | Per unit | A month | CPU-h a month |
|---|---|---|---|
| Map layers, 61 map hours | 2.7-4.7 CPU s a map hour, so 165-287 s | 60 | 2.75-4.78 |
| GFS spot sync, two families (20,700 predictions: the GFS and blend runs) | 26-49 s | 120 | 0.88-1.63 |
| ECMWF spot sync, two families (14,600 predictions) | 22-39 s | 60 | 0.36-0.65 |
| Metadata checks | 0.17-0.40 s | 4,320 | 0.20-0.48 |
| Hourly periodic batch | 0.13-0.22 s | 720 | 0.03-0.04 |
| Daily tides and cleanup | 14-23 s | 30 | 0.11-0.19 |
| Daemon starts (imports, init-db, first model import) | about 10 s | 30 | 0.08 |
| Kick polls | < 1 ms measured locally (Upstash TLS not measured, 2 ms assumed) | 43,200 | < 0.03 |
| **Total** | | | **4.4-7.9** |

That is 44-79 % of the quota. The map layers are more than half of it, and their horizon
and cycles are the levers: MAP_FIELD_STEPS, MAP_FIELD_MAX_LEAD_HOURS and MAP_FIELD_CYCLES.

"One sync per new run" is the optimistic case for GFS. Open-Meteo's GFS family has four
model domains (GFS-Wave 0.16 and 0.25, GFS 0.13 and 0.25), and when the last one comes
more than SYNC_COALESCE_MINUTES after the first, the run is synced with the domains that
are there and synced again when the last one arrives. That happened in three of the
observed cycles: on 2026-10-15 06Z the domains came 5.3, 6.0 and 8.2 h after the cycle
(docs/FORECAST_SOURCES.md), on 2026-10-21 06Z GFS 0.25 came 8.7 h after it, and on
2026-10-22 00Z GFS-Wave 0.16 was still on the previous cycle 9.3 h after it, when the
daemon synced the run (above). If every GFS cycle took two spot syncs, the GFS row
would double, to 1.76-3.26 CPU-h, and the total would be 5.3-9.5 CPU-h (53-95 %).
GEFS_ENABLED=true adds the ensemble acquisition, which was not measured here. Health
warns at 80 % of SYNC_CPU_BUDGET_HOURS, from the job logs' and the heartbeat's `cpu_s`.
