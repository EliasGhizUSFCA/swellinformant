# Swell Travel Agent — Architecture

> "Instead of choosing a destination and hoping for waves, let the waves determine the destination."

## 1. System overview

The same code runs in three topologies: the classic Docker Compose stack (below, Celery),
the hosted setup on Vercel (1.1) and the local Vercel-style stack that copies it (1.2).
`JOB_RUNNER` selects who runs the background jobs: `celery` (worker and beat) or `daemon`
(one `python -m app.cli sync daemon` process; on Vercel the refresh endpoint and the daily
cron when no daemon runs). All run the same refresh pipeline (1.3). Swell 2.0's design and
its implementation status: docs/architecture_audit.md; the decisions: docs/decisions.md,
ADR-038 to ADR-053.

**Classic Docker Compose stack** (`docker-compose.yml`, `JOB_RUNNER=celery`):

```
                ┌────────────────────────────────────────────────────────────────┐
 Browser ──────▶│ frontend (Next.js 16, React 19, Tailwind 4, Recharts, Leaflet) │
                │  • /api/* is rewritten (server-side proxy) to the backend      │
                └──────────────┬─────────────────────────────────────────────────┘
                               │ same-origin HTTP (session cookie + CSRF header)
                ┌──────────────▼──────────────┐       ┌──────────────────────────┐
                │ backend API (FastAPI)       │──────▶│ PostgreSQL 16            │
                │  auth, searches, spots,     │       │  all persistent state     │
                │  opportunities, webhooks    │       └──────────▲───────────────┘
                └──────────────┬──────────────┘                  │
                               │ enqueue / rate limits / locks   │
                ┌──────────────▼──────────────┐                  │
                │ Redis 7                     │                  │
                │  Celery broker + results,   │                  │
                │  rate limits, job locks,    │                  │
                │  API quota counters         │                  │
                └──────┬───────────────▲──────┘                  │
                       │               │                         │
          ┌────────────▼───┐   ┌───────┴────────┐                │
          │ celery beat    │   │ celery worker  │────────────────┘
          │ (scheduler)    │──▶│ jobs A–H       │──▶ NOAA open data (GFS, GFS-Wave, GEFS-Wave, bulletins)
          └────────────────┘   │                │──▶ ECMWF open data (IFS, ecWAM)
                               │                │──▶ AODN / Met Office / NDBC buoys (verification)
                               │                │──▶ Open-Meteo, Windy (optional sources)
                               │                │──▶ Duffel / Amadeus Enterprise (flights)
                               │                │──▶ Resend / SendGrid / SMTP (email)
                               └────────────────┘──▶ Twilio (SMS)
```

Monitoring runs entirely in the background runner (Celery worker + beat here, the sync
daemon when hosted). Nothing recurring runs in the browser or inside a web request handler.
The runner also watches the stack itself (job O1 below); because a stopped runner cannot
report its own failure, an external monitor should poll `GET /api/health/data` too, and on
Vercel a daily cron watchdog does (docs/operations.md).

### 1.1 Hosted on Vercel (Upgrade 4, Swell 2.0)

One Vercel project built from the repository root (`vercel.json`, Services mode;
docs/deploy-vercel.md):

```
                     ┌──────────────────────── Vercel project (repository root) ──────────────────────────┐
 Browser ──HTTPS────▶│ router: /api/(.*) ──▶ service "api"   FastAPI as ONE Python function (lean install, │
   │                 │                       app.main:app, maxDuration 300 s, JOB_RUNNER=daemon)           │
   │                 │         /(.*)     ──▶ service "web"   Next.js 16 (API_ROUTING=platform: no rewrite) │
   │                 │ CDN: caches public GETs per forecast version (?v=), never user-dependent responses  │
   │                 │ cron           GET /api/internal/cron/daily, 07:23 UTC, Bearer CRON_SECRET          │
   │                 │ daemon "sync"  optional: python -m app.cli sync daemon, SYNC_DAEMON_KINDS=map        │
   │                 └───────┬──────────────────────────────┬──────────────────────────────┬──────────────┘
   │  every 20 s while       │ pooled DATABASE_URL (API)     │ KV_URL (rediss://)           │ HTTPS
   │  visible: GET           │ direct DATABASE_URL_UNPOOLED  │                              │
   │  /api/forecasts/version ▼ (daemon, migrations)          ▼                              ▼
   │              Neon Postgres (Marketplace)       Upstash Redis (Marketplace)   Open-Meteo or Windy (spot points),
   │              all persistent state,             rate limits, locks, budgets,  NOAA NODD (map layers),
   │              forecast versions,                version marker, public        NOAA CO-OPS (tides),
   │              scales to zero                    snapshot cache, known runs    Resend (email), Duffel
   │
 QStash (Hobby) or Vercel Cron (Pro), every 5 min ──▶ POST /api/internal/refresh (Bearer CRON_SECRET)
```

* **api**: account and search endpoints, the spot and map reads, and the **refresh
  endpoint** `GET|POST /api/internal/refresh`: a free metadata check and, when a wave or
  wind run is new, a spot-only refresh that publishes a forecast version, then detection,
  matching, flights, alerts and delivery (docs/sync.md). Without the daemon it also takes
  the API's kicks ("match my search", "Re-check flights"), due email retries and the hourly
  data-health check (`app/services/sync/standin.py`). Under `JOB_RUNNER=daemon` it never
  imports Celery and sends account emails and SMS inline. The client IP comes from
  `x-vercel-forwarded-for` (`CLIENT_IP_HEADER`). Build command: `python -m app.cli migrate
  --on-build` (production builds migrate and seed through the direct URL).
* **scheduler**: Upstash QStash on Hobby (Vercel Cron runs at most daily there) or a
  `*/5` Vercel Cron entry on Pro, both outside the repository's `vercel.json` defaults
  (docs/deploy-vercel.md step 7b).
* **cron watchdog**: the data-health check, the periodic jobs no daemon ran on time and one
  refresh check as a backstop; it emails the admins (or `OPS_ALERT_EMAILS` when the database
  is unreachable).
* **sync daemon** (optional): every `SYNC_CHECK_MINUTES` (10) it reads the map layers'
  metadata (no database query) and writes its heartbeat; a new map cycle runs one map sync;
  the periodic jobs run at least hourly apart (`JOB_INTERVAL_FLOOR_MINUTES`). At start it
  runs `init-db`. Without it set `MAP_FIELDS_ENABLED=false`.
* **No object storage**: map fields stay in Postgres (two runs, about 71 MB each with
  partitions), served by the API with immutable CDN caching per run.
* **Manual fallback**: `.github/workflows/data-sync.yml` runs one sync on GitHub by hand.

### 1.2 The local Vercel-style stack

`docker-compose.vercel.yml`, driven by `scripts/vercel-local.sh` (docs/local-vercel-stack.md),
runs the hosted topology on one machine: a Caddy `edge` on :3100 does Vercel's routing and
sets its client-IP headers, `api` is the lean install (no Celery, no data libraries, no
`data/`), `web` is the Next.js production build with `API_ROUTING=platform`, `pgbouncer` in
transaction mode stands in for Neon's pooler in front of `postgres`, `redis` for Upstash,
`sync` is the daemon built from the same `Containerfile`, `mailpit` captures every email,
and `cron` calls the watchdog. Not emulated: the function runtime (cold starts), the CDN,
Neon's scale-to-zero, Upstash's TLS and pricing, the daemon quota, and the external
scheduler: the stack's daemon refreshes the spots itself (`SYNC_DAEMON_KINDS=spots,ensemble,map`),
so it does not test the daemon-less hosted path (docs/local-vercel-stack.md).

### 1.3 Data flow: new model run → refresh → version → open pages

```
provider publishes a run ──▶ refresh check (metadata only: Open-Meteo meta.json per model domain,
        │                    or Windy's sentinel fingerprint; Redis holds the known runs)
        │ a component (wave or atmosphere) new and servable?  no ─▶ "not_due" (0 SQL statements)
        ▼ yes
one sync under a Postgres lease (sync_runs row; one at a time):
   spots (fetch the due component for the active spots, carry the other with its own model run,
          validate every spot) ─▶ predict (score every hour) ─▶ PUBLISH (stage a forecast_versions row,
          then one transaction: previous → superseded, this → published) ─▶ detect ─▶ match
          ─▶ version marker in Redis ─▶ flights ─▶ alerts ─▶ deliver ─▶ prune
        │
        ▼
public reads (spot list, map conditions, spot pages and timeline, status, map index): computed once per
version (and UTC hour), kept in Redis ──▶ CDN (s-maxage 300 for a URL that names the current version
with ?v=; another version's URL answers no-store) ──▶ browsers
open tabs: GET /api/forecasts/version every 20 s while visible (CDN 15 s) ──▶ new id ──▶ re-read the
data on screen under the new ?v= URLs, keep the selected spot and time, no reload
```

A spot whose data failed keeps its previous run in the new version, labelled stale, and is
retried alone (5, 15, 45 minutes). The map layers have their own runs and publish gate; they
create no version. Rules, states and measurements: docs/sync.md; the cache and egress
figures: docs/map-layers.md; the version endpoint and the browser: README F.2.

## 2. Pipeline and jobs

| Job | Task name | Trigger | What it does |
|-----|-----------|---------|--------------|
| SYNC | `GET\|POST /api/internal/refresh` (hosted, every 5 min from a scheduler), `sync.tick` (Celery beat) or the daemon's own check | every 5 min hosted; every `SYNC_CHECK_MINUTES` (10) under Celery or the daemon | Reads provider metadata only; when a component's model run is newer than the published one, runs one sync of the stages below in order and publishes a forecast version (docs/sync.md). Since Upgrade 4 jobs A-C run as sync stages, not on timers. |
| A | `forecasts.acquire` | sync stage `spots` | Centralised fetch for all active spots (batched multi-point requests) of the component with a new run only (wave or atmosphere; the other is carried forward with its own model run), validated per spot; one `forecast_runs` row per refresh. Skips runs already ingested. |
| A2 | `forecasts.blend` | sync stage `blend` | With two or more independent model families, builds a blend run (one member per family, correlated-error weights, an active ML correction applied to its validated member) and per-hour `forecast_comparisons`. |
| A3 | `forecasts.acquire_ensemble` | sync stage `ensemble`, when `GEFS_ENABLED` | GEFS-Wave ensemble mean/spread at every spot → `forecast_ensemble_stats` (uncertainty only). |
| A4 | `forecasts.map_fields` | sync stage `map`, standalone map-fields mode only | Global ocean-layer fields (GFS-Wave Hs/Tp/direction, GFS 10 m wind and MSL pressure) of the newest complete GFS cycle, regridded to 0.5° and quantised → `map_fields`, under the fields-only source `noaa_gfs_fields` that no spot job reads. A no-op otherwise: with `noaa_gfs` as a provider, job A stores the fields on its NOAA run. See docs/map-layers.md. |
| B | `quality.predict` | sync stage `predict` | Breaking height (nearshore model v2), 80 % range and quality score 2.2.0 for every timestep of the new run → `surf_quality_predictions`; with `PREDICTION_ARCHIVE_ENABLED` (off by default) the published run is copied to `prediction_archive`. |
| P | `sync.publish` | sync stage `publish` | Stages and publishes a forecast version (`forecast_versions`) in one transaction, then the publication's `published_at`, the served map run and the blend run; the map index serves that map run. The version marker is written after `match`. |
| C | `detection.detect` | sync stage `detect` | Clusters daylight timesteps into `swell_events`; updates / downgrades / expires existing events. |
| D | `matching.match` | sync stage `match`; daily housekeeping 03:45 UTC; a "match my search" kick | Evaluates every active saved search (with its own alert lead time) against candidate events → `opportunity_matches`. |
| E | `flights.discover` | sync stage `flights` (after every publication's matching); a "refresh flights" kick; no timer since 2.0 (design D15) | Searches flights only for surf-matched opportunities; shared cache across users; buffer + tz logic; stores `flight_offers`. |
| F | `alerts.generate` | sync stage `alerts`; schedule every 15 min | Decides whether an alert is due (new / improved / price drop), dedups, queues `notifications`. |
| G | `notifications.deliver` | sync stage `deliver`; schedule every 2 min | Claims queued notifications (`FOR UPDATE SKIP LOCKED`), sends via provider, retries transient failures. |
| H | `maintenance.cleanup` | beat daily 03:30 UTC | Retention: old forecasts/predictions/flight searches, expired tokens/sessions, job logs; marks stale data. The prediction archive is kept `RETENTION_ARCHIVE_DAYS`; map fields only for the two best servable runs (also pruned right after every acquisition that stores fields). |
| T | `tides.acquire` | beat/daemon daily 02:20 UTC with `TIDE_PROVIDERS` (default on); also at beat start and in the daemon's start-up catch-up when overdue | NOAA CO-OPS and EOT20 tide predictions for the stations linked to active spots → `tide_predictions` (docs/tides.md). |
| V1 | `observations.ingest` | beat hourly when `OBSERVATIONS_ENABLED` | Buoy observations (AODN, Met Office, NDBC) → `buoy_observations`. |
| V2 | `verification.collect_point_forecasts` | beat 4×/day (00:50, 06:50, 12:50, 18:50 UTC) when `VERIFICATION_ENABLED` | GFS-Wave station bulletins (forecasts as issued) at the buoys → `point_forecasts`. |
| V3 | `verification.run` | beat daily 04:10 UTC when `VERIFICATION_ENABLED` | Pairs forecasts with observations (`prediction_validation`) and computes rolling `forecast_accuracy_metrics`. |
| S | `forecasts.check_staleness` | beat hourly | Logs a warning naming every configured source whose newest run is past its stale limit (kept for log-based monitoring; alerting is O1). |
| O1 | `ops.data_health` | beat or the daemon every `DATA_HEALTH_CHECK_MINUTES` (15 min; 60 hosted, run by the refresh endpoint without the daemon); the daily cron | Builds the data-health report (`services/health.py`: per-source freshness against each source's own stale limit, every scheduled job's last result, overdue and stuck jobs, map-field and buoy freshness, a job heartbeat → ok / degraded / failing with reasons) and emails verified admin accounts when a problem appears or escalates, at most once per problem per `DATA_HEALTH_ALERT_REPEAT_HOURS`, with a recovery notice (`services/notifications/ops_alerts.py`). Its de-duplication state lives in its own job-log rows. |

Schedule intervals are raised to at least `JOB_INTERVAL_FLOOR_MINUTES` (60 when hosted).
The schedule itself (entries, intervals, the stage chain of jobs run on their own) is built
in `app/jobs/schedule.py` without Celery; Celery beat reads it through
`app/workers/schedule.py` and the sync daemon runs it on its own timers, so the data-health
report checks exactly the jobs the current settings and runner schedule. Outside Celery: `GET /api/health/data` serves the same report (public:
operational metadata only, rate limited; `?strict=true` answers 503 when failing, for
external monitors), and the opt-in compose profile `backup` runs `scripts/backup-db.sh`
every `BACKUP_INTERVAL_HOURS` (docs/operations.md).

Reliability: Redis job locks (one instance of each job at a time), `acks_late` +
`task_reject_on_worker_lost`, idempotent upserts guarded by unique constraints,
`background_job_logs` for every run, per-provider daily quota counters, retry with
exponential backoff for transient HTTP failures, stale "sending" notifications are
reclaimed after a timeout so a worker crash never loses or duplicates an alert.

## 3. Backend layout

```
backend/app
├── api/            FastAPI routers (thin: validation + authorization + call services)
├── auth/           password hashing (Argon2id), sessions, CSRF, dependencies
├── core/           settings, deployment profiles, database, redis, HTTP cache headers, logging,
│                   rate limiting, errors
├── models/         SQLAlchemy 2.0 ORM models
├── schemas/        Pydantic request/response models
├── services/
│   ├── forecasts/      provider adapters (open_meteo, open_meteo_ecmwf, noaa, ecmwf, windy, demo),
│   │                   provider_manager (selection, health, Windy fallback), versions (forecast
│   │                   versions, the version marker), serving (published-version reads), carry
│   │                   (component carry-forward, validation), GRIB reads, blend, GEFS ensemble,
│   │                   map fields, ingestion, queries, staleness
│   ├── surf_quality/   breaking-height model, scoring components, confidence, sun times
│   ├── swell_detection/ clustering + event lifecycle
│   ├── flights/        provider adapters (duffel, amadeus, demo), travel windows, filters, ranking
│   ├── matching/       search ↔ event evaluation, opportunity scoring
│   ├── notifications/  email (resend, sendgrid, smtp, console), sms (twilio, console), templates, dedup,
│   │                   admin data-health alerts (ops_alerts)
│   ├── sync/           the refresh pipeline: detect (per-component plans), orchestrator (refresh_check,
│   │                   run_sync), refresh (spot refresh, staging), publish, standin (daemon-less
│   │                   hosting), daemon, CLI
│   ├── tides/          NOAA CO-OPS and EOT20 tide predictions, tide stage, tide health
│   ├── observations/   buoy observations (AODN, Met Office, NDBC): station catalog, ingestion
│   ├── verification/   forecast/observation pairing, accuracy metrics, backtests
│   ├── ml/             ML post-processing of offshore forecasts: datasets, registry, application
│   ├── research/       spot research evidence, research confidence, profile decisions
│   ├── map_fields.py   when map fields are produced, and the read side of /api/map/fields
│   ├── spot_views.py   read models for spots, forecasts and events
│   ├── timeline.py     the spot page's forecast timeline
│   ├── public_cache.py public snapshot cache of the anonymous reads (Redis, per publication and hour)
│   ├── health.py       data-health report and the admin problems (GET /api/health/data, ops.data_health)
│   ├── admin_freshness.py  the admin freshness dashboard (GET /api/admin/freshness)
│   ├── usage.py        API usage report (Open-Meteo, Windy, flights, emails)
│   ├── service_credentials.py  refused flight and email keys as admin problems
│   └── maintenance.py  retention (job H)
├── jobs/           the job registry, runner (locks, job logs, cpu_s) and schedule, without Celery
├── workers/        Celery adapter: app, beat schedule, task wrappers, dispatch
└── main.py
```

Operations tooling lives in `scripts/` (`backup-db.sh`, `restore-db.sh`, `restore-drill.sh`,
`check-health.sh`, `run-e2e.sh`, since Upgrade 4 `vercel-local.sh`,
`vercel-build-check.sh`, `measure_db_egress.py`, `check_api_bundle.py`, and since Swell 2.0
`measure_refresh_egress.py`, `load_test_public.py`, `generate_spot_research_docs.py`) and
`.github/workflows/` (`ci.yml`: backend, frontend, deploy-config, vercel-build and
end-to-end jobs; `data-sync.yml`: the manual sync); see docs/operations.md. The hosted
setup's files are `vercel.json`, the repo-root `Containerfile` (the daemon image),
`docker-compose.vercel.yml` with `docker/` (edge, lean API, PgBouncer) and
`.env.vercel.example`.

Business logic lives in `services/`; API routes and Celery tasks are thin wrappers so
the same code is exercised by tests, the API, the dev "run pipeline" endpoint and the
workers.

## 4. Key decisions

* **Sync SQLAlchemy + psycopg 3.** Celery workers are synchronous; using one
  synchronous data layer means the same service code runs in API threads and workers.
* **Opaque server-side sessions** (random 256-bit token in an `HttpOnly` cookie; only
  its SHA-256 hash is stored) instead of JWTs: instant revocation on logout, password
  reset and account deletion.
* **CSRF**: double-submit token (`sta_csrf` cookie echoed in `X-CSRF-Token`) required on
  every state-changing request that carries a session cookie; `SameSite=Lax` cookies.
* **Same-origin API** so cookies never need cross-site settings and no secrets ever reach
  the client bundle (no `NEXT_PUBLIC_*` secrets exist): Next.js rewrites `/api/*` to the
  backend in the classic stack (`API_ROUTING=proxy`); on Vercel the platform's router does
  (`API_ROUTING=platform`, docs/decisions.md ADR-021).
* **Refresh per model run and component, one runner** (Upgrade 4 ADR-023, Swell 2.0
  ADR-043 to ADR-045): data is pulled when a provider publishes a new run of a component
  (wave or atmosphere), the other component is carried with its own model run, and every
  refresh publishes a forecast version. On Vercel the refresh runs in the API function,
  called every 5 minutes by a scheduler; the daemon is optional (map layers only). The
  background runner is Celery or one sync daemon, never both.
* **No PostGIS.** With 50–500 spots, nearest-airport and distance queries are trivially
  fast with a haversine in Python; PostGIS would add an extension and image dependency
  without material benefit. The schema stores plain lat/lon and can adopt PostGIS later.
* **Forecast sources** (Swell 2.0, ADR-039 to ADR-042): one weather provider for the spot
  forecasts, `WEATHER_PROVIDER=auto`: Open-Meteo's NOAA GFS + GFS-Wave now, the Windy Point
  Forecast adapter once a written licence allows storing its data
  (`WINDY_STORAGE_AUTHORIZED`), Open-Meteo as its fallback. The two-family blend (GFS and
  ECMWF), the GEFS-Wave spread and NOAA/ECMWF GRIB2 read directly are kept but off
  (`docs/decisions.md` ADR-001 to ADR-003, ADR-024; docs/FORECAST_SOURCES.md); the world map
  layers come from NOAA NODD by byte range. Surfline has no public/authorized API, so it is
  not integrated (no scraping).
* **Flights** (ADR-052): searched only for a new or materially changed match, on request
  and right before an alert, never on a timer. The demo provider (clearly labelled mock
  offers) is the default; the Duffel adapter is kept, but its agreement forbids metasearch
  use, so production needs Duffel's written confirmation; Amadeus Self-Service was shut down
  on 2026-07-17 and the Amadeus adapter remains for Enterprise customers with a
  configurable base URL.
* **Event detection threshold** is deliberately low (Fair) so every user threshold is
  satisfiable; each search is evaluated against the event's individual timesteps, so a
  user who wants "Excellent" gets only the excellent sub-window of an event.

See `docs/SURF_SCORING.md`, `docs/MATCHING_AND_FLIGHTS.md` and `docs/FORECAST_SOURCES.md`
for algorithm details.
