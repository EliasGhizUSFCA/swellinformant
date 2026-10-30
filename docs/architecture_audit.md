# Architecture audit: Swell Travel Agent 2.0

> **Status of this document.** Sections 1-3 and 6 describe the system **as it was** at commit
> `18a5bee` (2026-10-26). Sections 4 and 5 are the **plan at audit time** (2026-10-27): the
> decisions recorded in the 2.0 design before implementation started. They are not a description
> of the delivered system. An "Implementation status" section (section 8) will be appended after integration; where
> it differs from sections 4-5, section 8 wins.

"Spec" below means the owner's master prompt for Swell Travel Agent 2.0 (received 2026-10-26);
"§n" is its section n. "D1-D16" are the 2.0 decisions of section 4. They are not the D1-D16 labels
that docs/decisions.md gives to the Upgrade 4 ADRs (ADR-021 to ADR-036).

Contents

1. [Scope and method](#1-scope-and-method)
2. [The old architecture](#2-the-old-architecture-commit-18a5bee)
3. [Findings by spec section](#3-findings-by-spec-section-1-36)
4. [Decisions and proposed changes (plan)](#4-decisions-and-proposed-changes-plan-at-audit-time)
5. [Reuse, switch-off and removal (plan)](#5-reuse-switch-off-and-removal-plan-at-audit-time)
6. [Measured numbers and external facts](#6-measured-numbers-and-external-facts)
7. [Open questions for the owner](#7-open-questions-for-the-owner-and-the-defaults-chosen)
8. [Implementation status](#8-implementation-status)

---

## 1. Scope and method

**Base.** Commit `18a5bee6f39c` on branch `claude/funny-hamilton-49k2m3`, committed
2026-10-26T22:02:59Z. Every `file:line` reference in this document is to that commit.

**When.** The audit and the research ran on 2026-10-27 between about 00:00 and 03:20 UTC. Every
external page cited here was retrieved on 2026-10-27.

**What was inspected.** Five area audits (247 items, each with requirement, status, evidence, gap
and proposed change) and three research topics (227 facts, each with a verbatim quote, URL and
retrieval date):

| Area | Spec sections | Items or facts | Main material read |
|---|---|---|---|
| Forecasting, sync, publication | §5-11, 13-15, 21, 22, 30 | 27 items | backend/app/services/forecasts/**, services/sync/**, services/public_cache.py, core/config.py, core/profiles.py, docs/sync.md |
| Frontend | §12, 15, 22-25 | 37 items | frontend/app, components, hooks, lib, tests, e2e |
| Surf quality and spot research | §2, 17-20 | 28 items | services/surf_quality/**, services/research/**, services/ml/**, data/surf_spots/*.json, data/tide_stations.json |
| Users, auth, flights, matching, notifications | §3 (auth), 26-29 | 44 items | backend/app/auth, services/accounts.py, services/flights/**, services/matching/**, services/notifications/**, the search wizard |
| Platform, deployment, operations, docs, tests | §3, 13, 16, 30-36 | 111 items | vercel.json, Containerfile, docker-compose*.yml, .github/workflows, docs/*.md, README.md, all test suites |
| Research: Windy Point Forecast API | §5, 6, 9 | 56 facts | api.windy.com docs, pricing, terms, Windy community answers |
| Research: Open-Meteo update metadata; Vercel limits and schedulers | §9, 13 | 128 facts | open-meteo.com docs and terms, live meta.json, NOAA NODD, vercel.com docs, Upstash, cron-job.org, GitHub docs |
| Research: flight APIs | §26, 27 | 43 facts | Duffel, Amadeus (secondary sources), Travelpayouts, Kiwi, Skyscanner, KAYAK, SerpApi, Ignav |

**Status values.** `exists` (the requirement is met), `partial` (met in part), `missing`,
`conflicts` (the current behaviour contradicts the spec, not merely incomplete), and `n/a` for
process instructions.

**How measurements were taken.**

* Read-only SQL on `swell_u3i_live_run`, a local database holding a real acquisition of the
  2026-10-16 00Z run (sources `open_meteo_noaa`, `open_meteo_ecmwf`, `noaa_gfs_fields`,
  `noaa_gefs_wave` and a `blend` run; 25,000 surf quality predictions). Anything that writes ran
  on a clone of it.
* Fresh scratch databases with the demo provider for forced syncs, SQL statement counts
  (SQLAlchemy `before_cursor_execute`), the partial-failure probe, the flight probes and the
  1,000-visitor load test (uvicorn with one worker; `pg_stat_database` transaction deltas).
* `curl` of Open-Meteo `meta.json`, NOAA NODD `.idx` headers (`Last-Modified`), and every
  provider documentation page quoted in section 6.
* pytest and vitest on the development sandbox (4 vCPUs shared with other work) and GitHub CI
  run 40414842129 on `18a5bee`.
* Frontend request counts with vitest fake timers on a scratch copy of `frontend/`.
* The scratch scripts and test files are kept outside the repository; section 6 names them by
  file name only.

**Limits of the method.** No Vercel, Neon, Upstash, Windy, Duffel or Open-Meteo commercial account
was used and nothing was deployed. No live Windy call was made (the sandbox refused even an
invalid-key probe). CDN behaviour is derived from the response headers and Vercel's
documentation, not measured. Wall times vary with the shared sandbox. Figures copied from repo
documents (mainly docs/sync.md, measured 2026-10-20 to 2026-10-22) are labelled as such and were
not re-measured.

**One conflict between sources.** Two audits quoted Vercel's Hobby daemon quota as 10 Active
CPU-hours a month. The research and a re-fetch of https://vercel.com/docs/daemons/limits at
2026-10-27 03:20 UTC both read "5 Active CPU-hours per month". This document uses 5.

---

## 2. The old architecture (commit 18a5bee)

### 2.1 Topologies and runners

| Runner | Files | What it runs | Notes |
|---|---|---|---|
| Vercel project, Services mode | vercel.json:3-24 | service `web` (Next.js, `frontend/`) and service `api` (FastAPI as one Python function, `app.main:app`, `maxDuration` 60, build command `python -m app.cli migrate --on-build`); `/api/(.*)` is routed to `api` | Offline `vercel build` passes in CI; never deployed |
| Vercel Daemon `sync` | vercel.json:25-33, Containerfile:69 (`python -m app.cli sync daemon`) | one 2 GiB instance in iad1, grace 600 s; every 60 s pops Redis kicks; every `SYNC_CHECK_MINUTES` (10) reads provider metadata and runs a sync when due; periodic jobs on in-memory timers at least `JOB_INTERVAL_FLOOR_MINUTES` (60 hosted) apart; `init-db` at start | Public Beta, production only (docs/decisions.md ADR-022) |
| Daily cron watchdog | vercel.json:34 (`23 7 * * *`), backend/app/api/internal.py:46,164-194 | `GET /api/internal/cron/daily` with `Bearer CRON_SECRET` (internal.py:51-59): the data-health check and admin emails | Runs no sync |
| Celery (classic stack) | docker-compose.yml (JOB_RUNNER=celery, the default: core/config.py:104), backend/app/workers/** | worker(s) and beat; beat runs `sync.tick` every `SYNC_CHECK_MINUTES` plus the periodic schedule of backend/app/jobs/schedule.py:155-162 | Not used on Vercel |
| Manual GitHub Actions | .github/workflows/data-sync.yml | `sync run --trigger github` on `workflow_dispatch` / `repository_dispatch`; deliberately no `schedule:` (data-sync.yml:7) | Fallback only |
| Command line | backend/app/services/sync/cli.py | `python -m app.cli sync check`, `run`, `daemon`, `publish`, `status` | docs/sync.md "Commands" |
| Local Vercel-style stack | docker-compose.vercel.yml, scripts/vercel-local.sh | Caddy edge, lean API, Next.js build, PgBouncer (transaction mode), Postgres, Redis, the daemon, Mailpit, a cron helper | Does not emulate cold starts, CDN, Neon scale-to-zero or the daemon quota |
| Inline dispatch | backend/app/workers/dispatch.py | under `JOB_RUNNER=daemon` the API sends account emails inline and writes "match my search" / "refresh flights" kicks to Redis | — |

### 2.2 Forecast data flow

```
PROVIDERS (backend/app/services/forecasts/registry.py:30-77)
  open_meteo        open_meteo.py        source open_meteo_noaa: marine ncep_gfswave016 + ncep_gfswave025,
                                          forecast gfs_global (= ncep_gfs013 wind + ncep_gfs025 gust/MSLP)
  open_meteo_ecmwf  open_meteo_ecmwf.py  source open_meteo_ecmwf: ecmwf_wam025 + ecmwf_ifs025, 00Z/12Z only
  map layers        noaa.py, fields.py   source noaa_gfs_fields: NOAA NODD GRIB, regridded to 0.5 deg
  ensemble          ensemble.py          noaa_gefs_wave (GEFS_ENABLED=false)
  windy             windy.py             gated by WINDY_TERMS_ACKNOWLEDGED; in no profile
  noaa_gfs, ecmwf_ifs noaa.py, ecmwf.py  direct GRIB point ingestion; in no profile
  demo              demo.py              default outside the hosted profile
  tides             services/tides/**    NOAA CO-OPS + EOT20 harmonic model, daily job, outside the sync
        |
        | metadata only: Open-Meteo meta.json per model domain, NODD .idx HEAD (0 SQL in the daemon)
        v
DETECTION  services/sync/detect.py plan_sync
  states new / waiting (coalescing up to SYNC_COALESCE_MINUTES=90) / in_progress / failed / unknown / disabled
  due when a component of a kind in SYNC_TRIGGER_SOURCES is new; known runs in Redis key sync:known
        | due
        v
ONE SYNC under a Postgres lease (sync_runs row, unique partial index uq_sync_runs_running; orchestrator.py)
  spots -> ensemble -> map -> blend -> predict -> PUBLISH -> detect -> match -> flights -> alerts -> deliver -> prune
  (services/sync/types.py:49-62; every stage is app.jobs.runner.run_job: a Redis lock + a background_job_logs row)
        |
        v
STORAGE (Postgres): forecast_sources, forecast_runs, wave_forecasts, forecast_ensemble_stats,
  forecast_comparisons, map_fields, surf_quality_predictions, prediction_archive, swell_events,
  sync_runs, background_job_logs
        |
        v
PUBLICATION  services/sync/publish.py:47-78: sync_runs.published_at, map_run_id, blend_run_id in one commit;
  public_cache.note_published() writes the marker p<sync_run_id>.<random> (public_cache.py:167-179).
  Spot pages do NOT read the publication: they read the newest scored runs (forecasts/queries.py:250-307).
        |
        v
PUBLIC API  api/spots.py, api/timeline.py, api/map_fields.py, api/system.py, api/tides.py
  -> Redis public snapshot: key epoch:marker:UTC hour:route, payload TTL 2 h, single-flight (public_cache.py)
  -> Vercel CDN: CDN-Cache-Control "public, s-maxage=300, stale-while-revalidate=3600" (api/spots.py:53-77)
  -> browser: Cache-Control "public, max-age=60"
        |
        v
FRONTEND  Next.js client components + TanStack Query (frontend/components/providers.tsx:15-16: staleTime 30 s,
  no focus refetch); only the map "now" snapshot and the field index refetch, every 300 s
  (frontend/hooks/queries.ts:194-203, frontend/hooks/map-fields.ts:25)
```

| Step | Where | Behaviour at 18a5bee |
|---|---|---|
| Provider selection | core/config.py:187, core/profiles.py:46, forecasts/ingestion.py:496-515, forecasts/queries.py:271-307 | `FORECAST_PROVIDERS` is an ordered list (default `demo`; hosted `open_meteo,open_meteo_ecmwf`); every listed provider is acquired each sync; "fallback" only at read time (fresh before stale, then list order) |
| Requests | forecasts/open_meteo.py:642-652,727-754,756-832; forecasts/windy.py:137-156 | Open-Meteo: comma-separated locations, `FORECAST_BATCH_SIZE`=50, one request per model (3 data requests per GFS refresh of 50 spots); masked wave cells re-read with neighbouring cells; Windy: 2 requests per spot |
| Run identity | open_meteo.py:563-571; windy.py:106-115; forecasts/base.py:199-205 | Open-Meteo: one run key per family joining every domain; `wave_model_run_at` / `atmosphere_model_run_at` = oldest init of their domains; Windy: nominal GFS cycle of now minus 5 h |
| Availability | core/config.py:216; open_meteo.py:245-247,573-614,931-939 | a run counts 10 min after `last_run_availability_time`; metadata re-read before and after the data requests |
| Coalescing | core/config.py:124; sync/detect.py:571-589 | wave and atmosphere domains, and the GFS spot run and the map layers of a map cycle, wait up to 90 min for each other |
| Triggers | core/config.py:137; core/profiles.py:64-65 | default `spots,map`; free-database profile `map` with `MAP_FIELD_CYCLES=06,18` (a sync only for a new 06Z or 18Z map cycle) |
| Ingestion | models/forecast.py:73-75,113; ingestion.py:61,250-257,347-361 | `forecast_runs` unique (source_id, run_key); `wave_forecasts` unique (run_id, spot_id, valid_time), `ON CONFLICT DO NOTHING` in chunks of 2,000; a stored run key is skipped |
| Blend | forecasts/blend.py (816 lines); core/config.py:269 | built when two families exist; `FORECAST_BLEND_ENABLED` defaults true |
| Scoring | surf_quality/predict.py:41,58,404-406,660-662; scoring.py:51 | algorithm 2.1.0; breaking height `linear_wave_v2`; wind fit; forecast confidence; ML hook imported at module level; prediction archive written every run |
| After publish | sync/orchestrator.py:100,454-458,520-541 | detect, match, flights, alerts, deliver, prune |
| Retention | services/maintenance.py:160-182; core/config.py:409-416 | keeps the two newest usable runs per source past `RETENTION_FORECAST_DAYS` (10; 1 in the free-database profile) |
| Staleness | forecasts/queries.py:1-21,103-121 | per source at read time: about 17.2 h GFS family, 24.7 h ECMWF family, `FORECAST_STALE_HOURS`=18 for sources without a cycle |

### 2.3 Users, searches, matching, flights and notifications

| Part | Files | Behaviour at 18a5bee |
|---|---|---|
| Authentication | auth/security.py:10-14, auth/sessions.py:130-198, core/middleware.py:23-78, core/ratelimit.py, services/accounts.py:130-154 | Argon2id (t=3, m=64 MiB, p=4); opaque server-side sessions (only the SHA-256 of a 256-bit token stored, 14-day sliding expiry, HttpOnly/SameSite=Lax/Secure cookie); double-submit CSRF; Redis rate limits; account lockout after 8 failures |
| Email verification and reset | services/accounts.py:54-93,162-226; core/config.py:175-176 | single-use hashed tokens, 48 h (verification) and 60 min (reset); resend endpoints; all sessions revoked on reset |
| Saved searches | models/search.py, services/searches.py:33, api/searches.py, frontend/components/search/search-wizard.tsx | up to 25 per user; origins, destinations, wave range, minimum quality, budget, fixed/flexible dates, layovers, flight time, buffers, travellers |
| Swell events | services/swell_detection | sync stage `detect` clusters daylight hours into events |
| Matching | services/matching/service.py:297-366; core/config.py:346-347; jobs/schedule.py:156 | every active search against every active event starting 5-10 days ahead; after each publish and on a 30-minute timer |
| Flights | services/flights/** (duffel.py, amadeus.py, demo.py default); base.py:36-49; service.py:154-196,426-443; windows.py:64-153; jobs/schedule.py:157 | shared cache by route key, TTL 6 h; `FLIGHT_DAILY_QUOTA`=300; `FLIGHT_MAX_QUERIES_PER_MATCH`=3; re-search after `FLIGHT_REFRESH_HOURS`=12; `flights.discover` on a 60-minute timer |
| Alerts | services/notifications/alerts.py:289-331,334-354,611-614 | material-change rules (`decide_kind`), unique `dedup_key`; every flight-found candidate re-validated before deciding; `alerts.generate` every 15 min |
| Delivery | services/notifications/delivery.py, providers.py, api/webhooks.py:79-119 | `FOR UPDATE SKIP LOCKED`, retries with backoff up to 5 attempts; Resend / SendGrid / SMTP / console; Twilio SMS; signed delivery webhooks; `notifications.deliver` every 2 min (60 min floor when hosted) |
| Admin alerts | services/health.py, services/notifications/ops_alerts.py:318-326 | data-health problems emailed to active, verified admins only, with dedup, repeat and recovery notices |

### 2.4 Caches

| Layer | Key and lifetime | Invalidated by | Files |
|---|---|---|---|
| Redis public snapshot cache | `public:v1:<epoch>:<marker>:<UTC hour>:<route>[:params]`; payload TTL 2 h; single-flight lock; per-process memo up to 32 MB | a new marker at publication, the next UTC hour, a Redis flush (new epoch) | services/public_cache.py:1-55,167-210,305-443 |
| Derived marker | with no marker in Redis, one DB statement per request derives it | — | public_cache.py:182-210 |
| Provider response cache | Redis, 200 keys of up to 2 MB | TTL; off in the hosted profile | forecasts/provider_cache.py; core/config.py:229-231; core/profiles.py:48 |
| Quota counters and sync state | Open-Meteo minute/hour/day/month windows; `sync:known` (7 days), `sync:heartbeat` (6 h), kicks (1 day) | TTL | forecasts/quota.py; docs/sync.md "Redis keys" |
| Vercel CDN | public GETs `s-maxage=300, stale-while-revalidate=3600`; map field bytes named by run: immutable, 1 year; other field URLs 300/300; `/api/health/data` 60 s | TTL only (no `Vercel-Cache-Tag`, no purge) | api/spots.py:53-77; core/http_cache.py:21-46; api/map_fields.py:42-44,105-113; api/system.py:35,113 |
| Browser HTTP cache | `max-age=60` on public GETs; `fetch` with the default cache mode | TTL | frontend/lib/api.ts:66-72 |
| TanStack Query | staleTime 30 s by default, 120 s on forecast hooks, 5 min on map conditions; gcTime 5 min; keys carry no version | mount; focus (opportunities only); the two 300 s timers | frontend/hooks/queries.ts:27-48,62-203 |
| ETag / 304 | timeline responses | content change | api/timeline.py:30-51 |
| Next.js server caches | none (every page is a client component) | — | — |
| Flight search cache | `flight_searches` rows shared across users, 6 h | expiry | services/flights/service.py:154-196 |

### 2.5 Profiles (what "hosted" meant)

`backend/app/core/profiles.py`; docs/deploy-vercel.md:249-281 tells the owner to deploy
`profile_env("hosted", "free-database")`.

| Profile | Settings that matter here |
|---|---|
| hosted (profiles.py:35-55) | `JOB_RUNNER=daemon`, `FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf`, `MAP_FIELDS_ENABLED=true`, `GEFS_ENABLED=false`, `PROVIDER_RESPONSE_CACHE_ENABLED=false`, `JOB_INTERVAL_FLOOR_MINUTES=60`, `SYNC_CPU_BUDGET_HOURS=10`, `COMMERCIAL_USE=false` |
| free-database (profiles.py:57-67) | `RETENTION_FORECAST_DAYS=1`, `RETENTION_ARCHIVE_DAYS=7`, `SYNC_PRUNE_EACH_PASS=true`, `SYNC_TRIGGER_SOURCES=map`, `MAP_FIELD_CYCLES=06,18` |
| full-retention (profiles.py:69-79) | `RETENTION_FORECAST_DAYS=10`, `RETENTION_ARCHIVE_DAYS=730`, `SYNC_PRUNE_EACH_PASS=false`, `SYNC_TRIGGER_SOURCES=spots,map`, `MAP_FIELD_CYCLES=00,12` |

### 2.6 Scoring and research

* Deterministic score, algorithm 2.1.0 (surf_quality/scoring.py:1-6,51). Weights are a constant
  (scoring.py:53-60): direction 0.20, period 0.20, size 0.20, wind 0.30, tide 0.05, consistency
  0.05, renormalised over the components present.
* Breaking height `linear_wave_v2` (nearshore.py); wind conventions and offshore/onshore
  relations in wind.py:3-14 (FROM bearings, `u = -V sin(theta)`, `v = -V cos(theta)`).
* Forecast confidence `forecast-v1.2` (forecast_confidence.py); research confidence `research-v1.2`
  (services/research/assess.py); "local confidence" = min(forecast class, research basis)
  (forecast_confidence.py:16-19,564-565).
* Research data: data/surf_spots/spots.json (50 spots, 31 fields), research_evidence.json (468
  sources, 1,730 claims), profile_decisions.json (88 records), physical_profiles.json,
  data/tide_stations.json.
* ML: services/ml (2,986 lines) and backend/ml_models (3.9 MB, copied into the image by
  backend/Dockerfile:51). `ML_CORRECTIONS_ENABLED` defaults to true (core/config.py:307) but a
  correction applies only with an ACTIVE model version (services/ml/apply.py:174-186); the live
  database has none.

---

## 3. Findings by spec section (1-36)

Backend paths are relative to `backend/app/` unless they start with `backend/`; `fe/` means
`frontend/`. Evidence is shortened; the audits hold the full text. Measured values are in section 6.

### 3.1 Product, architecture, stack, audit (§1-4)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 1 | 50 major surf spots | exists | 50 slugs in data/surf_spots/spots.json, equal to the DB; e2e expects 50 markers (fe/e2e/map-helpers.ts:59) | — |
| 1, 28 | Short-notice priority (AU to Bali/Indonesia, US to Central America) | conflicts | `DETECTION_LEAD_MIN_DAYS`=5 (core/config.py:346); probe: swells 3.5 and 4.0 days out gave 0 matches | The core short-notice case never produces an opportunity (3.8) |
| 2 A | One user-independent acquisition; visitors never trigger provider requests | exists | provider code runs only in sync stages; no provider call in api/*.py except api/dev.py, refused in production (core/config.py:534-535) | — |
| 2 | No repeated cross-referencing of providers | conflicts | hosted `FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf` and `MAP_FIELDS_ENABLED=true` (core/profiles.py:46-47); blend on (core/config.py:269); live DB holds a blend run of 10,400 records | Two families plus NOAA GRIB layers acquired and blended per run |
| 2 | No ML models or training infrastructure | conflicts | services/ml 2,986 lines; ml_models 3.9 MB in the image (backend/Dockerfile:51); scikit-learn and joblib in the sync extra (backend/pyproject.toml:43-56); 0 ML versions in the live DB | The pipeline ships and sits in the scoring import path (surf_quality/predict.py:58) |
| 2 A | Rolling forecast, target 10 days | partial | `FORECAST_DAYS` default 16 (core/config.py:188); stored lead 0-381 h | 6 days over the target |
| 2 A | Detect new data, refresh at once, update the website | partial | metadata detection exists (sync/detect.py); free-database profile triggers only on 06Z/18Z map cycles (core/profiles.py:64-65) | No browser update path (3.4) |
| 2 B | Published predictions → events → matches → dates → flights → notify | exists | detect → match → flights → alerts → deliver after publish (sync/orchestrator.py:100,520-541) | — |
| 2 B | Flight searches only when necessary | conflicts | `flights.discover` every 60 min (jobs/schedule.py:157); re-search after `FLIGHT_REFRESH_HOURS`=12 (flights/service.py:426-443) | 3.8 |
| 3 | Next.js App Router, React, TypeScript, Tailwind, shadcn/ui, Recharts, map library | exists | fe/package.json: next 16.4.1, react 19.3.0, recharts 3.10.1, leaflet 1.9.4, react-leaflet 5.0.0, @tailwindcss/postcss 4.3.3; fe/components.json | Leaflet is the existing compatible library |
| 3 | Evaluate FastAPI vs Next.js server code; no unnecessary migration | partial | backend/app 53,935 lines, 41,328 test lines, 46 tables; only ADR-021/025 (docs/decisions.md:444-533) | No written evaluation (now 4.3) |
| 3 | No permanently running process inside Vercel Functions | partial | the Daemon is a container, not a Function (vercel.json:25-33), but it runs permanently and is Public Beta | Spec names Cron, a managed queue or an external scheduler |
| 3 | Hosted Postgres; evaluate Neon, Supabase, Prisma Postgres | partial | Neon chosen (ADR-026, docs/decisions.md:535-557); Supabase compared in Upgrade 4 research | Prisma Postgres not evaluated (now 4.5) |
| 3 | Prisma or a compatible ORM | exists | SQLAlchemy 2 + Alembic, migrations 0001-0008 (backend/alembic/versions); `alembic check` in CI (.github/workflows/ci.yml:117-120) | — |
| 3 | Background jobs via Vercel Cron, a managed queue or an external scheduler | conflicts | Daemon + daily watchdog (vercel.json:34); manual GitHub workflow | No refresh endpoint any scheduler could call (3.4) |
| 3 | Established secure auth, Vercel-compatible | exists | Argon2id, opaque sessions, CSRF, rate limits, lockout; backend/tests/integration/test_auth_api.py (18 tests) | A composition of standard primitives, not an auth product; never run on a real Vercel deployment; auth/security.py:13 attributes RFC 9106 parameters to OWASP |
| 3 | Transactional email (Resend or SendGrid) | exists | services/notifications/providers.py | Real delivery unverified (no credentials) |
| 3 | Deploy through Vercel connected to GitHub | partial | offline `vercel build` RESULT ok (CI run 40414842129) | Never deployed |
| 4 | Audit; docs/architecture_audit.md | missing | absent at 18a5bee | This document |

### 3.2 Weather providers and variables (§5-7)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 5 | Windy preferred future provider; Open-Meteo now and as fallback | conflicts | docs call Windy "not recommended" (docs/API_SETUP_REQUIRED.md:56-58, README.md:537) | Stance contradicts the spec |
| 5 | Works without a Windy key | exists | no profile lists `windy`; `test_requires_key` (backend/tests/unit/test_forecast_providers.py:257-259) | — |
| 5 | Common interface and one normalized structure; scoring and UI provider-independent | exists | `ForecastProvider` (forecasts/base.py:158-169), `ForecastRecord` / `FORECAST_FIELDS` (base.py:35-76), registry.py:30-77; scoring reads `wave_forecasts` rows only | No cross-provider contract test |
| 5 | `WEATHER_PROVIDER=auto`; Open-Meteo when Windy is unconfigured, unavailable or unauthorised | missing | `FORECAST_PROVIDERS` list; `acquire_all` calls every listed provider (forecasts/ingestion.py:496-515); read-time preference only (forecasts/queries.py:271-307) | Listing `windy,open_meteo` would fetch and store both every run; no runtime fallback |
| 5 | Free Open-Meteo only for non-commercial use | exists | `COMMERCIAL_USE` refuses free hosts without a key (forecasts/open_meteo.py:392-402); health `licence` problem (services/health.py:1291-1309) | Spec name `OPEN_METEO_COMMERCIAL` absent |
| 5 | Env names of the spec | partial | present: `WINDY_API_KEY` (core/config.py:335), `OPEN_METEO_API_KEY` (:196), `CRON_SECRET` (:107), `DATABASE_URL` (:69); equivalents: `WINDY_TERMS_ACKNOWLEDGED` (:226), `COMMERCIAL_USE` (:117), `FORECAST_STALE_HOURS`=18 (:190), `APP_BASE_URL` (:60); `FORECAST_DAYS` default 16 (:188) | Missing: `WEATHER_PROVIDER`, `FORECAST_REFRESH_ENABLED`, `FORECAST_STALE_THRESHOLD_MINUTES`, `NEXT_PUBLIC_APP_URL` |
| 5 | Validate config on startup; never expose server keys | exists | pydantic validators (core/config.py:502-574), redacted errors (:800-821), `get_settings` in `create_app` (main.py:55-56); keys only in backend settings; CSP `connect-src 'self'` (fe/next.config.ts:57) | Missing credentials only warn at start (by design) |
| 5, 6 | A Windy key is not permission to store or redistribute | partial | `windy` refused without `WINDY_TERMS_ACKNOWLEDGED` (core/config.py:552-559; forecasts/registry.py:56-64) | Nothing stops a TESTING key once the flag is true |
| 6 | Windy adapter now: gfs and gfsWave, consolidated calls | partial | `POST https://api.windy.com/api/point-forecast/v2` (forecasts/windy.py:47); per spot one gfsWave request (waves, windWaves, swell1, swell2) and one gfs request (wind, windGust, pressure) (windy.py:137-156); u/v converted by `geo.wind_from_uv` (geo.py:84-97) | Run identity from the clock (3.3); invalid key not recognised; tests use 1 h steps where the API serves 3 h (backend/tests/unit/test_forecast_providers.py:262) |
| 6 | Never show Windy TESTING data as production | missing | the response `warning` field is never read (windy.py:117-130); only a docstring note (windy.py:4) | Testing data would be stored and scored |
| 6 | Activation by configuration; health check; test refresh; verification | partial | needs `FORECAST_PROVIDERS` edited and `WINDY_TERMS_ACKNOWLEDGED=true` | No provider-check command, no refresh endpoint |
| 6 | docs/WINDY_API_SETUP.md | missing | absent | — |
| 6 | Attribution per Windy terms | partial | "Forecast data © Windy.com Point Forecast API" (windy.py:97); text link (forecasts/queries.py:215) | Terms ask for the logo and "Contains data from the Windy database" |
| 7 | Wind speed, direction, gust | exists | `wind_speed_10m`, `wind_direction_10m`, `wind_gusts_10m`, `pressure_msl` (open_meteo.py:147-152) | Gust and MSLP come from a later domain (ncep_gfs025) than wind (ncep_gfs013) |
| 7 | Hs, period, direction, primary and secondary swell, wind waves | exists | 15 GFS-Wave variables incl. secondary and tertiary swell (open_meteo.py:127-146); absent partitions are None (`test_absent_partitions_are_none_never_zero`) | ECMWF family has no partitions |
| 7 | Separate tide provider | exists | `TIDE_PROVIDERS=noaa_coops,harmonic_model` (core/config.py:290); 8 CO-OPS stations, EOT20 cells for the other 42 spots | — |
| 7 | Metadata per forecast (provider, model, run id, init, valid, retrieval, units, location, freshness) | partial | `forecast_runs.run_key`, `wave_model_run_at`, `atmosphere_model_run_at`, `details.model_runs` per domain (models/forecast.py:65-100; ingestion.py:119-142) | Per run, not per component; oldest-domain init labels mixed gfs013/gfs025 data; units fixed by convention (forecasts/base.py:1-7), not returned |
| 7 | UTC storage; local time in the frontend | exists | timeline in the spot's zone (fe/components/forecast/forecast-timeline.tsx:117-123); backend and frontend time-zone tests | — |
| 7 | No fabricated or unsupported variables | partial | absent values are None | Windy `pressure-surface` ("Air pressure") stored as MSL pressure (windy.py:171-172) |

### 3.3 Central processing, detection, refresh pipeline, versioning (§8-11)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 8 | Backend-only acquisition for the 50 spots | exists | spot stage requests active seeded spots only (docs/sync.md:27-31) | — |
| 8 | About one atmosphere + one wave request per spot, no per-variable calls, batching | exists | Open-Meteo: 3 data requests for 50 spots per GFS refresh; `FORECAST_BATCH_SIZE`=50 (core/config.py:191-193); a 400 for one location splits the chunk (open_meteo.py:859-863) | A new run in any domain refetches both components |
| 8 | No queries for unchanged runs; dedupe | exists | stored run key skipped (ingestion.py:347-361); idle check 0 data requests (docs/sync.md:316-318) | — |
| 8 | Respect provider limits | exists | weighted budget in Redis (forecasts/quota.py:1-67,152-174); Open-Meteo 429 daily/monthly honoured (open_meteo.py:705-722) | Usage visible only through the CLI |
| 8 | Lightweight storage, bulk upserts, retention, bounded caches, indexes | partial | chunked `ON CONFLICT DO NOTHING`; retention settings; indexes (models/forecast.py:111-116,158-166) | Full copy of every hour per run when one component changed; members and blend both scored; archive always written (predict.py:660-662); about 69 MB of NOAA GRIB per map cycle for an optional layer |
| 9 | Open-Meteo: the four meta.json fields, availability time, eventual consistency, no full fetch to check | exists | `ModelMeta.parse` (open_meteo.py:208-243); 10 min wait (core/config.py:216); re-read around the data requests (open_meteo.py:573-614,931-939); a lagging server is ignored (sync/detect.py:308-315) | `last_run_modification_time` is deliberately unused (correct); servers disagreed by up to 62 min (6.1) |
| 9 | Refresh as soon as new data is available, not once or twice a day | conflicts | free-database profile `SYNC_TRIGGER_SOURCES=map`, `MAP_FIELD_CYCLES=06,18` (core/profiles.py:64-65), the documented production setup (docs/deploy-vercel.md:249-281); live check 2026-10-27 00:11 UTC: ECMWF 12Z "new (not a trigger)" | GFS 00Z/12Z and ECMWF runs wait up to about 12 h, to fit Neon Free egress |
| 9, 10 | Wave and atmosphere update independently | conflicts | one run key per family (open_meteo.py:563); 90 min coalescing (core/config.py:124; detect.py:571-589); live: GFS-Wave 18Z available 23:17 waited for ncep_gfs025 until 01:41 | New waves wait for a late wind domain; the late domain costs a second full refetch |
| 9 | Windy: authoritative run indicator, else lightweight polling and fingerprints; no invented webhook | conflicts | `current_run` = nominal GFS cycle of now minus 5 h for both models, `metadata_source` "nominal_cycle" (windy.py:106-115; base.py:199-205) | Data labelled with an unverified cycle; a 100-request refresh every 6 h even when unchanged; no webhook or run endpoint was invented (correct) |
| 10 | Steps 1-16 of the pipeline | partial | present: 1 detect, 2 `ensure_servable`, 3 Postgres lease, 4 `sync_runs` + job logs, 6-8, 10-12, 14-15 (orchestrator.py:147-198,388-458) | Missing: 5 (always all spots), 9 validation (only "no spot returned data" fails: ingestion.py:450-456), 13 browser notification, 16 availability-to-publication latency |
| 10 | Fully automatic | partial | daemon loop or Celery beat | On Vercel only while the beta Daemon runs; the fallback is manual |
| 10 | New wind run keeps wave data with its original model time | conflicts | any domain change re-stores all hours of both components as a new run (ingestion.py:380-433; predict.py:640-668) | — |
| 11 | Unique version id per published forecast | partial | publication = `sync_runs` row (models/sync.py:40-79; sync/publish.py:47-78); Redis marker `p<sync_run_id>.<random>` | Not exposed to clients; no `forecast_versions` table |
| 11 | Version names `wind_model_run` and `wave_model_run` | missing | runs carry them (models/forecast.py:65-100), publications do not | — |
| 11 | Staging vs published; never serve in-progress data | conflicts | spot pages read the newest scored runs (spot_views.py:816-819,873-888; forecasts/queries.py:250-307; sync/publish.py:8-9); `blend_run_id` read by no request path | A run scored but not published (SIGTERM, failed publish, `simulate-swell`) is already served |

### 3.4 Website updates, scheduling, partial failure, caches, monitoring (§12-16)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 12 | Small version endpoint (`GET /api/forecasts/version`) | missing | no route under `/api/forecasts` | — |
| 12 | Browser polls; on change invalidates, refetches, no reload | missing | QueryClient staleTime 30 s, `refetchOnWindowFocus` false (fe/components/providers.tsx:15-16); measured over 60 min: 1 request per spot-page query, 13 for the map "now" snapshot and the field index | An open page never shows a newer forecast |
| 12, 23 | Map markers and ratings update | conflicts | with ocean layers the markers use `useMapConditions(at)` with no refetch (fe/app/map/page.tsx:93,101; fe/hooks/queries.ts:201-202) | Up to one 3-hour map step behind; the comment at queries.ts:190-193 says otherwise |
| 12 | Preserve selected spot | exists | slug in page state / route (fe/app/map/page.tsx:65; fe/app/spots/[slug]/page.tsx:443) | — |
| 12 | Preserve selected time | partial | `SelectionStore` seeds once (fe/components/forecast/timeline-selection.tsx:62-66) | Versioned keys without `keepPreviousData` would unmount the timeline (page.tsx:246,260) |
| 12 | Visible publication timestamp | partial | only in the stale chip and tooltip (fe/components/map/map-freshness.tsx:72-113); "Fetched N ago" from `completed_at` (fe/components/forecast-provenance.tsx:112-116) | No "Forecast updated N min ago" tied to the published version |
| 12 | New version visible within 60 s; latency measured | missing | derived: spot page unbounded, markers up to about 3 h, map "now" 5-15 min | Not measured; no e2e |
| 13 | Research Vercel limits; implement a scheduler if Hobby is too infrequent | partial | researched 2026-10-18/22 (docs/deploy-vercel.md:631-682) | The daemon quota there (10 CPU-h) is now 5; no scheduler path |
| 13 | Scheduler → secure refresh endpoint → processing | missing | only `GET /api/internal/cron/daily` (data health); trigger `"http"` reserved but unused (sync/types.py:30) | — |
| 13 | Processing fits a Function | partial | spot-only live sync 24.9 CPU-s / 42.7 s (docs/sync.md:337); `maxDuration` 60 (vercel.json:14); predict.py imports pandas (predict.py:41) and, through `ml.hook`, sklearn, joblib, scipy, pyarrow | Not in the lean API bundle, not in 60 s; the map stage (165-287 CPU-s, ecCodes) does not fit with margin |
| 13 | Locks, dedup, bounded retries, backoff, quota protection, idempotent writes, safe restarts | exists | lease with `uq_sync_runs_running` (models/sync.py:43-49; orchestrator.py:147-270); Redis job locks; `request_with_retries` 3 attempts with backoff and jitter (services/http.py:21,112-170); `SYNC_RETRY_MINUTES` / `SYNC_MAX_ATTEMPTS` (core/config.py:133-134); stop between stages on SIGTERM | Sync-level retries every 15 min, not exponential; with Redis down nothing is published (docs/sync.md:285-292) |
| 13 | Scheduled endpoints secured with a server-side secret | exists | `Bearer CRON_SECRET`, `hmac.compare_digest`, 404 when unset (api/internal.py:51-59) | Guards only the watchdog |
| 14 | Publish the successful spots, keep the previous forecast for failed ones | exists | a partial run covers only its `details.spot_ids` (forecasts/queries.py:276-277,303-306); probe: 3 failed spots served from the previous run | — |
| 14 | Mark failed spots stale; queue retries | missing | PARTIAL counts as ingested (ingestion.py:352-356; detect.py:67,236-242); stale only after 17.2 h | — |
| 14 | Detailed errors | exists | `details.spot_errors` (ingestion.py:423-433) | — |
| 14 | Never publish incomplete records as complete | partial | waves without wind: the spot switches to the new run without wind (open_meteo.py:942-955) | A complete older forecast can be replaced |
| 14 | Never erase working data | partial | prune keeps the two newest usable runs per source (services/maintenance.py:160-182) | With 1-day retention a spot failing twice can lose its fallback run |
| 14 | Clear stale wording; thresholds per model | partial | "The model run is 20 h old ..." (fe/components/stale-banner.tsx:19-44); thresholds from constants (forecasts/queries.py:1-21,103-121) | Wording about run age, not site update; not configurable per model |
| 15 | Inspect every cache | exists | section 2.4 | — |
| 15 | Version-aware keys or targeted revalidation | partial | Redis snapshot keys carry the marker | CDN, browser and TanStack keys carry no version; no cache tags |
| 15 | No long TTLs that block updates | conflicts | `s-maxage=300` + `stale-while-revalidate=3600` on unversioned URLs (api/spots.py:60-62; core/http_cache.py:37-46) | Up to about 66 min for an idle key's first visitor (derived) |
| 15 | Test the deployed caching behaviour | missing | no deployment | — |
| 16 | Freshness dashboard for the admin | partial | public `/status` (fe/app/status/page.tsx) on public `/api/health/data` and `/api/system/status` (api/system.py:87-190) | No admin-only view for usage, error detail and latencies |
| 16 | Active provider; atmosphere model; wave model | partial | providers list (api/system.py:171-174); model runs stored (ingestion.py:119-142) but not in `SourceHealthOut` (schemas/health.py:30-43) | No resolved provider, no separate model runs |
| 16 | Last detected update; last acquisition; last scoring; last published version | partial | job `last_success_at` (schemas/health.py:46-56); `last_published` sync (schemas/sync.py:50-73) | First sighting only in Redis `sync:known` |
| 16 | Updated spots; stale spots; failed requests | partial | `spot_count` only in a fallback branch (fe/app/status/page.tsx:57-59); `http_status_counts` stored per run (forecasts/base.py:101-127) | Stale spots missing; failures never aggregated |
| 16 | API calls consumed; average processing time | partial | CLI `python -m app.cli engine budget` (forecasts/cli.py:125-152); last run's stage durations | Not on a dashboard; no flight or email usage |
| 16 | Availability → publication and publication → frontend latency | missing | `available_at` and `published_at` stored, never subtracted; nothing on the client | — |
| 16 | Alerts: run late, stale, repeated failures, credentials, provider unavailable, quotas | partial | stale (services/health.py:299-366), `sync_failed` and `job_failed` (:446-578,1133-1150), CPU, DB size and Redis memory (:778-855,1079-1091) | Missing: credentials (401/403), run late vs expected, provider unreachable, Open-Meteo and flight quota warnings |
| 16 | Never email admin failures to users | exists | recipients are active, verified admins (notifications/ops_alerts.py:318-326); `OPS_ALERT_EMAILS` when the DB is down (api/internal.py:142-154) | — |

### 3.5 Spot research and scoring (§17-20)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 17 | Research the actual 50 DB spots | exists | DB slugs = spots.json; research_evidence.json covers 50/50 (468 sources, 403 read directly; 1,730 claims) | — |
| 17 | Height, period, direction, wind, tide, season and geography fields | partial | sourced values (of 50): min/max height 46, ideal bearing 50, offshore wind 50, tide stage 48, prime months 50, period 20, shelter 5, strong-wind limit 1, orientation 2 | No field for ideal height, short/long-period behaviour, blocked directions, cross-offshore and onshore sectors, ideal wind speed, rising/falling or size-dependent tide, secondary season, seasonal winds (table in 6.5) |
| 17 | docs/surf_spot_research/, one file per spot | missing | absent; the inputs exist (`compute_assessment`, services/research/assess.py:588-615) | — |
| 17 | No fabricated values for poorly documented spots | conflicts | every scoring field has a value at all 50 spots; values with zero supporting sources drive scores (period 16 spots, size 6, tide 11 incl. 5 nulls); only tide is gated on research class (surf_quality/scoring.py:220-232); `height_factor` unsourced at 50/50 | Unsourced values look like sourced ones in score and UI |
| 18 | Deterministic research-driven scoring, no ML | exists | scoring.py:1-6, algorithm 2.1.0; 0 of 25,000 live predictions ML-corrected | The ML hook is still imported unconditionally (predict.py:58) and `ML_CORRECTIONS_ENABLED` defaults true (core/config.py:307); see §2 |
| 18 | Inputs incl. secondary swell, tide, spot conditions | exists | `scoring.evaluate` (scoring.py:355-470): all partitions weighted by Hb^2.5, wind fit, tide when established | — |
| 18 | 0-100 with components and configurable weights | partial | `WEIGHTS` constant (scoring.py:53-60), renormalised (:393-395); contributions stored per hour (:410-413) | Weights change only in code |
| 18 | Smooth; big waves not universally high | exists | continuous curves; size falls to 0 at 1.5 x max (scoring.py:140-164); continuity tests (backend/tests/unit/test_surf_quality.py:257) | — |
| 18 | Longer period not always better | partial | `score_period` returns 1.0 above the ideal (scoring.py:146-148) | No spot-specific penalty for over-long periods |
| 18 | No "statistically validated" claims | exists | docs/SURF_SCORING.md:8-17; scoring.py:4-5 | — |
| 18 | Per-hour output incl. favourable and unfavourable conditions, research and forecast confidence, model version | partial | score, label, breaking range with p10/p90, wind relation/strength/favourability, explanation, components, forecast confidence + reasons, algorithm version (schemas/spots.py:40-104,290-350) | No structured favourable/unfavourable lists (the UI parses text: fe/lib/rating.ts:172-174); research class per spot, not per hour |
| 19 | Surfing definition of offshore; FROM bearings; u>0 east, v>0 north; per-spot orientation; consistent arrows | exists | wind.py:3-14,176-182; geo.py:84-97; fe/lib/directions.ts (`arrowRotationDeg` = from + 180); 72-direction round-trip test | — |
| 19 | Light offshore, calm, light cross-offshore favourable; light onshore marginal; strong onshore unfavourable | partial | probe on a west-facing beach: onshore 8 km/h Q 0.792 ("favourable"), 9 km/h 0.728, 15 km/h 0.367; offshore 40 km/h 0.61 | Light onshore up to about 8.6 km/h is "favourable" (pinned by backend/tests/unit/test_wind.py:231-237) |
| 19 | Tests for opposing coastline orientations | partial | `test_every_spot_is_offshore_at_its_offshore_direction` over the 50 spots | No paired test (e.g. ocean-beach-sf ~90 deg vs cloud-9 ~260 deg) |
| 20 | Research and forecast confidence separate, never combined | conflicts | "local confidence" = min(forecast, research) (surf_quality/forecast_confidence.py:16-19,564-565), shown on the spot page and the opportunity, event and notification cards (fe/app/spots/[slug]/page.tsx:148; fe/components/opportunity-card.tsx:60; fe/components/event-card.tsx:98; fe/app/notifications/page.tsx:121) | A merged class reaches users |
| 20 | Forecast confidence from lead time, freshness, uncertainty; heuristic label | exists | caps at 72/168/240 h, spread, staleness (forecast_confidence.py); "None of these classes is a calibrated probability" (:35-36) | Source-specific performance off (`VERIFICATION_ENABLED=false`) |
| 20 | Research confidence from source quality, agreement, bathymetry, observations, missing info | partial | tiers, independence, agreement, conflict downgrades, bathymetry cap (services/research/assess.py:1-46) | No historical observations; 8 variables only |
| 20 | Documentation matches the code | partial | `METHOD = "forecast-v1.2"`; docs/SURF_SCORING.md:560 says forecast-v1.1 | One stale reference |

### 3.6 Shared database and website delivery (§21-22)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 21 | Managed Postgres, normalized schema | exists | 46 tables after `init-db` (45 + `alembic_version`) | — |
| 21 | Suggested entities | partial | surf_spots; spot_profiles ≈ surf_spots columns + spot_physical_profiles; spot_research_sources / _claims / _assessments; forecast_model_runs ≈ forecast_sources + forecast_runs (per source run, not per component); forecast_data ≈ wave_forecasts; surf_predictions ≈ surf_quality_predictions; swell_events; refresh_jobs ≈ sync_runs + background_job_logs; users; saved_searches; flight_searches; flight_offers; travel_opportunities ≈ opportunity_matches + opportunity_offers; notifications | No forecast_versions; no per-component model runs |
| 21 | Forecast records carry version, publication time, freshness | partial | spot, run, valid time, measurements, score | No version id or publication time per record |
| 21 | Indexes, unique constraints, no duplicates of unchanged records, transactions | partial | unique (run_id, spot_id, valid_time); a run is `running` until committed | An unchanged component is stored again with each run |
| 21 | Observe storage and redistribution permissions | exists | Windy gate; Open-Meteo commercial guard | — |
| 22 | Website reads our published DB, never the provider per visitor | partial | never the provider: CSP `connect-src 'self'`, no provider host in fe/ | Reads the newest scored runs, not the published version (3.3) |
| 22 | `GET /api/spots`, `/api/spots/:id`, `/api/spots/:id/forecast` | exists | api/spots.py:108-153 (ids are slugs) | — |
| 22 | `GET /api/spots/:id/quality` | partial | quality per step is part of `/api/spots/{slug}/timeline` (api/timeline.py:30-51) and `/forecast` | — |
| 22 | `GET /api/forecasts/version`, `/api/forecasts/status` | missing | status exists as `/api/health/data` and `/api/system/status` (api/system.py:87-190) | Names and small payloads missing |
| 22 | `GET /api/opportunities`; private endpoints secured | exists | user-scoped, `no-store` (api/opportunities.py:25-28); fe/proxy.ts:9-20 | — |
| 22 | Performance test: 1,000 users on one spot | partial | measured with a scratch script (6.2): 3,000 requests, 8 DB transactions with the marker in Redis, 5,954 without | Not committed; propagation under load not measured; without a Redis marker every hit costs about 2 DB transactions |

### 3.7 Map, charts, frontend simplification (§23-25)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 23 | All 50 spots, quality markers, select, summaries, popups | exists | `SpotMarker` coloured by score (fe/components/map/forecast-map.tsx:211-264,363-378); tooltip (:257-262); `SpotPanel` (fe/components/map/spot-panel.tsx:30-170) | — |
| 23 | Bounded navigation, date-line wrapping, mobile | exists | fe/lib/map-bounds.ts:18-52; fe/components/map/surf-map.tsx:28-102; Pixel 7 Playwright project (fe/playwright.config.ts:48-49) | e2e not re-run in the audit |
| 23 | Markers update on a new version | conflicts | see §12 | — |
| 23 | Wind arrows, swell arrows, wave-height labels on markers | missing | plain circles (forecast-map.tsx:249-262); arrows only in the gridded overlays | Arrow helpers exist (fe/lib/directions.ts:8-22) |
| 23 | Global overlays optional, licensed, never invented from 50 points | exists | NOAA GFS and GFS-Wave grids from NODD, US government work (docs/map-layers.md:50-59); "Ocean layers are unavailable right now." fallback (fe/app/map/page.tsx:229) | — |
| 24 | Multi-day timeline (local days, hourly selection, now and selected markers, smooth scroll) | exists | fe/components/forecast/forecast-timeline.tsx:76-510; cursor-layer.tsx | Steps are the stored valid times (1, 3 or 6 h) |
| 24 | Quality timeline with best windows | partial | quality strip (fe/components/forecast/quality-strip.tsx:27-86); best day in the day navigation | Best windows not marked on the strip |
| 24 | Wave-height bars, ft/m, uncertainty | partial | p10-p90 band (fe/components/forecast/wave-bars.tsx) | Height unit only from the signed-in profile (fe/app/spots/[slug]/page.tsx:393) |
| 24 | Swell details; wind with gusts, arrows, offshore class | exists | swell-partitions.tsx; wind-row.tsx | — |
| 24 | Tide curve, high/low, rising/falling, suitability | partial | fe/components/forecast/tide-row.tsx:94-163 | No per-step tide suitability on the row |
| 24 | Shared cursor | exists | one `SelectionStore` (timeline-selection.tsx:29-85) | — |
| 24 | Latest published data; no decorative values | partial | values only from the API | The page never refetches (§12) |
| 25 | Always visible: spot, quality, height, wind, period, direction, best time, forecast confidence | exists | `SurfOutlook` (fe/app/spots/[slug]/page.tsx:71-165) | — |
| 25 | Expandable details | exists | "Why this rating?" (fe/components/rating.tsx:69-132), "Why this forecast?", "Research details", "About this forecast" | — |
| 25 | Headline example ("Excellent Surf — 9/10 — ...") | exists | `RatingHeadline` (rating.tsx:14-51) | One decimal (8.9/10); cosmetic |

### 3.8 Flights, matching, short notice, emails (§26-29)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 26, 27 | Search only on request, a new match or a material change; never requery for an unchanged forecast | conflicts | `flights.discover` every 60 min (jobs/schedule.py:157); re-search after 12 h (flights/service.py:426-443); every flight-found candidate re-validated before `decide_kind` (notifications/alerts.py:611 vs 614) and expired offers reset to `PENDING_FLIGHTS` (alerts.py:466-473); probe: `flight_searches` 3 → 4 → 5 over three hourly cycles, forecast unchanged | — |
| 26 | Explicit user request | partial | `POST /api/opportunities/{id}/refresh-flights`, 6 per hour (api/opportunities.py:51-63) | Only for an existing opportunity; inside the 6 h cache it returns cached, possibly expired offers |
| 26 | Material change | partial | matching re-queues on new dates or airport (matching/service.py:249-253,274-291) | Price changes noticed only through the timer paths |
| 26 | Research commercially usable flight APIs | partial | Duffel adapter matches current docs (flights/duffel.py:62-115); Amadeus Self-Service closed (flights/amadeus.py:3-9) | Duffel terms never reviewed (docs/API_SETUP_REQUIRED.md:333); see 4.9 |
| 26 | Never fabricate availability or prices | exists | demo offers `is_mock` (models/flight.py:64,121), "MOCK FARE — not a real price" (notifications/templates.py:217,269); revalidation never invents a price (flights/service.py:507-510) | — |
| 26 | Preferences: origin, destinations, wave range, minimum quality, budget, fixed and flexible dates, flight time, buffers, travellers | exists | models/search.py; schemas/searches.py; fe/components/search/search-wizard.tsx | Adults only; `wave_preferred_ft` unused |
| 26 | Multiple origins | partial | schema accepts 5 (schemas/searches.py:87) | `MAX_ORIGINS`=3 (flights/service.py:61,231) drops origins 4-5 silently |
| 26 | Maximum layovers | partial | 0-3 stored | Queried as min(max_layovers, 2) (flights/service.py:259) |
| 26 | Multiple saved searches per user | exists | 25 per user (services/searches.py:33) | — |
| 27 | On a new version: changed events → searches → new matches → windows → flights → store → notify | partial | after publish (orchestrator.py:100); full re-evaluation; matching also every 30 min (jobs/schedule.py:156) | Timer runs re-evaluate unchanged data |
| 27 | Buffers, time zones, transfers (Nov 10-13 → arrive Nov 8, depart Nov 14) | exists | `compute_travel_window` (flights/windows.py:64-153); verified at Uluwatu/DPS and Playa Hermosa/SJO | No test with the literal Nov dates (the Aug test is equivalent) |
| 27 | Dedupe identical searches | exists | `cache_key` shared across users (flights/base.py:36-49; service.py:154-167) | Sharing a quote across users not checked against Duffel's terms |
| 27 | Cache valid results briefly | partial | `FLIGHT_CACHE_TTL_HOURS`=6 (core/config.py:364) | Ignores offer expiry (about 30 min at Duffel): a search whose offers all expired is a cache hit |
| 27 | Revalidate prices when necessary; avoid expired offers | partial | `revalidate_offer` (flights/service.py:507-535); expired offers rejected (flights/filtering.py:108-109) | Revalidation runs on every alerts run, not only before sending |
| 27 | Respect quotas; per-user limits | partial | `FLIGHT_DAILY_QUOTA`=300 per provider (core/redis.py:147-162); refresh 6/h, 25 searches per user | No per-user budget of automatic queries |
| 28 | Initial markets | exists | 8 Indonesian spots, Costa Rica 3, Panama 1, El Salvador 1, Mexico 1; AU origins ADL BNE HBA MEL OOL PER SYD; 153 airports | No Nicaragua or Guatemala spots; small regional primary airports (BMU, BWX, LOP, BOC, PXM) |
| 28 | Prioritise windows about 3-7 days ahead | conflicts | new matches only 5-10 days ahead (core/config.py:346-347; matching/service.py:352-360); no lead-time term in the opportunity score (matching/scoring.py:16-79) | — |
| 28 | Communicate uncertainty without a fixed day-5/6 cut-off | partial | limitation and email copy (services/opportunity_views.py:239-240; notifications/templates.py:256-260) | The 5-day matching floor is the real cut-off; landing copy "~5–10 days out" (fe/app/page.tsx:23,107) |
| 28 | Users choose how far ahead they are alerted | missing | no lead field on saved_searches, travel_preferences or notification_preferences | — |
| 29 | Verification: tokens, expiry, status, resend, password reset | exists | services/accounts.py:54-93,162-226; api/auth.py:91-190 | Sent inline inside a 60 s function |
| 29 | Alert content (spot, dates, score, height, wind, confidence, flight dates, fare, airports, booking and dashboard links) | partial | notifications/alerts.py:200-286; templates.py:191-294 | Booking link is always a Google Flights search (alerts.py:410-420); airports as IATA codes only; no operating-carrier full name |
| 29 | No duplicates | partial | `dedup_key` UNIQUE (models/opportunity.py:276-298), `ON CONFLICT DO NOTHING` | No Resend `Idempotency-Key`; a POST is retried after a timeout (services/http.py:119,149); a stuck `sending` row is re-queued after 10 min |
| 29 | Delivery status, retries, preferences, unsubscribe, no private-data leaks | exists | signed webhooks (api/webhooks.py:79-119); backoff (delivery.py:188-205); `channels_for` (alerts.py:357-373); RFC 8058 one-click (api/notifications.py:198-229) | Hosted retries wait up to 60 min (`JOB_INTERVAL_FLOOR_MINUTES`) |
| 29 | New runs alert only on a material change or a new match | exists | `decide_kind` (alerts.py:289-331) | Downgrades are not notified |

### 3.9 Cost, setup docs, tests, README (§30-33)

| § | Requirement | Status | Evidence | Gap |
|---|---|---|---|---|
| 30 | Minimal recurring cost | partial | metadata-only checks, batched requests, publish-once snapshots, retention | Optional cost drivers (map GRIB, second family, blend, archive, 16 days) forced the 06Z/18Z cadence; the daemon estimate 4.4-7.9 CPU-h a month (5.3-9.5 with two syncs per GFS cycle) exceeds the 5 CPU-h Hobby quota over most of its range |
| 30 | No production LLM calls | exists | no openai/anthropic/langchain import in backend/app or frontend sources | — |
| 31 | API_SETUP_REQUIRED: Windy | conflicts | section 2.9 "(gated, not recommended)" (docs/API_SETUP_REQUIRED.md:310-323) | No quotas, no rights checklist, no testing-key caveat |
| 31 | Open-Meteo | exists | docs/API_SETUP_REQUIRED.md:61-95,150-206 | — |
| 31 | Flight API | partial | section 2.10 (:325-339) | No rate limits, markets or terms review; "booking links" overstated (:329) |
| 31 | Database | exists | :107, :428, :440-449 | Supabase and Prisma Postgres not discussed |
| 31 | Vercel | partial | plans and costs (:97-116) | No refresh schedule for Hobby vs Pro; stale daemon quota |
| 31 | Email | exists | :341-353 | — |
| 31 | No hardcoded secrets | exists | secret-pattern `git grep` over tracked files: no match | — |
| 32 | Acquisition tests | partial | exist: missing key, time zones, missing variables; partial: Open-Meteo (recorded captures), Windy (mocks with 1 h steps), invalid credentials (fake provider only), normalization (per provider) | Missing: provider fallback |
| 32 | Refresh tests | partial | exist: new run, unchanged run, job dedup, locking, publication; partial: partial failures, cache invalidation (Redis only) | Missing: independent wind/wave updates; frontend updates conflict (fe/tests/database-polling.test.tsx asserts no timers) |
| 32 | CRITICAL live update test (12 steps) | missing | steps 1-5 and 10-12 covered piecemeal in backend tests | Steps 6-9 have no mechanism |
| 32 | Surf quality tests | exists | 738 unit + 41 integration tests pass | No paired opposing-coast test; no "default settings apply no ML" test |
| 32 | User tests (signup, login, verification, saved searches, flight preferences, notifications) | exists | test_auth_api.py, test_account_and_searches_api.py, test_pipeline.py; fe/e2e/scenario.spec.ts | — |
| 32 | Deployment tests (build, hosted DB, migrations, cron, background work, env validation, endpoints) | partial | offline Vercel build in CI; migration and env-validation tests; fe/e2e/vercel-smoke.spec.ts on the local stack | PgBouncer tests skip in CI; no real deployment; only the watchdog cron |
| 32 | Suites pass | exists | backend unit 2,050 passed / 4 skipped, integration 512 / 3 skipped, vitest 486, Playwright 127 (CI run 40414842129) | — |
| 33 | README: local development | exists | README.md D, E.1-E.3, J | — |
| 33 | README: weather setup, automatic updates, Vercel deployment | partial | F.1-F.5, E.7, L | Windy "not recommended"; no versioning or browser-update text; no scheduler recipes |
| 33 | README: flight API, email | exists | section I, "Email delivery" | — |
| 33 | Exact commands verified | partial | 30/30 `python -m app.cli` commands answer `--help`; 10/10 referenced scripts exist | "Latest full run" table dated 2026-10-23 (2,040/503 vs 2,050/512 now) |

### 3.10 Order, non-negotiables, definition of done (§34-36)

§34 (order of work) is a process instruction: `n/a` for the audit.

| §35 | Non-negotiable | Status at 18a5bee | See |
|---|---|---|---|
| 1 | Deployable to Vercel | partial | 3.1 §3 |
| 2 | Works before a Windy key | exists | 3.2 §5 |
| 3 | Windy configured without code changes | partial | 3.2 §6 |
| 4 | Open-Meteo fallback | missing | 3.2 §5 |
| 5 | Licensing respected | partial | Windy testing guard and attribution (3.2); Duffel metasearch clause (4.9) |
| 6 | All 50 spots monitored centrally | exists | 3.3 §8 |
| 7 | No per-visitor fetching | exists | 3.1 §2 |
| 8 | Rolling 10-day horizon | partial | 3.1 §2 |
| 9 | Automatic new-model detection | partial | Open-Meteo exists; Windy clock-based (3.3 §9) |
| 10 | New run triggers processing promptly | conflicts | 3.3 §9 |
| 11 | New version appears on the site automatically | missing | 3.4 §12 |
| 12 | No browser refresh needed | missing | 3.4 §12 |
| 13 | Reliable publication and cache invalidation | partial | 3.3 §11, 3.4 §15 |
| 14 | New versions trigger matching | exists | 3.8 §27 |
| 15 | Flight requests personalized and targeted | conflicts | 3.8 §26 |
| 16 | Researched spot conditions | partial | 3.5 §17 |
| 17 | Offshore winds favoured appropriately | partial | 3.5 §19 |
| 18 | Research and forecast confidence separate | conflicts | 3.5 §20 |
| 19 | No fabricated forecast or flight information | partial | unsourced profile values (3.5 §17); Windy testing data (3.2 §6); mock fares are labelled |
| 20 | No unnecessary ML training infrastructure | conflicts | 3.1 §2 |
| 21 | No uncontrolled large local weather downloads | exists | GRIB decoded in memory; `MAP_FIELD_MAX_BYTES_PER_RUN`=600 MB (core/config.py:326) |
| 22 | Secure authentication | exists | 3.1 §3 |
| 23 | Email verification and notifications work | exists | real delivery unverified (3.8 §29) |
| 24 | Important functionality tested | partial | 3.9 §32 |
| 25 | README with verified instructions | partial | 3.9 §33 |

§36 definition of done, at 18a5bee:

| Status | Items |
|---|---|
| exists | Open-Meteo acquisition works (live runs 2026-10-16 and 2026-10-20/21); Windy storage disabled without authorization (gate); all 50 spots configured; scores generated; offshore scoring works; central acquisition; shared storage under the provider's licence (Open-Meteo CC BY 4.0); new swell events detected; searches matched; a clearly labelled test flight adapter (demo); email verification works; notifications work; frontend simple and responsive; tests pass; unverified integrations documented (README.md M; docs/API_SETUP_REQUIRED.md section 4) |
| partial | Vercel production build (offline only); hosted DB (PgBouncer emulation, never Neon); Windy adapter; profiles contain sourced research; model update detection; automatic processing (Daemon only); versioning; reliable publication; cache invalidation; stale forecasts identified; partial failures handled; flight requests deduplicated but not targeted; API usage monitorable (CLI only); README updated |
| missing | Windy activation instructions (docs/WINDY_API_SETUP.md); active sessions detect updates; charts update without reload |
| conflicts | markers update automatically |

---

## 4. Decisions and proposed changes (PLAN at audit time)

> This section is the plan of 2026-10-27, taken from the 2.0 design. It is not a description of
> the delivered code. Section 8, appended after integration, records what was actually built.

### 4.1 Summary

| D | Decision | Main reason | Main cost or risk |
|---|---|---|---|
| D1 | Keep FastAPI, SQLAlchemy 2, Alembic, Neon, Upstash and the current auth | migration buys nothing the spec needs | refresh path must stay lean for a Function |
| D2 | `WEATHER_PROVIDER` = auto / open_meteo / windy / demo, default auto; `FORECAST_PROVIDERS` becomes an advanced override | spec §5 selection semantics | auto stays on Open-Meteo until Windy is licensed |
| D3 | Windy rules: storage flag, testing-key refusal, gfs + gfsWave, fingerprint detection, daily counter, attribution, credential errors | no run metadata, standard terms forbid storage | licence may never be granted; Windy ingest delay unknown |
| D4 | Provider manager with health check and runtime fallback to Open-Meteo | spec §5, §35.4 | fallback changes step length (3 h → 1 h) between versions |
| D5 | One model family by default (Open-Meteo GFS + GFS-Wave); the rest off but kept; 10-day horizon | spec §2 (no cross-referencing), cost, delay | no independent-family input to confidence |
| D6 | Independent atmosphere and wave components with carried-forward data and original run times | spec §10 | correct labelling of carried data is critical |
| D7 | `forecast_versions` table; stage then publish in one transaction; serve only the published version | spec §11 | upgrade path before the first version |
| D8 | Secure refresh endpoint + external scheduler on Hobby (QStash every 5 min), Vercel Cron on Pro; Daemon optional | Hobby cron is daily; daemon quota 5 CPU-h | depends on a third-party scheduler on Hobby |
| D9 | Per-spot status, stale marking and targeted retries | spec §14 | a whole-request failure still fails all spots |
| D10 | Staleness = model interval + `FORECAST_STALE_THRESHOLD_MINUTES` | spec §5, §14 | threshold choice vs real publication lags |
| D11 | CDN-cached version endpoint, browser polling while visible, versioned data URLs, measured latency | spec §12, §15 | Upstash command budget |
| D12 | Admin-only freshness endpoint and page; new admin problems | spec §16 | Neon, Upstash and Vercel usage stay in their consoles |
| D13 | Scoring 2.2.0: structured reasons, separate confidences, configurable weights, light onshore capped | spec §18-20 | rescoring changes labels users saw |
| D14 | Generated per-spot research docs from quoted claims only | spec §17, no fabrication | many fields will read "not documented" |
| D15 | Flight searches only on request, new match, material change, pre-alert revalidation; per-user budget; lead times | spec §26-28 | Duffel metasearch clause; offer expiry |
| D16 | Cost levers: one provider, free metadata checks, one-component refreshes, 10 days, spot-only Function refreshes, visible-tab polling, no LLM | spec §30 | — |

### 4.2 Decisions in detail

**D1. Stack.**
*Decision:* keep FastAPI + SQLAlchemy 2 + Alembic + Neon Postgres + Upstash Redis + the existing
Argon2id / opaque-session auth. The spec's TypeScript layout maps to Python modules (4.11).
*Reason:* about 54k lines of Python, much of it numeric (numpy in the API; pandas, scikit-learn and
ecCodes in the sync), and 41k lines of tests; Vercel deploys FastAPI as a Function in Services mode; Python
became generally available on Vercel on 2026-10-15; Vercel Cron and external schedulers call any
HTTP route.
*Alternatives:* Next.js route handlers (4.3), Prisma (4.4), Supabase or Prisma Postgres (4.5),
Auth.js / Better Auth / Clerk (4.10).
*Cost:* none new.
*Risks:* Vercel Services is still Beta; the API function is 127.2 MiB of the 500 MB Python limit;
any module the refresh path imports must not import pandas, scikit-learn, scipy, pyarrow, joblib
or ecCodes at module level.

**D2. Provider selection.**
*Decision:* new `WEATHER_PROVIDER` (auto / open_meteo / windy / demo, default auto).
`FORECAST_PROVIDERS` stays as an advanced, explicit override. When it is unset the effective list
is derived: demo → `demo`; open_meteo → `open_meteo`; windy → `windy` (runtime fallback, D4);
auto → `windy` only when `WINDY_API_KEY` is set and `WINDY_STORAGE_AUTHORIZED=true`, otherwise
`open_meteo`. Tests, `.env.example` and the compose files keep `demo`; `.env.vercel.example` uses
auto.
*Reason:* spec §5 names and semantics; exactly one primary provider.
*Alternatives:* keep only `FORECAST_PROVIDERS` (rejected: listed providers are all fetched and
stored); select Windy whenever a key exists (rejected: a key is not a licence); Windy vs
Open-Meteo as the provider: 4.8.
*Risk:* auto silently stays on Open-Meteo while the flag is false. That is intended; the
provider-check command prints the reasons.

**D3. Windy rules.**
*Decision:* `WINDY_STORAGE_AUTHORIZED` (alias of `WINDY_TERMS_ACKNOWLEDGED`) must be true before
Windy data is stored, scored or published. A testing key is detected on every response by its
warning text ("The trial API version is for development purposes only..."); testing data is never
stored or published (the run fails with reason `windy_testing_key`, health shows it, auto and
windy fall back to Open-Meteo). Models: `gfs` (wind, windGust, pressure) and `gfsWave` (waves,
windWaves, swell1, swell2), one request each per spot: 100 requests per full refresh of 50 spots,
50 for a one-component refresh. Detection polls one sentinel spot per model every
`WINDY_CHECK_MINUTES` (15), only inside the expected availability window of each 6-hourly GFS
cycle, and compares a fingerprint (sha256 of `ts[]` and the first 24 values of each requested
series). A changed fingerprint is a new run; its run time is estimated from the newest
00/06/12/18Z cycle whose expected availability has passed and labelled `estimated` wherever shown.
Daily request counter in Redis, `WINDY_DAILY_REQUEST_LIMIT` (10000, reset 00:00 UTC). Attribution
"Contains data from the Windy database" with a link (the logo is an owner step in
docs/WINDY_API_SETUP.md). HTTP 400 `{"message":"Invalid API key"}` or 401/403 is a permanent
error and a credentials problem.
*Reason:* the response has only `ts`, `units` and an optional `warning`; no run field and no
webhook are documented; the standard terms forbid storage and derived databases; testing data is
"randomly shuffled and slightly modified" (6.6).
*Alternatives:* the current wall-clock cycle (rejected: labels data with a run it may not
contain); polling all 50 spots (rejected: 100 requests per poll); `ts[0]` alone as identity
(unknown whether it changes reliably).
*Cost:* Professional plan 990 EUR a year (owner's choice). Requests: 100 per full refresh; the
forecast audit estimated at most 688 a day with probes every 10 minutes all day plus four full
refreshes, against 10,000 a day.
*Risks:* Windy's delay between a GFS cycle and its availability is undocumented; whether paid
responses are deterministic (needed for fingerprints) is unknown; whether a key restricted to
"User Domains" works from a server without an `Origin` header is unknown; Windy serves fixed 3 h
steps.

**D4. Provider manager** (`backend/app/services/forecasts/provider_manager.py`, new).
*Decision:* `active_provider()`, `health_check(provider)` (Open-Meteo: one `meta.json`; Windy:
one sentinel request that detects a testing or invalid key) and `fallback_for(provider)`. When
Windy is primary and a refresh fails with a credential, testing-key, quota or availability error,
that refresh runs on Open-Meteo (same normalized structure); the version records
`provider=open_meteo`, `fallback_from=windy`, and the admin problem `provider_fallback` is raised.
One primary provider at a time; no cross-referencing by default. CLI:
`python -m app.cli forecast provider-check [--provider X]` (prints selection, eligibility reasons,
health; stores nothing).
*Alternatives:* fetch both and choose at read time (rejected: double cost, cross-referencing).
*Cost:* on fallback, an Open-Meteo refresh (about 125 weighted calls at 10 days).
*Risk:* consecutive versions from different providers differ in time step (3 h vs 1 h); the
version names its provider.

**D5. Simplified default.**
*Decision:* one family in every profile: Open-Meteo GFS 0.13/0.25 (atmosphere) and GFS-Wave 0.25,
the same NOAA models Windy serves as gfs/gfsWave. Off by default but kept and switchable: the
ECMWF family and the blend, GEFS, NOAA direct point data, the prediction archive (new
`PREDICTION_ARCHIVE_ENABLED`, default false) and ML corrections. No code is deleted.
`FORECAST_DAYS` defaults to 10 (maximum 16). The component split (D6) applies to the single-family
path; with the blend enabled the family-level behaviour of 18a5bee applies (documented
limitation).
*Note:* the design lists ML corrections as "`ML_CORRECTIONS_ENABLED=false` already". At 18a5bee
the code default is true (core/config.py:307; .env.example:259); nothing is applied only because
no ACTIVE model exists. The implementation should set the default to false explicitly.
*Reason:* spec §2; an ECMWF refresh costs 124.3 weighted calls, is read only at 00Z/12Z and adds
waits; the blend doubles predictions.
*Alternatives:* keep two families and the blend (rejected for the reasons above).
*Saving:* the GFS refresh drops from 142.9 to 125.0 weighted calls (10 days instead of 16); the
ECMWF refreshes disappear; stored rows drop by about a third.
*Risk:* forecast confidence loses the independent-family disagreement input and relies on lead
time, freshness and the heuristic label.

**D6. Independent components.**
*Decision:* a spot forecast source has two components: `atmosphere` (wind speed, direction, gust,
MSLP) and `wave` (Hs, Tp, direction, swell partitions, wind sea, sea level when present). Each has
its own run key from its own model domains (Open-Meteo: wave = ncep_gfswave025; atmosphere =
ncep_gfs013 + ncep_gfs025), its own availability time and detection state. A component is due as
soon as any of its domains advanced (monotonic) and is servable (availability + 10 min).
`SYNC_COALESCE_MINUTES` defaults to 0 and applies only within a component. A refresh fetches only
the due components (Open-Meteo: marine request for wave, forecast request for atmosphere; Windy:
gfsWave / gfs) and builds the new run by carrying the other component forward from the serving
run with its original model run times (`forecast_runs.wave_model_run_at` /
`atmosphere_model_run_at`, `details.model_runs`, `details.components`). Carried data is never
labelled with the new run. Scores are recomputed for the whole new run.
*Reason:* spec §10; measured 46-107 min between GFS-Wave and GFS 0.25 availability for the same
cycle, and wind (ncep_gfs013) and gust/MSLP (ncep_gfs025) are two domains published 40-100 min
apart.
*Alternatives:* keep 90-minute coalescing (rejected: delays publication); coalescing 0 without the
split (a sync per domain arrival, each refetching both components, about 143 weighted calls).
*Cost:* per GFS cycle a wave refresh (75 weighted calls at 10 days) and one or two atmosphere
refreshes (50 each), one per atmosphere domain arrival.
*Risk:* hours past the carried component's horizon have only one component; labelling and
validation must handle that.

**D7. Versions.**
*Decision:* new table `forecast_versions` (Alembic 0009): status staging / published / superseded
/ failed; provider; source code; forecast run; previous version; sync run; wind and wave model and
run (with `*_run_estimated`); `fallback_from`; `components` and `spot_runs` JSONB; counts of
spots total / updated / stale / failed; detected, upstream-available, acquired, scored, staged,
published and superseded times; timings. A unique partial index allows one published version.
Every refresh stages a version after acquisition, scoring and validation and publishes it in one
transaction (previous published → superseded). Serving reads only the published version
(`versions.serving_runs()` maps spot → run; failed spots map to their previous run and are marked
stale). Before any version exists, the legacy selection of 18a5bee is used. Map layers keep their
own pointer (`sync_runs.map_run_id`, publish gate); the version endpoint reports the map run too.
The same migration adds `saved_searches.alert_lead_min_days` / `alert_lead_max_days` (D15).
*Reason:* spec §11; at 18a5bee spot pages serve runs that were never published.
*Alternatives:* new columns on `sync_runs` (rejected: a version needs a per-spot run map and its
own lifecycle, while `sync_runs` stays the job record).
*Cost:* one row per refresh; prune keeps the published and the previous published version and
every run they reference.

**D8. Scheduling.**
*Decision:* new `GET` and `POST /api/internal/refresh` (`Bearer CRON_SECRET`,
`hmac.compare_digest`, 404 when `CRON_SECRET` is unset). `FORECAST_REFRESH_ENABLED=false` answers
`{"status":"disabled"}`. It takes the Postgres sync lease (busy → 200 `{"status":"busy"}`, never a
retry storm), runs the free metadata plan and, when a spot component is due, a spot-only sync (no
map stage) inside the function budget, returning a JSON summary (plan, version id, timings). It is
idempotent. Scheduling:
* Hobby: an Upstash QStash schedule every 5 minutes (288 calls a day of the free 1,000; QStash
  waits up to 15 minutes for the response and forwards the bearer through
  `Upstash-Forward-Authorization`). Alternative: cron-job.org (free, every minute, but it closes
  the connection after 30 s, so it is documented as unverified for longer runs).
* Pro: a `*/5 * * * *` Vercel Cron entry (documented). On Hobby, vercel.json keeps only daily
  crons, because a sub-daily expression fails a Hobby deployment.
* The existing daily cron (`/api/internal/cron/daily`) also runs one refresh check (backstop).
* The Daemon becomes optional: new `SYNC_DAEMON_KINDS` (default `spots,ensemble,map`; hosted
  profile `map`), so on Hobby it refreshes only the optional GRIB map layers
  (`MAP_FIELD_CYCLES=06,18`); without it the map overlay ages and is labelled stale.
  `SYNC_CPU_BUDGET_HOURS` hosted default 5.
* The API function's `maxDuration` rises to 300 s (the Hobby maximum). The refresh path imports
  lean: predict.py loses its pandas use and imports the ML hook lazily only when ML corrections
  are enabled and an active model exists (covered by tests/unit/test_lean_imports.py).
* Celery and the Daemon honour `FORECAST_REFRESH_ENABLED`.
*Reason:* Hobby cron runs once a day (±59 min); the Hobby daemon quota is 5 Active CPU-hours a
month, while the repo's own estimate is 4.4-7.9 CPU-h (5.3-9.5 with two syncs per GFS cycle); at
the quota the daemon stops until the 1st of the next month; Daemons are Public Beta and
production-only; spec §13 prefers scheduler → secure endpoint.
*Alternatives:* 4.6.
*Cost:* QStash free tier; Vercel Pro $20 a month if chosen; Function Active CPU on Hobby (4 CPU-h a
month included). Not measured on Vercel: at a 5-minute cadence the metadata checks alone come to
about 0.4-1.0 CPU-h a month (8,640 checks at the 0.17-0.40 CPU-s measured in docs/sync.md;
derived), plus the spot-only syncs (24.9 CPU-s measured for one live sync).
*Risks:* QStash retries count against the 1,000 a day; Vercel Cron does not retry and may deliver
twice (the lease makes this harmless); Open-Meteo warns that free-tier callers on Vercel may be
rate-limited earlier; the map stage (165-287 CPU-s) stays out of Functions.

**D9. Partial failures.**
*Decision:* per-spot status in the version (`spot_runs`). A spot whose data failed (request error,
masked or missing cells, validation failure) keeps its previous run with status `stale` and a
redacted error; the sync status is `partial`; a retry of only the failed spots is planned
(component state `failed` with `retry_spot_ids`, backoff 5 / 15 / 45 min, bounded by
`SYNC_MAX_ATTEMPTS`), and its success publishes a version that updates just those spots. A version
with zero updated spots is never published; a whole-component failure publishes nothing.
Validation before staging: per spot at least `FORECAST_MIN_VALID_FRACTION` (0.8) of hours carry Hs
and wind, values in physical ranges; otherwise the spot fails.
*Reason:* spec §14; the probe showed failed spots silently waiting for the next model run.
*Alternatives:* wait for the next run (current behaviour).
*Risk:* Open-Meteo batches all 50 spots in one request, so a transient whole-request failure still
fails every spot and becomes a component retry.

**D10. Staleness.**
*Decision:* a spot or component is stale when now minus its last successful update exceeds the
model's update interval (`update_interval_seconds` from `meta.json`: 6 h for GFS; Windy 6 h) plus
`FORECAST_STALE_THRESHOLD_MINUTES` (360), or when its latest refresh failed while other spots
updated. Optional per-model override `FORECAST_STALE_THRESHOLD_MINUTES_BY_MODEL` (JSON). The UI
says "Forecast last updated 7 hours ago" (relative, from `last_updated_at`) and never labels stale
data current.
*Reason:* spec §5 and §14 ask for a configurable minutes threshold.
*Alternatives:* keep the derived constants (17.2 h GFS; rejected: not configurable); a blanket
360 minutes (rejected: the platform audit noted it would mark every GFS run stale before its
successor, because domains arrive 5.3-9.3 h after the cycle).

**D11. Live updates.**
*Decision:* `GET /api/forecasts/version` reads Redis or the per-process memo only (one DB read
re-seeds Redis when the marker is missing; it never wakes Neon while a version is cached). Body:
version id, published time, provider, source code, wind and wave model and run (with
`*_estimated`), spot counts, map run id and cycle, attribution. Headers: `Cache-Control: no-cache`
and `CDN-Cache-Control: public, s-maxage=15, stale-while-revalidate=15`. The browser polls it every
`NEXT_PUBLIC_FORECAST_VERSION_POLL_MS` (20000) only while the tab is visible, and at once when it
becomes visible. A change invalidates the public forecast queries, whose keys and URLs carry
`?v=<version_id>` (`keepPreviousData`; selected spot and time kept, snapped to the nearest valid
time), updates a visible "Forecast updated hh:mm" label and announces it (`aria-live`). The map
conditions query loses its 300 s timer. The public snapshot marker becomes the version id plus the
map run id; a request whose `v` is not the current version is answered `no-store`. Latency: the
endpoint records `first_served_at` per version (Redis `SETNX`); the client sends
`POST /api/forecasts/seen {version_id}` with `navigator.sendBeacon` once per version per tab after
rendering; the server keeps receipt time minus `published_at` in a capped Redis list (200) for
p50/p95 on the admin page. `GET /api/forecasts/status` gives a public freshness summary, cached per
version.
*Reason:* spec §12 and §15: a version-check endpoint is acceptable initially; polling a CDN-shared
response costs per CDN region, not per tab, and keeps Neon asleep.
*Alternatives:* 4.7.
*Cost:* at `s-maxage=15`, about 2,592,000 / 15 = 172,800 Upstash commands a month per active CDN
region (the frontend audit's formula), on top of the about 386,000 projected with steady visitors
(docs/operations.md:358) against the 500,000 free tier. Over the limit Upstash bills $0.2 per
100,000.
*Expected latency (derived, to be measured):* up to about 30 s at the CDN (15 fresh + 15 stale)
plus the 20 s poll plus the refetch, i.e. about 50 s worst case; the frontend audit's variant
without stale-while-revalidate and `s-maxage=10` gave about 32 s.

**D12. Monitoring.**
*Decision:* admin-only `GET /api/admin/freshness` (session and `is_admin`, 403 otherwise; never
CDN-cached) with every §16 metric: active provider and fallback state; per component model, run,
estimated flag, availability, last detected, last acquired; last published version and counts;
stale spots; failed requests (24 h by provider and status); API usage (Open-Meteo windows and
limits, Windy day, flights today and quota, emails today); processing time (average and p95 of the
last 20 refreshes by stage); latency (upstream → publication from `upstream_available_at` →
`published_at`; publication → frontend from the seen samples; first-served lag); the last 20
refresh jobs; problems. Admin page `/admin/freshness`. Public `/status` and `/api/health/data`
stay (uptime monitors use them). New admin problems, emailed to verified admins only:
`model_run_late`, `forecast_stale_spots`, `refresh_failures` (3 or more consecutive),
`provider_credentials`, `provider_unavailable` (metadata unknown for more than 30 min, or fallback
active), `provider_fallback`, `quota_near` (`QUOTA_ALERT_FRACTION` 0.8 of Open-Meteo day/month,
Windy day, flight day), `refresh_scheduler_silent` (no check for
`REFRESH_SCHEDULER_EXPECTED_MINUTES` 30; 0 disables).
*Alternatives:* extend the public report (rejected: error text and usage are admin data); move
`/status` behind auth (rejected: external uptime monitors read it).
*Risk:* Neon, Upstash and Vercel usage are still read in their own consoles.

**D13. Scoring 2.2.0.**
*Decision:* per-hour output adds `favourable` and `unfavourable` (short plain reasons from the
component scores), `research_confidence` (the spot's research class, separate),
`forecast_confidence` (lead and freshness heuristic, labelled heuristic), `offshore_class`,
`breaking_height` where defensible and `model_version`. Weights configurable through
`SCORING_WEIGHTS_JSON` (validated; defaults = the current constants). Light onshore below about
8.6 km/h is capped at "workable". The combined local confidence class is removed from the API,
the UI and alerts. Tests on opposing coastlines (ocean-beach-sf offshore about 90 deg, cloud-9
about 260 deg, and a synthetic pair) prove the same wind scores opposite.
*Reason:* spec §18-20.
*Alternatives:* keep local confidence as an expert detail (rejected: spec §20 forbids combining);
down-weight unsourced components (not chosen; open question 7.11).
*Cost:* a rescoring pass; tests that pin the old behaviour change on purpose
(backend/tests/unit/test_wind.py:231-237).

**D14. Research documents.**
*Decision:* `docs/surf_spot_research/<slug>.md` for all 50 spots, generated by
`scripts/generate_spot_research_docs.py` from spots.json, research_evidence.json,
profile_decisions.json and a new research_conditions.json, with a unit test that fails when a
document is out of date. research_conditions.json holds the §17 fields extracted only from
existing quoted claims (each value cites claim ids); a field with no supporting quote is "not
documented".
*Reason:* spec §17 without fabricated values.
*Alternatives:* hand-written files (rejected: drift); a new research round (not in this plan; see
open question 7.17).
*Risk:* many fields will read "not documented" (6.5); 15 spots are Low overall, including 4 of
the 8 Indonesian spots.

**D15. Flights (system B).**
*Decision:* flight searches only on (a) explicit user request, (b) a new qualifying match, (c) a
material change (travel dates, destination airport or surf window moved; quality class crossing
the search threshold), (d) price revalidation right before an alert is sent. The periodic
`flights.discover` timer and the `FLIGHT_REFRESH_HOURS` re-search are removed. Cache TTL =
min(`FLIGHT_CACHE_TTL_HOURS`, earliest offer expiry). Per-user daily budget
`FLIGHT_USER_DAILY_QUOTA` (20) and round-robin across users. All origins (up to 5) searched within
the per-match budget; up to 3 layovers honoured; `wave_preferred_ft` used in ranking. Per-search
alert lead time `alert_lead_min_days` / `alert_lead_max_days` (defaults 2 / 10) replace the global
5-day floor (`DETECTION_LEAD_MIN_DAYS` default 1); short-notice boost for 3-7 days
(`SHORT_NOTICE_MIN_DAYS` / `_MAX_DAYS`). Matching runs on every publication plus a daily
housekeeping run for expired windows. Resend `Idempotency-Key` per notification and account
email. Google Flights / Skyscanner links labelled "route search (fares may differ)". Operating
carrier names shown. The labelled demo provider stays the default; the Duffel adapter is kept
with a documented warning about the metasearch clause (written confirmation needed); Amadeus
Self-Service is closed (secondary sources), Enterprise only, untested.
*Reason:* spec §26-28; measured re-search loop and short-notice gap (6.4).
*Alternatives:* 4.9.
*Cost:* with Duffel and no orders, (9,000 − 1,500) × $0.005 ≈ $37.50 a month at the 300-a-day
quota (research reading of the Services Agreement; the travel audit's $45 assumed no free
allowance).
*Risks:* Duffel may class the app as metasearch; offers expire in about 30 minutes; Duffel prices
come in the account's billing currency; small regional destination airports may not be covered.

**D16. Cost.**
*Decision:* one provider; metadata-only detection (free); one-component refreshes; a 10-day
horizon; spot-only refreshes in Functions; Redis/CDN version polling only while a tab is visible;
no LLM calls. Recurring prices known at audit time:

| Service | Free tier or price (source in 6.6) | Use in the plan |
|---|---|---|
| Vercel | Hobby $0, non-commercial personal use only; Pro $20 a month platform fee | Hobby-compatible configuration; Pro documented |
| Neon | Free: 1 GB storage, 5 GB egress, 100 CU-hours per project | kept; egress to be re-measured |
| Upstash Redis | Free: 500K commands a month; pay-as-you-go $0.2 per 100K | locks, budgets, snapshots, version marker |
| Upstash QStash | Free: 1,000 messages a day, 10 schedules | 5-minute refresh schedule on Hobby (288 a day) |
| Open-Meteo | free for non-commercial use (10,000 calls a day); Standard 1M calls a month ($29 a month in 2023; 2026 price not readable) | default provider |
| Windy | Professional 990 EUR a year, 10,000 requests a day | only after a written licence |
| Duffel | $3 per order + 1 % managed content; $0.005 per search over a 1500:1 search-to-order ratio | adapter kept, not default |

### 4.3 FastAPI vs Next.js route handlers

| Criterion | Keep FastAPI (chosen) | Migrate to Next.js route handlers |
|---|---|---|
| Code to move | none | 53,935 lines of Python (services 43,757; routes 1,925) and 41,328 lines of tests (1,540 test functions) |
| Numeric work | numpy in the API; pandas, scikit-learn, ecCodes in the sync | rewrite scoring, nearshore physics, detection and GRIB decoding in TypeScript |
| Vercel support | FastAPI and Next.js "deploy together with shared routing" in Services mode; Python GA on 2026-10-15 | native |
| Schedulers | any HTTP route | same |
| Background time | the Python SDK lists no `wait_until` | `after()` / `waitUntil()` run only within `maxDuration` |
| Risk | none new | large effort, regression risk in scoring and detection |

Thin Next.js route handlers would be used only where a TypeScript endpoint is clearly better;
none is planned.

### 4.4 ORM

SQLAlchemy 2 + Alembic is the "appropriate compatible ORM" (8 migrations, `alembic check` in
CI). Prisma would need a TypeScript backend: its Python client on PyPI is version 0.15.0 (uploaded
2024-08-16) with classifier "Development Status :: 4 - Beta" (read 2026-10-27).

### 4.5 Neon vs Supabase vs Prisma Postgres

| Criterion | Neon (chosen) | Supabase | Prisma Postgres |
|---|---|---|---|
| Free tier | 100 CU-hours per project, 1 GB storage, 5 GB egress | 500 MB database, 5 GB egress | 200k operations a month, 2 GB storage |
| Pausing | scales to zero | free projects paused after 1 week of inactivity; 2 active projects | — |
| Paid entry | Launch (not priced in this audit) | — | Starter $10 a month, 1M operations, then $8 per million |
| Metering | compute and egress | size and egress | every query is one operation; unlimited data transfer |
| Fit with this app | already wired (pooled and direct URLs, Marketplace, PgBouncer-mode tests) | 500 MB and the weekly pause rule it out for the free profile | 475 SQL statements per forced demo sync; about 83k statements a month at the old free-database cadence (estimate) |
| Unknowns | egress after the 2.0 simplifications | — | direct TCP connections from psycopg; whether an `executemany` batch counts as one operation |

Decision: keep Neon and re-measure egress (`scripts/measure_db_egress.py`) with one family, 10
days, no archive and spot-only syncs. If Neon Free's 5 GB egress binds, the options are Neon Launch
or Prisma Postgres once direct connections are confirmed.

### 4.6 Schedulers

| Option | Frequency and timeout | Cost | Caveats | Role in the plan |
|---|---|---|---|---|
| Vercel Cron, Hobby | once a day, ±59 min | $0 | a sub-daily expression fails the deployment; no retries; duplicates possible | daily watchdog + backstop refresh |
| Vercel Cron, Pro | once a minute, per-minute precision | Pro $20 a month | no retries; duplicates possible | `*/5` refresh on Pro |
| Vercel Daemon | permanent container | Hobby: 5 Active CPU-h a month, then stopped until the 1st | Public Beta; production only; not with a subfolder Root Directory; SIGTERM grace 30 s by default | optional; map layers only on Hobby |
| Upstash QStash | cron schedules, can fire every minute; waits up to 15 min for the response; signed requests; 3 retries by default | Free: 1,000 messages a day (retries count), 10 schedules | third-party dependency | recommended Hobby scheduler, every 5 min |
| cron-job.org | every minute; closes after 30 s; custom headers | free, donation-financed, no SLA | disables a job after more than 25 failures | alternative; unverified for runs over 30 s |
| GitHub Actions schedule | 5-minute minimum; delays and dropped jobs at load | free for public repos | disabled after 60 days without activity (public repos); terms discourage use "as part of a serverless application" | manual fallback only (existing data-sync.yml) |
| Vercel Queues / Workflows | Beta; visibility timeout up to 60 min | Hobby: 1,000,000 queue operations; 50,000 workflow events | Beta | not needed: one refresh fits one Function call |
| Celery beat | needs a long-running process | — | unsupported on Vercel | classic compose stack only |

Detection latency is bounded by the providers, not by the poll: Open-Meteo published the
2026-10-26 18Z GFS run 5.3-7.1 h after its cycle, so a 5-minute check plus the 10-minute wait adds
at most about 15 minutes.

### 4.7 Live updates: polling vs SSE vs WebSockets

| Option | Facts | Decision |
|---|---|---|
| Version polling of a CDN-cached endpoint | one origin call per CDN region per `s-maxage`; Redis-only endpoint keeps Neon asleep | chosen (D11) |
| WebSockets in Vercel Functions | generally available since 2026-10-20; connections close at the function's maximum duration (300 s on Hobby); billed as function time | not used |
| Server-Sent Events | a streaming Function response, so the same duration limit and billing apply (inference; not researched separately) | not used |
| Managed realtime service | not evaluated in this audit | not used |
| CDN purge by cache tag | `Vercel-Cache-Tag` + purge, all plans, Hobby 50 purge calls/s per team; invalidation marks the tag stale, so one stale answer is still served | not needed with versioned URLs |

### 4.8 Weather provider: Windy vs Open-Meteo

The spec (§5) makes Windy the preferred future provider and Open-Meteo the current provider and the
fallback. For this app both serve the same NOAA models (GFS and GFS-Wave). The choice therefore
turns on licence, request shape, time step and update detection. Forecast skill was not compared:
no live Windy call was made in this audit.

| Criterion | Open-Meteo | Windy Point Forecast API |
|---|---|---|
| Models used | GFS: `ncep_gfs013` (10 m wind) and `ncep_gfs025` (gust, MSLP) under `gfs_global`; GFS-Wave `ncep_gfswave025`; ECMWF IFS and WAM also offered (opt-in family) | `gfs` (wind, windGust, pressure) and `gfsWave` (waves, windWaves, swell1, swell2); ECMWF is excluded from Point Forecast "due to licensing conditions" |
| Requests for 50 spots | comma-separated locations: 3 data requests per GFS-family refresh; 125.0 weighted calls at 10 days (wave 75, atmosphere 50) | the body takes one `lat`/`lon`: one request per model per spot, 100 per full refresh, 50 per one-component refresh |
| Time step | hourly (the API interpolates to 1 h; the underlying model may be 3- or 6-hourly) | fixed 3 h (Windy staff, 2026); a staff example response has 80 steps 3 h apart, 10 days of `gfs` |
| Horizon | 16 days; GFS-Wave returned 384 hourly steps for `forecast_days=16` (6.1); the plan uses 10 | each response carries the whole available forecast |
| Run metadata | `meta.json` per domain: init, availability, modification time, update interval; metadata calls do not count against the limits | none: the response schema has only `ts`, `units` and an optional `warning`; no metadata endpoint or webhook is documented |
| Detection in the plan | metadata check every 5 min, then the 10 min wait (D6, D8) | one sentinel spot per model every 15 min inside the expected window, fingerprint comparison, run time labelled `estimated` (D3) |
| Publication delay | 2026-10-26 18Z: GFS-Wave +317 min, GFS 0.13 +322, GFS 0.25 +363 to +424 (6.1) | undocumented; staff: "the same times as the standard version"; gfsWave at most every 6 h |
| Free tier | under 10,000 calls a day, 5,000 an hour, 600 a minute; non-commercial only; apps on Vercel may be rate-limited earlier | Testing: free, 500 requests a day, "randomly shuffled and slightly modified data", development only |
| Paid tier | Standard 1M calls a month ($29 a month in 2023; the 2026 price could not be read); customer endpoint with `apikey` | Professional 990 EUR a year (annual, auto-renewing), 10,000 requests a day, more by agreement |
| Storing and showing the data to every user | CC BY 4.0: allowed with attribution; commercial use needs a subscription | the standard terms forbid storing, derived databases, redistribution and paywall-only use: a written licence is needed |
| Attribution | a link next to the data ("Weather data by Open-Meteo.com") | the Windy logo (unscaled, clickable, 100 % opacity) and "Contains data from the Windy database" |
| Errors | HTTP 429 with daily or monthly limit messages (handled: open_meteo.py:705-722) | documented: 200, 204, 400, 500; an invalid key reportedly returns HTTP 400 `{"message":"Invalid API key"}`; the quota-exceeded response is undocumented |
| Keys | free hosts take no key; customer hosts take `OPEN_METEO_API_KEY` | Point Forecast keys are separate from Map Forecast keys; public-application keys work only from the registered User Domains |
| State at 18a5bee | hosted default; live acquisitions on 2026-10-16 and 2026-10-20/21 | adapter present, gated by `WINDY_TERMS_ACKNOWLEDGED`, 3 mock-only tests; never called live |

*Decision:* Open-Meteo stays the default and the fallback. The Windy adapter is completed per D3
and selected only when `WEATHER_PROVIDER` is `windy`, or `auto` with `WINDY_API_KEY` set and
`WINDY_STORAGE_AUTHORIZED=true` (D2). One primary provider at a time: the two are never fetched
together and never cross-referenced (D4, spec §2).
*Alternatives:* switch to Windy now (rejected: the standard terms forbid what the app does with the
data, and a testing key returns modified data); fetch both and choose at read time (rejected: double
requests and cross-referencing, D4); drop Windy (rejected: spec §5 makes it the preferred provider).
*Cost:* Windy Professional 990 EUR a year once licensed. The forecast audit's Windy budget is at
most 688 requests a day (sentinel probes every 10 min all day plus four full refreshes) against
10,000. The plan's 15-minute probes inside the availability windows stay below that.
*Risks:* the time step changes between 3 h (Windy) and 1 h (Open-Meteo) when the fallback runs, so
each version names its provider; Windy's delay after a GFS cycle is undocumented; whether paid
responses are deterministic enough for fingerprints is unknown; the licence may never be granted.

### 4.9 Flight APIs

| Provider | Access | Data | Cost | Terms | Plan |
|---|---|---|---|---|---|
| Duffel | self-serve; test tokens `duffel_test_`; live needs KYC | live offers, round trips as two slices, expire about 30 min after quoting; covers Qantas, Jetstar, Virgin Australia, Garuda, Copa, United, American, Delta, Alaska, Avianca, Volaris | $3 per order + 1 %; $0.005 per search above 1500:1, zero orders count as one | no metasearch use; show the operating carrier's full name; refresh supplier data at least weekly | adapter kept; needs written confirmation before production |
| Amadeus Self-Service | decommissioned 2026-07-17 (secondary sources); Enterprise on request | former Flight Offers Search / Price shapes | contract | — | adapter kept as an untested Enterprise option |
| Travelpayouts / Aviasales Data API | free with an affiliate token | cached prices found by users (e.g. last 48 h) with `expires_at`; round trips | affiliate commission | the real-time search API requires user-initiated searches, 200 queries/h/IP, 9 % and 5 % conversion minimums | not in the plan; candidate cached-price source if Duffel declines |
| Kiwi Tequila | invitation-only since May 2024 | — | — | — | not an option |
| Skyscanner Travel API | application-only, "established business with a large audience" | mixed live and cached prices, deeplinks | commission | — | not available now |
| KAYAK Flights / Price Insights | affiliate network; sandbox; production keys after approval | cached cheapest prices with deeplinks | — | — | candidate after approval |
| SerpApi (Google Flights) | self-serve | scraped Google Flights; round trip needs extra requests | 250 free a month; $25 per 1,000 to $150 per 15,000 | Google's terms and robots.txt disallow `/travel/flights/search` | rejected |
| Google Flights API | none since QPX Express closed on 2018-04-10 | — | — | — | user-clicked route links only |
| Ignav | self-serve | round-trip fares and booking links | 1,000 free, then $2 per 1,000 | terms not readable | not evaluated further |

### 4.10 Authentication

Keep the current scheme (Argon2id, opaque server-side sessions, CSRF, rate limits, lockout,
single-use hashed tokens). A move to Auth.js or Better Auth (TypeScript; Better Auth hashes with
scrypt but accepts a custom hasher) or Clerk (free for the first 50,000 monthly retained users; its
import accepts argon2id hashes) would move session ownership out of FastAPI, rewrite the auth
dependency and CSRF, re-implement verification and reset outside the delivery tracking, and map
vendor ids to the UUID foreign keys of nine tables. No security gap was found that a migration
would close; the missing pieces are features (MFA, social login). Revisit if those become
requirements. The comment at auth/security.py:13 should cite RFC 9106, not OWASP.

### 4.11 The spec's TypeScript layout mapped to Python modules

| Spec file (§5) | Python module(s) | Notes |
|---|---|---|
| `lib/weather/providers/types.ts` | backend/app/services/forecasts/base.py | `ForecastProvider`, `ForecastRecord`, `FORECAST_FIELDS`, `RunInfo`, `ModelRunRef`, `FetchResult`; 2.0 adds `ModelRunRef.component`, `RunInfo.component_keys`, `.estimated`, `.testing`, a `components` argument to `fetch`, `FetchResult.components`, `COMPONENT_FIELDS` |
| `lib/weather/providers/windy.ts` | forecasts/windy.py | rewritten per D3 |
| `lib/weather/providers/open-meteo.ts` | forecasts/open_meteo.py (+ open_meteo_ecmwf.py, opt-in) | component-aware fetch (D6) |
| `lib/weather/provider-manager.ts` | forecasts/provider_manager.py (new) + forecasts/registry.py | D2, D4 |
| `lib/weather/normalize.ts` | inside each adapter, onto `ForecastRecord`; services/geo.py `wind_from_uv` | both providers return the same structure |
| `lib/weather/refresh.ts` | services/sync/detect.py, sync/orchestrator.py, forecasts/ingestion.py, api/internal.py (refresh endpoint), sync/daemon.py (optional runner) | D6, D8, D9 |
| `lib/weather/publish.ts` | forecasts/versions.py (new) + sync/publish.py + services/public_cache.py | D7, D11 |
| `lib/weather/quality.ts` | surf_quality/scoring.py, wind.py, nearshore.py, predict.py, forecast_confidence.py | D13 |
| browser side | frontend/lib/api.ts, frontend/hooks/queries.ts, new frontend/hooks/use-forecast-version.ts | D11 |

| Spec example endpoint (§22) | Endpoint in this app |
|---|---|
| `GET /api/spots` | `GET /api/spots` |
| `GET /api/spots/:id` | `GET /api/spots/{slug}` |
| `GET /api/spots/:id/forecast` | `GET /api/spots/{slug}/forecast` |
| `GET /api/spots/:id/quality` | `GET /api/spots/{slug}/timeline` (per-step quality; no separate route) |
| `GET /api/forecasts/version` | new (D11) |
| `GET /api/forecasts/status` | new (D11); `/api/health/data` and `/api/system/status` stay |
| `GET /api/opportunities` | `GET /api/opportunities` |

### 4.12 New or aliased settings (plan)

| Env | Default | Meaning |
|---|---|---|
| `WEATHER_PROVIDER` | auto | auto / open_meteo / windy / demo (D2) |
| `FORECAST_PROVIDERS` | unset (derived) | advanced explicit list |
| `WINDY_STORAGE_AUTHORIZED` (alias `WINDY_TERMS_ACKNOWLEDGED`) | false | written licence in place (D3) |
| `WINDY_CHECK_MINUTES` / `WINDY_DAILY_REQUEST_LIMIT` | 15 / 10000 | D3 |
| `OPEN_METEO_COMMERCIAL` (alias `COMMERCIAL_USE`) | false | requires `OPEN_METEO_API_KEY` when true |
| `FORECAST_DAYS` | 10 (3-16) | horizon |
| `FORECAST_REFRESH_ENABLED` | true | kill switch for every runner |
| `FORECAST_STALE_THRESHOLD_MINUTES` (+ `_BY_MODEL`) | 360 | grace past the model interval (D10) |
| `FORECAST_MIN_VALID_FRACTION` | 0.8 | per-spot validation (D9) |
| `NEXT_PUBLIC_APP_URL` (alias of `APP_BASE_URL`) | — | public URL |
| `SYNC_COALESCE_MINUTES` | 0 | within a component only |
| `SYNC_TRIGGER_SOURCES` | `spots,map` (free-database: `spots`) | no more map-only trigger |
| `SYNC_DAEMON_KINDS` | `spots,ensemble,map` (hosted: `map`) | D8 |
| `FORECAST_VERSION_CDN_SECONDS` | 15 | D11 |
| `PREDICTION_ARCHIVE_ENABLED` | false (true in full-retention) | D5 |
| `SCORING_WEIGHTS_JSON` | unset | D13 |
| `FLIGHT_USER_DAILY_QUOTA` | 20 | D15 |
| `ALERT_LEAD_DEFAULT_MIN_DAYS` / `_MAX_DAYS` | 2 / 10 | D15 |
| `SHORT_NOTICE_MIN_DAYS` / `_MAX_DAYS` | 3 / 7 | D15 |
| `DETECTION_LEAD_MIN_DAYS` | 1 (was 5) | outer bound |
| `QUOTA_ALERT_FRACTION` | 0.8 | D12 |
| `REFRESH_SCHEDULER_EXPECTED_MINUTES` | 30 (0 disables) | D12 |
| `NEXT_PUBLIC_FORECAST_VERSION_POLL_MS` (frontend) | 20000 | D11 |

Startup validation in production refuses: `WEATHER_PROVIDER=windy` without a key or without
`WINDY_STORAGE_AUTHORIZED`; `OPEN_METEO_COMMERCIAL=true` without `OPEN_METEO_API_KEY`; the refresh
endpoint with a `CRON_SECRET` shorter than 16 characters; invalid JSON in the two JSON settings.

---

## 5. Reuse, switch-off and removal (PLAN at audit time)

> Like section 4, this is the plan of 2026-10-27. Section 8 records what was actually done.

### 5.1 Reused unchanged

* Authentication and accounts: backend/app/auth/{security,sessions,deps}.py, core/middleware.py
  (CSRF), core/ratelimit.py, services/accounts.py (registration, verification, reset, deletion).
* Travel-window maths: services/flights/windows.py (`compute_travel_window`,
  `outbound_query_dates`).
* Flight filtering and ranking: services/flights/filtering.py; the shared `cache_key`
  (flights/base.py); the labelled demo provider (flights/demo.py).
* Delivery: services/notifications/delivery.py, delivery_events.py, transactional.py,
  api/webhooks.py (signed webhooks), the unsubscribe flow.
* Reliability core: the Postgres lease and lease keeper (sync/orchestrator.py), app.jobs.runner
  `run_job`, services/http.py `request_with_retries`, forecasts/quota.py (weighted budget).
* Open-Meteo detection rules: `ModelMeta`, the availability wait, `ensure_servable`, the
  domains-behind and monotonic checks (forecasts/open_meteo.py, sync/detect.py). The rules stay;
  the run key becomes per component (listed below as changed).
* Public cache mechanics: single-flight, miss rate limit (services/public_cache.py);
  core/http_cache.py `cache_headers`; the timeline ETag.
* `_authorize` pattern of api/internal.py; the health problem framework and the ops-alert
  dedup/repeat/recovery (services/health.py, notifications/ops_alerts.py).
* Research data and code: research_evidence.json, profile_decisions.json, physical_profiles.json
  (`wind_profile`), data/tide_stations.json; services/research/evidence.py `parse_evidence`,
  assess.py `compute_assessment`, decisions.py.
* Wind conventions: surf_quality/wind.py and frontend/lib/directions.ts.
* Tides: services/tides/**.
* Frontend: map bounds and navigation (lib/map-bounds.ts, components/map/surf-map.tsx), the
  timeline rows, `SelectionStore`, the disclosures, the search wizard, the notifications and
  settings pages.
* Deployment harness: scripts/vercel-build-check.sh, scripts/vercel-local.sh,
  docker-compose.vercel.yml, frontend/e2e/vercel-smoke.spec.ts, .github/workflows/ci.yml; the
  profiles mechanism (core/profiles.py) and the `AliasChoices` pattern in core/config.py.
* Every existing test suite stays as the regression base; a test changes only where the spec
  changes the behaviour it pins.

Reused and changed by the plan: forecasts/windy.py, registry.py, open_meteo.py, ingestion.py,
sync/detect.py, orchestrator.py, publish.py, daemon.py, forecasts/queries.py (serving through
versions), surf_quality/predict.py (lean imports), scoring.py (2.2.0), forecast_confidence.py
(output), flights/service.py, notifications/alerts.py, matching/service.py and scoring.py,
jobs/schedule.py, services/public_cache.py (marker), api/spots.py and api/timeline.py (headers),
frontend/hooks/queries.ts, core/config.py, core/profiles.py, vercel.json and the docs.

### 5.2 Switched off by default but kept

| Feature | Default after the plan | Setting that re-enables it | Code kept in |
|---|---|---|---|
| ECMWF family and the blend | off | `FORECAST_PROVIDERS=open_meteo,open_meteo_ecmwf` and `FORECAST_BLEND_ENABLED=true` (family-level behaviour of 18a5bee; no component split) | forecasts/open_meteo_ecmwf.py, blend.py |
| GEFS-Wave ensemble | off (already) | `GEFS_ENABLED=true` | forecasts/ensemble.py |
| NOAA and ECMWF GRIB point ingestion | not listed (already) | `FORECAST_PROVIDERS` tokens `noaa_gfs`, `ecmwf_ifs` | forecasts/noaa.py, ecmwf.py |
| Windy | gated | `WEATHER_PROVIDER=windy` or `auto`, `WINDY_API_KEY`, `WINDY_STORAGE_AUTHORIZED=true` | forecasts/windy.py |
| Demo provider | tests and local stacks | `WEATHER_PROVIDER=demo` | forecasts/demo.py |
| Prediction archive | off (on in full-retention) | `PREDICTION_ARCHIVE_ENABLED=true` | forecasts/archive.py |
| ML offshore-Hs corrections | off; not imported unless enabled and an ACTIVE model exists | `ML_CORRECTIONS_ENABLED=true` and an activated model (`ml` CLI) | services/ml/**, backend/ml_models |
| Verification and backtests | off (already) | `VERIFICATION_ENABLED=true` | services/verification/** |
| Buoy observations | off (already) | `OBSERVATIONS_ENABLED=true` | services/observations/** |
| Spot syncs in the Daemon | off on hosted (map layers only) | `SYNC_DAEMON_KINDS=spots,ensemble,map` | services/sync/daemon.py |
| World map GRIB layers on hosted | only through the Daemon; labelled stale without it | `MAP_FIELDS_ENABLED` and `map` in `SYNC_DAEMON_KINDS` | forecasts/noaa.py, fields.py, services/map_fields.py |
| Coalescing | 0 | `SYNC_COALESCE_MINUTES` > 0 (within a component) | sync/detect.py |
| Provider response cache | off on hosted (already) | `PROVIDER_RESPONSE_CACHE_ENABLED=true` | forecasts/provider_cache.py |
| Celery runner | classic compose only | `JOB_RUNNER=celery` | backend/app/workers/** |
| Manual GitHub data sync | manual | `workflow_dispatch` | .github/workflows/data-sync.yml |
| Automatic refresh | on | `FORECAST_REFRESH_ENABLED=false` pauses every runner | — |

### 5.3 Removed

* The periodic flight-discovery timer (`flight-discovery`, jobs/schedule.py:157) and the
  `FLIGHT_REFRESH_HOURS` re-search (flights/service.py:426-443; core/config.py:367).
* Re-validation of every flight-found candidate before deciding an alert (it moves to just before
  sending), and the reset of expired offers to `PENDING_FLIGHTS`.
* The 30-minute matching timer (replaced by publication-triggered matching and a daily
  housekeeping run).
* The 300 s timer of the map conditions query (fe/hooks/queries.ts:201).
* The combined local confidence class in the API, the UI and alerts (the stored column is not
  dropped).
* Cross-component coalescing (`SYNC_COALESCE_MINUTES` 90 → 0).
* The clock-based Windy run identity (replaced by fingerprints, D3).
* The map-only trigger of the free-database profile (`SYNC_TRIGGER_SOURCES=map` → `spots`).
* The pandas use in surf_quality/predict.py (replaced by plain Python; scores must be identical
  before and after, on demo data and on a clone of a real run).
* The global 5-day detection floor (`DETECTION_LEAD_MIN_DAYS` 5 → 1, per-search lead times).
* Stale documentation statements: Windy "not recommended"; the 10 Active CPU-hour daemon quota
  (about 15 places listed by the research).

Nothing else is removed: no module, table or database record is deleted (spec §4; D5).

---

## 6. Measured numbers and external facts

All values below come from the audit and research records of 2026-10-27. "When" is the
measurement time; values from repo documents were measured earlier and were not re-measured.

### 6.1 Forecast acquisition, detection and timing

| What | Value | How | When |
|---|---|---|---|
| Weighted Open-Meteo calls per full GFS-family refresh (50 spots) | 142.857 (85.714 waves + 57.143 atmosphere); 125.0 with `FORECAST_DAYS=10` | `forecast_runs.details.stats` of run 1 (2026-10-16 00Z) in the live DB; `quota.call_weight` | 2026-10-27 |
| Weighted calls per ECMWF-family refresh | 124.286 (107.143 + 17.143 for a neighbour-cell request for 2 masked spots); 100.0 + 16.0 with 10 days | same, run 4; `details.warnings` name puerto-escondido and keramas | 2026-10-27 |
| HTTP per refresh | GFS: 3 data requests, 11 HTTP responses, 329,421 B; ECMWF: 3 data requests, 7 responses, 172,195 B | read-only SQL on `forecast_runs.details` | 2026-10-27 |
| Weighted calls per day | about 786 with a sync per run (4 GFS + 2 ECMWF); 500 at the free-database cadence; free limit 10,000 | computed from the call weights | 2026-10-27 |
| Stored horizon | GFS family lead 0-381 h, 208 steps, 10,400 rows; ECMWF 0-354 h, 84 steps, 4,200 rows; blend 0-381 h | SQL min/max(valid_time − issued_at) per run | 2026-10-27 |
| Live `meta.json` at 00:07 UTC | GFS-Wave 0.25 18Z available 23:17:13; GFS-Wave 0.16 18Z 23:16:55; GFS 0.13 18Z 23:21:53; GFS 0.25 still 12Z (17:36:44); ECMWF WAM 0.25 12Z 19:39:47; IFS 0.25 12Z 21:41:47 (modification 21:32:04); `update_interval_seconds` 21600 for all six | `curl` of each domain's `meta.json` | 2026-10-27 00:07 UTC |
| Live sync plan, hosted + free-database profile | not due; GFS family waiting (GFS 0.25 on 12Z) until 01:41; ECMWF 12Z "new (not a trigger)"; map 18Z waiting for the GFS spot run | `python -m app.cli sync check --json` on a scratch DB | 2026-10-27 00:11 UTC |
| Publication delay after init, 2026-10-26 18Z | GFS-Wave 0.25 and 0.16 +317 min; GFS 0.13 (10 m wind) +322; GFS 0.25 (gust, MSLP) +363 by modification time, +363 or +424 by availability depending on the server; ECMWF WAM HRES 9 km +356; WAM 0.25 +421; IFS 0.25 +454; MFWAM 12Z +726 | `meta.json` polls | 2026-10-27 02:25-02:50 UTC |
| Server disagreement | the same GFS 0.25 18Z run reported available at 00:02:47Z (7 of 16 polls) or 01:04:28Z (9 of 16): 62 min apart; HRRR: one server still served 00Z 13+ min after another reported 01Z | minute polls of `meta.json` | 2026-10-27 02:26-02:48 UTC |
| Metadata timeouts | ncep_gfs025 2/18, ecmwf_ifs025 3/7, ncep_hrrr_conus 5/10, ncep_gfs013 1/3, all marine-api domains 0/30 (partly the sandbox network) | same polls | 2026-10-27 |
| Upstream NOAA NODD | f000 posted +212..217 min, f384 +309..325 min after init (2026-10-26 cycles); 18Z f240 complete at 22:37Z (atmosphere) and 22:33Z (waves), +277 / +273 min | HEAD on NODD `.idx`, `Last-Modified` | 2026-10-27 02:41 UTC |
| Which domain feeds which variable | under `models=gfs_global`: wind speed and direction equal ncep_gfs013 (240/240 h); gusts and MSLP equal ncep_gfs025; `models=ncep_gfs025` returns HTTP 200 with all-null values | one forecast request at a Bali point, 10 days | 2026-10-27 02:47 UTC |
| Marine horizon | `forecast_days=16` with ncep_gfswave025 for two locations: HTTP 200, 32 kB, 0.67 s, 384 hourly steps (docs table says 0-8 days) | one Marine API request | 2026-10-27 02:29 UTC |
| Earlier captured cycle, 2026-10-13 18Z | GFS 0.25 modification +348 / availability +367 min; GFS 0.13 +324/+335; GFS-Wave +319; IFS 0.25 +424/+428; WAM 0.25 +420; MFWAM 12Z +724 | repo fixture backend/tests/fixtures/open_meteo/meta.txt | captured 2026-10-14 |
| Worst logged delays | GFS 0.25 06Z available 8.7 h after its cycle (2026-10-21); GFS-Wave 0.16 00Z still on the previous cycle 9.3 h after it (2026-10-22) | docs/FORECAST_SOURCES.md:160-166 | 2026-10-21/22 |
| Partial failure, 3 of 50 spots | run B `partial` (18,048 rows, 3 `spot_errors`); the failed spots served from run A, the others from B; run A not stale at +6 h, stale at +18 h; re-acquiring run B "skipped: run already ingested" (no retry); provider fetched once | scratch probe `partial_failure_probe.py` (demo provider subclass dropping 3 spots) | 2026-10-27 |
| Predictions and archive per demo sync pair | 37,248 predictions for 2 runs; 4,050 archive rows | the same probe | 2026-10-27 |
| Public cache windows | browser 60 s; CDN `s-maxage` 300 s + `stale-while-revalidate` 3600 s; field bytes named by run immutable for a year; `/api/health/data` CDN 60 s | read from api/spots.py:60-62, api/map_fields.py:42-44, api/system.py:35 | 2026-10-27 |
| Worst-case publication → fresh public response (derived) | about 360 s for a busy edge key (300 CDN + 60 browser); up to about 3,960 s for the first visitor of an idle key | from the headers and Vercel's SWR semantics; not measured on Vercel | 2026-10-27 |
| Detection, provider and staleness unit tests | 66 passed in 2.81 s | pytest on test_sync_detect.py, test_forecast_providers.py, test_staleness.py | 2026-10-27 |
| Windy adapter tests | 3 passed in 0.40 s (mock-only) | pytest `-k windy` on test_forecast_providers.py | 2026-10-27 |
| Daemon CPU estimate (repo docs) | 4.4-7.9 Active CPU-h a month with a sync per run, map layers 2.75-4.78 of it; 5.3-9.5 if every GFS cycle needs two spot syncs | extrapolation in docs/sync.md:476-518 | 2026-10-20/22 |
| Spot-only live sync (repo docs) | 24.9 CPU-s, 42.7 s wall, 3 data requests, 333 kB; spots stage 1.69-3.11 CPU-s; predict 7.84-16.70; detect 1.92-4.05; map stage 165-287 CPU-s for 61 map hours; heavy imports 3.5 CPU-s per process | docs/sync.md:331-357,495 | 2026-10-20/21 |

### 6.2 Platform: syncs, imports, sizes, tests

| What | Value | How | When |
|---|---|---|---|
| Forced demo sync on a fresh DB (50 spots, 19,200 records, map and ensemble off) | spots 5.0 s / 3.20 CPU-s; blend 1.07 / 0.01; predict 18.9 / 11.61; publish 0.37 / 0.01; detect 11.0 / 1.25; match 0.28 / 0.25; flights, alerts, deliver about 0.02 each; sync 38.1 s, 16.6 CPU-s; process 41.6 s wall, 17.9 user + 2.5 sys CPU-s, max RSS 478 MB | `python -m app.cli sync run --force --trigger cli`, then `sync status --limit 1` (script `measure.sh`) | 2026-10-27 |
| Import cost of the scoring path | 2.7 s wall, 3.2 CPU-s, 227 MB RSS; loads pandas, sklearn, joblib, scipy, pyarrow | `python -c 'import app.services.surf_quality.predict'` timed; `sys.modules` listed | 2026-10-27 |
| Installed sizes (backend/.venv) | pyarrow 167M, scipy 109M, pandas 72M, eccodeslib 49M, sklearn 48M, numpy 43M, joblib 2.5M | `du -sh` | 2026-10-27 |
| Vercel API function | 127.2 MiB (python3.13, `maxDuration` 60 s) vs the 500 MB Python limit | CI run 40414842129, job "Vercel build (offline, nothing deployed)" | 2026-10-26 22:45 UTC |
| SQL statements of one forced demo sync | 475 (358 SELECT, 88 INSERT of which 74 `executemany` batches, 29 UPDATE); 20.9 s wall, 15.8 CPU-s | SQLAlchemy `before_cursor_execute` counter around `run_sync(force=True)` (script `count_sync.py`) | 2026-10-27 |
| `init-db` on an empty database | 12.9 s wall, 4.9 user + 1.2 sys CPU-s, 263 MB RSS; 46 tables afterwards; 8 Alembic migrations | timed run; `information_schema.tables` count | 2026-10-27 |
| 1,000 simulated visitors × 3 public GETs (`/api/spots/pipeline/forecast`, `/api/spots/pipeline`, `/api/map/conditions`), 50 concurrent, uvicorn 1 worker, demo data | marker in Redis: 3,000 × 200, one distinct payload per route, 8 DB transactions (cold snapshots), 2 on a repeat pass, 9.3 s wall, p50 98-235 ms, p95 138-322 ms. Marker absent (Redis flushed): 5,954 DB transactions and 1.65 M tuples read for 3,000 requests, p50 267-295 ms, 18.1 s wall | script `load.py`; `pg_stat_database` `xact_commit + xact_rollback` delta; `redis-cli flushdb` for the cold case, `public_cache.note_published(1)` for the marker case | 2026-10-27 |
| Backend suites at 18a5bee | unit 2,050 passed, 4 skipped in 73.5 s; integration 512 passed, 3 skipped (no `TEST_PGBOUNCER_URL`) in 655.3 s | pytest on scratch DBs | 2026-10-27 |
| GitHub CI on 18a5bee | run 40414842129 success: backend unit 2,050 / 4 skipped, integration 512 / 3 skipped, lean-import 27 passed, vitest 486 in 42 files, Playwright 127 passed (10.2 min), offline Vercel build RESULT ok | `gh run view` and job logs | run 2026-10-26 22:45 UTC; read 2026-10-27 |
| Test inventory | 1,540 backend test functions (1,046 unit in 86 files, 494 integration in 58 files); 438 vitest call sites in 42 files; 101 Playwright call sites in 13 spec files; 41,328 lines of backend test code | grep and `wc -l` | 2026-10-27 |
| README commands | 30/30 distinct `python -m app.cli` commands answer `--help` (exit 0); 10/10 referenced scripts exist | bash loop over the README; `[ -e scripts/... ]` | 2026-10-27 |
| Code size | backend/app 53,935 lines of Python (services 43,757, api 1,925) | `find`, `cat` and `wc -l` over every `*.py` file | 2026-10-27 |
| Documentation size | docs/*.md 16,744 lines in 26 files; README.md 1,418 lines | `wc -l` | 2026-10-27 |

### 6.3 Frontend

| What | Value | How | When |
|---|---|---|---|
| Requests an open, focused page sends in 60 min, per hook (incl. mount) | map "now" snapshot 13; field index 13; map snapshot at a time 1; spots 1; spot 1; forecast 1; timeline 1; spot events 1; events 1; system status 1; a window focus after 60 min added 0 | vitest with fake timers and the app's QueryClient defaults on a scratch copy (`zz-audit-refetch.test.tsx`, 10 tests passed) | 2026-10-27 |
| Current publication → visible latency per surface (derived) | spot page: unbounded until navigation or reload; map markers with ocean layers: until the next map step (3-hourly to f120) plus the CDN window, up to about 3 h; map "now" snapshot, field index, landing horizon: about 5-15 min; any fresh load: up to 300 s CDN + 60 s browser | code and headers (hooks/queries.ts:201; hooks/map-fields.ts:25; app/map/page.tsx:93,101; api/spots.py:60-62) | 2026-10-27 |
| Payload sizes | `/api/spots` 281.0 KiB raw / 24.8 KiB gzip; `/api/map/conditions` 84.0 / 6.8; `/api/spots/{slug}` 12.6-13.4 / 3.5-3.8; `/forecast?days=10` 707.1-734.7 / 44.5-58.9; `/timeline?days=10` (158 steps) 337.3-363.9 / 26.3-33.0 (jeffreys-bay, pipeline, uluwatu); per spot-page view about 1.07 MiB raw / 90 KiB gzip | services rendered with the response TypeAdapters, gzip level 6, on a read-only clone of the live DB at now=2026-10-16T09:00Z (script `measure_payloads.py`) | 2026-10-27 |
| Production build | Next.js 16.4.1 (Turbopack): compiled in 13.1 s, TypeScript 16.2 s, 32.8 s wall; 21 routes; first-load JS gzip: / 214 KiB, /map 258, /spots/[slug] 253, /dashboard 230, /status/accuracy 339 (largest) | `npm ci --ignore-scripts`, `next build` on a scratch copy | 2026-10-27 |
| Frontend unit tests | 42 files, 486 tests passed | `npx vitest run` on the scratch copy (32 s) | 2026-10-27 |
| Local install vs lockfile | `next` 16.4.0 installed in the main checkout's node_modules while package.json and package-lock.json pin 16.4.1 (lockfile correct; `npm ci` fixes it) | `require('./node_modules/next/package.json').version` | 2026-10-27 |

### 6.4 Travel

| What | Value | How | When |
|---|---|---|---|
| Integration tests: auth, accounts and searches, email delivery, public and opportunity API, pipeline | 71 passed in 120.92 s | pytest on a scratch DB | 2026-10-27 |
| Related unit and integration tests (travel windows, flight and notification providers, delivery events, matching, IP header, detection idempotency, workers, inline runner) | 121 passed in 28.94 s | same | 2026-10-27 |
| Flight provider and window tests | 13 passed (test_flight_providers.py), 17 passed (test_travel_windows_and_filtering.py); mock-only | pytest | 2026-10-27 |
| Argon2id cost (m=65536, t=3, p=4) | hash 102 ms; first verify 180 ms (includes the lazily computed dummy hash); later verify 104 ms; 4 vCPUs | `hash_password` / `verify_password` timed | 2026-10-27 |
| Travel window for the spec's Nov 10-13 example | Uluwatu via DPS and Playa Hermosa via SJO: arrival 2026-11-08, departure 2026-11-14; arrive-by 23:14 local (23:59 minus a 45 min transfer) at Uluwatu; origin-local query dates Nov 8 and Nov 7 from Sydney and Los Angeles | script `nov_window.py` calling `compute_travel_window` / `outbound_query_dates` | 2026-10-27 |
| Re-search loop, forecast unchanged, offers expiring 30 min after quoting, three cycles 61 min apart | per cycle: matches created 0, alerts invalidated 1, alerts queued 0, provider queries 1, cache hits 1; `flight_searches` rows 3 → 4 → 5 | scratch test `test_probe_flight_loop.py` (demo flight provider) | 2026-10-27 |
| Short-notice matching, qualifying simulated swell, default search | 3.5 days ahead: 1 event, 0 matches; 4.0: 1 / 0; 5.5: 1 / 1; 7.0: 1 / 1 | scratch test `test_probe_short_notice.py` | 2026-10-27 |
| Flight cache with expired offers | cached search 1 h old (5 h of TTL left), its only offer expired 30 min earlier: cache hit 1, provider queries 0 | script `cache_expiry.py` (provider raises if called; rolled back) | 2026-10-27 |
| Offer eligibility after quoting | now+0:00 eligible 1; now+0:45 eligible 0 ("offer expired"); now+5:00 eligible 0 | script `expiry_demo.py` with the repo's filter | 2026-10-27 |
| Market data | 50 spots, 153 airports; Indonesia 8 spots, Costa Rica 3, Panama 1, El Salvador 1, Mexico 1; AU origins ADL BNE HBA MEL OOL PER SYD | read-only SQL on the live DB | 2026-10-27 |
| Duffel search cost if the app never books (derived) | travel audit: about $45 a month at 300 searches a day (9,000 × $0.005, no free allowance); flight research: (9,000 − 1,500) × $0.005 ≈ $37.50, since zero orders count as one order | Duffel pricing and Services Agreement (6.6) | 2026-10-27 |

### 6.5 Scoring and spot research

| What | Value | How | When |
|---|---|---|---|
| Research confidence overall (High / Moderate / Low) | 0 / 35 / 15 | `compute_assessment` over the committed data files (script `coverage.py`); equal to the stored current rows in the live DB | 2026-10-27 |
| Per variable (H / M / L) | swell direction 25/23/2; swell size 4/23/23; swell period 4/16/30; wind 11/26/13; tide 4/14/32; bathymetry 11/36/3; seasonality 24/23/3; local behaviour 38/10/2 | same | 2026-10-27 |
| Sources | 468 (403 read directly, 65 via search), 1,730 claims; per spot min 4 / median 9 / max 16; a tier-A source at 14 spots; last accessed 2026-10-12 (46 spots) or 2026-10-13 (4 spots) | same | 2026-10-27 |
| Profile values with zero supporting sources (spots) | swell period 16; tide 11 (incl. 5 null preferences); swell size 6; direction, wind, seasonality 0 | script `support.py` (`independent_supporting == 0`) | 2026-10-27 |
| Conflicts | 41 spots fire a conflict rule on at least one variable; 33 spots have a disputed decision; 88 decisions = 48 disputed (wind 11, tide 21, size 14, swell window 2), 36 kept, 4 changed | `coverage.py` and a tally of profile_decisions.json | 2026-10-27 |
| Tide gating, live run | 25,000 hours: tide scored in 8,328; excluded "sources disagree" 10,500, "research confidence is low" 3,000, "no preference recorded" 2,500, "No tide prediction" 672 | read-only SQL on `surf_quality_predictions` | 2026-10-27 |
| ML in the production path | `ml_model_versions` 0 rows; predictions with an ML version 0 / 25,000; breaking method `linear_wave_v2` 25,000 / 25,000; spots with calibration 0 / 50 | read-only SQL | 2026-10-27 |
| Forecast / local confidence pairs, live run | low/low 6,463; moderate/moderate 4,950; moderate/low 4,699; high/low 4,292; high/moderate 4,121; high/high 475 | read-only SQL | 2026-10-27 |
| Daylight label distribution, live run | poor 4,186; fair 2,085; good 1,930; very good 2,181; excellent 2,196; exceptional 247 (12,825 daylight hours; 36 % very good or better) | read-only SQL | 2026-10-27 |
| Wind fit on a west-facing beach (offshore 90 deg) | onshore 6/8/9/10/15/20 km/h → Q 0.90/0.792/0.728/0.661/0.367/0.196; offshore 8/20/30/40 → 1.0/1.0/0.983/0.61; cross-offshore (60 deg) 8/20 → 0.979/0.75; cross-shore 20/30 → 0.661/0.367; calm 1 | `wind.wind_fit` called from the repo venv | 2026-10-27 |
| Evidence excerpt drift | 16 excerpts checked: 14 still verbatim on the live page; one page reworded (Surf-Forecast Pipeline); one page renders no text to a plain fetch (Strike Mission, Salsa Brava) | re-fetch of 13 random fetched sources plus 2 Pipeline sources | 2026-10-27 |
| Scoring tests | unit 738 passed in 8.10 s (16 files); integration 41 passed in 63.4 s (5 files) | pytest on a scratch DB | 2026-10-27 |
| ML footprint | services/ml 2,986 lines; services/verification 4,492 lines; ml_models 3.9 MB; docs/ml-evaluation.md 1,950 + docs/ml-pipeline.md 538 lines | `wc -l`, `du -sh` | 2026-10-27 |

Spec-17 field coverage over the 50 spots (profile has a value / a sourced value exists), from
`coverage.py` on 2026-10-27:

| Field | Profile / sourced | Field | Profile / sourced |
|---|---|---|---|
| height: minimum suitable | 50 / 46 | wind: ideal speed | 0 / 0 |
| height: ideal range | 0 / 0 | wind: strong-wind limits | 1 / 1 |
| height: maximum suitable | 50 / 46 | wind: sheltering | 5 / 5 |
| height: offshore vs breaking | 50 / 0 | tide: stage | 45 / 48 |
| period: ideal | 50 / 20 | tide: rising/falling | 0 / 0 |
| period: acceptable minimum | 50 / 20 | tide: size-dependent | 0 / 0 |
| period: short-period behaviour | 0 / 0 | tide: local data availability | 50 / 8 |
| period: long-period behaviour | 0 / 0 | season: prime months | 50 / 50 |
| direction: ideal bearing | 50 / 50 | season: secondary | 0 / 0 |
| direction: acceptable range | 50 / 50 | season: swell sources | 50 / 0 |
| direction: blocked | 0 / 0 | season: wind patterns | 0 / 0 |
| wind: offshore direction | 50 / 50 | geography: break type | 50 / 50 |
| wind: cross-offshore directions | 0 / 0 | geography: orientation | 1 / 2 |
| wind: onshore directions | 0 / 0 | geography: exposure | 50 / 50 |
| | | geography: bathymetry | 50 / 11 |

Free-text mentions in claims for fields without a structured value (spots): ideal or best size 28;
short period / wind swell 22; long period / groundswell 46; blocked or shadowed 1; cross-offshore
4; onshore 6; light/calm/glassy 21; strong wind 5; shelter 8; rising/falling tide 24;
size-dependent tide 7; secondary season 8; storm or swell source 22; trades/monsoon/prevailing
wind 18.

### 6.6 External facts

Every fact was retrieved on 2026-10-27; the quote is verbatim from the page (an ellipsis marks a
cut inside the source text, as recorded).

**Open-Meteo**

| Fact | Quote | URL |
|---|---|---|
| Modification time is not availability | "last_run_modification_time: The time when the data download and conversion were completed, which does not indicate when the data became available on the API." | https://open-meteo.com/en/docs/model-updates |
| Wait after availability | "To ensure all API calls use the most recent data, please wait 10 minutes after the availability time." | https://open-meteo.com/en/docs/model-updates |
| Eventual consistency | "Data across all Open-Meteo servers is eventually consistent, meaning there may be instances where the API indicates a weather model has been updated, but not all servers have been fully updated yet." | https://open-meteo.com/en/docs/model-updates |
| Metadata calls are free | "Note that API calls to the metadata API are not counted toward daily or monthly request limits." | https://open-meteo.com/en/docs/model-updates |
| Hourly output is interpolated | "By default, the API interpolates the data to a 1-hour resolution. However, the underlying model may only provide data in 3 or 6-hourly steps." | https://open-meteo.com/en/docs/model-updates |
| Multi-location requests | "Multiple coordinates can be comma separated. E.g. &latitude=52.52,48.85&longitude=13.41,2.35. To return data for multiple locations the JSON output changes to a list of structures." | https://open-meteo.com/en/docs |
| Free-tier limits and use | "Less than 10'000 API calls per day, 5'000 per hour and 600 per minute. ... You may only use the free API services for non-commercial purposes." | https://open-meteo.com/en/terms |
| Free tier on Vercel | "If you are using providers like Vercel, Netlify, Render, or Google Sheets you might be rate-limited earlier then expected." | https://open-meteo.com/en/terms |
| Commercial subscription | "A subscription grants a commercial use licence and an API key for the dedicated customer endpoint at customer-api.open-meteo.com." | https://open-meteo.com/en/pricing |
| Call weighting | "Requests for data covering more than 10 weather variables or extending over a period of more than 2 weeks for a single location are considered multiple API calls." | https://open-meteo.com/en/pricing |
| Attribution on paid plans | "While you are permitted to utilise and distribute the data, including for commercial purposes, attribution is a requirement under this licence." | https://open-meteo.com/en/pricing |
| Attribution link | "You must include a link next to any location Open-Meteo data are displayed" | https://open-meteo.com/en/licence |
| Last readable price (2023) | "The "Standard Plan" is priced at $29 per month and provides 1 million API calls per month." | https://openmeteo.substack.com/p/api-subscriptions-for-commercial |
| Marine horizon | "By default, we provide forecasts for 7 days, but you can access forecasts for up to 16 days." | https://open-meteo.com/en/docs/marine-weather-api |
| Model sea level is not a tide | "This data is not suitable for coastal navigation." | https://open-meteo.com/en/docs/marine-weather-api |

**Windy Point Forecast API**

| Fact | Quote | URL |
|---|---|---|
| Endpoint | "Requests are sent to the following endpoint: POST https://api.windy.com/api/point-forecast/v2" | https://api.windy.com/point-forecast/docs |
| No run field in the response schema | "PointForecastResponseDto:{type:"object",properties:{ts:{type:"array",items:{type:"number"}},units:{type:"object"},warning:{type:"string"}},required:["ts","units"]}" | https://api.windy.com/point-forecast/docs |
| Wind components | "The component u defines the speed of a wind blowing from the West towards the East (a negative value therefore implies the opposite direction)." | https://api.windy.com/point-forecast/docs |
| Latest forecast only | "Point forecast returns exclusively the most recent forecast values." | https://api.windy.com/point-forecast/pricing |
| ECMWF excluded | "The ECMWF model is not included in point forecast due to licensing conditions." | https://api.windy.com/point-forecast/pricing |
| Testing tier; Professional 990 EUR a year, 10,000 requests a day | "Returns randomly shuffled and slightly modified data - Development purpose only, not intended for production FREE" | https://api.windy.com/point-forecast/pricing |
| Storage and derived databases prohibited | "store, extract, modify, distribute, use the weather data or other content of the Services, create any weather works or databases derived therefrom" | https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use |
| Redistribution prohibited | "Redistribution of any part of the API or the included data to any third party is forbidden." | https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use |
| No paywall-only use | "the User is not entitled to implement the Services solely in the paid part of the User Application" | https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use |
| Attribution | "The publication of the following text “Contains data from the Windy database” ... is deemed to be the indication of the source of the weather data." | https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use |
| Trial warning in responses (staff example) | "The trial API version is for development purposes only. This data is randomly shuffled and slightly modified." | https://community.windy.com/topic/36428/point-forecast-api-404-client-error-not-found-for-url |
| Fixed 3 h steps (staff, 2026) | "API offer 3 hours intervals/steps and we talk about the fixed interval." | https://community.windy.com/topic/44845/what-is-the-forecast-time-interval-returned-by-the-point-forecast-api |
| Update times follow windy.com (staff) | "each model has different update times. The forecast gets updated at the same times as the standard version." | https://community.windy.com/topic/22943/point-forecast-api-data-update-timings |
| Point Forecast keys are separate | "Keys generated for other services, e.g. Map Forecast or Webcams, are a distinct category, and do not work for the Point Forecast API." | https://api.windy.com/point-forecast/docs |
| Public keys only from registered domains | "API Keys for Public Applications may be used only from User Domains. Using an API Key from any other Domain will be refused." | https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use |
| Annual auto-renewing subscription | "they authorize the Provider to charge them automatically each year until they cancel" | https://account.windy.com/agreements/windy-api-map-and-point-forecast-terms-of-use |
| gfsWave updates at most every 6 h (staff) | "unfortunately, more frequent updates of wave height are not possible, as new calculations are simply not available more often." | https://community.windy.com/topic/40284/live-wave-data |
| Daily counter reset (staff) | "The reset of counter is at 0:00 UTC." | https://community.windy.com/topic/28724/daily-limit-exceeded |
| Invalid key (user report, not official) | "{"message":"Invalid API key","error":"Bad Request","statusCode":400}" | https://community.windy.com/topic/43988/invalid-api-key-test-api |

**Vercel**

| Fact | Quote | URL |
|---|---|---|
| Hobby cron is daily (±59 min precision; Pro once a minute) | "Hobby accounts are limited to daily cron jobs. This cron expression would run more than once per day." | https://vercel.com/docs/cron-jobs/usage-and-pricing |
| No retries | "Vercel will not retry an invocation if a cron job fails." | https://vercel.com/docs/cron-jobs/manage-cron-jobs |
| Duplicates | "Cron delivery can also occasionally invoke the same scheduled run more than once." | https://vercel.com/docs/cron-jobs/manage-cron-jobs |
| CRON_SECRET header | "The value of the variable will be automatically sent as an `Authorization` header when Vercel invokes your cron job." | https://vercel.com/docs/cron-jobs/manage-cron-jobs |
| Function duration | "Hobby: 300s default and maximum. Pro and Enterprise: 300s default, 800s maximum, and 1800s extended maximum Beta." | https://vercel.com/docs/functions/limitations |
| Python bundle limit | "For Python functions, the maximum uncompressed size is **500 MB**" | https://vercel.com/docs/functions/limitations |
| `waitUntil` shares the timeout | "Promises passed to `waitUntil()` will have the same timeout as the function itself." | https://vercel.com/docs/functions/functions-api-reference/vercel-functions-package |
| Hobby daemon quota (re-checked 03:20 UTC) | "5 Active CPU-hours per month" | https://vercel.com/docs/daemons/limits |
| At the quota | "When a Hobby team reaches the quota, Vercel stops the team's daemons until the first of the next month (UTC) or until the team upgrades to Pro." | https://vercel.com/docs/daemons/limits |
| Daemons are beta | "Daemons are available in Public Beta on all plans" | https://vercel.com/docs/daemons |
| Services | "a Next.js frontend and a FastAPI backend in the same repository deploy together with shared routing" | https://vercel.com/docs/services |
| Python GA (2026-10-15) | "Python is now a first-class language and runtime on Vercel." | https://vercel.com/changelog/python-on-vercel-is-now-generally-available |
| Celery beat unsupported | "`celery beat` requires a long-running process." | https://vercel.com/docs/frameworks/backend/celery |
| Hobby is non-commercial | "**Hobby teams** are restricted to non-commercial personal use only." | https://vercel.com/docs/limits/fair-use-guidelines |
| Pro price | "$20/month Pro platform fee" | https://vercel.com/docs/plans/pro-plan |
| Hobby Function CPU included: 4 CPU-hours a month | "4 CPU-hrs" | https://vercel.com/docs/plans/hobby |
| WebSockets GA (2026-10-20) | "WebSocket support in Vercel Functions is now generally available." | https://vercel.com/changelog/websocket-support-is-now-generally-available |
| CDN `s-maxage` semantics | "sets the number of seconds a response is considered "fresh" by the CDN. After this period ends, Vercel's CDN will serve the "stale" response from the edge until the response is asynchronously revalidated" | https://vercel.com/docs/caching/cache-control-headers |
| Purge by tag | "When you purge by cache tag, Vercel purges all three types of cache: CDN cache, Runtime Cache, and Data Cache." | https://vercel.com/docs/caching/cdn-cache/purge |

**Schedulers and Redis**

| Fact | Quote | URL |
|---|---|---|
| QStash Free: 1,000 messages a day, 10 active schedules, 15-minute maximum response duration; each trigger billed | "Scheduled messages are billed once for each time the schedule triggers." | https://upstash.com/pricing/qstash |
| QStash retries | "By default, we retry a failed delivery 3 times." | https://upstash.com/docs/qstash/features/retry |
| QStash signatures | "We send a JWT with each request. This JWT is signed by your individual secret signing key and sent in the Upstash-Signature HTTP header." | https://upstash.com/docs/qstash/howto/signature |
| cron-job.org frequency | "Every cronjob can be executed up to 60 times an hour, i.e. every minute." | https://cron-job.org/en/faq/ |
| cron-job.org timeout | "If your URL/script does not finish after 30 seconds, it will timeout and we will close the connection to prevent delays in the execution of the jobs of other users." | https://cron-job.org/en/faq/ |
| GitHub schedule minimum | "The shortest interval you can run scheduled workflows is once every 5 minutes." | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows |
| GitHub schedule delays | "event can be delayed during periods of high loads of GitHub Actions workflow runs. High load times include the start of every hour. If the load is sufficiently high enough, some queued jobs may be dropped." | https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows |
| GitHub terms | "don't use Actions as a content delivery network or as part of a serverless application, but a low benefit Action could be ok if it’s also low burden" | https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features |
| Upstash Redis free tier | "Monthly Commands 500K" | https://upstash.com/pricing/redis |

**Databases**

| Fact | Quote | URL |
|---|---|---|
| Neon Free | "Includes 100 projects, 100 CU-hours/project, 1 GB Postgres storage per project ... and 5 GB of egress per project" | https://neon.com/docs/introduction/plans |
| Supabase Free | "Free projects are paused after 1 week of inactivity. Limit of 2 active projects." (and "500 MB database size", "5 GB egress") | https://supabase.com/pricing |
| Prisma Postgres | "Prisma Postgres - 200k operations / month - 2 GB storage"; "Your database on Prisma Postgres counts every query as one operation"; "Unlimited Prisma Postgres data transfer" | https://www.prisma.io/pricing |

**Flights**

| Fact | Quote | URL |
|---|---|---|
| Duffel excess search fee ($0.005) | "This fee is charged monthly if you exceed a search to book ratio of 1500:1." | https://duffel.com/pricing |
| Zero orders count as one | "For the avoidance of doubt, zero Orders shall be treated as one Order for the purposes of calculating the Search-to-Order Ratio." | https://duffel.com/services-agreement |
| Metasearch clause | "access or use the Services for metasearch purposes (including to build a metasearch on top of the Duffel Platform and/or to redistribute to a metasearch platform)" | https://duffel.com/services-agreement |
| Live mode needs verification | "your Account will be available to you on a preliminary basis only, such that you can only access a test environment" | https://duffel.com/services-agreement |
| Offers expire | "An offer is only available to create an order for a limited time by the traveller before it expires, typically within 30 minutes." | https://duffel.com/docs/api/offers |
| Operating carrier name | "When presenting offers to your customers, you should always show the full name of the operating carrier of each segment (slices[].segments[].operating_carrier.name)." | https://duffel.com/docs/api/offer-requests |
| Duffel Links country requirement | "In order to access Duffel Links, your Duffel organisation must be set up with a country that is supported by Duffel Payments." | https://duffel.com/docs/guides/duffel-links |
| Amadeus Self-Service closed (secondary source) | "Amadeus says its Self-Service API portal has been decommissioned. Its developer site now provides Enterprise APIs, with access on request." | https://ignav.com/docs/amadeus-self-service-shutdown |
| Amadeus key shutdown date (secondary source) | "on July 17, 2026, all self-service API keys will be disabled and the portal will become inaccessible." | https://airlabs.co/amadeus-self-service-api-shutdown |
| Kiwi Tequila | "Any new partnerships on the Tequila platform will be on an invitation only basis" | https://media.kiwi.com/articles-and-interviews/better-for-business-kiwi-com-takes-a-new-approach-to-partnerships/ |
| Travelpayouts Data API is cached | "Dear partners! Attention, the data is transferred from the cache, so it is recommended to use them to generate static pages." | https://travelpayouts-data-api.readthedocs.io/ |
| Travelpayouts real-time search | "Each search query must be initiated by the user and the results must be shown to the user in full." | https://travelpayouts.github.io/slate/ |
| Skyscanner | "If you’re an established business with a large audience, you can apply for our Travel API." | https://www.partners.skyscanner.net/product/travel-api |
| KAYAK | "Once your use case is approved as a full affiliate integration, you can request production API keys for the products you plan to use" | https://affiliates.kayak.com/apis |
| No Google flight API | "Come April 10, 2018 the search giant will shut down access to its QPX Express API that's used by the likes of Kayak and Orbitz for airfare data" | https://www.engadget.com/2017-11-01-google-flight-data-api-shutdown.html |
| SerpApi round trips | "To obtain the returning flight information for Round Trip (1), you need to make another request using a departure_token." | https://serpapi.com/google-flights-api |
| Google terms | "using automated means to access content from any of our services in violation of the machine-readable instructions on our web pages (for example, robots.txt files that disallow crawling, training, or other activities)" | https://policies.google.com/terms?hl=en |

**Email and authentication**

| Fact | Quote | URL |
|---|---|---|
| Resend idempotency | "Resend checks whether an email with the same idempotency key has already been sent in the last 24 hours" | https://resend.com/docs/dashboard/emails/idempotency-keys |
| Argon2id parameters used here | "Argon2id with t=3 iterations, p=4 lanes, m=2^(16) (64 MiB of RAM) ... This is the SECOND RECOMMENDED option" | https://www.rfc-editor.org/rfc/rfc9106.html |
| OWASP minimum | "m=19456 (19 MiB), t=2, p=1" | https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html |
| Better Auth hashing | "uses scrypt to hash passwords" | https://www.better-auth.com/docs/authentication/email-password |
| Clerk free tier | "Free for your first 50,000 monthly retained users" | https://clerk.com/pricing |

---

## 7. Open questions for the owner and the defaults chosen

The five questions the plan depends on most:

| # | Question | Why it matters | Default chosen until the owner answers |
|---|---|---|---|
| 7.1 | **Vercel plan:** Hobby or Pro? | Hobby cron runs once a day and Hobby is for non-commercial personal use only; Pro ($20 a month) allows a `*/5` Vercel Cron and commercial use | Hobby-compatible: vercel.json keeps daily crons only; refreshes come from an Upstash QStash schedule every 5 minutes calling `/api/internal/refresh`; the Pro recipe is documented |
| 7.2 | **Windy licence:** will Windy grant, in writing, the right to store point forecasts, derive scores and show them to all users? | The standard terms forbid storage, derived databases and redistribution; a key is not a licence | `WINDY_STORAGE_AUTHORIZED=false`; `WEATHER_PROVIDER=auto` resolves to Open-Meteo; the Windy adapter is implemented and testable through `provider-check` without storing data |
| 7.3 | **Open-Meteo commercial plan:** will the site have subscriptions or ads, and should production use a paid key? | The free API is non-commercial only, and its terms warn that apps on Vercel may be rate-limited earlier; the 2026 price could not be read | `OPEN_METEO_COMMERCIAL=false`, free hosts, non-commercial use; setting `OPEN_METEO_API_KEY` (customer hosts, already wired) is recommended before a production launch on Vercel |
| 7.4 | **Duffel:** written confirmation that a surf-alert app that shows fares and links out is not "metasearch" under the Services Agreement? | Without it, Duffel can only be used as a seller (Orders or Duffel Links, KYC, a Duffel Payments country) | `FLIGHT_PROVIDER=demo` with clearly labelled mock fares; the Duffel adapter is kept with the warning documented; outbound links labelled "route search (fares may differ)" |
| 7.5 | **Daemon or not?** | Public Beta, production-only, 5 Active CPU-h a month on Hobby, stopped at the quota | Optional: hosted profile `SYNC_DAEMON_KINDS=map` (only the world map layers, `MAP_FIELD_CYCLES=06,18`), `SYNC_CPU_BUDGET_HOURS=5`; spot forecasts never depend on it; without it the map overlay ages and is labelled stale |

Further owner decisions raised by the audits, with the plan's default:

| # | Question | Default |
|---|---|---|
| 7.6 | World map GRIB layers on or off by default? | kept as an optional layer, refreshed only by the Daemon |
| 7.7 | Does Neon Free's 5 GB egress hold with a refresh per new model run after the simplifications? | keep Neon; re-measure with `scripts/measure_db_egress.py`; Neon Launch or Prisma Postgres if it does not |
| 7.8 | ML: disable and keep, or delete services/ml and ml_models? | disable and keep (no code deleted); the ADR-020 approval for seven US spots is not activated |
| 7.9 | Local confidence: drop or keep as an expert detail? | removed from the API, UI and alerts (D13); the stored column stays |
| 7.10 | Light onshore below about 8.6 km/h: "favourable" or "workable"? | capped at "workable" (class only, D13) |
| 7.11 | Unsourced profile values (period 16 spots, size 6, tide 11): label only, or down-weight? | label only ("not documented" in the research docs); weights unchanged |
| 7.12 | Default alert lead window | 2-10 days per search, short-notice ranking boost for 3-7 days |
| 7.13 | Expired cached offers: re-search at most once a day, or re-check only at alert time / on request? | re-check only at alert time or on explicit request |
| 7.14 | Notify users when an alerted opportunity is downgraded or disappears? | no (dashboard only, as now) |
| 7.15 | SMS (Twilio) still wanted? | unchanged; spec §29 requires email only |
| 7.16 | Freshness dashboard public or admin-only? | both: public `/status` stays, an admin-only `/admin/freshness` is added |
| 7.17 | Budget and order for new spot research | not in this plan; suggested first: the Low-confidence spots of the priority markets (uluwatu, desert-point, lakey-peak, rifles, playa-hermosa-cr) |
| 7.18 | Is a CDN-cached version endpoint with a 20 s poll acceptable for the 60 s target? | yes (D11); the latency is measured after integration |

---

## 8. Implementation status

Written after integration and kept up to date through four review rounds, the final
documentation pass, the final gate and the documentation verification (2026-10-29 and 30, branch `wf6/int`; the commits are on top of 18a5bee).
Sections 1-7 above are the audit and the plan as they stood before the work; this section
records what was built (8.1, 8.2), what was measured (8.3), the test results (8.4) and what
remains (8.5). The decisions as records: docs/decisions.md, ADR-038 to ADR-053. The
owner-facing summary, the Definition of Done and the 21 deliverables are in
docs/UPGRADE_REPORT.md, "Swell Travel Agent 2.0".

### 8.1 The decisions as built

| D | Status | Where | Differences from the plan, and fixes from the review |
|---|---|---|---|
| D1 | built | the stack is unchanged: FastAPI, SQLAlchemy 2, Alembic (migration 0009), Neon, Upstash | — |
| D2 | built | `WEATHER_PROVIDER` (core/config.py), `provider_manager.py`, `python -m app.cli forecast provider-check` | — |
| D3 | built | forecasts/windy.py, provider_manager.py, quota.py; docs/WINDY_API_SETUP.md | the run identity is the per-component sentinel fingerprint (same-run keys, partly read checks); never exercised with a real key |
| D4 | built | provider_manager.py; the fallback runs inside the refresh (sync/refresh.py) | — |
| D5 | built | hosted profile and Settings defaults: one family, no blend, no GEFS, `FORECAST_DAYS=10` | the docs still recommended the blend and GEFS until review r1 (finding SPEC-4); now off by default and described as advanced |
| D6 | built | forecasts/carry.py, sync/detect.py, forecasts/ingestion.py `refresh_source` | review r1: detection is monotonic per model domain (F1: a lagging GFS 0.25 server could make the site go back a cycle); hours the carried component lacks are dropped and never scored (F4); a composed run names each component's own model run on the spot page and the map panel (FE-1). Review r2 (N2): the carried component is fetched again **from its same run** when the spot's served run ends more than HORIZON_SLACK (3 h) before the horizon, so the 10-day horizon keeps rolling: a deliberate trade-off against spec 8 ("no queries for unchanged runs") for non-negotiable 8, on about one or two refreshes a day with Open-Meteo GFS (up to about 150 more weighted calls a day; docs/sync.md "One refresh"). Review r3: that refetch is best effort, never a condition for publishing the due component (R3-1), and Open-Meteo's horizon end is the component model's last native step, so the ECMWF family's 6-hourly steps do not make every refresh a refetch (R3-4) |
| D7 | built | `forecast_versions` (models/version.py, migration 0009), forecasts/versions.py, sync/publish.py, forecasts/serving.py | review r1: a run that dies between publication and the marker write rewrites the marker at the next sync (F2) |
| D8 | built | `GET\|POST /api/internal/refresh` (api/internal.py, sync/orchestrator.py `refresh_check`); `vercel.json` keeps only the daily cron; the QStash schedule is documented (docs/deploy-vercel.md step 7b), not deployed | review r1: the daemon was optional in name only (F3, SPEC-2): explicit flight re-checks, "match my search", email retries and the periodic jobs ran only under the daemon or Celery, and data health failed without it. sync/standin.py now runs the kicks and due retries from the refresh endpoint and the overdue periodic jobs from the daily cron; health judges the daemon only for the map layers. The deploy guide had no scheduler step (SPEC-1): added. Review r2: the build seeds the reference data and the refresh endpoint runs the data-health check (SPEC-2, SPEC-3). Review r3: a lasting unreadable component no longer runs that check at every 5-minute call (R3-2); a build that cannot read `data/` fails on an empty database (SPEC-6); without the daemon `MAP_FIELDS_ENABLED=false` keeps data health `ok` (SPEC-1, documented) |
| D9 | built | per-spot status and stale marking (versions `spot_runs`), retries 5/15/45 min | — |
| D10 | built | `FORECAST_STALE_THRESHOLD_MINUTES` (and per model); staleness labels in the UI | review r1: stale spots are marked in the map's best-spots list and on /spots (FE-2); an open tab re-reads the public data each UTC hour, so staleness by age reaches it (FE-6) |
| D11 | built | api/forecasts.py (version, seen, status), `?v=` keys and URLs (frontend hooks/use-forecast-version.ts), CDN headers | review r1: label fixes (FE-4, FE-5, FE-9), alerts lists re-read after a version (FE-7) |
| D12 | built | api/admin.py, services/admin_freshness.py, usage.py, /admin/freshness, the admin problems in health.py | review r1: refused flight and email keys raise `provider_credentials:flights_<p>` / `email_<p>` (SPEC-7, services/service_credentials.py) |
| D13 | built | scoring 2.2.0: surf_quality/outputs.py, reasons.py, weights.py; separate research and forecast confidences | — |
| D14 | built | data/surf_spots/research_conditions.json, docs/surf_spot_research/ (50 spots and a README) | — |
| D15 | built | flights/service.py, budget.py, airports.py; per-search lead times (migration 0009) | review r1: alerts are decided in a fixed order (best opportunity first), not the database's row order (the root cause of an intermittent integration failure; section 8.4) |
| D16 | built | as D5, D6, D8 and D11 | the hosting budgets were recomputed for 2.0 in review r1 (SPEC-6; docs/API_SETUP_REQUIRED.md 1.4) |

Section 5 as built: everything listed in 5.1 and 5.2 is kept and switched as planned. Every
removal in 5.3 was made (the flight-discovery timer and `FLIGHT_REFRESH_HOURS`, the
30-minute matching timer, the map query's 300 s timer, the combined local confidence in the
API and UI, cross-component coalescing, the clock-based Windy run identity, the map-only
trigger, pandas in predict, the 5-day detection floor); the stale "10 Active CPU-hours"
statements were corrected in review r1 (SPEC-6) in README, deploy-vercel.md and
API_SETUP_REQUIRED.md, and in review r2 (SPEC-5) also in the owner checklist of
UPGRADE_REPORT.md section 44, which r1 had missed; historical Upgrade 4 sections of that
report keep their own figures. No module, table or record was deleted.

### 8.2 Files created and modified, per package

`git diff --stat 18a5bee..c85474f` (the code and documentation of the final review round): 356 files changed, 88649 insertions(+), 3687 deletions(-); with the final documentation pass, the final gate and the documentation verification: <!-- diffstat -->359 files changed (150 added, 209 modified), 90,453 insertions, 3,835 deletions<!-- /diffstat -->. Per package (the
foundation commits, then each package's merge into `wf6/int`, then the integration and
review fixes on `wf6/int`):

**Foundation: settings, the provider contract with independent components, `forecast_versions` (migration 0009), demo forecasts kept explicit** (7 added, 28 modified)

* Added: `backend/alembic/versions/0009_forecast_versions.py`, `backend/app/models/version.py`, `backend/app/services/forecasts/versions.py`, `backend/tests/integration/test_forecast_versions.py`, `backend/tests/unit/test_forecast_components.py`, `backend/tests/unit/test_forecast_versions.py`, `backend/tests/unit/test_weather_provider_settings.py`
* Modified: `.env.example`, `.env.vercel.example`, `.github/workflows/data-sync.yml`, `backend/app/core/config.py`, `backend/app/core/profiles.py`, `backend/app/models/__init__.py`, `backend/app/models/search.py`, `backend/app/services/forecasts/base.py`, `backend/app/services/forecasts/demo.py`, `backend/app/services/forecasts/ecmwf.py`, `backend/app/services/forecasts/ensemble.py`, `backend/app/services/forecasts/noaa.py`, `backend/app/services/forecasts/open_meteo.py`, `backend/app/services/forecasts/windy.py`, `backend/tests/conftest.py`, `backend/tests/integration/test_public_and_opportunity_api.py`, `backend/tests/unit/test_compose_classic.py`, `backend/tests/unit/test_free_database_cadence.py`, `backend/tests/unit/test_lean_imports.py`, `backend/tests/unit/test_local_vercel_stack.py`, `backend/tests/unit/test_settings_hosting.py`, `docker-compose.vercel.yml`, `docker-compose.yml`, `docs/API_SETUP_REQUIRED.md`, `docs/deploy-vercel.md`, `frontend/app/layout.tsx`, `frontend/e2e/full-flow.spec.ts`, `scripts/run-e2e.sh`

**P10 docs-audit: docs/architecture_audit.md** (1 added, 0 modified)

* Added: `docs/architecture_audit.md`
* Modified: none

**P7 research: per-spot research conditions and docs/surf_spot_research** (54 added, 2 modified)

* Added: `backend/tests/unit/test_spot_research_docs.py`, `data/surf_spots/research_conditions.json`, `docs/surf_spot_research/README.md`, `scripts/generate_spot_research_docs.py`, `docs/surf_spot_research/<spot>.md` (50 spots)
* Modified: `data/surf_spots/README.md`, `docs/research-confidence.md`

**P1 providers: Windy adapter, provider manager, quota usage snapshot** (10 added, 6 modified)

* Added: `backend/app/services/forecasts/provider_cli.py`, `backend/app/services/forecasts/provider_manager.py`, `backend/tests/integration/test_windy_acquisition.py`, `backend/tests/unit/test_forecast_usage.py`, `backend/tests/unit/test_provider_check_cli.py`, `backend/tests/unit/test_provider_manager.py`, `backend/tests/unit/test_windy_provider.py`, `backend/tests/unit/test_windy_setup_doc.py`, `backend/tests/windy_fixtures.py`, `docs/WINDY_API_SETUP.md`
* Modified: `backend/app/cli.py`, `backend/app/services/forecasts/quota.py`, `backend/app/services/forecasts/registry.py`, `backend/app/services/forecasts/windy.py`, `backend/tests/unit/test_forecast_providers.py`, `docs/FORECAST_SOURCES.md`

**P2 refresh: per-component detection, refresh endpoint, versioned publication** (6 added, 29 modified)

* Added: `backend/app/services/forecasts/carry.py`, `backend/app/services/sync/refresh.py`, `backend/tests/integration/test_refresh_components.py`, `backend/tests/unit/test_open_meteo_components.py`, `backend/tests/unit/test_sync_components.py`, `scripts/measure_refresh_egress.py`
* Modified: `backend/app/api/dev.py`, `backend/app/api/internal.py`, `backend/app/core/middleware.py`, `backend/app/services/forecasts/demo.py`, `backend/app/services/forecasts/ingestion.py`, `backend/app/services/forecasts/open_meteo.py`, `backend/app/services/maintenance.py`, `backend/app/services/sync/cli.py`, `backend/app/services/sync/daemon.py`, `backend/app/services/sync/detect.py`, `backend/app/services/sync/orchestrator.py`, `backend/app/services/sync/publish.py`, `backend/app/services/sync/state.py`, `backend/app/services/sync/types.py`, `backend/tests/integration/test_internal_cron.py`, `backend/tests/integration/test_migration_0008.py`, `backend/tests/integration/test_pipeline.py`, `backend/tests/integration/test_prune_pass.py`, `backend/tests/integration/test_sync_orchestrator.py`, `backend/tests/integration/test_sync_spot_scope.py`, `backend/tests/unit/test_foundation_stubs.py`, `backend/tests/unit/test_free_database_cadence.py`, `backend/tests/unit/test_local_vercel_stack.py`, `backend/tests/unit/test_sync_contracts.py`, `backend/tests/unit/test_sync_detect.py`, `backend/tests/unit/test_vercel_deploy.py`, `docker/edge/Caddyfile`, `docs/sync.md`, `vercel.json`

**P3 serving: published-version reads, version/seen/status endpoints, versioned cache marker** (7 added, 17 modified)

* Added: `backend/app/api/forecasts.py`, `backend/app/schemas/forecasts.py`, `backend/app/services/forecasts/serving.py`, `backend/tests/integration/test_forecast_serving.py`, `backend/tests/integration/test_public_load.py`, `backend/tests/unit/test_forecast_serving.py`, `scripts/load_test_public.py`
* Modified: `backend/app/api/map_fields.py`, `backend/app/api/spots.py`, `backend/app/api/system.py`, `backend/app/api/timeline.py`, `backend/app/core/http_cache.py`, `backend/app/core/middleware.py`, `backend/app/main.py`, `backend/app/schemas/spots.py`, `backend/app/schemas/timeline.py`, `backend/app/services/forecasts/queries.py`, `backend/app/services/public_cache.py`, `backend/app/services/spot_views.py`, `backend/app/services/swell_detection/service.py`, `backend/app/services/tides/api.py`, `backend/app/services/timeline.py`, `backend/tests/integration/test_public_snapshot_cache.py`, `backend/tests/unit/test_http_cache.py`

**P6 scoring: scoring 2.2.0 per-hour outputs, separate confidences, lean predict** (9 added, 29 modified)

* Added: `backend/app/services/surf_quality/outputs.py`, `backend/app/services/surf_quality/reasons.py`, `backend/app/services/surf_quality/weights.py`, `backend/tests/fixtures/scoring/scores_2_1_0.json`, `backend/tests/integration/test_lean_predict.py`, `backend/tests/integration/test_scoring_outputs.py`, `backend/tests/unit/test_scoring_conditions.py`, `backend/tests/unit/test_scoring_weights.py`, `backend/tests/unit/test_tide_stages.py`
* Modified: `.env.example`, `backend/app/core/config.py`, `backend/app/schemas/spots.py`, `backend/app/schemas/timeline.py`, `backend/app/services/forecasts/archive.py`, `backend/app/services/ml/hook.py`, `backend/app/services/spot_views.py`, `backend/app/services/surf_quality/forecast_confidence.py`, `backend/app/services/surf_quality/models.py`, `backend/app/services/surf_quality/predict.py`, `backend/app/services/surf_quality/scoring.py`, `backend/app/services/surf_quality/wind.py`, `backend/app/services/timeline.py`, `backend/tests/integration/test_blend.py`, `backend/tests/integration/test_egress_columns.py`, `backend/tests/integration/test_forecast_confidence.py`, `backend/tests/integration/test_free_tier_egress.py`, `backend/tests/integration/test_migration_0008.py`, `backend/tests/integration/test_public_and_opportunity_api.py`, `backend/tests/integration/test_rescore_wind.py`, `backend/tests/integration/test_retention_bounds.py`, `backend/tests/unit/test_lean_imports.py`, `backend/tests/unit/test_matching_wind.py`, `backend/tests/unit/test_surf_quality.py`, `backend/tests/unit/test_surf_scoring_doc.py`, `backend/tests/unit/test_wind.py`, `docker-compose.yml`, `docs/SURF_SCORING.md`, `docs/ml-pipeline.md`

**P8 flights: targeted flight searches, per-search alert lead time, alert emails** (8 added, 40 modified)

* Added: `backend/app/services/flights/airports.py`, `backend/app/services/flights/budget.py`, `backend/tests/integration/test_flight_targeting.py`, `backend/tests/integration/test_search_lead_times_api.py`, `backend/tests/unit/test_flight_targeting.py`, `frontend/components/search/use-search-defaults.ts`, `frontend/e2e/search-lead-time.spec.ts`, `frontend/tests/search-lead-time.test.ts`
* Modified: `.env.example`, `backend/app/api/opportunities.py`, `backend/app/api/searches.py`, `backend/app/core/config.py`, `backend/app/jobs/schedule.py`, `backend/app/schemas/opportunities.py`, `backend/app/schemas/searches.py`, `backend/app/services/flights/amadeus.py`, `backend/app/services/flights/base.py`, `backend/app/services/flights/duffel.py`, `backend/app/services/flights/links.py`, `backend/app/services/flights/service.py`, `backend/app/services/matching/scoring.py`, `backend/app/services/matching/service.py`, `backend/app/services/notifications/alerts.py`, `backend/app/services/notifications/delivery.py`, `backend/app/services/notifications/providers.py`, `backend/app/services/notifications/templates.py`, `backend/app/services/notifications/transactional.py`, `backend/app/services/opportunity_views.py`, `backend/app/services/searches.py`, `backend/tests/integration/test_data_health.py`, `backend/tests/integration/test_forecast_confidence.py`, `backend/tests/integration/test_pipeline.py`, `backend/tests/integration/test_sync_daemon.py`, `backend/tests/unit/test_celery_routing.py`, `backend/tests/unit/test_job_schedule_floor.py`, `backend/tests/unit/test_notification_providers.py`, `backend/tests/unit/test_sync_health.py`, `docs/MATCHING_AND_FLIGHTS.md`, `frontend/app/dashboard/page.tsx`, `frontend/app/opportunities/[id]/page.tsx`, `frontend/app/searches/[id]/page.tsx`, `frontend/app/searches/new/page.tsx`, `frontend/components/opportunity-card.tsx`, `frontend/components/search/search-wizard.tsx`, `frontend/lib/validation.ts`, `frontend/tests/word-budget.test.tsx`, `frontend/types/openapi.d.ts`, `frontend/types/openapi.json`

**P9 monitoring: admin freshness dashboard, admin-only alerts, usage report** (16 added, 7 modified)

* Added: `backend/app/api/admin.py`, `backend/app/schemas/admin.py`, `backend/app/services/admin_freshness.py`, `backend/app/services/usage.py`, `backend/tests/admin_fixtures.py`, `backend/tests/integration/test_admin_alerts.py`, `backend/tests/integration/test_admin_freshness.py`, `backend/tests/unit/test_admin_problems.py`, `frontend/app/admin/freshness/page.tsx`, `frontend/components/admin/freshness-dashboard.tsx`, `frontend/components/admin/freshness-format.ts`, `frontend/components/admin/freshness-sections.tsx`, `frontend/components/admin/freshness-types.ts`, `frontend/components/admin/use-admin-freshness.ts`, `frontend/e2e/admin-freshness.spec.ts`, `frontend/tests/admin-freshness.test.tsx`
* Modified: `backend/app/main.py`, `backend/app/services/health.py`, `backend/app/services/notifications/ops_alerts.py`, `docs/operations.md`, `frontend/app/status/page.tsx`, `frontend/types/openapi.d.ts`, `frontend/types/openapi.json`

**P4 live: version polling, versioned query keys, forecast-updated label, live-update e2e** (5 added, 9 modified)

* Added: `frontend/components/forecast-updated.tsx`, `frontend/e2e/live-update-mocked.spec.ts`, `frontend/e2e/live-update.spec.ts`, `frontend/hooks/use-forecast-version.ts`, `frontend/tests/forecast-version.test.tsx`
* Modified: `frontend/app/map/page.tsx`, `frontend/app/spots/[slug]/page.tsx`, `frontend/components/providers.tsx`, `frontend/e2e/confidence.spec.ts`, `frontend/hooks/map-fields.ts`, `frontend/hooks/queries.ts`, `frontend/lib/api.ts`, `frontend/tests/database-polling.test.tsx`, `scripts/run-e2e.sh`

**P5 ui: map markers, spot summary, separate confidences, stale labels** (15 added, 31 modified)

* Added: `frontend/components/forecast/provider-credit.tsx`, `frontend/components/forecast/rating-details.tsx`, `frontend/components/map/spot-glyphs.tsx`, `frontend/e2e/map-markers.spec.ts`, `frontend/e2e/spot-summary.spec.ts`, `frontend/lib/best-windows.ts`, `frontend/lib/forecast-fields.ts`, `frontend/lib/map-markers.ts`, `frontend/lib/provider.ts`, `frontend/lib/rating-sentence.ts`, `frontend/tests/chart-annotations.test.tsx`, `frontend/tests/forecast-fields.test.ts`, `frontend/tests/height-unit.test.tsx`, `frontend/tests/map-glyphs.test.tsx`, `frontend/tests/spot-summary.test.tsx`
* Modified: `frontend/app/map/map.css`, `frontend/app/map/page.tsx`, `frontend/app/notifications/page.tsx`, `frontend/app/opportunities/[id]/page.tsx`, `frontend/app/spots/[slug]/page.tsx`, `frontend/components/confidence.tsx`, `frontend/components/event-card.tsx`, `frontend/components/forecast-breakdown.tsx`, `frontend/components/forecast-provenance.tsx`, `frontend/components/forecast/forecast-timeline.tsx`, `frontend/components/forecast/quality-strip.tsx`, `frontend/components/forecast/selected-summary.tsx`, `frontend/components/forecast/tide-row.tsx`, `frontend/components/forecast/why-this-forecast.tsx`, `frontend/components/map/forecast-map.tsx`, `frontend/components/map/legend-drawer.tsx`, `frontend/components/map/spot-panel.tsx`, `frontend/components/map/surf-map.tsx`, `frontend/components/opportunity-card.tsx`, `frontend/components/rating.tsx`, `frontend/components/stale-banner.tsx`, `frontend/e2e/confidence.spec.ts`, `frontend/e2e/map-navigation.spec.ts`, `frontend/lib/confidence.ts`, `frontend/lib/format.ts`, `frontend/lib/glossary.ts`, `frontend/lib/units.ts`, `frontend/tests/confidence.test.tsx`, `frontend/tests/forecast.test.tsx`, `frontend/tests/selected-summary.test.tsx`, `frontend/tests/word-budget.test.tsx`

**Integration on `wf6/int`: cross-package seams, the review round 1 fixes and the 2.0 documentation** (7 added, 112 modified)

* Added: `backend/app/services/service_credentials.py`, `backend/app/services/sync/standin.py`, `backend/tests/integration/test_daemonless_hosted.py`, `backend/tests/integration/test_refresh_fallback.py`, `backend/tests/integration/test_service_credentials.py`, `frontend/lib/model-runs.ts`, `frontend/tests/model-runs.test.tsx`
* Modified: `.env.vercel.example`, `.github/workflows/ci.yml`, `README.md`, `backend/app/api/internal.py`, `backend/app/core/profiles.py`, `backend/app/core/redis.py`, `backend/app/schemas/opportunities.py`, `backend/app/schemas/spots.py`, `backend/app/schemas/timeline.py`, `backend/app/services/flights/amadeus.py`, `backend/app/services/flights/duffel.py`, `backend/app/services/forecasts/base.py`, `backend/app/services/forecasts/carry.py`, `backend/app/services/forecasts/ingestion.py`, `backend/app/services/forecasts/open_meteo.py`, `backend/app/services/forecasts/provider_manager.py`, `backend/app/services/forecasts/queries.py`, `backend/app/services/forecasts/windy.py`, `backend/app/services/health.py`, `backend/app/services/notifications/alerts.py`, `backend/app/services/notifications/delivery.py`, `backend/app/services/notifications/providers.py`, `backend/app/services/opportunity_views.py`, `backend/app/services/public_cache.py`, `backend/app/services/spot_views.py`, `backend/app/services/surf_quality/forecast_confidence.py`, `backend/app/services/surf_quality/predict.py`, `backend/app/services/swell_detection/service.py`, `backend/app/services/sync/detect.py`, `backend/app/services/sync/orchestrator.py`, `backend/app/services/sync/refresh.py`, `backend/app/services/timeline.py`, `backend/app/services/usage.py`, `backend/tests/integration/test_admin_freshness.py`, `backend/tests/integration/test_forecast_confidence.py`, `backend/tests/integration/test_forecast_serving.py`, `backend/tests/integration/test_free_tier_egress.py`, `backend/tests/integration/test_internal_cron.py`, `backend/tests/integration/test_pipeline.py`, `backend/tests/integration/test_public_and_opportunity_api.py`, `backend/tests/integration/test_refresh_components.py`, `backend/tests/integration/test_scoring_outputs.py`, `backend/tests/unit/test_admin_problems.py`, `backend/tests/unit/test_forecast_confidence.py`, `backend/tests/unit/test_lean_imports.py`, `backend/tests/unit/test_provider_manager.py`, `backend/tests/unit/test_reason_split.py`, `backend/tests/unit/test_staleness.py`, `backend/tests/unit/test_sync_components.py`, `backend/tests/unit/test_sync_detect.py`, `backend/tests/unit/test_timeline.py`, `backend/tests/unit/test_wind_dispute.py`, `backend/tests/unit/test_windy_provider.py`, `docker/edge/Caddyfile`, `docs/API_SETUP_REQUIRED.md`, `docs/ARCHITECTURE.md`, `docs/MATCHING_AND_FLIGHTS.md`, `docs/SURF_SCORING.md`, `docs/UPGRADE_REPORT.md`, `docs/WINDY_API_SETUP.md`, `docs/architecture_audit.md`, `docs/deploy-vercel.md`, `docs/local-vercel-stack.md`, `docs/operations.md`, `docs/sync.md`, `frontend/app/dashboard/page.tsx`, `frontend/app/map/map.css`, `frontend/app/map/page.tsx`, `frontend/app/opportunities/[id]/page.tsx`, `frontend/app/page.tsx`, `frontend/app/searches/[id]/page.tsx`, `frontend/app/searches/page.tsx`, `frontend/app/spots/[slug]/page.tsx`, `frontend/app/spots/page.tsx`, `frontend/components/conditions.tsx`, `frontend/components/confidence.tsx`, `frontend/components/forecast-breakdown.tsx`, `frontend/components/forecast-provenance.tsx`, `frontend/components/forecast-updated.tsx`, `frontend/components/forecast/rating-details.tsx`, `frontend/components/forecast/selected-summary.tsx`, `frontend/components/live-swells.tsx`, `frontend/components/map/forecast-map.tsx`, `frontend/components/map/spot-list.tsx`, `frontend/components/stale-banner.tsx`, `frontend/components/status/sync-status.tsx`, `frontend/e2e/confidence.mock.ts`, `frontend/e2e/confidence.spec.ts`, `frontend/e2e/live-update-mocked.spec.ts`, `frontend/e2e/live-update.spec.ts`, `frontend/e2e/map-navigation.spec.ts`, `frontend/e2e/spot-summary.spec.ts`, `frontend/e2e/vercel-smoke.spec.ts`, `frontend/hooks/map-fields.ts`, `frontend/hooks/use-forecast-version.ts`, `frontend/lib/api.ts`, `frontend/lib/confidence.ts`, `frontend/lib/forecast.ts`, `frontend/tests/confidence.test.tsx`, `frontend/tests/database-polling.test.tsx`, `frontend/tests/forecast-version.test.tsx`, `frontend/tests/forecast.test.tsx`, `frontend/tests/freshness.test.tsx`, `frontend/tests/map-fields-hooks.test.tsx`, `frontend/tests/map-markers.test.tsx`, `frontend/tests/map-spot-list.test.tsx`, `frontend/tests/status-sync.test.tsx`, `frontend/tests/word-budget.test.tsx`, `frontend/types/api.ts`, `frontend/types/openapi.d.ts`, `frontend/types/openapi.json`, `scripts/vercel-local.sh`

**Review rounds 2 and 3 on `wf6/int`: the horizon refetch (best effort), the hosted path without the daemon (build seed, data-health check), trip freshness and credits, the hourly re-read, the final documentation** (3 added, 57 modified)

* Added: `backend/tests/integration/test_hosted_first_deploy.py`, `frontend/components/trip-freshness.tsx`, `frontend/tests/trip-freshness.test.tsx`
* Modified: `README.md`, `backend/app/api/internal.py`, `backend/app/cli.py`, `backend/app/schemas/opportunities.py`, `backend/app/schemas/spots.py`, `backend/app/services/forecasts/base.py`, `backend/app/services/forecasts/carry.py`, `backend/app/services/forecasts/demo.py`, `backend/app/services/forecasts/ingestion.py`, `backend/app/services/forecasts/open_meteo.py`, `backend/app/services/forecasts/queries.py`, `backend/app/services/health.py`, `backend/app/services/notifications/ops_alerts.py`, `backend/app/services/notifications/providers.py`, `backend/app/services/opportunity_views.py`, `backend/app/services/service_credentials.py`, `backend/app/services/spot_views.py`, `backend/app/services/sync/orchestrator.py`, `backend/app/services/sync/standin.py`, `backend/tests/integration/test_daemonless_hosted.py`, `backend/tests/integration/test_forecast_serving.py`, `backend/tests/integration/test_public_and_opportunity_api.py`, `backend/tests/integration/test_refresh_components.py`, `backend/tests/integration/test_service_credentials.py`, `backend/tests/unit/test_cli_migrate_on_build.py`, `backend/tests/unit/test_open_meteo_components.py`, `docker-compose.vercel.yml`, `docs/API_SETUP_REQUIRED.md`, `docs/UPGRADE_REPORT.md`, `docs/architecture_audit.md`, `docs/deploy-vercel.md`, `docs/local-vercel-stack.md`, `docs/operations.md`, `docs/sync.md`, `frontend/app/dashboard/page.tsx`, `frontend/app/map/page.tsx`, `frontend/app/opportunities/[id]/page.tsx`, `frontend/app/searches/[id]/page.tsx`, `frontend/app/spots/[slug]/page.tsx`, `frontend/components/event-card.tsx`, `frontend/components/forecast-breakdown.tsx`, `frontend/components/forecast-updated.tsx`, `frontend/components/forecast/rating-details.tsx`, `frontend/components/live-swells.tsx`, `frontend/components/opportunity-card.tsx`, `frontend/components/stale-banner.tsx`, `frontend/e2e/confidence.mock.ts`, `frontend/hooks/use-forecast-version.ts`, `frontend/lib/forecast-fields.ts`, `frontend/lib/model-runs.ts`, `frontend/tests/forecast-version.test.tsx`, `frontend/tests/freshness.test.tsx`, `frontend/tests/spot-summary.test.tsx`, `frontend/tests/word-budget.test.tsx`, `frontend/types/openapi.d.ts`, `frontend/types/openapi.json`, `scripts/vercel-local.sh`

**Review round 4 on `wf6/int`: SMTP reply codes, a failed request's cost in the run's stats, trips judged at the hour start, data credits on /spots and the saved search's history, switched-off map layers, the first-deploy seed after a failed build** (1 added, 39 modified)

* Added: `frontend/tests/spots-credits.test.tsx`
* Modified: `README.md`, `backend/app/schemas/map_fields.py`, `backend/app/schemas/spots.py`, `backend/app/services/forecasts/ingestion.py`, `backend/app/services/health.py`, `backend/app/services/map_fields.py`, `backend/app/services/notifications/providers.py`, `backend/app/services/opportunity_views.py`, `backend/app/services/spot_views.py`, `backend/tests/integration/test_map_fields.py`, `backend/tests/integration/test_public_and_opportunity_api.py`, `backend/tests/integration/test_refresh_components.py`, `backend/tests/integration/test_service_credentials.py`, `docs/FORECAST_SOURCES.md`, `docs/UPGRADE_REPORT.md`, `docs/architecture_audit.md`, `docs/deploy-vercel.md`, `docs/local-vercel-stack.md`, `docs/map-layers.md`, `docs/operations.md`, `docs/sync.md`, `frontend/app/map/page.tsx`, `frontend/app/searches/[id]/page.tsx`, `frontend/app/spots/page.tsx`, `frontend/components/layout/site-footer.tsx`, `frontend/components/map/legend-drawer.tsx`, `frontend/components/opportunity-card.tsx`, `frontend/e2e/confidence.mock.ts`, `frontend/e2e/map-fields.mock.ts`, `frontend/e2e/map-layers.spec.ts`, `frontend/e2e/vercel-smoke.spec.ts`, `frontend/hooks/map-fields.ts`, `frontend/tests/map-colors.test.ts`, `frontend/tests/map-fields-hooks.test.tsx`, `frontend/tests/map-freshness.test.tsx`, `frontend/tests/map-legend.test.tsx`, `frontend/tests/trip-freshness.test.tsx`, `frontend/types/openapi.d.ts`, `frontend/types/openapi.json`

**Final documentation pass on `wf6/int`: the README's spec-33 sections, the settings reference, the 2.0 decision records and deliverables** (1 added, 12 modified)

* Added: `backend/tests/unit/test_readme_commands.py`
* Modified: `.env.example`, `.env.vercel.example`, `README.md`, `backend/tests/unit/test_api_setup_doc.py`, `docs/API_SETUP_REQUIRED.md`, `docs/ARCHITECTURE.md`, `docs/UPGRADE_REPORT.md`, `docs/architecture_audit.md`, `docs/decisions.md`, `docs/deploy-vercel.md`, `docs/operations.md`, `docs/sync.md`

**Final gate on `wf6/int`: the order-dependent map test made self-contained, the Vercel smoke's daily-cron keys, the tides on Vercel without the daemon (documented), the final results** (0 added, 11 modified)

* Added: none
* Modified: `README.md`, `backend/tests/conftest.py`, `backend/tests/integration/test_map_fields.py`, `docs/MATCHING_AND_FLIGHTS.md`, `docs/UPGRADE_REPORT.md`, `docs/architecture_audit.md`, `docs/deploy-vercel.md`, `docs/local-vercel-stack.md`, `docs/operations.md`, `docs/sync.md`, `frontend/e2e/vercel-smoke.spec.ts`

**Documentation verification on `wf6/int`: the documented commands run, numbers traced to their records** (0 added, 7 modified)

* Added: none
* Modified: `README.md`, `docs/API_SETUP_REQUIRED.md`, `docs/UPGRADE_REPORT.md`, `docs/architecture_audit.md`, `docs/decisions.md`, `docs/deploy-vercel.md`, `docs/operations.md`

### 8.3 Measurements

All on this development machine (4 vCPUs shared with other work, local PostgreSQL 16 and
Redis 7, outbound HTTPS through a proxy); none on Vercel, Neon or Upstash.

| What | Measured | Where |
|---|---|---|
| Live Open-Meteo refresh, both components (2026-10-29 01:17 UTC, 18Z run) | 38.7 s wall: check 26.5 s (two TLS handshake timeouts through the proxy), refresh 12.1 s (spots 5.06 s / 0.94 CPU s, predict 5.75 s / 4.21 CPU s, detect 0.96 s, publish 40 ms); 125.0 weighted calls; 50 spots | integration deploy notes |
| Live natural wave-only refresh (2026-10-29 05:31 UTC, GFS-Wave 00Z) | 47.1 s wall: check 26.8 s (two ConnectTimeouts), refresh 20.3 s (spots 6.14 s, predict 7.01 s, detect 1.78 s, match 134 ms); 75.0 weighted calls; the wind carried forward with its 18Z run; 2 marine data requests | same |
| Checks with nothing new (live) | 4 `meta.json` requests each, 0 data requests, 0 weighted calls; 20 checks from 03:49 to 05:27 UTC (80 `meta.json` answers, 5 proxy timeouts): 16 took 1.2-1.8 s wall, 3 about 13 s (one proxy timeout) and 1 26.9 s (two) | same (the poller's log and request log; re-counted at the documentation verification) |
| Open-Meteo servers disagreeing (2026-10-29 06:06-06:50 UTC) | GFS 0.13 `meta.json` answered 00Z and 18Z alternately (e.g. 4 × 00Z, 3 × 18Z of 8); three syncs saw the new run and skipped it without fetching; an atmosphere-only refresh was not reached | same |
| Demo refresh through the endpoint | 12.2-12.5 s wall, 8.5-9.7 CPU s (2026-10-27); local Vercel-style stack 19.3 s on 1 vCPU, 9.8 CPU s; lean install 12.4 s / 9.1 CPU s; this review's README check 15.2 s / 10.1 CPU s with the integration suite running; the documentation pass 13.1-13.7 s / 10.8-11.8 CPU s; the final gate's stack 21.9-26.3 s / 11.2-11.5 CPU s on 1 vCPU; the documentation verification's README check (wave-only) 16.0 s / 10.9 CPU s | docs/sync.md "Measured (2.0)", docs/local-vercel-stack.md, README F.2 |
| Publication to new data on an open page | p50 13.8 s (16 samples, 9.7-22.9 s; 20 s poll, no CDN, local stack); on the final code p50 16.5 s (12 samples, 7.1-24.4 s, same setup, 2026-10-30, docs/local-vercel-stack.md); the admin dashboard's beacons p50 20 s, p95 22 s (28 beacons) | integration deploy notes |
| Database egress per spot refresh (demo) | 6.29-7.45 MB DB → app (2026-10-29), 8.30-9.45 MB (2026-10-27); a check reads nothing | docs/sync.md, `scripts/measure_refresh_egress.py` |
| Load through the local edge | 4,000 requests, 64 concurrent, 0 errors, p50 222-314 ms, p95 705-956 ms (1 vCPU API); a new version reached all routes 3.2 s after publication | integration deploy notes |
| Lean API | cold `import app.main` 1.47-1.79 s; the refresh path adds 22-29 ms of imports | same |
| Vercel build (offline) | `RESULT: ok`, API function 128.3 MiB (3,469 files; limit 500 MiB), `maxDuration` 300 s (2026-10-29; the same on the final code, 2026-10-30) | `scripts/vercel-build-check.sh --offline` |

### 8.4 Test results

Run on 2026-10-30 by the final gate on `wf6/int`, on this development machine, on
commit c1490b3: the code and tests of 0899b83 (the last commit that changes code or
tests) plus documentation. The commits after it change only documentation: two
places that still said the daily cron acquires the tides without the daemon (ffca5d3),
then these results (this one, README "Latest full run" and docs/UPGRADE_REPORT.md section 49), the file list of docs/architecture_audit.md 8.2,
the limitations (docs/UPGRADE_REPORT.md section 51, docs/architecture_audit.md 8.5)
and docs/local-vercel-stack.md (4eff5b1), then the documentation verification below.
Each check ran once on that code (the map-performance
spec once more, as its timing bound allows); the backend's unit and integration suites
as separate runs, every exit code read directly. The e2e suite was started a second time
because the machine rebooted during the first run (76 tests had passed and none had
failed); the complete second run is counted.

| Check | Result |
|---|---|
| `ruff format --check .`, `ruff check .`, `mypy app` | pass (425 files formatted, 219 files type-checked) |
| `alembic upgrade head`, `downgrade -1`, `upgrade head`, `alembic check` on an empty database | pass: 0001 to 0009, no new operations |
| Upgrade of a copy of a real database (at 0006: 50 spots, 25,000 forecast rows and predictions) to 0008, then from 0008 to head | pass: row counts and a checksum (md5) of the forecast rows identical before and after; `alembic check` clean; no version until the first refresh |
| `pytest tests/unit` | 2,911 passed, 6 skipped (the lean-install tests), 84 s |
| `pytest tests/unit/test_lean_imports.py` with `LEAN_PYTHON` (a lean install from `uv.lock`) | 32 passed, a demo spot-only refresh in the lean install included |
| `pytest tests/integration` | 671 passed, 3 skipped (the PgBouncer tests, without `TEST_PGBOUNCER_URL`), 19.0 min. `test_map_fields.py::test_a_fields_only_run_never_changes_spot_forecasts`, order-dependent until 24d114e, passed in it, and also alone, first, right after the Redis-down tests, with its own file, and last (after the 72 other integration files: 631 passed, 3 skipped) |
| `npx tsc --noEmit`, `npx eslint . --max-warnings=0`, `npx vitest run` | pass; Vitest 628 passed (53 files) |
| `npm ci`, `npm run build` | pass |
| `./scripts/run-e2e.sh` (Chromium and Pixel 7) | 154 passed, 1 failed (18.7 min). `e2e/live-update.spec.ts` (through the real refresh, in Chromium: publication to the open map 4.3 s and to the spot page 4.7 s with the e2e build's 2 s poll) and `live-update-mocked.spec.ts` (Chromium and Pixel 7) passed. Failed: `e2e/map-performance.spec.ts`, the crest-dash frame draw time p95 6.6 ms against its 5 ms bound; re-run once, alone, on the idle machine (load 0.7): 5.2 ms, failed again. No frontend code changed since review round 4, where it measured 4 ms, and every frame cost the test records rose alike (crest dashes 1.28 to 2.17 ms of script per frame, wind particles 22.3 to 28.8-32.1 ms of task time per frame), which points to this machine's speed after its reboot rather than the code; the bound was not changed (docs/UPGRADE_REPORT.md section 51) |
| `scripts/vercel-build-check.sh --offline` | `RESULT: ok`, API function 128.3 MiB (3,469 files; limit 500 MiB), `maxDuration` 300 s (2026-10-30); the daily cron is in the output config; the function's files hold no `data/` (docs/UPGRADE_REPORT.md section 51, the tides) |
| `backend/.venv/bin/python scripts/check_api_bundle.py --max-mb 200` | 163.7 of 200 MB |
| `docker compose config -q` (`docker-compose.yml`, `docker-compose.vercel.yml`) | pass, each with `.env.example`, `.env.vercel.example` and no env file |
| shellcheck 0.11 on `scripts/run-e2e.sh`, `scripts/vercel-local.sh`, `scripts/vercel-build-check.sh`, `scripts/export-openapi.sh` and (`-s sh`) the operations scripts | clean (the CI's two commands; `run-e2e.sh` and `vercel-local.sh` are the shell scripts 2.0 changed) |
| `npm audit --omit=dev` | 1 moderate advisory (fast-uri below 3.1.9, under @hookform/resolvers and ajv; docs/decisions.md ADR-037). Dev-only: Vitest 3's tinypool (2 critical) and @vitest/mocker (moderate), not upgraded |
| The local Vercel-style stack (2026-10-30, demo data, docs/local-vercel-stack.md) | built and run on this code: the build command migrated and seeded an empty database; `/api/internal/refresh` 401 without or with a wrong token; six simulated model runs refreshed through the edge, versions 2 to 7, an open map and spot page updated without a reload 12 of 12 times; register and verify through Mailpit; the daily cron 200 with its `jobs` summary; `smoke` 22 of 22 shell checks and 6 of 6 browser checks after one fix (0899b83); with the daemon stopped the cron's `tides.acquire` failed (docs/UPGRADE_REPORT.md section 51) |

Earlier rounds on the same branch: review round 1 (the code of 5b9ce29) 2,882 unit, 638
integration, 604 Vitest and 154 e2e tests passed; review round 2 (a4560d6) 2,889, 650, 614
and 154; review round 3 (1775c67) 2,894, 657, 622 and 154; review round 4 (c85474f) 2,894,
671, 628 and 155. Every test added since is counted above; none was removed or weakened.
Expectations changed on purpose: the build seed without `data/` now fails on an empty
database and skips only when spots exist, so its unit test is split into those two cases
(review r3-spec-SPEC-6); the Vercel smoke's map check with the ocean layers off
(`MAP_FIELDS_ENABLED=false`, the stack's default) expects the "Spot forecasts only" note
instead of the outage notice (review r4-spec-R4-SPEC-2); at the final gate, the fields-only
map test counts the served `version_id` as bookkeeping and asserts that it moves exactly one
per run of the stages (every publication is a version since 2.0; the test passed in full
runs only because an earlier test's Redis fail-open window left the version marker
unwritten, and the test fixture now forgets that window, 24d114e), and the Vercel smoke
expects the daily cron's `jobs` key, answered since review round 1 (0899b83).

**The final documentation pass** (2026-10-30, on top of e29141a; documentation, the two env
examples, one new and one extended unit test, no application code) re-ran: `ruff format
--check .` and `ruff check .` (425 files), `mypy app` (219 files, no issues), `pytest
tests/unit` with `LEAN_PYTHON` (2,917 passed, 0 skipped, 93.5 s: the 15 tests of the new
`tests/unit/test_readme_commands.py` and 2 new ones in `test_api_setup_doc.py` included),
the 117 doc-reading unit tests, `docker compose config -q` with `.env.example` and with
`.env.vercel.example`, `scripts/generate_spot_research_docs.py --check` (exit 0) and
`scripts/measure_refresh_egress.py` (docs/sync.md, "Measured (2.0)"). The integration
suite, Vitest, the e2e suite and the offline Vercel build were not re-run: none of them reads
a file this pass changed.

**The documentation verification** (2026-10-30, after the final gate; documentation only)
ran the commands of README.md, docs/API_SETUP_REQUIRED.md, docs/WINDY_API_SETUP.md,
docs/deploy-vercel.md and docs/operations.md that need no Docker, Vercel CLI or account,
on scratch databases: `--help` of every `python -m app.cli` sub-command; `init-db` (also with
the lean install and a `postgresql://` URL), `check-config` (exit 1 with the missing keys
listed), `pipeline` and `simulate-swell` (demo), `sync check`, `run`, `run --force`,
`publish`, `status`, `daemon --once`, `migrate`, `migrate --on-build`, `tides seed`,
`acquire`, `list`, `prune`, `engine budget`, `research lint`, `ml list`,
`obs seed-stations`, `make-admin`, `forecast provider-check` (Open-Meteo live; Windy
without a key `not_configured`, also without Redis); the refresh block of README F.2 with
`curl` against a local API (401 without or with a wrong token; 404 for both routes without
`CRON_SECRET`, checked in process); the real-data block of README F.5 (Open-Meteo answered with its daily limit,
the rest succeeded); a Celery worker and beat; `scripts/check-health.sh`, `backup-db.sh`,
`restore-db.sh --create --target-db`, `restore-drill.sh` (passed, 47 tables),
`measure_db_egress.py pipeline` (6.18 MB a pass), `generate_spot_research_docs.py --check`
and the other scripts' `--help`; `uv lock --check`; the lean install from the lock into a
venv of its own (132 MB); `npm run typecheck`, `npm run lint`, `npm test` (628 passed);
`ruff format --check .`, `ruff check .`, `mypy app`, `alembic upgrade head`,
`alembic check`; `pytest tests/unit` with `LEAN_PYTHON` (2,917 passed, 0 skipped, 134 s).
It corrected the documentation where a number did not match its record (the live idle
checks: 20 checks and 1.2-1.8 s, not 98 checks and 1.2-1.7 s; the egress per refresh from
6.3 MB; the demo refresh CPU up to 11.8 s; the Upstash pay-as-you-go example; the per-area
diff table of docs/UPGRADE_REPORT.md section 48) or was out of date (the local stack's run
on the final code in README E.6 and docs/deploy-vercel.md section 10).

The intermittent `test_free_tier_egress.py` narrow-reads failure seen during integration
(integration notes: once in a full run, never alone) did not recur in two full integration runs with the diagnostic plugin on 2026-10-29 (637 and 638 passed). Its cause was found by
analysis and proved with a deterministic reproduction: the alert candidate query had no
ORDER BY, and when two overlapping saved searches of one user surface the same swell the
one alert (keyed by user and event) was told from whichever match the database returned
first. With the candidate rows answered in the opposite order, only
`notifications[2].snapshot.window_end` and `.confidence` differed, the signature of the
observed failure (only the notifications differed). Alerts are now decided in
`alerts.candidate_order`; `test_alerts_do_not_depend_on_the_database_row_order` fails without
it.

### 8.5 What remains

Nothing below blocks a first deployment; each item says what would close it.

**Needs a real deployment or an account (cannot be verified here)**

* Vercel itself: no deployment was made. Unverified until the first one: the CDN's caching of
  the versioned reads and of `/api/forecasts/version` (the publication-to-page latency was
  measured without a CDN), Neon (only PgBouncer in transaction mode was tested), Neon's
  suspend and wake and its CU-hour accounting, real Upstash command counts, the daemon's
  build and start, and whether the api build (rooted at `backend/`) can read the
  repository's `data/` for the build-time seed (a build that cannot fails on an empty
  database, docs/deploy-vercel.md step 6).
* The scheduler: the QStash schedule was never created (a 5-minute POST loop stood in for it);
  cron-job.org is documented as unverified for refreshes longer than its 30 s.
* Windy: no key. The activation, the testing-key refusal and the fallback are tested with
  recorded and synthetic responses only; Windy's delay after a cycle, whether paid answers
  are deterministic (the fingerprint detection needs it), the quota error's body and the
  price of a storage licence are open (docs/WINDY_API_SETUP.md, "What is not verified yet").
* Open-Meteo's commercial hosts (`OPEN_METEO_API_KEY`), Duffel, Amadeus Enterprise, Resend,
  SendGrid and Twilio: mock-tested only. Email delivery is verified end to end with Mailpit.

**Verified here only in part**

* The local Vercel-style stack ran on the final code (2026-10-30: smoke 22 of 22 and 6 of 6,
  six simulated model runs shown on open pages without a reload, 8.4). Its daemon refreshes
  the spots itself, so it tests the daemon-less hosted path only through the daily cron with
  the daemon stopped (the tides failed there, below); that path is covered by
  `tests/integration/test_daemonless_hosted.py` and `test_hosted_first_deploy.py`.
* A live atmosphere-only refresh was never observed (Open-Meteo's servers answered GFS 0.13
  00Z and 18Z alternately for over 36 minutes; three syncs skipped it safely). Each such flap
  costs a sync row, a lease and up to 13 s; waiting for a stable metadata answer (seen twice)
  before starting a sync would avoid it (not built).
* When GFS 0.13 and GFS 0.25 publish a cycle at different times, the atmosphere key changes
  twice, which may cost two atmosphere refreshes per cycle (about 50 weighted calls each,
  derived, not observed to completion).
* The hosting budgets (docs/API_SETUP_REQUIRED.md 1.4) are arithmetic on unit costs measured
  here; steady traffic with a tab open in a CDN region all month can pass Upstash Free's 500K
  commands.

**Known gaps in the code and the tests**

* The tides on Vercel without the daemon: tide acquisition reads `data/tide_stations.json`
  and `data/tides/`, outside the api service's root `backend/`, which the API function does
  not include; on the local stack with the daemon stopped the daily cron's `tides.acquire`
  failed with `FileNotFoundError` (`job_failed:tides.acquire`). Documented (docs/deploy-vercel.md
  section 4: run `python -m app.cli tides acquire` from a checkout, or keep the daemon);
  bundling `data/` into the function would close it and the build seed's `../data` question.
* `frontend/e2e/map-performance.spec.ts` failed once on a shared machine in review round 3
  (a frame-time p95 of 6.5 ms against its 5 ms bound) and passed in the re-run; at the final
  gate it failed in the full run (6.6 ms) and in its one re-run on the idle machine (5.2 ms)
  with the map code of review round 4 (4 ms then), every frame cost it records 1.2 to 2
  times round 4's. The bound was not changed; it needs a run on a quiet machine of known
  speed, or a decision on what the bound should be on a 4-vCPU shared host.
* `FORECAST_DAILY_REQUEST_QUOTA` is declared in `Settings` but read by no code (the
  per-provider budgets replaced it); documented as such in docs/API_SETUP_REQUIRED.md
  section 6.
* Optional follow-ups the integrator listed: `frontend/lib/forecast-fields.ts` readers take
  `unknown` rather than the generated types (they tolerate hours scored before 2.2.0); the
  admin dashboard's hook polls the version itself instead of the shared store;
  `ComponentRun.update_interval_seconds` is filled only where the provider exposes it; there
  is no `/api/spots/:id/quality` alias (per-step quality is in `/timeline`).
* `npm audit --omit=dev`: one moderate advisory (fast-uri, ADR-037); the dev-only Vitest 3
  advisories need Vitest 5.
* The browser tests ran in Chromium and its Pixel 7 emulation only.

**Owner steps**

* Before production: decide whether the site is commercial (then Vercel Pro and an
  Open-Meteo plan), create the refresh schedule (docs/deploy-vercel.md step 7b), and set
  `MAP_FIELDS_ENABLED=false` when the daemon is not kept.
* Windy: the written licence (storage, derived scoring, display to all visitors), then the
  Windy logo next to the credit line.
* Duffel: its written confirmation that this use is not "metasearch", before real fares.
* A domain verified with the email provider before the site is publicised.
* Scoring weights, breaking heights and both confidence classes stay reasoned defaults until
  nearshore observations exist to fit or verify them.
