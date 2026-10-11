# Upgrade report: debugging, research and forecasting

Branch `claude/funny-hamilton-49k2m3`, 2026-10-11. Details live in the linked documents;
everything stated as verified below was run, with the results shown.

## 1. Bugs fixed

| Bug | Root cause | Change | Tests | Status |
|---|---|---|---|---|
| Map tiles showed "API KEY REQUIRED" | CARTO raster basemaps now need a key; the app defaulted to a key-less CARTO URL, which returns HTTP 200 with that text | OSM by default with attribution and Referer; CARTO only with a key; CSP derived from the tile host | `tests/map-tiles.test.ts`, `e2e/map.spec.ts` (6 tests) | fixed, verified live |
| Map grey until resize, overlapping the header, untargetable markers, silent tile failures, slider label mismatch | Leaflet sizing, stacking context, `className` handling, no tile error handling, browser vs server "now" | ResizeObserver, `isolation`, direct `className`, tile-failure notice, server-time slider | `e2e/map.spec.ts` | fixed |
| Verification emails "sent" but never received | dev default captured mail in an invisible file; no delivery record | Mailpit inbox in Compose; `email_messages` with honest statuses (`queued → sending → accepted / captured`, `delivered/bounced` only from signed webhooks); dashboard status banner | `test_email_delivery.py`, `test_delivery_events.py`, `test_auth_api.py`, e2e | fixed, verified with Mailpit |
| Reused or superseded verification link said "invalid token" | used tokens treated as forged | `already_verified` / superseded responses; public, rate-limited resend | `test_auth_api.py` | fixed |
| Alerts silently dropped (unverified address, daily cap) or labelled "sent" when only captured | suppression not persisted; status vocabulary too small | suppressed alerts stored with a reason and sent when it clears; honest statuses; complete alert content | `test_pipeline.py`, `test_notification_providers.py` | fixed, verified with Mailpit |
| Event/alert headline "0.16 m swell" while predicting 6-8 ft (live data) | headline was WW3's primary swell partition even under a 2 m wind sea | partition with the largest predicted breaking height (`dominant_component`) | `test_dominant_component.py` | fixed |
| ECMWF CC BY 4.0 attribution missing when the blend was shown | blend source had only generic text | blend repeats every member's attribution | `test_blend.py` | fixed |
| Map ranked spots at night as "Exceptional" | sorted by score regardless of daylight | daylight first, night tagged | e2e map | fixed |
| pytest exit 139 after every test passed | `eccodeslib` and `psycopg-binary` bundle different OpenLDAP builds; ecCodes loaded first crashes at exit | `grib.load_eccodes()` loads psycopg first via `importlib` | full suite exit 0 | fixed |
| 15-step e2e scenario failed in a full-suite run | other specs left simulated swells at the same spot | scenario uses its own spot | full e2e suite | fixed |
| Ingestion review (7) and observations/ML review (15) defects | seam-cell longitude wrap, PARTIAL vs SUCCESS, memory, error isolation, artifact path traversal, artifact location, criterion (c) gaps, transferable pairings, 403 as "missing", partial NDBC results, ... | fixed on the review branches with failing-first tests | unit + integration suites | fixed (low-risk residuals listed in the review notes) |

Full list with evidence: [`debug-audit.md`](debug-audit.md).

## 2. Research findings

[`surf-forecast-model-research.md`](surf-forecast-model-research.md) (18 sections, 93
sources). The recommended approach, and why:

1. **Consume operational models, don't re-run them**: NOAA GFS-Wave (WAVEWATCH III) and GFS,
   ECMWF IFS/ecWAM as an independent family, GEFS-Wave for spread.
2. **Combine by dependency family with correlated-error weights**, not equal averaging
   (copies of the same model are not independent evidence).
3. **A physical nearshore baseline** (linear wave theory + Komar-Gaughan per partition,
   directional spreading, shore normal, shadowing, focusing) instead of an ML surf-height
   model: there are no surf-height observations to train or validate one.
4. **ML only as verified post-processing of offshore Hs** against buoys, with
   chronological splits, gaps, baselines and pre-registered deployment criteria; published
   gains are small and regional, so "no model deployed" is an acceptable outcome.
5. **Uncertainty as labelled ranges** until coverage is verified.

## 3. Forecasting implementation

* **Data sources** (verified live 2026-10-11, all 50 spots, 0 errors):
  NOAA GFS-Wave + GFS direct (4,050 records, 1.91 GB, ~4 min), ECMWF IFS open data
  (3,250 records, 847 MB, 112 s), GEFS-Wave statistics (2,050 rows, 201 MB, 29 s);
  blend + 11,350 predictions + archive + detection. Observations: AODN delayed-QC and
  realtime, Met Office, GFS-Wave station bulletins (live); NDBC (formats only, blocked here).
* **Physical model**: `linear_wave_v2` + scoring 2.0.0 ([`SURF_SCORING.md`](SURF_SCORING.md)).
* **ML**: ridge / random forest / gradient boosting on the log Hs residual
  ([`ml-pipeline.md`](ml-pipeline.md)).
* **Validation** ([`ml-evaluation.md`](ml-evaluation.md)): raw GFS-Wave vs QC'd spectral
  Hm0 at three Western Australian buoys (2021, 209,658 pairs): RMSE 0.40 m at 24 h, 0.47 m
  at 72 h, 0.75 m at 168 h, 1.01 m at 240 h; biased low ~0.23 m; beats persistence at
  every lead, no skill against long-term climatology at 240 h. At Cape Naturaliste a ridge
  correction cut held-out RMSE 0.360 → 0.331 m (-8.1 %) and passed every criterion; RF
  and GBT were rejected. **It ships as an inactive candidate**: on live gridded inputs at
  the spots 18 % of the corrected hours fell outside its tested factor range (ADR-013).
* **Known limitations**: breaking height is not validated (no nearshore observations);
  spot profiles are estimates; the 80 % range is heuristic; quality scores saturate near
  the top on big swells; one-region, one-season ML evidence; NDBC unverified live.

## 4. Application changes

* **Map**: OSM tiles, forecast-time control, filters, legend, our prediction vs raw model
  data, daylight-first ranking.
* **Spot page**: our prediction with its 80 % range, every model's raw values, GEFS
  spread, disagreement flags, physical profile with provenance, member attributions.
* **Email**: Mailpit inbox, delivery tracking, signed webhooks, suppression with reasons,
  80 % range in alerts, sender/DNS guide.
* **Forecasting**: direct NOAA/ECMWF/GEFS ingestion, families, blend, nearshore v2,
  uncertainty, immutable prediction archive, observation ingestion, verification, ML
  pipeline with registry and a pipeline hook (ADR-012).
* **Database** (migrations 0002, 0003): `email_messages`, `spot_physical_profiles`,
  `forecast_ensemble_stats`, `forecast_comparisons`, `prediction_archive`,
  `buoy_stations`, `buoy_observations`, `point_forecasts`, `nearshore_observations`,
  `ml_experiments`, `ml_model_versions`, `prediction_validation`,
  `forecast_accuracy_metrics`; new prediction columns (mid, p10, p90, uncertainty kind,
  ML version).
* **Endpoints**: `/api/auth/verification-status`, public resend, `/api/webhooks/{resend,
  sendgrid}`, `/api/admin/accuracy/{overview,metrics,pairs}`, `/api/dev/run-pipeline`
  switches, `/api/dev/test-email`; spot forecast/map responses carry models, ensemble,
  blend and uncertainty.
* **Background jobs**: `forecasts.blend`, `forecasts.acquire_ensemble`,
  `observations.ingest`, `verification.collect_point_forecasts`, `verification.run`.
* **CLI**: `engine`, `obs`, `verify`, `ml` groups; `make-admin`.

## 5. Test results (final run, 2026-10-11)

| Command | Result |
|---|---|
| `cd backend && ruff check app tests && ruff format --check app tests && mypy app` | pass (137 files) |
| `alembic upgrade head && alembic check` | pass, no drift |
| `cd backend && pytest -q` (one process, PostgreSQL + Redis) | **521 passed**, exit 0 |
| `cd frontend && npx tsc --noEmit && npx eslint . && npx vitest run` | pass, 41 tests |
| `cd frontend && npm run build` | pass |
| `E2E_MAILPIT=1 PLAYWRIGHT_CHROMIUM_PATH=/opt/pw-browsers/chromium ./scripts/run-e2e.sh` | **15 passed** (incl. the 15-step scenario) |
| `docker compose build && docker compose up -d` (sandbox override for the proxy) | all 7 services up (6 with health checks, all healthy); ecCodes decodes a real JPEG2000 GFS-Wave record in the image; manifest import works in the container |
| Live ingestion: `engine ensemble`, `pipeline` with `noaa_gfs,ecmwf_ifs` | success, 0 errors (section 3) |

Not verified: real Resend/SendGrid/Twilio/Duffel/Amadeus delivery (no credentials), NDBC
(blocked host), outbound HTTP from inside the sandbox's Docker network.

## 6. Running the updated application

```bash
cp .env.example .env                 # optional; demo defaults work
docker compose up --build            # web :3000, API :8000, Mailpit :8025
# live forecasts instead of demo (in .env):
#   FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs
#   GEFS_ENABLED=true
docker compose exec backend python -m app.cli make-admin you@example.com   # accuracy dashboard
```

## 7. External setup remaining

* Email: a verified sending domain (SPF, DKIM, DMARC), a Resend or SendGrid key, and the
  signed webhook secret/public key (README "Email delivery").
* Flights: `DUFFEL_ACCESS_TOKEN` (or Amadeus Enterprise credentials).
* SMS: Twilio credentials and the STOP webhook.
* Map: a tile provider key for more than light use.
* Observations: network access to `www.ndbc.noaa.gov` to verify NDBC; newer AODN
  delayed-mode Hm0 (2022+) to re-evaluate the correction; any nearshore surf-height
  observations to validate breaking heights.

## 8. Recommended next steps (by expected impact)

1. **Archive gridded GFS-Wave at the buoys** (`sample_gfs_points`) as point forecasts, so
   the ML correction is trained on the same inputs the spots use; re-run `ml train`
   including a winter test season, then decide on activation.
2. **Collect nearshore observations** at a few spots (surf cams with manual reads, partner
   reports) to calibrate and validate breaking heights; this is the largest error source.
3. **Measure spot profiles** (shore normal, controlling depth, shadow tables from
   bathymetry) for the most-alerted spots.
4. **Calibrate the quality score and the 80 % range** once observations exist (coverage
   check before calling it `calibrated`).
5. Verify NDBC live and add NDBC-paired spots (Hawaii, California) to verification.
6. Add an ECMWF/GEFS date check, a total ECMWF retry budget, and an advisory lock for
   concurrent `ml train` runs (review residuals).
