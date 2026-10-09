# Implementation checklist

Status of each part of the product spec. "Verified" means the behaviour was exercised by
an automated test or by running the stack, not merely that code exists. See README §K for
limitations.

## Spec coverage

| § | Area | Status |
|---|------|--------|
| 3 | Tech stack | Next.js 16 / React 19 / TypeScript / Tailwind 4 / shadcn-style Radix components / Recharts / Leaflet · FastAPI / Pydantic 2 / SQLAlchemy 2 / Alembic / pandas / NumPy / HTTPX · PostgreSQL 16 / Redis 7 / Celery + beat · Docker Compose |
| 4 | 50 surf spots | `data/surf_spots/spots.json` (50 spots, 153 airports), validated on seed |
| 5 | Accounts | All of: register, verify email, login/logout, reset/change password, profile, saved airports, notification preferences, phone verification, delete account. Backend ownership checks on every resource |
| 6 | Saved searches | 6-step wizard, flexible/fixed dates, spots/regions/all, surf/wind/period/direction filters, travel constraints, edit/pause/resume/duplicate/delete |
| 7 | Forecast acquisition | Open-Meteo (NOAA GFS + GFS-Wave) adapter verified live; Windy adapter (mock-tested), demo provider (default), batching, model-run metadata, idempotent runs, staleness, retention |
| 8 | Quality engine | Score, labels, breaking-height estimate, confidence, explanations. See SURF_SCORING.md |
| 9 | Swell detection | Daylight clustering, multi-day events, update in place / merge / downgrade / pass, overlap exclusion constraint |
| 10 | Flight search | Duffel and Amadeus adapters (mock-tested), demo provider, shared cache, quotas, travel windows, filters, revalidation |
| 11 | Matching and ranking | Per-search sub-windows, 5–10 day lead time, opportunity score, ranking profiles |
| 12 | Email and SMS alerts | Console/SMTP/Resend/SendGrid and console/Twilio, dedup, follow-ups, daily cap, unsubscribe, STOP |
| 13 | Frontend | Landing, auth, dashboard, wizard, live map, spot detail, opportunity detail, search management, notification history, settings, status |
| 14 | Database | 24 tables, one Alembic migration, round-trip tested (upgrade → downgrade → upgrade, plus model/migration diff) |
| 15 | Background jobs | Jobs A–H, beat schedule, chaining, Redis locks, retries, job log, startup kick-off |
| 16 | Security | Argon2id, opaque sessions, CSRF, rate limits, lockout, CSP and headers, log redaction, no secrets in client bundle |
| 18 | Tests | 171 backend (unit + integration), 29 frontend unit, 5 Playwright e2e. See below |
| 19 | Docker | `docker compose up --build` verified: all services healthy, worker runs jobs and delivers alerts, Mailpit profile delivers SMTP |
| 20 | README | Sections A–K. Every command was run against this repository (Docker and non-Docker paths, from a fresh clone) |

## Edge cases → tests

| Edge case (spec §18) | Test(s) |
|----------------------|---------|
| Missing forecast data | `test_missing_forecast_data_for_some_spots`, `test_missing_inputs_renormalise_weights`, `test_land_points_with_all_null_waves_are_reported`, `test_missing_sea_level_is_tolerated` |
| Failed API requests | `test_failed_forecast_provider_is_recorded`, `test_transient_errors_are_retried`, `test_persistent_outage_is_recorded_per_spot`, `test_transient_flight_failure_keeps_match_pending`, `test_permanent_flight_failure_is_reported`, `test_auth_failure_is_permanent_and_5xx_is_transient` |
| Expired flight offers | `test_expired_offer_rejected`, `test_expired_offer_is_never_alerted_and_triggers_research` |
| Overnight international flights | `test_overnight_international_flight_accepted_on_real_arrival_time` |
| International date line | `test_date_line_crossing_eastbound_arrives_before_it_departs`, `test_query_dates_account_for_time_zones` |
| Destinations without practical airports | `test_destination_without_practical_airport` |
| Duplicate swell events | `test_events_update_in_place_and_never_duplicate`, plus the DB exclusion constraint |
| Unavailable SMS service | `test_unavailable_sms_service_is_reported`, `test_unconfigured_or_disabled_sms_is_unavailable`, `test_outage_is_transient` |
| Stale forecasts | `test_stale_forecasts_are_flagged_and_never_alerted` |
| Multiple overlapping searches | `test_overlapping_searches_send_one_alert` |
| Password reset flows | `test_password_reset_flow`, `test_password_reset_token_expires` |
| Unauthorized access | `test_users_cannot_access_each_others_data`, `test_opportunity_endpoints_and_ownership`, `test_unauthenticated_requests_are_rejected`, `test_csrf_is_required_for_state_changes` |
| Worker restarts | `test_worker_crash_mid_send_is_recovered`, `test_interrupted_forecast_run_is_retried`, `test_lock_expires_after_a_crashed_worker`, `test_real_worker_consumes_from_redis` |

The e2e journey (`frontend/e2e/full-flow.spec.ts`) covers spec steps 1–11: account,
login, search, dashboard, simulated swell, mock flights, opportunity, queued and sent
notification, dashboard card, pause, no new notifications.

## Defects found and fixed during verification (selection)

* `FOR UPDATE` on an outer join made swell detection silently fail. Fixed with
  `FOR UPDATE OF`.
* A stale ORM relationship hid the best offer after flight discovery. Relationships are
  now assigned, and sessions are expired between stages.
* A fixed-date search could match a swell outside the user's dates when no flights
  existed.
* Overlapping searches could alert twice for one swell. Dedup is now per user + event.
* Expired cached offers could be re-alerted. Failed revalidation now invalidates the
  cache.
* Demo offer IDs used Python's per-process `hash()`. Now SHA-256.
* Deleting a search left its queued alerts deliverable. They are now withdrawn, and
  delivery re-checks the search.
* Worker log lines were duplicated, and Celery's handlers were not redacted.
* `.env.example` inline comments became values under Docker Compose.
* CLI JSON output was mixed with log lines. Logs now go to stderr.
* A plain-text 5xx from the proxy showed a JSON parse error in the UI.
* The map used key-less CARTO tiles, which CARTO now answers with "API KEY REQUIRED"
  placeholders (found on a real Mac). It now defaults to OpenStreetMap, derives its CSP
  host from the tile URL, passes all map settings into the Docker build, shows a notice
  when tiles fail, and no longer paints over the sticky header.

## Open items / known gaps

* Live provider calls for Windy, Duffel, Amadeus, Resend, SendGrid and Twilio were not
  executed: outbound access to those APIs is blocked in the build environment.
  Open-Meteo has been verified live.
* No observed-surf calibration. Heights and scores are uncalibrated estimates.
* Not implemented: booking, social login / 2FA, push notifications, an admin UI.
