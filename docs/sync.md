# Forecast refresh pipeline

"The website must update as soon as new model data is available." The background pipeline
does not run on fixed timers. A cheap **check** reads the providers' **metadata** and finds,
per **component** of every forecast source, whether a newer model run is available; when
one is, a **refresh** fetches just that component for the 50 spots, carries the other
component forward with its original model run, validates and scores the result and
**publishes a new forecast version** in one transaction. The world map layers are refreshed
the same way but on their own (they create no forecast version). The code is
`backend/app/services/sync/`; the same refresh runs in every runner: the refresh endpoint
called by a scheduler (the hosted path on Vercel), the optional sync daemon, Celery's
`sync.tick` in the classic compose stack, the command line and the manual GitHub Actions
workflow.

```
provider metadata ──► check (per component) ──► refresh (spot-only) ──► stage version ──► publish
  meta.json, .idx       new? due? retry?          due component only      spot_runs,          one transaction,
  sentinel fingerprint  no data request,          + carry the other one   components,          version marker,
                        no database when idle     + validate every spot   timings              public cache hook
```

## What it guarantees

* **The check is free.** It reads Open-Meteo `meta.json` (not charged to the call budget),
  NOAA NODD `.idx` HEAD requests or Windy's sentinel fingerprint (providers package), never
  a forecast. With the known runs in Redis it makes **no database query**, so a
  scale-to-zero database (Neon) stays asleep while nothing is new.
* **Each component is refreshed on its own.** A spot forecast source has two components:
  `atmosphere` (10 m wind speed and direction, gust, MSLP) and `wave` (significant height,
  period, direction, the swell partitions, wind sea, sea level when present;
  `forecasts.base.COMPONENT_FIELDS`). Each has its own run key built from its own model
  domains, its own availability and its own detection state. A new wind run fetches the
  atmosphere only (the forecast API) and keeps the waves; a new wave run fetches the waves
  only (the marine API) and keeps the wind. The carried component keeps its ORIGINAL model
  run: `forecast_runs.wave_model_run_at` / `atmosphere_model_run_at`,
  `details.model_runs` (per domain, with its component), `details.component_keys`,
  `details.components` and the version's `wind_model_run` / `wave_model_run`. Carried data
  are never labelled with the new run. Scores are recomputed for the whole new run.
* **One refresh per new run of a component.** A component whose available run key is the
  published one is current; a refresh finds a run it already stored (a refresh stopped
  before publishing) and publishes it instead of fetching it again; a duplicate call (two
  runners, a scheduler retry, `--force`) finds the lease taken or nothing due.
* **Nothing is published half-way.** A refresh stages a forecast version (status `staging`)
  after acquisition, validation and scoring, and publishes it in one transaction
  (`versions.publish_version`: the published version becomes `superseded`, the staged one
  `published`). A failure after staging marks the staged version `failed` and leaves the
  previous one published. Public reads serve only the published version
  (`versions.serving_runs`).
* **A failed spot keeps its forecast.** A spot whose data failed (request error,
  missing cells, validation) keeps its previous run in the new version, marked `stale`
  with the error (redacted); it is retried alone, 5, 15 and 45 minutes later. A whole
  component that fails publishes nothing and keeps everything served.
* **Point forecasts only for the surf spots.** A refresh requests the active seeded spots
  only (`surf_spots.is_active`), in one batched request per model for all of them. Global
  grids are downloaded only for the optional world map layers.

## Components

`app.services.sync.detect.build_probes` lists the components of a plan:

| Component | Source code | What tells a new run | Present when |
|---|---|---|---|
| `spots` `wave` and `spots` `atmosphere` of the one FORECAST_PROVIDERS token (the WEATHER_PROVIDER selection) | `open_meteo_noaa` (Open-Meteo GFS family), `windy`, `demo` | the provider's `current_run(now)` with `RunInfo.component_keys`: Open-Meteo `meta.json` per model domain (wave = `ncep_gfswave016` + `ncep_gfswave025`; atmosphere = `ncep_gfs013` + `ncep_gfs025`, the domains behind `gfs_global`), used OPEN_METEO_AVAILABILITY_WAIT_MINUTES (10) after its availability time; Windy: the sentinel fingerprint of `gfsWave` / `gfs` (estimated run times); demo: the 6-hourly cycle and the simulated runs of `POST /api/dev/simulate-model-run` | one provider |
| `spots` per FORECAST_PROVIDERS token, one component per source | `open_meteo_noaa`, `open_meteo_ecmwf`, ... | the provider's whole run key | several providers (the blend path, design D5: the family-level behaviour of Upgrade 4 applies) |
| `ensemble` | `noaa_gefs_wave` | `.idx` of the last GEFS-Wave mean step | `GEFS_ENABLED=true` |
| `map` | `noaa_gfs_fields` | `.idx` of the last map hour of the map atmosphere file and `global.0p25`, map cycles only | the standalone map-fields mode (`MAP_FIELDS_ENABLED`, docs/map-layers.md) |

A component that is not configured is `disabled`. Providers that name no component keys (the
direct GRIB ingesters) are one component per source, as before.

Measured on 2026-10-27 (`meta.json` read at 11:31 and 15:25 UTC): the 06Z GFS-Wave 0.16
and 0.25 domains became available at 11:15 UTC, GFS 0.13 at 11:20 and GFS 0.25 at 11:54. So
the waves of a cycle are servable a few minutes before its wind, and the two atmosphere
domains can be more than half an hour apart: with SYNC_COALESCE_MINUTES=0 the atmosphere is
refreshed when GFS 0.13 is servable (GFS 0.25, which `gfs_global` reads for some variables,
still on the previous cycle and recorded so in `details.model_runs`) and again when GFS 0.25
follows. The research behind the timings (Open-Meteo publishes GFS-Wave about 5.3 h and the
GFS atmosphere 5.6-7.1 h after the cycle; its servers are "eventually consistent", hence the
10-minute wait after `last_run_availability_time`; https://open-meteo.com/en/docs/model-updates)
is in docs/FORECAST_SOURCES.md.

## The rules (a plan, `detect.plan_sync`)

Per component the plan compares the provider's newest servable run with the **stored**
run key: the published forecast version's component run (`forecast_versions.components`)
for its source; for another source, or before any version, the newest usable run that
completed before the newest publication (`detect.load_known`; simulated demo runs and retry
runs are ignored).

1. **New.** The component's available key differs from the stored one. Detection is
   monotonic: a key whose newest `YYYYMMDDHH` cycle is older than the stored one, or whose
   domains are the stored ones with none newer and one older (a lagging Open-Meteo server),
   is never new. A metadata timeout or error makes the component `unknown`: never due,
   never raised, never "nothing new". Per component: when only the marine host's
   `meta.json` fails, the wave component is `unknown` and the atmosphere is planned from its
   own metadata (`current_run(partial=True)` returns an `open_meteo.PartlyReadRun` naming the
   `unread` component; with several providers a source stays one component and is `unknown`).
   A refresh then fetches the atmosphere and carries the waves with their published run; a
   spot whose served run lacks them fails and keeps its run, and with no wave run published
   at all (the first refresh) the refresh fails, publishes nothing and is retried. Every
   `meta.json` request, in a check or a refresh, waits at most 10 s
   (`open_meteo.META_TIMEOUT_SECONDS`, 3 attempts), so a hanging host costs a refresh that
   carries its component about half a minute.
2. **Servable.** Open-Meteo labels a domain with its previous run until 10 minutes after
   the new run's availability. While one component's domains are being published, that
   component is `waiting` ("not served yet", `detect.PartlyServedRun`) and the other one is
   planned as usual.
3. **Coalescing within a component** (state `waiting`). A component whose model domains
   are on different cycles (GFS 0.13 published, GFS 0.25, from which `gfs_global` takes
   some variables, not yet) waits for the late domain up to SYNC_COALESCE_MINUTES after its own
   first sighting; then whatever is available is refreshed. The default is **0**: a
   component is due as soon as any of its domains advanced. There is no waiting across
   components (wave and atmosphere, or the spots and the map layers).
4. **Triggers.** A plan is due when a component of a kind in SYNC_TRIGGER_SOURCES (default
   `spots,map`; `spots` in the free-database profile) is `new`. Each runner then limits the
   plan to the kinds it syncs (`detect.limit_plan`): the refresh endpoint `spots`, the
   daemon SYNC_DAEMON_KINDS. When none of a runner's kinds is a trigger, its own kinds
   trigger.
5. **Component retries.** A component whose acquisition failed is retried
   SYNC_RETRY_MINUTES (15) after the attempt, or later when the provider asked for a longer
   wait (`retry_after`); state `waiting` meanwhile. After SYNC_MAX_ATTEMPTS (6) failed
   attempts for one run key it is `failed` until the provider offers the next run.
6. **Spot retries** (design D9). When the published version has spots that failed
   (`stale` or `missing` with an error), the component that refresh fetched carries a retry
   of those spots (`retry_spot_ids`): `waiting` for 5 minutes after the refresh, then `new`;
   each retry that does not recover every spot waits longer: 15, then 45 minutes
   (`types.SPOT_RETRY_BACKOFF_MINUTES`), at most SYNC_MAX_ATTEMPTS retries per run
   (`failed` after that). The retries are counted in the database (the retry runs,
   `<run key>#retry<n>`), so every runner sees the same state. A newer run of the component
   supersedes the retry: it refreshes every spot anyway.
7. **One sync at a time.** A sync that holds a valid lease makes new components
   `in_progress` and the plan not due (with the database); without it the next sync attempt
   gets SyncBusy.
8. **Force** (`sync run --force`) is always due and acquires every configured component;
   the acquisitions themselves still skip runs already stored.

States: `current`, `new`, `waiting`, `in_progress`, `failed`, `unknown`, `disabled`
(app.services.sync.types). The known runs (stored keys per component, first sightings,
attempts, the spot retry) are kept in Redis (`sync:known`, 7 days), so a check needs only
Redis and the providers; they are reloaded from the database before and after every sync.

## One refresh (the spot stage, `refresh.refresh_spots`, `ingestion.refresh_source`)

1. **Target.** The fetched components take the provider's available run, the others keep
   the published one: target keys `{wave, atmosphere}`, run key `"<wave key>|<atmosphere
   key>"` (Open-Meteo's own format; one key when both are the same, as for the plain demo
   run). A retry stores its run as `<run key>#retry<n>`.
2. **Per spot.** A carried component is copied from the run the spot is served from
   (`versions.serving_runs`) when that run holds the carried run key; otherwise the spot
   fetches that component too, when the provider still serves exactly that run, or fails
   (and keeps its previous run). Spots are fetched in at most three groups by the components
   they need (normally one: the due components for every spot), each one batched request
   per model: for Open-Meteo the marine request for `wave`, the forecast request for
   `atmosphere`.
   **The rolling horizon.** Carried as is, the forecast would never reach past the last
   refresh that fetched both components, while a provider's window rolls on (Open-Meteo: a
   request covers FORECAST_DAYS from today 00:00 GMT, so the same run read on a later UTC
   day reaches up to 24 h further). So a spot whose served run ends more than HORIZON_SLACK
   (3 h, one GFS step after 120 h) before `provider.horizon_end` (where both component runs
   end when fetched now; for Open-Meteo the component model's last native step) fetches the
   carried component again, **from the same run** it is carried from, in the due
   component's request (`details.components[c].refetched_spots`). With Open-Meteo GFS that
   happens on about one or two component refreshes a day (when the UTC window moved past
   the carried run). It is a deliberate trade-off against "no queries for unchanged runs"
   (spec 8): the horizon (non-negotiable 8) needs it, and it costs those extra calls
   (README F.4). The refetch is best effort (review r3-backend-R3-1): when its request
   fails (a marine 5xx, a timeout, the call budget), or one spot's carried data fail, those
   spots are fetched again once with the due components alone and keep the carried run with
   its own end; when the provider does not serve the carried run now (its domain lags, a
   newer run is being published), the refetch is dropped before any request
   (`details.components[c].refetch_failed_spots`, and a warning). A failed refetch never
   holds back the new run of the due component.
3. **Store, then carry in the database.** The fetched rows are inserted with the other
   component's columns empty; one `UPDATE ... FROM` (`forecasts.carry.carry_component`)
   fills them from the source run, matched on spot and valid time, server-side (no forecast
   row travels through Python). An hour the source run does not have is deleted
   (`carry.drop_uncarried_hours`), never published half filled: scored without waves it
   would read as flat water. The two components' runs can step differently (GFS and
   GFS-Wave are hourly to 120 h, 3-hourly after), and a carried run that ends sooner cuts
   the new run to its end (until the next refetch above).
4. **Validate every spot** (`carry.validate_spots`, one aggregate statement): at least
   FORECAST_MIN_VALID_FRACTION (0.8) of the run's hours carry both a wave height and a wind
   speed, and no value is outside `carry.PHYSICAL_RANGES` (heights 0-30 m, periods 0-40 s,
   directions 0-360, wind 0-400 km/h, gusts 0-500, MSLP 850-1100 hPa). A spot that fails is
   removed from the run (its rows deleted) and keeps its previous run.
5. **Outcome.** No valid spot: the run is `failed` (a provider or whole-component
   failure), nothing is published, the component counts a failed attempt. Some spots failed:
   the run is `partial`, the sync `partial`. A provider answering with Windy TESTING data
   (`RunInfo.testing`) is never stored.

Then `predict` scores the new run (the whole run), and `publish` stages and publishes its
version.

## Versions and publication

The publish stage (`orchestrator._publish_body`), in this order:

1. **One version per stored refresh** (`refresh.stage_refresh_version`): `spot_runs` names the
   spots the refresh handled (updated ones point at the new run; failed ones keep their
   previous run as `stale` with the redacted error); every other spot is carried over from
   the published version, so a retry of three spots updates just those three.
   `components` are the refresh's `ComponentRun`s (a carried one with its original run key,
   init and retrieval time, `changed: false`, `carried_from_run_id`; a fetched one with its
   model's `update_interval_seconds` from Open-Meteo's `meta.json`, the longest of its
   domains, which the staleness rule of design D10 reads); a retry keeps the published
   version's components. A component's init time, the version's `wind_model_run`
   or `wave_model_run`, is its OLDEST domain's (`versions.ComponentRun.from_model_runs`):
   with GFS 0.13 on 12Z and GFS 0.25 still on 06Z the atmosphere is labelled 06Z, the cycle
   of its gusts and MSLP, and the run's `details.model_runs` names each domain's cycle. Timings: `detected_at` (the component's first
   sighting), `upstream_available_at` (the newest availability of the changed components),
   `acquired_at`, `scored_at`, `staged_at`, `published_at`, and `timings` (`kind`:
   refresh, retry or selection; the stage durations and CPU so far).
2. **Publish it** (`refresh.publish_staged` → `versions.publish_version`): one transaction
   under an advisory lock; a version with zero updated spots is refused. On any error the
   version is marked `failed` and the previous one stays published; the stage then fails
   and the sync is `failed`.
3. **The map pointer** (`publish.publish`): `sync_runs.published_at`, `map_run_id` (the best
   servable map run) and `blend_run_id`. Map layers create no version.
4. **The public cache hook**: `public_cache.note_published(sync_run_id)` as before (and
   once more at the end of the sync, after detection).
5. **The version marker**: `versions.write_version_marker(versions.version_payload(...))`
   with the published map run, so the version endpoint answers from Redis. It is written
   after the `match` stage (or at the end of a sync stopped before it), once `detect` has
   re-evaluated the swell events of the new version: the browsers switch to the new version
   when its event and opportunity lists are ready, so nothing fetched (and cached) for the
   new version holds the previous version's events. This adds the `detect` and `match`
   time, 1.1-1.8 s in the demo measurements below, to the publication-to-browser latency.
   Outside a sync (`sync publish`, the developer pipeline) `publish.publish` writes it.

With several providers (the blend path) the version is the run selection of the public
reads (`refresh.publish_selection_version`: one run per spot, `queries.latest_runs_by_spot`,
`timings.kind = "selection"`). So is the version of the developer pipeline
(`python -m app.cli pipeline`, `POST /api/dev/run-pipeline`) and of `sync publish`: what they
store is served as before.

`detect`, `match`, `flights`, `alerts` and `deliver` run after a publication; `prune` too,
with SYNC_PRUNE_EACH_PASS.

## The stages

| Stage | Job log name | Runs when |
|---|---|---|
| `spots` | `forecasts.acquire` | a spot component is new or has a due spot retry (one provider: `refresh.refresh_spots`; several: `acquire_all(only=)`) |
| `ensemble` | `forecasts.acquire_ensemble` | the ensemble is new |
| `map` | `forecasts.map_fields` | the map is new (never on the refresh endpoint) |
| `blend` | `forecasts.blend` | several providers, FORECAST_BLEND_ENABLED, and new or unpublished member data (one provider: skipped, "nothing to blend", without importing the blend's data libraries) |
| `predict` | `quality.predict` | always (pending runs only); failed, not skipped, when another holder has its lock |
| `publish` | `sync.publish` | anything new was stored, or stored data is unpublished; never after a failed `predict` unless forced |
| `detect`, `match`, `flights`, `alerts`, `deliver` | `detection.detect`, `matching.match`, `flights.discover`, `alerts.generate`, `notifications.deliver` | after a publication |
| `prune` | `maintenance.prune_pass` | after a publication, with SYNC_PRUNE_EACH_PASS=true |

Each stage runs through `app.jobs.runner.run_job` (one `background_job_logs` row under the
job name above, with `cpu_s`, and the job's Redis lock); `sync_runs.stages` holds one entry
per stage, `{name, status, started_at, duration_ms, cpu_s, detail}`. An acquisition's
`detail.components` lists each planned component's outcome (`stored`, `current`, `deferred`,
`failed`; with `component` and that component's run key) and the spot stage's
`detail.refreshes` each refresh (`run_id`, `changed`, `spots_updated`, `failed_spot_ids`,
`retry`). The lease, the lease keeper, `stop` and the statuses (`success`, `partial`,
`failed`, `skipped`, `abandoned`) are as in Upgrade 4: the lease is a `sync_runs` row with
status `running` (unique partial index `uq_sync_runs_running`), renewed after every stage
and extended every third of SYNC_LEASE_MINUTES while a stage runs; an expired lease is
marked `abandoned` and taken over. A sync the refresh endpoint runs (trigger `http`) holds a
lease of at most 10 minutes (`orchestrator.HTTP_LEASE_MINUTES`): the platform stops a
Function at its maxDuration (300 s), and the lease of a stopped Function then expires
within about 13 minutes instead of SYNC_LEASE_MINUTES (45), so the scheduler's next checks
are not answered `busy` for long.

## Runners

| Runner | Kinds | How often | Notes |
|---|---|---|---|
| **`GET\|POST /api/internal/refresh`** (Bearer CRON_SECRET) | `spots` only, never the map/GRIB stage | every 5 minutes from a scheduler (below) | the hosted path: runs inside the API Function (maxDuration 300 s) |
| `GET /api/internal/cron/daily` (Vercel Cron, daily) | `spots` | once a day | after the watchdog, one refresh check: the backstop when the scheduler stops |
| sync daemon (`python -m app.cli sync daemon`) | SYNC_DAEMON_KINDS (default `spots,ensemble,map`; hosted profile `map`) | every SYNC_CHECK_MINUTES (10) | optional on Vercel (Public Beta, 5 Active CPU-hours a month on Hobby) |
| Celery `sync.tick` (classic compose) | all | every SYNC_CHECK_MINUTES | |
| `python -m app.cli sync run` / GitHub Actions `data-sync.yml` | all (or `--only`) | by hand | |

**The refresh endpoint** answers 404 without CRON_SECRET (the same body as an unknown route),
401 for a wrong token (constant-time comparison), and otherwise 200 with
`Cache-Control: private, no-store` and a JSON summary:

```json
{"status": "success", "due": true, "plan": {"components": [...], "reasons": [...]},
 "sync_run_id": 12, "version_ids": [7], "published_at": "2026-10-27T17:31:04+00:00",
 "stages": [{"name": "spots", "status": "success", "duration_ms": 2140, "cpu_s": 1.9}, ...],
 "refreshes": [{"source": "open_meteo_noaa", "changed": ["wave"], "spots_updated": 50,
                "failed_spot_ids": [], "run_id": 31, "retry": null}],
 "timings": {"check_ms": 412, "sync_ms": 20512, "total_ms": 20924, "cpu_s": 14.2}}
```

(the numbers in this example are illustrative). `status` is `disabled` with the kill switch,
`not_due` when nothing is new (the check read no database), `busy` when another sync holds
the lease (still 200: a scheduler must not retry into a storm; the next check comes anyway),
else the sync's status (`success`, `partial`, `failed`, `skipped`). An unexpected error
answers 503 `{"status": "error"}`. The route is exempt from the CSRF check (it is
authenticated by its own secret, like the webhooks). Calls are idempotent: a repeated or
concurrent call finds nothing due or the lease taken.

**The kill switch.** `FORECAST_REFRESH_ENABLED=false` stops every runner's automatic
refreshes: the refresh endpoint and the daily backstop answer `disabled`, Celery's tick
answers `disabled`, `sync run` (also with `--force`) prints `{"status": "disabled"}` and
exits 0, and the daemon keeps checking (heartbeat note "refreshes disabled") but never
syncs. The data already published stays served.

**The developer simulation** (contract C6, only with ENABLE_DEV_ENDPOINTS and the demo
provider): `POST /api/dev/simulate-model-run?component=wave|atmosphere|both` (signed in)
advances the demo provider's available run of that component by one simulated model run (a
counter per component in Redis, `sync:demo:advance`, so the planner of every process sees
it). The new run's values differ visibly and deterministically (wave heights x1.35, x0.75,
x1.2, x0.85 and periods +2, -1.5, +1, -1 s; wind +12, -6, +8, -4 km/h and turned 45, -30,
90, -60 degrees, by advance; hours of a simulated swell keep their values). The next refresh
check detects it, fetches that component (and the other one again from its same run when
the horizon needs it, step 2 of "One refresh") and publishes a new version.

## The daemon (`python -m app.cli sync daemon`)

Optional on Vercel (the spot forecasts come from the refresh endpoint), the runner of the
local Vercel-style stack. At start: `init-db` (migrate, seed, tide catalog; idempotent) when
SYNC_DAEMON_INIT_DB is true (`--no-init-db` skips it), the known runs from the database, then
the start-up catch-up (SYNC_DAEMON_CATCH_UP): every periodic job that has not succeeded within
its longest gap runs once before the first check (a job started within the last hour is
skipped, so a restart loop does not ask a provider again at every start). A start-up failure
(e.g. the database still waking) is retried every minute. Then, until SIGTERM or SIGINT:

* every SYNC_DAEMON_POLL_SECONDS (60): the API's kicks from Redis (`sync:kick:*`, one Lua
  call): "match my search" runs `matching.match` and the stages after it, "refresh flights"
  `flights.discover` for the matches asked for;
* every SYNC_CHECK_MINUTES (10): the plan from the known runs, limited to SYNC_DAEMON_KINDS
  (`detect.limit_plan`), and the heartbeat in Redis (`sync:heartbeat`); when due, one sync of
  those kinds (`run_sync(only=...)`), then the known runs are reloaded. With the kill switch
  the check still runs (heartbeat note "refreshes disabled") but nothing syncs;
* the periodic jobs of `app.jobs.schedule.build_schedule` except the sync's own: `Every` jobs
  on in-memory timers at least JOB_INTERVAL_FLOOR_MINUTES apart (60 when hosted), so they run
  back to back in one database wake an hour; `DailyAt` jobs (tides, cleanup) by the UTC clock.

An idle iteration reads only Redis and the providers' metadata (zero SQL statements and pool
checkouts, `tests/integration/test_sync_daemon.py`), and the connection pool is emptied after
every wake, so a scale-to-zero database can suspend. The metadata requests of a check use a
10 s HTTP timeout (`detect.PROBE_TIMEOUT_SECONDS`); a provider that does not answer is
`unknown` until the next check. The heartbeat's `cpu_s_month` is this month's daemon CPU
outside job runs; job logs plus the heartbeat count every CPU second once
(SYNC_CPU_BUDGET_HOURS, 5 when hosted: the Hobby daemon quota).

## Scheduling options

Vercel's limits decide the runner on each plan (pages read on 2026-10-27; the quotes are
from the pages linked):

* **Vercel Cron on Hobby runs once a day** with per-hour precision ("Hobby ... Once per day
  ... Per-hour (±59 min)"), and "Hobby accounts are limited to daily cron jobs. This cron
  expression would run more than once per day." fails the deployment
  (https://vercel.com/docs/cron-jobs/usage-and-pricing). `vercel.json` therefore keeps only
  the daily watchdog cron, which also runs one refresh check as a backstop. Cron does not
  retry and can deliver twice; both are safe here.
* **Recommended on Hobby: Upstash QStash** calling the refresh endpoint every 5 minutes.
  Free: 1,000 messages a day (a 5-minute schedule is 288; retries and every trigger count),
  10 active schedules, and it waits up to 15 minutes for the response, so a 300 s refresh
  fits (https://upstash.com/pricing/qstash). Headers are forwarded when prefixed:
  "Simply add them prefixed with Upstash-Forward- and we will include them in the message."
  (https://upstash.com/docs/qstash/howto/publishing). A schedule:

  ```bash
  curl -XPOST -H "Authorization: Bearer $QSTASH_TOKEN" \
    -H "Upstash-Cron: */5 * * * *" \
    -H "Upstash-Forward-Authorization: Bearer $CRON_SECRET" \
    "https://qstash.upstash.io/v2/schedules/https://<your-app>.vercel.app/api/internal/refresh"
  ```

  ("To create a schedule, you simply need to add the Upstash-Cron header to your publish
  request.", https://upstash.com/docs/qstash/features/schedules.) QStash retries a failed
  delivery 3 times by default; the endpoint answers 200 for busy and failed refreshes, so
  only real errors (503) are retried. QStash also signs each request (Upstash-Signature); the
  endpoint checks the bearer secret only.
* **Alternative: cron-job.org**, free and down to once a minute with custom headers, but "If
  your URL/script does not finish after 30 seconds, it will timeout and we will close the
  connection" (https://cron-job.org/en/faq/): whether the Function finishes a refresh longer
  than 30 s after the caller hung up is **not verified**, so it is documented as unverified
  for refreshes that fetch and score (a check with nothing due answers in well under a
  second, measured below). It auto-disables a job after more than 25 consecutive failures.
* **On Vercel Pro**, Cron runs once a minute with per-minute precision: add
  `{"path": "/api/internal/refresh", "schedule": "*/5 * * * *"}` to `crons` in
  `vercel.json` (Vercel sends CRON_SECRET as the Authorization header). This entry fails a
  Hobby deployment, so it is not in the repository's `vercel.json`.
* **The Vercel Daemon** is optional: Hobby daemons get "5 Active CPU-hours per month" and are
  stopped until the 1st of the next month when the quota runs out
  (https://vercel.com/docs/daemons/limits). The hosted profile sets SYNC_DAEMON_KINDS=map,
  so the daemon only refreshes the optional GRIB map layers (MAP_FIELD_CYCLES 06,18); without
  it the map overlay ages and is labelled stale while the spot forecasts keep refreshing
  through the endpoint. The daemon's other work has stand-ins
  (`app/services/sync/standin.py`): the refresh endpoint takes the API's kicks ("Re-check
  flights", a saved search to match; one Redis EVAL when none is pending) and a due
  notification retry (one Redis GET), and the daily cron runs every periodic job whose
  newest run is older than its own interval (maintenance cleanup, matching housekeeping,
  tides, alerts, delivery, the staleness check; within 150 s, before its refresh backstop).
  Data health then reports a missing daemon heartbeat only as degraded, for the map layers
  (`tests/integration/test_daemonless_hosted.py`). That problem, and `map_fields_missing`,
  last for as long as no daemon runs and the admins are reminded of them every 12 h: without
  a daemon set MAP_FIELDS_ENABLED=false (the map shows the spot markers only, with a "Spot
  forecasts only" note and no layer tabs, and data health stays `ok`; reviews r3-spec-SPEC-1
  and r4-spec-R4-SPEC-2), or keep the daemon.
* **GitHub Actions** schedules (5-minute minimum, delayed or dropped runs, the "serverless
  application" clause of the terms) stay out: `data-sync.yml` is manual only.

## Commands

From `backend/` (`cd backend`):

| Command | What it does |
|---|---|
| `python -m app.cli sync check`, with `--json` for JSON | the plan (Redis and provider metadata; the database only the first time) |
| `python -m app.cli sync run` | sync every component with a newer run (and a due spot retry), no waiting |
| `python -m app.cli sync run --if-due` | only when the rules say a sync is due |
| `python -m app.cli sync run --force` | every stage now; stored runs are still skipped |
| `python -m app.cli sync run --only spots` | limit the acquisitions (spots, ensemble, map) |
| `python -m app.cli sync daemon`, with `--once` and `--no-init-db` | the daemon |
| `python -m app.cli sync publish` | publish what is stored now (the run selection as a version) |
| `python -m app.cli sync status`, with `--limit 10` | the newest syncs and their stages |
| `curl -X POST -H "Authorization: Bearer $CRON_SECRET" https://<host>/api/internal/refresh` | one refresh check through the API |

`sync run` exits 0 for success, partial, skipped and disabled, 1 for failed (or abandoned), 3
when another sync holds the lease. Against a production database only with the Production
environment loaded (docs/operations.md): with only `DATABASE_URL` set, a sync stores
synthetic demo forecasts and publishes them.

To re-download a run that is already stored use `python -m app.cli pipeline --force-acquire`
or `python -m app.cli engine map-fields --force`. `engine map-fields` stores map layers
without publishing them: with SYNC_PUBLISH_GATE the map keeps showing the published run
until the next sync or `sync publish` (`pipeline` publishes on its own).
`python -m app.cli migrate --on-build` (the API's build command) reads MIGRATE_ON_BUILD:
`off` (default) never migrates, `production` only when VERCEL_ENV=production, `all` every
build; it prints `skipped`, `migrated` or `failed` (exit 1: the build fails rather than
deploying against an old schema).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| FORECAST_REFRESH_ENABLED | true | the kill switch of every runner |
| CRON_SECRET | none | the bearer secret of `/api/internal/*` (at least 16 characters); the routes are 404 without it |
| SYNC_CHECK_MINUTES | 10 | metadata checks of the daemon and of Celery's `sync.tick` (the endpoint: the scheduler's cadence) |
| SYNC_COALESCE_MINUTES | 0 | how long a component waits for its own late model domains (no wait across components) |
| SYNC_TRIGGER_SOURCES | `spots,map` (free-database profile: `spots`) | component kinds whose new run makes a sync due |
| MAP_FIELD_CYCLES | `00,12` (free-database profile: `06,18`) | the map cycles read (docs/map-layers.md) |
| SYNC_DAEMON_KINDS | `spots,ensemble,map` (hosted profile: `map`) | the kinds the daemon syncs |
| SYNC_RETRY_MINUTES | 15 | wait after a failed component acquisition |
| SYNC_MAX_ATTEMPTS | 6 | failed attempts per run key (and spot retries per run) before waiting for the next run |
| FORECAST_MIN_VALID_FRACTION | 0.8 | share of a spot's hours that must carry a wave height and a wind speed |
| OPEN_METEO_AVAILABILITY_WAIT_MINUTES | 10 | wait after Open-Meteo's availability time before a run is read |
| SYNC_LEASE_MINUTES | 45 (15 under JOB_RUNNER=daemon) | lease lifetime |
| SYNC_PUBLISH_GATE | true | the map index serves the published map run |
| SYNC_PRUNE_EACH_PASS | false (hosted: true) | prune after every publication |
| SYNC_PREWARM_PUBLIC_CACHE | false | after a publication, compute the public spot list and conditions at once |
| SYNC_DAEMON_POLL_SECONDS, SYNC_DAEMON_INIT_DB, SYNC_DAEMON_CATCH_UP | 60, true, true | the daemon's kick polls, init-db and start-up catch-up |
| SYNC_CPU_BUDGET_HOURS | none (hosted: 5) | monthly daemon CPU budget health warns about |
| JOB_INTERVAL_FLOOR_MINUTES | 0 (hosted: 60) | minimum interval of every periodic job the daemon runs |

## Retention

`maintenance.prune_forecast_runs` (daily cleanup and every prune stage) never deletes a run
that the published version, the previous published version (the newest superseded one) or a
staging version references: their `forecast_run_id` and every run their `spot_runs` serve,
so a stale spot never loses its previous run (`maintenance.version_run_ids`). The superseded
member predictions of those runs are kept too. Superseded and failed versions older than
RETENTION_FORECAST_DAYS are deleted, except the previous published one
(`maintenance.prune_versions`).

## Redis keys

`sync:known` (stored keys per component, first sightings, attempts, the spot retry; 7 days),
`sync:heartbeat` (the daemon's last check; 6 h), `sync:refresh-check` (the last check of the
refresh endpoint or the daily backstop: when, caller, status, sync run, versions and the
plan's components with their state and detail; 7 days; for the scheduler-silence alert and,
without a daemon, the provider-unavailable alert), `sync:kick:matching.match` and `sync:kick:flights.discover` (1
day), `sync:demo:advance` (the simulated demo runs, developer tool). The version marker and
the first-served and seen-latency keys are under the public cache namespace
(`<ns>:forecast-version:*`, app.services.forecasts.versions). Reading and writing them is
best effort; a sync itself needs Redis for its job locks (as in Upgrade 4).

Measured on 2026-10-22 (Upgrade 4) with Redis refusing connections (demo provider,
`sync run --force`): the spots, blend, predict and publish stages failed on their job lock
(`ConnectionError`) in 0.4 s, nothing was published, and the command exited 1. Each such sync
counts a failed attempt per due component, so an outage longer than SYNC_MAX_ATTEMPTS
attempts SYNC_RETRY_MINUTES apart leaves the run `failed` until the provider offers the next
one (once Redis is back, `sync run` acquires it at once).

## Measured (2.0)

On this development sandbox (4 vCPUs shared with another agent's work, local PostgreSQL 16
and Redis), 2026-10-27. Wall times are the caller's, CPU is the serving process's user plus
system time (`/proc/<pid>/stat` or `getrusage`), the stage figures are the `duration_ms` and
`cpu_s` the endpoint returns. FORECAST_DAYS=10 (240 hourly steps, 12,000 point forecasts for
50 spots).

**Demo provider through `POST /api/internal/refresh`** (synthetic data, a fresh scratch
database after `init-db`, `uvicorn` on localhost and an HTTP client sending the bearer
secret; the next refreshes after `POST /api/dev/simulate-model-run?component=wave`, then
`atmosphere`, signed in; 1-minute load average 0.9-1.0):

| Call | Wall s | Server CPU s | spots | predict | detect | publish | rest |
|---|---|---|---|---|---|---|---|
| first refresh, both components | 12.24 | 9.71 | 1.90 s / 1.62 CPU | 8.95 / 7.18 | 1.04 / 0.65 | 0.05 / 0.03 | 0.13 / 0.08 |
| wave-only refresh | 12.26 | 8.48 | 2.22 / 1.71 | 8.57 / 5.86 | 1.26 / 0.79 | 0.04 / 0.02 | 0.07 / 0.03 |
| atmosphere-only refresh | 12.51 | 8.77 | 2.38 / 1.75 | 8.69 / 6.10 | 1.25 / 0.80 | 0.04 / 0.02 | 0.06 / 0.03 |
| check with nothing new | 0.006 | < 0.01 | | | | | |

Every refresh updated all 50 spots and published one version; the check made no database
query (it read the known runs from Redis) and answered `not_due`. A spot-only refresh uses
about 4 % of the 300 s Function limit; the predict stage is two thirds to three quarters of
it.

**Database egress of one spot-only refresh** (the counting TCP proxies of
`scripts/measure_db_egress.py` between the app and PostgreSQL and Redis, the same refreshes
through the route in process with FastAPI's TestClient:
`backend/.venv/bin/python scripts/measure_refresh_egress.py --redis-url
redis://localhost:6379/12`; wall 12.1-13.5 s and 9.7-10.9 CPU s per refresh in that
process):

| Refresh | DB -> app | app -> DB | Redis | of which DB -> app: spots, predict, detect, match, publish |
|---|---|---|---|---|
| first, both components | 8.30 MB | 58.6 MB | 41 commands, 5.9 kB | 0.53, 5.09, 2.43, 0.19, 0.03 MB |
| wave-only | 9.27 MB | 59.5 MB | 41 commands, 5.6 kB | 0.54, 5.79, 2.70, 0.16, 0.05 MB |
| atmosphere-only | 9.45 MB | 61.3 MB | 41 commands, 5.7 kB | 0.55, 5.80, 2.84, 0.19, 0.05 MB |
| check, nothing new | 0 | 0 | 4 commands, 0.8-1.0 kB | |

A check here is 4 Redis commands: GET and SET `sync:known`, SET `sync:refresh-check` and,
for the demo provider only, HGETALL `sync:demo:advance`. A 5-minute schedule makes about
26,000-35,000 commands a month, against Upstash Free's 500K (docs/map-layers.md); a refresh
adds about 41.

The carried component never travels through Python (the spots stage's 0.5 MB is the spot
list, the served runs' details and the validation aggregates); what a refresh reads back is
the scoring (predict) and the swell detection of the whole new run. Per new component run
that is about 9.5 MB: at 4 GFS cycles a day with the wave and the atmosphere refreshed
separately, about 2.3 GB a month (8 refreshes a day; 3.4 GB if the late GFS 0.25 domain makes
a third refresh per cycle), against Neon Free's 5 GB (docs/map-layers.md). These are demo
figures; the scoring stage is the lever (and SYNC_COALESCE_MINUTES, which lets the
atmosphere wait for its late domain).

**Live Open-Meteo** (a clone of a real-data database, migrated to 0009,
WEATHER_PROVIDER=open_meteo, 2026-10-27 and 28; `orchestrator.refresh_check`, what the
refresh endpoint runs, called every 2 or 5 minutes in one process with every HTTP request
counted; the sandbox reaches Open-Meteo through a proxy, and some TLS handshakes timed out):

* 15:25 UTC, first refresh (the clone's runs were 11 days old, so both components were new:
  the 06Z run): 20.1 s wall, 10.4 CPU s; 3 data requests (2 marine, 1.02 MB; 1 forecast,
  0.39 MB) and 20 `meta.json` (12 kB), all HTTP 200; spots 5.97 s / 0.95 CPU s, predict
  9.95 / 7.72, detect 1.57 / 0.90, publish 0.22 / 0.20; 50 spots updated, one version.
* Checks every 2 minutes after it: 4 `meta.json` each, no data request, 0.02 CPU s,
  1.9-2.3 s wall (one took 13.3 s through the sandbox's proxy), `not_due`.
* 20:27 UTC, the 12Z run: GFS-Wave 0.16 and GFS 0.13 were on 12Z, GFS-Wave 0.25 and GFS
  0.25 still on 06Z (`meta.json`), so both component keys had changed: 43.6 s wall, 9.3 CPU
  s; 2 marine requests (1.02 MB), 1 forecast request (0.39 MB), 20 `meta.json`, all HTTP
  200; spots 11.6 s / 0.88 CPU s, predict 24.7 / 6.78; one version. Both components are
  labelled 06Z (their oldest domain) and `details.model_runs` names each domain's cycle.
* 27 checks every 5 minutes from 20:32 to 22:46 UTC, all `not_due` (the two 0.25 domains
  stayed on 06Z): 26 read 4 `meta.json` each, no data request, 0.02-0.03 CPU s, 1.2-14.4 s
  wall. At 21:29 one `meta.json` request timed out three times (36 s): that check found the
  source `unknown` (both components: the provider then read its domains' metadata
  together; since the second review only the component whose `meta.json` failed is
  `unknown`), refreshed nothing and raised nothing, and the next check was normal.
* 2026-10-28 01:20 UTC, the 18Z run, a **wave-only refresh**: both GFS-Wave domains were
  servable on 18Z, while GFS 0.25 had become available at 01:18 (read from 01:28), so the
  atmosphere was `waiting` ("not served yet: Open-Meteo is publishing ncep_gfs025 run
  2026-10-27 18Z ...") and was carried. 2 marine requests (1.02 MB), no forecast request,
  20 `meta.json`. The version: `wave_model_run` 18Z, `wind_model_run` 06Z (the carried
  atmosphere keeps its key `gfs013:2026102712|gfs025:2026102706`, `changed: false`); its run
  stores `wave_model_run_at` 18Z and `atmosphere_model_run_at` 06Z. 59.9 s wall, 9.0 CPU s:
  spots 8.4 s / 0.81 CPU s, predict 46.0 / 6.84 (the sandbox had restarted 8 minutes
  before, so the database cache was cold, and another agent's end-to-end tests ran
  alongside), publish 0.23 / 0.20, detect 2.2 / 0.68.
* 01:24 and 01:27 UTC, two checks during the race the 10-minute wait is for: the plan's
  `meta.json` already showed GFS 0.25 on 18Z as servable, the acquisition's (read seconds
  later) did not ("Open-Meteo is publishing ncep_gfs025 run 2026-10-27 18Z (available 01:18
  UTC); it is read 10 min after that"). Both syncs deferred the atmosphere (status
  `skipped`: no data request, nothing stored or published, no failed attempt); 12
  `meta.json` each, 15-36 s wall, 0.1 CPU s.
* 01:29:57 UTC, an **atmosphere-only refresh**: 1 forecast request (0.39 MB), no marine
  request, 20 `meta.json`. The version: `wind_model_run` 18Z, `wave_model_run` 18Z carried
  from the wave-only run (`changed: false`, `carried_from_run_id` that run);
  `detected_at` 01:24:20 (the first sighting), `upstream_available_at` 01:18:15,
  `published_at` 01:29:56. 46.1 s wall, 4.7 CPU s: spots 37.6 s / 0.70 CPU s (the requests
  through the sandbox's proxy), predict 5.6 / 3.29, detect 1.0 / 0.61.
* The carried values, compared in the database row by row (same spot and valid time): the
  wave-only run's wind, gust and MSLP equal the previous run's in all 7,128 matching rows
  (its last 622 rows, past the end of the previous run, have no wind and list it in
  `missing_fields`); the atmosphere-only run's waves equal the wave-only run's in all 7,750
  rows, while its wind differs in 7,710 of them.

Every live refresh fitted the 300 s Function limit (the slowest took 60 s).

**Review re-check, 2026-10-28** (same sandbox, 1-minute load average 2.8-3.3 because
another agent's test suites ran alongside; after the review fixes: the refresh check asks
only the spot providers, and the version marker is written after `match`):

* The demo route measurement again (`measure_http.py` of the builder, scratch database and
  Redis): first refresh 14.8 s wall / 11.1 server CPU s, wave-only 14.2 / 10.2,
  atmosphere-only 14.1 / 9.8 (predict 9.7-10.7 s, detect 1.3-1.8 s, match 0.03-0.06 s),
  checks with nothing new 0.007 s. `scripts/measure_refresh_egress.py --redis-url
  redis://localhost:6379/4`: 8.30, 9.29 and 9.46 MB database to app per refresh, 41 Redis
  commands, a check 0 database bytes and 4 Redis commands: the same as above.
* Live Open-Meteo on a fresh clone of the real-data database with MAP_FIELDS_ENABLED=true
  (the hosted profile's map mode): the clone's Upgrade-4 runs have no component keys and no
  version exists. The check found both components new (their runs were 12 days old): 2
  marine and 1 forecast request (1.02 and 0.39 MB) and 20 `meta.json`, all HTTP 200, and no
  NOAA request (the map component "not checked here"); 18.2 s wall, 10.2 CPU s, one
  version with both components on the 00Z run, the marker naming it. With that run's
  component keys and parts removed and the version deleted (the run then looks like an
  Upgrade-4 run), the next check found both components current from the whole run key: 4
  `meta.json`, no data request, 1.7 s. With the same run relabelled as if its atmosphere
  were 18Z of the day before, the check found the waves current and the atmosphere new and
  refreshed the atmosphere only (served from the provider response cache, so no data
  request that time), carrying the waves from that run: 8,000 of 8,000 wave rows equal,
  the version's `wave_model_run` the run's own 00Z, `carried_from_run_id` that run.

**Second review re-check, 2026-10-28** (same sandbox after a restart, 1-minute load average
1.1-1.7; after the per-component metadata fix and the 10 s `meta.json` timeout):

* The demo route measurement (`measure_http.py`, real `uvicorn`, scratch database and
  Redis): first refresh 18.7 s wall / 14.3 server CPU s, wave-only 16.6 / 11.9,
  atmosphere-only 16.5 / 12.0 (predict 11.2-13.7 s, spots 3.1-3.6 s, detect 1.4-1.7 s),
  checks with nothing new 0.005-0.007 s: 15-25 % slower than the first re-check, mostly
  in the predict stage, which these fixes do not touch (11.2-13.7 s against 9.7-10.7 s);
  every refresh used under 7 % of the 300 s limit. `scripts/measure_refresh_egress.py --redis-url
  redis://localhost:6379/4`: 8.35, 9.32 and 9.50 MB database to app per refresh, 41 Redis
  commands, a check 0 database bytes and 4 Redis commands.
* Live Open-Meteo on a fresh clone of the real-data database: the first check refreshed
  both components (2 marine and 1 forecast request, 20 `meta.json`; 22.7 s wall, 12.0 CPU
  s; both components' `update_interval_seconds` 21600 from `meta.json`). Then the published
  atmosphere was relabelled one cycle older, the provider cache emptied and the marine
  host's `meta.json` made to fail inside the measuring process (a synthetic outage): the
  check found the waves `unknown` ("ConnectTimeout ... after 3 attempts") and the
  atmosphere new, and the refresh made 1 forecast request and no marine data request,
  carrying the waves (`changed: false`, their run key kept; 7,900 of 7,900 wave rows equal
  to the source run's); 23.3 s wall, 9.2 CPU s. The refresh check's Redis record named the
  unknown component with its error.

**Documentation pass, 2026-10-30** (same sandbox, on the final tree, demo provider):
`backend/.venv/bin/python scripts/measure_refresh_egress.py --redis-url
redis://localhost:6379/12 --database <scratch>` took 48 s in all: first refresh (both
components) 13.7 s wall / 11.8 CPU s, 6.32 MB database to app and 56.53 MB app to database,
42 Redis commands; wave-only 13.1 s / 11.0 CPU s, 7.31 MB; atmosphere-only 13.5 s / 10.8
CPU s, 7.51 MB; the checks with nothing new 0 database bytes and 4 Redis commands each. The
same as the deployment verifier's 6.29, 7.26 and 7.45 MB of 2026-10-29.

**Monthly Function CPU (extrapolation, not a measurement).** Vercel Hobby includes 4
Active CPU-hours of Functions a month (https://vercel.com/docs/plans/hobby, read
2026-10-27). With a 5-minute QStash schedule (8,640 checks a month at about 0.02 CPU s, the
live figure) and 8-12 component refreshes a day at 9-11 CPU s, the refresh endpoint needs
about 0.65-1.15 CPU-hours a month, plus cold starts (imports, not measured here) and what
the API serves to visitors. Whether Vercel's Active CPU equals this sandbox's process CPU is
not verified.

## Measured with Upgrade 4 (2026-10-20 to 2026-10-22)

These measurements are of the Upgrade 4 code (one coordinated sync, the daemon as the
only hosted runner, two model families). They stay valid for the map stage and the
daemon's idle checks; the 2.0 refresh path is measured in "Measured (2.0)" above.

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

### Checks (Upgrade 4)

| Check | CPU s | Database | Downloaded |
|---|---|---|---|
| First `sync check` of a process, Redis empty (the stored keys are loaded once) | 1.82, 2.86 (provider imports included) | 1 statement | 6 `meta.json` (2.2 kB) and 2 `.idx` HEAD |
| `sync check` after a sync | 0.18, then 0.40 and 0.33 | **0 statements, 0 checkouts** | the same metadata, **0 data requests** |
| Daemon idle check (3 in a row) | 0.17-0.18, 0.29-0.32 | **0** | the same, 0 data requests |
| Daemon kick poll without a kick (10 in a row) | < 0.01 in total | 0 | none |
| Hourly periodic batch (6 jobs, no users) | 0.13, 0.22 | 62-64 statements | none |
| Daily `tides.acquire` | 13.6, 23.3 | 283 statements | 140 kB (16 NOAA CO-OPS requests; EOT20 tides are computed locally) |
| Daily `maintenance.cleanup` | 0.04, 0.05 | 21-22 statements | none |

These checks ran on the code of 2026-10-21. Since the cleanup of 2026-10-26 the provider
HTTP clients share one TLS context. On 2026-10-26 the same sandbox measured `plan_sync` (an
idle check: live metadata requests through the proxy, GEFS on, after one warm-up check) at
193-222 ms CPU on the earlier code and 47-53 ms on the cleaned code, in two alternating
rounds of three checks each. The rows above and the monthly estimate below keep the earlier
figures.

### Syncs (Upgrade 4)

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

### The daemon, live (Upgrade 4)

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

### Monthly CPU estimate (Upgrade 4)

Upgrade 4 assumed a Hobby daemon quota of 10 Active CPU-hours a month; Vercel now gives
Hobby daemons 5 ("5 Active CPU-hours per month", https://vercel.com/docs/daemons/limits,
read 2026-10-27). This is an extrapolation, not a
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

That is 44-79 % of the old 10-hour quota and 88-158 % of the current 5-hour one, which
is why Swell 2.0 moved the spot refreshes out of the daemon. The map layers are more than
half of it, and their horizon and cycles are the levers: MAP_FIELD_STEPS,
MAP_FIELD_MAX_LEAD_HOURS and MAP_FIELD_CYCLES.

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
