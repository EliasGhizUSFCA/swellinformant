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
          │ (scheduler)    │──▶│ jobs A–H       │──▶ Open-Meteo (NOAA GFS + GFS-Wave/WW3)
          └────────────────┘   │                │──▶ Windy Point Forecast API (optional, licensed key)
                               │                │──▶ Duffel / Amadeus Enterprise (flights)
                               │                │──▶ Resend / SendGrid / SMTP (email)
                               └────────────────┘──▶ Twilio (SMS)
```

Monitoring runs entirely in the Celery worker + beat processes. Nothing recurring
runs in the browser or inside a web request handler.

## 2. Pipeline (jobs A–H)

| Job | Task name | Trigger | What it does |
|-----|-----------|---------|--------------|
| A | `forecasts.acquire` | beat (every 3 h) | Centralised fetch of wave + wind + sea level for all active spots (batched multi-point requests), one `forecast_runs` row per source/model run. Skips runs already ingested. |
| B | `quality.predict` | chained after A, beat hourly safety net | Scores every new forecast timestep → `surf_quality_predictions`. |
| C | `detection.detect` | chained after B, beat hourly | Clusters daylight timesteps into `swell_events`; updates / downgrades / expires existing events. |
| D | `matching.match` | chained after C, beat every 30 min | Evaluates every active saved search against candidate events → `opportunity_matches`. |
| E | `flights.discover` | chained after D, beat hourly | Searches flights only for surf-matched opportunities; shared cache across users; buffer + tz logic; stores `flight_offers`. |
| F | `alerts.generate` | chained after E, beat every 15 min | Decides whether an alert is due (new / improved / price drop), dedups, queues `notifications`. |
| G | `notifications.deliver` | chained after F, beat every 2 min | Claims queued notifications (`FOR UPDATE SKIP LOCKED`), sends via provider, retries transient failures. |
| H | `maintenance.cleanup` | beat daily | Retention: old forecasts/predictions/flight searches, expired tokens/sessions, job logs; marks stale data. |

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
│   └── notifications/  email (resend, sendgrid, smtp, console), sms (twilio, console), templates, dedup
├── workers/        Celery app, beat schedule, task wrappers, locks
└── main.py
```

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
* **Forecast source:** Open-Meteo's JSON API serving NOAA **GFS** (atmosphere) and NOAA
  **GFS-Wave** (WAVEWATCH III, `ncep_gfswave025`). It needs no GRIB tooling, supports
  multi-point batch requests, and exposes model-run metadata. Windy's Point Forecast API
  is an optional licensed adapter. Surfline has no public/authorized API, so it is not
  integrated (no scraping).
* **Flights:** Duffel is primary (self-serve API keys). Amadeus Self-Service was shut
  down on 2026-07-17; the Amadeus adapter remains for Enterprise customers with a
  configurable base URL. A demo provider produces clearly labelled mock offers.
* **Event detection threshold** is deliberately low (Fair) so every user threshold is
  satisfiable; each search is evaluated against the event's individual timesteps, so a
  user who wants "Excellent" gets only the excellent sub-window of an event.

See `docs/SURF_SCORING.md`, `docs/MATCHING_AND_FLIGHTS.md` and `docs/FORECAST_SOURCES.md`
for algorithm details.
