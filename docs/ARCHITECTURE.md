# Swell Travel Agent — Architecture

> "Instead of choosing a destination and hoping for waves, let the waves determine the destination."

## 1. System overview

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

Monitoring runs entirely in the Celery worker + beat processes. Nothing recurring
runs in the browser or inside a web request handler. The worker also watches the stack
itself (job O1 below); because a stopped worker cannot report its own failure, an external
monitor should poll `GET /api/health/data` too (docs/operations.md).

## 2. Pipeline (jobs A–H)

| Job | Task name | Trigger | What it does |
|-----|-----------|---------|--------------|
| A | `forecasts.acquire` | beat (every 3 h) | Centralised fetch of wave + wind + sea level for all active spots (batched multi-point requests), one `forecast_runs` row per source/model run. Skips runs already ingested. |
| A2 | `forecasts.blend` | chained after A | With two or more independent model families, builds a blend run (one member per family, correlated-error weights, an active ML correction applied to its validated member) and per-hour `forecast_comparisons`. |
| A3 | `forecasts.acquire_ensemble` | beat 4×/day (01:40, 07:40, 13:40, 19:40 UTC) when `GEFS_ENABLED` | GEFS-Wave ensemble mean/spread at every spot → `forecast_ensemble_stats` (uncertainty only). |
| A4 | `forecasts.map_fields` | beat every `FORECAST_REFRESH_MINUTES` (3 h) and at beat start, standalone map-fields mode only | Global ocean-layer fields (GFS-Wave Hs/Tp/direction, GFS 10 m wind and MSL pressure) of the newest complete GFS cycle, regridded to 0.5° and quantised → `map_fields`, under the fields-only source `noaa_gfs_fields` that no spot job reads. A no-op otherwise: with `noaa_gfs` as a provider, job A stores the fields on its NOAA run. See docs/map-layers.md. |
| B | `quality.predict` | chained after A2, beat every 30 min (safety net) | Breaking height (nearshore model v2), 80 % range and quality score for every new timestep → `surf_quality_predictions`; the authoritative run is copied to `prediction_archive`. |
| C | `detection.detect` | chained after B, beat hourly | Clusters daylight timesteps into `swell_events`; updates / downgrades / expires existing events. |
| D | `matching.match` | chained after C, beat every 30 min | Evaluates every active saved search against candidate events → `opportunity_matches`. |
| E | `flights.discover` | chained after D, beat hourly | Searches flights only for surf-matched opportunities; shared cache across users; buffer + tz logic; stores `flight_offers`. |
| F | `alerts.generate` | chained after E, beat every 15 min | Decides whether an alert is due (new / improved / price drop), dedups, queues `notifications`. |
| G | `notifications.deliver` | chained after F, beat every 2 min | Claims queued notifications (`FOR UPDATE SKIP LOCKED`), sends via provider, retries transient failures. |
| H | `maintenance.cleanup` | beat daily 03:30 UTC | Retention: old forecasts/predictions/flight searches, expired tokens/sessions, job logs; marks stale data. The prediction archive is kept `RETENTION_ARCHIVE_DAYS`; map fields only for the two best servable runs (also pruned right after every acquisition that stores fields). |
| V1 | `observations.ingest` | beat hourly when `OBSERVATIONS_ENABLED` | Buoy observations (AODN, Met Office, NDBC) → `buoy_observations`. |
| V2 | `verification.collect_point_forecasts` | beat 4×/day (00:50, 06:50, 12:50, 18:50 UTC) when `VERIFICATION_ENABLED` | GFS-Wave station bulletins (forecasts as issued) at the buoys → `point_forecasts`. |
| V3 | `verification.run` | beat daily 04:10 UTC when `VERIFICATION_ENABLED` | Pairs forecasts with observations (`prediction_validation`) and computes rolling `forecast_accuracy_metrics`. |
| S | `forecasts.check_staleness` | beat hourly | Logs a warning naming every configured source whose newest run is past its stale limit (kept for log-based monitoring; alerting is O1). |
| O1 | `ops.data_health` | beat every `DATA_HEALTH_CHECK_MINUTES` (15 min) | Builds the data-health report (`services/health.py`: per-source freshness against each source's own stale limit, every scheduled job's last result, overdue and stuck jobs, map-field and buoy freshness, a job heartbeat → ok / degraded / failing with reasons) and emails verified admin accounts when a problem appears or escalates, at most once per problem per `DATA_HEALTH_ALERT_REPEAT_HOURS`, with a recovery notice (`services/notifications/ops_alerts.py`). Its de-duplication state lives in its own job-log rows. |

The schedule itself (entries, intervals, the A→G chain) is built in
`app/workers/schedule.py`, so the data-health report checks exactly the jobs the current
settings schedule. Outside Celery: `GET /api/health/data` serves the same report (public:
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
├── core/           settings, database, redis, logging, rate limiting, errors
├── models/         SQLAlchemy 2.0 ORM models
├── schemas/        Pydantic request/response models
├── services/
│   ├── forecasts/      provider adapters (open_meteo, windy, demo), ingestion, staleness
│   ├── surf_quality/   breaking-height model, scoring components, confidence, sun times
│   ├── swell_detection/ clustering + event lifecycle
│   ├── flights/        provider adapters (duffel, amadeus, demo), travel windows, filters, ranking
│   ├── matching/       search ↔ event evaluation, opportunity scoring
│   ├── notifications/  email (resend, sendgrid, smtp, console), sms (twilio, console), templates, dedup,
│   │                   admin data-health alerts (ops_alerts)
│   ├── health.py       data-health report (GET /api/health/data, job ops.data_health)
│   └── maintenance.py  retention (job H)
├── workers/        Celery app, beat schedule (schedule.py), task wrappers, locks
└── main.py
```

Operations tooling lives in `scripts/` (`backup-db.sh`, `restore-db.sh`, `restore-drill.sh`,
`check-health.sh`, `run-e2e.sh`) and `.github/workflows/ci.yml` (backend, frontend and
end-to-end jobs); see docs/operations.md.

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
* **Same-origin API via Next.js rewrites** so cookies never need cross-site settings and
  no secrets ever reach the client bundle (no `NEXT_PUBLIC_*` secrets exist).
* **No PostGIS.** With 50–500 spots, nearest-airport and distance queries are trivially
  fast with a haversine in Python; PostGIS would add an extension and image dependency
  without material benefit. The schema stores plain lat/lon and can adopt PostGIS later.
* **Forecast sources:** NOAA GFS-Wave + GFS and ECMWF IFS read directly from their
  open-data buckets as GRIB2 by byte range, blended across independent model families,
  with GEFS-Wave spread for uncertainty (`docs/decisions.md` ADR-001 to ADR-003).
  Open-Meteo and Windy remain optional adapters. Surfline has no public/authorized API,
  so it is not integrated (no scraping).
* **Flights:** Duffel is primary (self-serve API keys). Amadeus Self-Service was shut
  down on 2026-07-17; the Amadeus adapter remains for Enterprise customers with a
  configurable base URL. A demo provider produces clearly labelled mock offers.
* **Event detection threshold** is deliberately low (Fair) so every user threshold is
  satisfiable; each search is evaluated against the event's individual timesteps, so a
  user who wants "Excellent" gets only the excellent sub-window of an event.

See `docs/SURF_SCORING.md`, `docs/MATCHING_AND_FLIGHTS.md` and `docs/FORECAST_SOURCES.md`
for algorithm details.
