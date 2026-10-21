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
  them is new for a map cycle (00Z, 12Z: `MAP_FIELD_CYCLES`) and the other is not yet, the
  sync waits up to `SYNC_COALESCE_MINUTES` (90) from the first sighting of either, then
  syncs whatever is available.
* **Point forecasts only for the surf spots.** The spot stage requests the active seeded
  spots only (`surf_spots.is_active`), one Open-Meteo request per model for up to 50 spots.
  Global grids are downloaded only for the world map layers. `init-db`, which the daemon
  runs at every start, sets `is_active` from `data/surf_spots`, so a spot is switched off
  there, not in the database.
* **Nothing is published half-way.** A sync that stops before `publish` (SIGTERM, a crash)
  leaves its data unpublished; the next check treats it as new and the next sync publishes
  it (below, "stored" means "stored and published").
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
   one (its newest `YYYYMMDDHH` cycle is earlier, e.g. a metadata cache serving the
   previous run) is never new. A map cycle whose layers are already published is current.
2. **Coalescing** (state `waiting`). A run whose model domains are on different cycles
   (Open-Meteo published GFS-Wave, GFS 0.13 and GFS 0.25 5.3, 6.0 and up to 8.2 h after the
   cycle on 2026-10-14/15, docs/FORECAST_SOURCES.md) waits for the late domains; the GFS
   spot run of a map cycle and the map layers of that cycle wait for each other. Both waits
   end SYNC_COALESCE_MINUTES (90) after the first sighting of anything new for that source
   or cycle; then whatever is available is synced. The first sighting is kept when the
   available run key changes again before a sync, so a domain that keeps changing never
   starves the wait. `SYNC_COALESCE_MINUTES=0` turns coalescing off.
3. **Triggers.** A sync is **due** when a component of a kind in SYNC_TRIGGER_SOURCES
   (default `spots,map`) is `new`. A new ensemble run (not a trigger by default) is
   acquired by the next sync anything else triggers.
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
does the work.

| Stage | Job log name | Runs when |
|---|---|---|
| `spots` | `forecasts.acquire` | a spot component is new (only those providers: `acquire_all(only=)`) |
| `ensemble` | `forecasts.acquire_ensemble` | the ensemble is new |
| `map` | `forecasts.map_fields` | the map is new |
| `blend` | `forecasts.blend` | new spot or ensemble data (stored now, or stored but unpublished) |
| `predict` | `quality.predict` | always (pending runs only) |
| `publish` | `sync.publish` | anything new was stored, or stored data is unpublished |
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
the other stages are recorded as skipped ("stopped"), the lease is released.

## The daemon (`python -m app.cli sync daemon`)

At start: `init-db` (migrate, seed, tide catalog; idempotent) when SYNC_DAEMON_INIT_DB is
true (the default; `--no-init-db` skips it), then the known runs from the database. A
start-up failure (e.g. the database still waking) is retried every minute. Then, until
SIGTERM or SIGINT:

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

```bash
cd backend
python -m app.cli sync check [--json]   # the plan (Redis + metadata; the DB only the first time)
python -m app.cli sync run              # sync every component with a newer run, no waiting
python -m app.cli sync run --if-due     # only when the rules say a sync is due (like the daemon)
python -m app.cli sync run --force      # every stage now; stored runs are still skipped
python -m app.cli sync run --only map   # limit the acquisitions (spots, ensemble, map)
python -m app.cli sync daemon [--once] [--no-init-db]
python -m app.cli sync publish          # publish what is stored now
python -m app.cli sync status [--limit 10]
python -m app.cli migrate --on-build    # migrate a production build (MIGRATE_ON_BUILD)
```

`sync run` exits 0 for success, partial or skipped, 1 for failed (or abandoned), 3 when
another sync holds the lease. To re-download a run that is already stored use
`python -m app.cli pipeline --force-acquire` or `python -m app.cli engine map-fields --force`.

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
| SYNC_TRIGGER_SOURCES | `spots,map` | component kinds whose new run makes a sync due |
| SYNC_RETRY_MINUTES | 15 | wait after a failed acquisition |
| SYNC_MAX_ATTEMPTS | 6 | failed attempts per run key before waiting for the next run |
| SYNC_LEASE_MINUTES | 45 | lease lifetime, renewed after every stage |
| SYNC_PUBLISH_GATE | true | the map index serves the published map run |
| SYNC_PRUNE_EACH_PASS | false | prune superseded predictions and map fields after every sync |
| SYNC_DAEMON_POLL_SECONDS | 60 | kick polling |
| SYNC_DAEMON_INIT_DB | true | init-db at daemon start |
| SYNC_CPU_BUDGET_HOURS | none | monthly CPU budget health warns about (10 when hosted) |
| JOB_INTERVAL_FLOOR_MINUTES | 0 | minimum interval of every periodic job (60 when hosted) |

## Redis keys

`sync:known` (stored keys, first sightings, attempts; 7 days), `sync:heartbeat` (the last
check; 6 h), `sync:kick:matching.match` and `sync:kick:flights.discover` (1 day). All are
best effort: with Redis down the daemon still syncs (it loads the stored keys from the
database), the kicks wait for Redis.

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
pool `checkout` events and every httpx response (requests and bytes by kind). Celery and
kombu imports were blocked, and none was attempted. Open-Meteo TLS handshakes through the
sandbox's proxy sometimes timed out and were retried, so wall times are long and vary. CPU
varied by up to 2x between the two days with the machine's load. Where a cell has two
values, they are the two days.

### Checks

| Check | CPU s | Database | Downloaded |
|---|---|---|---|
| First `sync check` of a process, Redis empty (the stored keys are loaded once) | 1.82, 2.86 (provider imports included) | 1 statement | 6 `meta.json` (2.2 kB) and 2 `.idx` HEAD |
| `sync check` after a sync | 0.18, then 0.40 and 0.33 | **0 statements, 0 checkouts** | the same metadata, **0 data requests** |
| Daemon idle check (3 a day) | 0.17-0.18, 0.29-0.32 | **0** | the same, 0 data requests |
| Daemon kick poll without a kick (10 a day) | < 0.01 in total | 0 | none |
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

One exception is a detection defect outside the sync. Re-running `detection.detect` over
unchanged data can merge an event into its neighbour and then recreate it, adding one
cancelled row per re-run. On 10-21 this happened at 1 of the 50 spots.

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
06:29 UTC, against the database of the first sync. A sampler read `/proc/<pid>/stat` (process CPU) and `pg_stat_database.sessions`
(client sessions started) once a minute. Results:

* **Start-up.** `init-db` and the first check took 3.75 CPU s, with an RSS of 263 MB.
* **Idle.** No sync was due in that window. The map layers of 00Z were first seen at
  05:03, and the GFS spot run of 00Z at 05:33, with its GFS 0.25 domain still on 18Z. Both
  were waiting for each other (coalescing) until 06:33, after the sample ended; the first
  sightings are in `sync:known`. ECMWF was not served. Over 2 h 41 min the daemon used
  3.62 CPU s, or 1.35 CPU s an hour: 16 checks and 2 hourly batches.
* **Database.** It opened exactly one database session per hourly batch (04:43 and 05:43
  UTC) and none between them. No connection stayed open between wakes.

### Monthly CPU estimate

The Hobby daemon quota is 10 Active CPU-hours a month. This is an extrapolation, not a
measurement, and it rests on these assumptions:

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
GEFS_ENABLED=true adds the ensemble acquisition, which was not measured here. Health
warns at 80 % of SYNC_CPU_BUDGET_HOURS, from the job logs' and the heartbeat's `cpu_s`.
