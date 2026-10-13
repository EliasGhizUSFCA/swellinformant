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

---

# Phase 2: map redesign, simplified frontend, research and forecast confidence

Branch `claude/funny-hamilton-49k2m3`, 2026-10-11 to 2026-10-12. Each part was built, then
adversarially reviewed and fixed before it was merged.

## 9. Interactive map ([`map-layers.md`](map-layers.md))

* **Look and controls** (as in the reference screenshot): full-screen dark map (Natural
  Earth land and borders, country and city labels), ocean colour field with animated
  particles, layer tabs *Wind Speed / Wave Height / Wave Energy / Wave Period*, a
  *Pressure* toggle (isobars every 4 hPa), *Settings* (units, particles, dark or street
  basemap, every spot filter), a *Legend* drawer, locate-me, and a bottom timeline with day
  labels, 3-hour ticks, a local-time bubble and play. Spot markers, the ranked list
  ("Best spots now") and the spot panel are kept; on phones the panel is a bottom sheet.
* **Data**: NOAA GFS-Wave (global 0.25°) and GFS fields, regridded to 0.5° (area-weighted
  box means, directions as unit vectors), quantised to bytes and served by
  `GET /api/map/fields`. They come with every NOAA acquisition, or from a standalone job
  (`MAP_FIELDS_ENABLED=true`) next to demo spot forecasts. Measured on the real 12Z cycle of
  2026-10-11: 81 steps to 240 h, 380 MB downloaded, 66 s, 39 MB stored; served wave height
  equals an independent box mean of the raw NOAA cells within 0.05 m.
* **Honesty**: the legend says these are offshore model values, not surf height; wave
  energy is labelled as the deep-water approximation 0.49·Hs²·Te. Colour-field redraw at
  1440×900: median 13.5 ms.
* Review fixes included a phone attribution overlay that caught taps, run-keyed field
  caching, trail fading, prefetch bandwidth, the job's skip and retention rules, and the
  API middleware that stopped browsers from caching the fields.

## 10. Simplified frontend

* Primary information always visible: our breaking-height prediction with its 80 % range,
  the rating ("Excellent Surf — 8.9/10") with a one-line reason derived from the actual
  factors, the best upcoming window, wind, swell period and direction, and forecast
  confidence. Secondary information (model comparison, tide and secondary swells, research
  detail, scoring method, technical spot data, sources) sits behind "Why this rating?" and
  other expandable sections or "Learn more" popovers that work by mouse, keyboard and touch.
* Applied to the dashboard, spot list and detail pages, charts, travel opportunities, saved
  searches, notifications and the map panel. A before/after inventory confirmed that every
  measurement is still reachable; nothing needed for travel decisions was removed.

## 11. Research confidence ([`research-confidence.md`](research-confidence.md))

* Per spot and per condition: how well the break's ideal conditions are documented
  (methodology `research-v1.1`). Source tiers (survey, peer-reviewed, official; guidebook,
  encyclopedia, forecast-site guide, magazine; community), independence by publisher,
  agreement computed against the spot profile, conflicts, recency at sand breaks; the
  overall class is not an average and never rises with the number of filled fields.
  Assessments are history rows (never updated), so earlier assessments stay available.
* **Evidence**: `data/surf_spots/research_evidence.json`, all 50 spots, 259 sources and 805
  claims, each re-checked by an independent verifier. Web pages could not be opened in the
  build environment, so the evidence came from domain-restricted web searches and every
  source is marked `retrieval: web_search`; such evidence is capped at Moderate
  (ADR-016). Result: 18 spots Moderate, 32 Low, none High. Without the cap three spots
  would be High.
* **Second pass (2026-10-12)**: with direct page access every source that could be read was
  re-read: 449 sources, 384 read directly (excerpts confirmed verbatim on the page, every
  claim judged by an independent verifier), 1,711 claims; 65 sources behind bot protection
  stay `web_search`. Result (research-v1.1): 34 Moderate, 16 Low, none High. Section 14
  builds on it.

## 12. Forecast confidence ([`forecast-confidence.md`](forecast-confidence.md))

* Per forecast hour (`forecast-v1`): the 0–100 index class adjusted by lead time (at most
  Moderate beyond 7 days, Low beyond 10, High at 3–7 days only with a small ensemble
  spread), GEFS spread, disagreement between independent model families only (GFS and
  GEFS share NCEP forcing and never confirm each other), run-to-run change, missing
  variables, per-source staleness, verification where it exists and demo data. Plain
  reasons are stored and shown; the classes are not probabilities.
* *Local confidence* at the break = min(forecast class, research basis), with its own
  reason. Research and forecast confidence are stored and shown separately and never
  combined into one number; both are archived with every prediction.
* **Staleness defect fixed** (ADR-015): with one 18 h limit, ECMWF (12-hourly, published
  ~8 h after the cycle) dropped out of the blend for several hours every day. Limits are
  now per source (NOAA 16 h, GEFS 18.5 h, ECMWF 25 h). On the real 2026-10-11 runs ECMWF
  stayed in the blend 18 of 19 hours instead of 11.
* Live distribution (2026-10-11 runs, 4,050 hours, High/Moderate/Low): 0–1 day
  159/86/5, 1–3 days 544/239/17, 3–7 days 811/715/74, 7–10 days 0/920/280, beyond 10 days
  0/0/200.

## 13. Test results (2026-10-12, integrated branch)

| Command | Result |
|---|---|
| `ruff check`, `ruff format`, `mypy app` | pass (150 files) |
| `alembic upgrade head && alembic check` (to 0005) | pass, no drift |
| `pytest tests/unit` / `pytest tests/integration` (separate processes) | **500 passed** / **186 passed** |
| `npx tsc --noEmit && npx eslint . && npx vitest run` | pass, 147 tests |
| `./scripts/run-e2e.sh` (production build, demo backend) | **51 passed** |

Not verified: research classes against observed surf, forecast-confidence calibration
(no nearshore observations), Safari/Firefox rendering of the map particles, and a
long-running Celery beat refreshing the map fields.

---

# Phase 3: evidence-based spot profiles

Branch `wf/spot-profiles`, 2026-10-13 ([`decisions.md`](decisions.md) ADR-018).

## 14. Research-v1.2, profile decisions, measured bathymetry, disputed winds

* **research-v1.2** ([`research-confidence.md`](research-confidence.md) §3, §9): swell-size
  values are compared bound by bound (a stated maximum more than 25 % above the profile's,
  or a minimum more than 25 % below, contradicts), record waves are not working ranges, and
  the tier rules are written down (tourism boards and World Surf League editorial pages are
  magazine). Seven tourism sources were reclassified and five values removed, each noted on
  its source. On a fresh database: **35 Moderate, 15 Low, none High** (research-v1.1 on the
  second pass: 34 / 16); swell size is now contradicted at 25 spots instead of 1, and the
  research basis of local confidence falls (High 8 → 3, Low 12 → 24 spots).
* **Profile decisions** (`data/surf_spots/profile_decisions.json`): 82 records for every
  contradicted field (4 changed, 48 disputed, 30 kept) and 6 geometry notes, each with its
  sources, rationale and before/after effect on the real 2026-10-11 run
  (`scripts/profile_sensitivity.py`, read-only). A value changes only when two independent,
  directly read publishers agree on it and outrank every direct source of the old value; no
  offshore wind met that (11 disputed, 9 kept). Changed: Ocean Beach, Snapper Rocks and
  Punta Roca tide → all, Cloudbreak 4-20 → 2-20 ft. Wind records report the model wind at
  the forecast point (e.g. Chicama: from 163°, 99 % of hours within 45° of the profile's
  150°, none within 45° of the disputed 67.5°); it never decides.
* **Disputed winds** (forecast-v1.1, [`forecast-confidence.md`](forecast-confidence.md)
  §3a): on the real run 15 spots carry "Sources disagree about which wind is offshore here."
  on every hour (1,215 hours), and on 1,101 of them the explanation names both relations;
  scores, `wind_relation` and both classes are unchanged by it.
* **Measured bathymetry** (NOAA BlueTopo, `scripts/bathymetry_profiles.py`,
  [`SURF_SCORING.md`](SURF_SCORING.md) physical profiles): survey depths at the forecast
  points of Mavericks (57.9 m), Ocean Beach (36.4 m) and Rincon (96.5 m) and a measured
  shore normal at Trestles (210.9°); Trestles' forecast point lies in BlueTopo's NBS
  Generalization surface (no survey), so its depth stays unknown (deep water either way);
  the other three contour normals failed the
  documented acceptance rules and are kept in the provenance; no controlling depth, because
  every break coordinate is a shoreline position. On the real run: Mavericks' mean central
  breaking height 9.48 → 9.82 ft (mean |Δ score| 1.4), Ocean Beach 8.14 → 8.60 ft (0.21),
  Trestles 5.77 → 5.72 ft (0.06), Rincon 1.08 → 1.09 ft (0). 19 tiles were added as
  `survey_data` sources: 468 sources (403 read directly), 1,730 claims.
* **`python -m app.cli research lint`**: of the 19 spots whose offshore wind is more than
  60° from the estimated straight-offshore direction, 18 now have a decision record or a
  geometry note; Restaurants stays listed (no source describes its orientation).

Not verified: whether any changed, kept or disputed value is right at the break (no
nearshore observations), the BlueTopo depths at the take-off zones (no surveyed lineup
positions), and the model wind as a climatology (one 10-day October sample).

**Review (2026-10-13, branch `wf/spot-profiles-review`).** Re-run independently: research
load + assess on a fresh database (35 Moderate, 15 Low; research basis High 3, Moderate
23, Low 24), `research lint` (Restaurants only), the sensitivity and model-wind tables
(all records reproduced except Cloudbreak's wind record, 20.14 → 20.31, whose baseline now
has the changed size range), the bathymetry script (byte-identical output from the
cached tiles, whose checksums match a fresh download of the tile scheme and of three
tiles), an independent sampling of the tiles (same depths and plane-fit azimuths without
the meridian-convergence step), the four changed fields against their pages (every
directly read excerpt found again, two independent directly read publishers each) and the prediction job on a
re-seeded clone (the numbers above). Fixed: a publisher that both supports and contradicts
a value no longer outweighs its own contradiction (Anchor Point's wind, Lobitos' swell
window and Waimea Bay's size range kept → disputed; no prediction changes, Anchor Point
already carried the note), and Trestles' 758.7 m forecast-point depth, an NBS
Generalization cell outside any survey, is no longer labelled measured (no prediction
changes: deep water either way).
