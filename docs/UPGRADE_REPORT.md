# Upgrade report: debugging, research and forecasting

> Chronological record: each section describes the state on its date. Current setup:
> README.md and docs/deploy-vercel.md; the owner checklist is section 44 (with the refresh
> schedule of docs/deploy-vercel.md step 7b added by Swell 2.0, section 46).

Branch `claude/funny-hamilton-49k2m3`, 2026-10-11. Details live in the linked documents;
everything stated as verified below was run, with the results shown.

Contents: sections 1-8 (debugging, research, forecasting, 2026-10-11), Phase 2 (sections
9-13: map redesign, simplified frontend, confidence), Phase 3 (sections 14-18:
evidence-based spot profiles, validation, operations), **Upgrade 3 (2026-10, sections
19-37: forecasting, map, weather visualisation and UI)**, **Upgrade 4 (2026-10, sections
38-45: a website on Vercel, data synced to model runs, the wave look; the owner checklist is
section 44)** and **Swell 2.0 (2026-10, sections 46-51: automatic model-run detection,
per-component refreshes, versioned publication, live page updates, researched spots,
targeted flights, freshness monitoring)** at the end.

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
#   FORECAST_PROVIDERS=noaa_gfs,ecmwf_ifs   (since Upgrade 3: open_meteo,open_meteo_ecmwf, README F.5)
#   GEFS_ENABLED=true
docker compose exec backend python -m app.cli make-admin you@example.com   # accuracy dashboard
```

## 7. External setup remaining

* Email: a verified sending domain (SPF, DKIM, DMARC), a Resend or SendGrid key, and the
  signed webhook secret/public key (README "Email delivery").
* Flights: `DUFFEL_ACCESS_TOKEN` (or Amadeus Enterprise credentials).
* SMS: Twilio credentials and the STOP webhook.
* Map: a tile provider key for more than light use.
* Observations: any nearshore surf-height observations to validate breaking heights (NDBC
  was verified live in phase 3, §16).

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
5. ~~Verify NDBC live and add NDBC-paired spots (Hawaii, California) to verification.~~
   Done in phase 3 (§16).
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

## 15. Second research pass (2026-10-12)

With direct page access all 50 spots were re-researched: every readable source was re-read,
its excerpts replaced by verbatim page text (checked mechanically on the page) and every
claim judged by an independent verifier. 449 sources (384 read directly; 65 behind bot
protection kept from the first, search-only pass) and 1,711 claims; 41 claims were rejected
by the verifiers (e.g. record or contest heights encoded as working ranges, a Surfline
forecast quoted in a WSL post). Research classes moved from 18 Moderate / 32 Low to
34 / 16 (35 / 15 after research-v1.2 and the BlueTopo sources, §14).

## 16. Validation against buoys ([`ml-evaluation.md`](ml-evaluation.md) §11-12, ADR-017)

* **NDBC verified live** (formats, missing-value codes, WVHT = spectral Hm0, matched against
  CDIP files) and paired with the 8 US spots (10 buoys). Raw GFS-Wave vs NDBC Hm0, 24
  months, 409,344 pairs: bias -0.23 m; RMSE 0.38 m at 24 h, 0.51 m at 168 h, 0.64 m at
  240 h; skill vs the long-term climatology 0.58 at 24 h, 0.23 at 168 h, negative at 240 h.
* **80 % range**: the app's single-model range covers 53 % at 24 h (too narrow; misses
  mostly above p90 because the model is low). A lead-dependent scale fitted on the older
  year covers 75-80 % on the held-out year pooled; it is labelled calibrated only where the
  spot's own buoy agrees at every lead (Jaws, Trestles) and is not yet wired into the
  prediction job. The ensemble range and the breaking range stay heuristic.
* **Confidence classes**: with a GEFS-Wave spread the three classes are ordered by realised
  error on both years (RMSE 0.34 / 0.49 / 0.75 m); single-model hours without an ensemble
  show no High-vs-Moderate difference. A cap fixing that ("C2") was rejected in review
  because its test was written after seeing the results; forecast-v1 classes stay, and the
  gap is documented. Live hours all have ensemble data.
* **ML correction**: a random forest trained on NDBC + AODN spectral Hm0 passed the
  unchanged pre-registered criteria (held-out RMSE 0.43 → 0.32 m) and the transfer check,
  and is approved for 7 spots, leads 3-240 h. It is **not activated**: activation is an
  operator step, and the review found its features mean slightly different things at the
  spots (expected benefit smaller than at the buoys) and a post-correction bias of
  +0.09 m.
* Not validated: breaking heights (no nearshore observations), rules needing two
  independent model families, gridded values at the spots.

## 17. Production operations ([`operations.md`](operations.md))

* **CI**: GitHub Actions (backend lint, types, migrations, unit and integration tests;
  frontend tsc, eslint, vitest, build; e2e). First run on GitHub, 2026-10-13: success.
* **Data health**: `/api/health/data` and the status page report each source against its
  own staleness limit and each scheduled job; a beat job emails admins once per problem
  (de-duplicated) and sends a recovery notice.
* **Backups**: `scripts/backup-db.sh`, `restore-db.sh` (refuses a schema-revision mismatch),
  `restore-drill.sh`, a compose `backup` profile; restore drill: 42 tables and 34,800 rows
  identical.
* **Live Docker run**: downloads from inside the Docker network (NOAA 18Z 213 MB in 65 s,
  GEFS at its own cron time), a 30-minute soak with 63 jobs and 0 failures, peak memory
  worker 849 MiB / API 471 MiB / PostgreSQL 187 MiB; a backup race after a daemon restart
  was found live and fixed.
* Date- and time-of-day-dependent tests found in review were made deterministic.

## 18. Test results (2026-10-13, integrated branch `0c42bc6`)

| Command | Result |
|---|---|
| `ruff format --check`, `ruff check`, `mypy app` | pass (159 files) |
| `alembic upgrade head && alembic check` (to 0005) | pass, no drift |
| `pytest tests/unit` / `pytest tests/integration` (separate processes) | **635 passed** / **229 passed** |
| `npx tsc --noEmit && npx eslint . && npx vitest run` | pass, 156 tests |
| `./scripts/run-e2e.sh` | 50 passed + the timeline test fixed and re-run (26 map tests passed) |
| GitHub Actions CI run 1 | success |

**Final sweep (2026-10-13).** Security, code-health and product reviews of the integrated
tree, each fixing only demonstrated issues with tests: secrets and tokens no longer reach
logs through tracebacks or the access log; a client-written X-Forwarded-For can no longer
bypass per-IP rate limits (`--no-proxy-headers`); backup dumps cannot be staged by git; CI
actions are pinned to commits without persisted credentials; two inputs that caused 500s
or a loader crash are handled; concurrent test runs no longer collide on fixed scratch
database names; the spot page's hour view keeps the local-confidence reason; the
disputed-wind note is shown next to the wind instead of only inside collapsed details;
map markers open from the keyboard; the status page fits a phone; demo data is no longer
called "live" on the landing page. Final run on the integrated tree: ruff, mypy and
`alembic check` pass; **644 unit**, **233 integration**, **160 vitest** and **62 e2e**
tests pass.

---

# Upgrade 3 (2026-10): forecasting, map, weather visualisation and UI

The owner's request of 2026-10-13 asked for bounded map navigation, API-first weather data
instead of gigabytes of GRIB, physically correct wind and wave animation, spot-specific
offshore wind scoring, a unified forecast timeline, tides, separate confidences, shorter text,
tests and documentation. Branch `wf3/integration`, from the audit base `2bd2b50` to
`18739b4` before this report, 2026-10-13 to 2026-10-16. Work order: the audit, a foundation
of shared contracts, eight packages built in parallel (point-data, map-fields, map-shell,
map-flow, wind-scoring, tides-obs, spot-timeline, confidence-text), an adversarial review of
each, the integration, a live run on real data, five cross-cutting reviews (physics, UX,
security and licensing, docs, regression) and their fixes.

Everything below is measured, ran or read as stated. "Computed" means arithmetic on measured
values; "extrapolated" says what was assumed. Nothing here is marked working unless it was
run.

## 19. Audit

[`forecast_upgrade_audit.md`](forecast_upgrade_audit.md): ten area audits on `2bd2b50`
(map navigation, wind visualisation, wave visualisation, data acquisition, external APIs,
observations and tides, offshore wind and scoring, charts and spot API, confidence and text,
platform and tests), with root causes, affected files, planned changes and the test
requirements of spec 27. 144 findings: 25 high, 63 medium, 56 low; the lead re-checked the
most consequential ones by code reading or by running them. Baseline then: 644 backend unit
tests, 233 integration tests (listed, not run), 160 vitest tests, 62 Playwright tests
(listed, not run).

## 20. Root causes

| Area | Root cause (audit ID) | Fix |
|---|---|---|
| Map blank regions | No `maxBounds`; a fixed `minZoom` 2 showed 1.88 world copies at 1920 px; markers only in world copy 0 (MAPNAV-1, -2, -3) | Hard latitude band 84°N-78.5°S, a one-world minimum zoom per container, markers in the copy nearest the view centre, `worldCopyJump` off |
| Map stutter | `closePath()` after each of 2,558 basemap rings: 214 ms of a 234 ms redraw (MAPNAV-5) | Removed; dark basemap redraw 6.2-7.8 ms |
| Gigabytes downloaded | GRIB2 records are whole-globe: a byte range picks a record, not a region, so 50 spots cost about 2.9 GB per set of cycles (DA-1) | Spot forecasts from Open-Meteo point requests (two families); GRIB kept only for the gridded map layers and GEFS, trimmed |
| Open-Meteo not equivalent to direct sampling | Land cells for wind, one interpolated wave model, no tertiary swell, interpolated hours stored as native, 0/0/0 stored for absent partitions (DA-5, API-03, -04, -05, -07) | `cell_selection=sea` with a masked-cell neighbour search, per-latitude wave models, tertiary swell, native steps only, absent partitions stay absent |
| Waves looked like wind | Every wave tab used the wind particle engine (WAVEVIZ-1); no height or physical wavelength (WAVEVIZ-2); partitions never stored (WAVEVIZ-3) | A separate crest renderer from height, period and direction per system; partitions stored and served |
| Wind trails veil | Trail fade relied on a `float16` canvas; 8-bit canvases kept a 6/255 veil (WINDVIZ-1) | Explicit trail renderer on 8-bit canvases |
| Offshore always ideal | Offshore Q = 1.0 from 0 to 35 km/h with a 0.4 floor (80 km/h offshore scored 73); cross-shore and onshore stopped getting worse at 20 km/h (SW-1, -2) | Continuous Q_wind from V·n with no floor; strong offshore penalised |
| Wrong swell scored | Direction and period scored from the primary swell even when another system made the breaking waves: 758 of 4,050 real hours (SW-3) | Every partition weighted by its share of the breaking energy flux |
| Unreadable charts | Day ticks only at samples at local midnight: 42 of 50 spots had no axis labels (CH-1); no shared selection or x layout (CH-2, -3); "three hours later" moved 9 h (CH-4) | One timeline, one time axis, one selection store, ticks from the axis in the spot's zone |
| No tides | No live tide source: 0 of 11,350 rows had a sea level (OBS-TIDE-1, -2) | NOAA CO-OPS and EOT20, with datum, station and high/low events |
| Mixed confidences | One combined reasons list rendered under "Forecast confidence", research sentence included (CT-1); model output titled "Now" (CT-2) | Forecast-only reasons, separate badges, "Forecast" with valid and run times |
| Windy terms | The adapter stored, blended and archived data the terms forbid storing (API-01) | Refused unless `WINDY_TERMS_ACKNOWLEDGED=true` |

Found later, by the reviews and the live run: the blend averaged wind as a scalar speed plus
unit-vector direction, inventing winds neither model forecast (physics review blocker,
PHYS-1); "good hours" and event hours counted steps instead of hours of light (PHYS-3, -4);
model credits were missing on the landing page, dashboard, opportunity pages and emails
(security review blocker, SEC-1); the CO-OPS and NDBC byte counters counted decoded bodies,
about 15× and 7× the bytes transferred; the map readout kept the previous place's values after
the map moved under a still mouse.

## 21. Files changed, by area

`git diff --numstat 2bd2b50..18739b4`: 343 files, +63,796 / -3,899 lines.

| Area | Files | Lines added / removed | Main new files |
|---|---|---|---|
| Backend forecasts (providers, blend, map fields) | 19 | +2,916 / -506 | `services/forecasts/open_meteo_ecmwf.py`, `quota.py`, `provider_cache.py` |
| Backend scoring and confidence | 8 | +1,128 / -107 | `services/surf_quality/wind.py` |
| Backend tides and observations | 11 | +2,311 / -10 | `services/tides/` (CO-OPS client, harmonic model, catalog, ingest, stage, API blocks) |
| Backend API, schemas, models, workers, other services | 39 | +3,332 / -250 | `api/timeline.py`, `services/timeline.py`, `services/timesteps.py`, `api/tides.py`, `models/tides.py` |
| Migration | 1 | +182 | `alembic/versions/0006_forecast_upgrade.py` (map-field partitions, tides, spot wind profiles) |
| Backend tests (with fixtures) | 110 | +22,238 / -171 | |
| Frontend spot timeline | 12 | +3,007 | `components/forecast/*`, `lib/timeline.ts` |
| Frontend map | 22 | +3,281 / -570 | `components/map/wavefronts.ts`, `trails.ts`, `map-freshness.tsx`, `lib/map-waves.ts`, `lib/map-bounds.ts` |
| Frontend other (pages, confidence, units, types) | 44 | +5,441 / -1,014 | `lib/units.ts`, `lib/directions.ts`, `components/stale-banner.tsx`, `forecast-provenance.tsx` |
| Frontend tests (vitest, e2e) | 51 | +9,899 / -884 | |
| Data | 3 | +4,906 | `data/tide_stations.json`, `data/tides/constants_eot20.json` (51 kB) |
| Scripts | 3 | +733 / -15 | `scripts/extract_tide_constants.py` |
| Docs | 14 | +4,217 / -356 | the audit, `forecast-charts.md`, `map-navigation.md`, `map-rendering.md`, `tides.md`, `API_SETUP_REQUIRED.md` |
| Other (config, CI, compose) | 6 | +205 / -16 | |

## 22. Map improvements

Measured with Playwright on production builds ([`map-navigation.md`](map-navigation.md),
[`map-rendering.md`](map-rendering.md)):

| What | Before | After |
|---|---|---|
| Blank rows after drags (1440×900) | 805 of 807 after one 600 px drag north | worst 1 row outside the band over 14 drag/zoom checks per size; 0 rows past the Mercator edge |
| Minimum zoom | fixed 2 (1.88 world copies at 1920 px) | one world: 2.49 at 1440×807, 2.91 at 1920×987, recomputed on resize |
| Markers at the date line | 20 of 38 spots in view had a marker (centre 179°E) | every marker in its nearest copy; 0.00 px offset during a 25-frame drag |
| Cloudbreak → Pipeline flight | 335.2° of longitude | 24.76° |
| Marker Leaflet calls over 5 timeline steps | 1,000 each of `setLatLng`, `setStyle`, `on`, `off` | 0 |
| Dark basemap redraw at zoom 2 | 234 ms | 6.2 ms (final run; 5.1-9.4 ms across runs) |
| Colour-field redraw 1440×900 | median 13.5 ms (2026-10-12) | median 11.2-18.1 ms across runs (guard < 250 ms) |

Also: a stale chip ("Forecast out of date — model run N h old"), the model run and lead time
beside the timeline bubble, `?t=` links to the spot page, zone-named times, a side sheet for
phones in landscape, the best-spots list naming its time, x/10 marker tooltips, and marker
colours from a dark-surface ramp with at least 3:1 against the sea colour.

## 23. Weather API integrations

* **Open-Meteo, two independent families** (GFS + GFS-Wave and ECMWF IFS + WAM), with
  weighted-call quotas charged before each request, a bounded Redis response cache keyed by
  every domain's init time, run identity from `meta.json` with the documented 10-minute wait,
  `cell_selection=sea` with a neighbour search for masked wave cells, tertiary swell, peak
  periods, and native model steps only.
* **Parity with direct sampling, same cycle, 50 spots** (2026-10-14, re-checked 2026-10-15):
  GFS-family Hs within 0.05 m of NOAA NODD at all 50 spots (max 0.01 m); GFS wind within
  0.21-0.24 km/h and 2.4-4.6° of NOAA's own 13 km grid; ECMWF family Hs within 0.05 m of ECMWF
  open data (max 0.0098 m), wind max 0.24 km/h.
* **GEFS-Wave** trimmed to `GEFS_STAT_FIELDS` (Hs, Tp): 201.6 MB → 69.4 MB per run (measured).
  **ECMWF direct** trimmed to the variables used: about 0.85-0.9 GB → 0.48 GB per run
  (computed from `.index` offsets).
* **Map layers standalone** from GFS `pgrb2.0p50` (0.86 MB per hour instead of 2.93 MB for
  `pgrb2.0p25`) and GFS-Wave `global.0p25` with nine partition records, cycles 00Z and 12Z,
  a byte budget per run (verified live: a 20 MB budget stopped at 19.6 MB with a PARTIAL run).
* **Tides**: NOAA CO-OPS for the 8 US spots (1.7-18.6 km away), EOT20 constants committed for
  the other 42 (cells 1.1-13.3 km from the break), synthesised locally; matches pyTMD 3.0.9
  within 0.012 mm over 30 days at all 50 points; against CO-OPS RMSE 2.7-4.4 cm, r 0.995-0.998,
  timing 6-10 min ([`tides.md`](tides.md)).
* **NDBC** `5day2` files instead of the 45-day `realtime2` files: 5.59 MB → 0.86 MB of files
  per hourly run (both decoded), 0.12 MB on the wire (measured 2026-10-12 and 2026-10-16).
* **Windy** refused without `WINDY_TERMS_ACKNOWLEDGED=true`; its wind-sea keys fixed
  (`wwaves_*`).
* **Retries**: transient failures now retry for real (a transient `forecasts.acquire` failure
  retried after 120 s in the regression review); quota exhaustion fails the run with the
  provider's message and keeps the previous run.

## 24. Storage and caching

| What | Before | After |
|---|---|---|
| Files on disk from acquisition | none (GRIB decoded in memory) | none: strace over all 9 jobs of the live run found only the driver's report |
| Redis | no `maxmemory`, `noeviction`, no monitoring; 917 KB-1.04 MB used | `maxmemory` 256 MB, `noeviction`, data-health check on the fraction used; 1.90 MB → 2.43 MB over a full live pass |
| Provider response cache | none | at most 200 entries of up to 2 MB, compressed, one model run |
| Archive row | 1,506 B (81 % confidence inputs) | 555 B with the confidence digest (746 B with indexes) |
| Member-run predictions | kept 10 days (about 119 MB a day) | pruned once superseded (`PRUNE_SUPERSEDED_MEMBER_PREDICTIONS`) |
| Prediction row (2.1.0 stores wind detail, contributions, partitions, tide detail) | 2,596 B (2.0.0) | 3,403 B |
| Database size at the 730-day archive retention | about 30 GB of archive alone (audit) | about 17 GB in all (computed from fresh-table row sizes; a lower bound) |
| Size alarm | none (`DATA_HEALTH_DATABASE_MAX_GB` unset) | 20 GB default, alert only, above the computed two-year steady state (lower it for a small disk, section 36) |
| Map fields in the browser | LRU of 24 (evicted the step on screen) | LRU of 48, at most 64 MB; 56.7 MB of array buffers after 48 downloads |

## 25. Offshore wind scoring

[`SURF_SCORING.md`](SURF_SCORING.md) sections 3b-3e. Offshore means wind from land to sea:
the offshore component is V·n, with u = -V sin θ, v = -V cos θ and n the seaward normal at
the spot's researched `offshore_wind_direction` + 180°, never the nearshore shore normal
(at Chicama that would be 120° off). Q_wind is continuous with no floor: calm and light
offshore score 1, Q(10 km/h offshore) ≥ 0.95, Q(50 km/h offshore) = 0.32, Q(100) < Q(60);
cross-shore and onshore wind lose more with speed. Strength classes follow Beaufort; six spots
have researched wind profiles (tolerance, shelter sectors, extra offshore bearings).
Algorithm 2.1.0 also scores swell direction and period over every partition by its share of
the breaking energy flux, tapers the swell window, and scores the tide only where the research
supports a preference.

On the real 2026-10-11 run (4,050 steps), re-scored twice on independent clones:

| Check | 2.0.0 | 2.1.0 |
|---|---|---|
| Offshore steps at 45 km/h or more rated Very Good or better | 0 of 3 | 0 of 3 |
| Offshore steps at 30 km/h or more rated Very Good or better | 37 | 19 |
| Cross-shore steps at 30 km/h or more rated Good or better | 37 of 127 | 0 of 151 |
| Labels changed | | 1,313 (32.4 %); mean \|Δ score\| 7.73 |

With real tides stored for that window, 1,354 steps at 18 spots are tide-scored, 126 more
labels change and the acceptance numbers stay the same. Review mutation testing: 22 of 22
code mutations break the tests.

The blend fix (PHYS-1): wind direction from the speed-weighted mean wind vector. On a clone of
the live 2026-10-16 run, blended directions more than 45° from both members fell from 555 to
111 steps, and wind relations matching neither member from 394 to 72; 342 labels changed, and
702 of 10,400 hours are flagged "Models disagree on the wind direction" (forecast confidence
then at most Moderate). Example: Trestles 2026-10-17 18Z went from 92 Excellent,
cross-offshore, High to 69 Good, onshore, Moderate.

## 26. Wind visualisation

* Particles move with the GFS 10 m wind at 12 CSS px per second per m/s, time-based: 120 px ±
  1 in 1 s for 10 m/s at 60, 120 and 144 Hz (vitest with fake frame times).
* The sign of v and the screen vector `(u, -v)` are unit-tested (WINDVIZ-7); Web Mercator is
  conformal, so no `cos(lat)` factor (0.000° projection error measured in the audit).
* 8-bit canvases (Firefox, Safari, iOS) draw explicit trails: alpha 5/6/7 counts after 5 s
  4,071 / 6,423 / 4,012 (no spike at 6; the float16 fade renderer 11,290 / 8,922 / 8,647).
  Tested in Chromium forced to an 8-bit canvas; Firefox and WebKit not run here.
* A new forecast step keeps the particles (no reseed); the field index is re-read every
  5 minutes and on focus; a field whose run or valid time differs from the request is never
  drawn (WINDVIZ-3, -4).
* mph, kn and m/s next to km/h, one preference shared with the spot charts; the readout says
  "from"; on the dark basemap land is translucent on the Wind layer (in the audit 49 % of the
  particles in the default view sat over land or ice, under opaque land).
* Main-thread cost per frame (headless, software raster): 1.66 ms script, 28.4 ms task.

## 27. Wave visualisation

* A crest renderer separate from the particles: sets of curved crest lines across the travel
  direction, with troughs, from each system's height, period and direction; primary solid
  white, secondary dashed amber, wind sea dotted cyan, combined sea solid.
* Deep-water physics: L = gT²/(2π), crests at the phase speed, sets at the group speed.
  Stated scales in the legend: spacing 0.12 px per metre of wavelength (exaggeration E =
  0.12 × metres per pixel, 4,696× at zoom 2 at the equator), time 4× faster than real.
* E2E on synthetic fields (Chromium and Pixel 7): crest orientation for a swell from 225° at
  12 s 45.0° (coherence 0.98); motion along the travel direction 5.0 px in 0.61 s; spacing
  12 / 48 px at 8 / 16 s (ratio 4.00); ink 10,117 at 1 m against 35,411 at 4 m; two systems
  give orientation peaks at 92.5° and 2.5°; crests stop at the coast (land probes at alpha 0
  on both basemaps).
* Real data: on the NOAA 2026-10-15 00Z run the review raised the world view from 23 to 79
  sets on desktop (5 to 23 on a phone) and removed a 17 px crest seam at the antimeridian.
* Crest frame p95 settled 3.8 ms with 9,531 vertices at 1440×900; main-thread cost 2.69 ms
  script per frame.
* Partitions are served only where the model run has them (an absent partition is no data,
  byte 255); the chooser disables missing systems.

## 28. Chart redesign

[`forecast-charts.md`](forecast-charts.md): one 10-day timeline with a selected-time summary
and "Why this forecast?", day buttons, a header with hour ticks in the spot's zone, night
bands from first and last light, a "Now" marker, then rows for surf quality, breaking height
(central bar, likely range, sets tick, optional offshore Hs and buoy readings), swell
partitions, wind (speed bars, gust ticks, favourability colour and hatching, relation labels)
and tide (curve, high and low waters, datum, station, preference). Each step is drawn as wide
as it lasts. The quality ramp: every class at least 3:1 against the card and on the dark
surface, labels 4.5:1, luminance strictly ordered (vitest; the old "fair" was 1.92:1). Spec
27's 15 chart test items each map to a vitest and/or e2e test (forecast-charts.md §11).
Payload: `/timeline` 132,905 B against 262,304 B for `/forecast` on the real 3-hourly J-Bay
run.

## 29. Timeline synchronisation

* One selection store (`useSyncExternalStore` with selectors): rows are created once and
  never re-render on hover, selection, keys or day buttons (React Profiler counter: 0
  renders); only the cursor layer, tooltip, summary and day buttons re-render.
* Day boundaries and the selected step line up within 1 px in every row (e2e bounding boxes
  at 1280 px and on Pixel 7).
* Selecting steps sends 0 additional requests (e2e request log); the API sends
  `Cache-Control`, a weak ETag and 304s.
* Interaction to next frame (production build, demo J-Bay, hourly, 10 days): hover / click /
  ArrowRight median 25 / 29 / 32 ms at 1440 px, 20 / 25 / 24 ms in Pixel 7 emulation.
* The map readout agrees with the spot charts at the same place and time (live, Jeffreys Bay
  09Z: map SW 224° 13 s, chart SW 224° 13 s; secondary E 90° against E 92°).

## 30. Usability

* Visible-word budgets per block, enforced by vitest on the rendered DOM: spot summary,
  forecast card and cards at most 45 words, the breakdown's prediction block at most 50 (81
  before), research block with four gaps at most 25 plus its summary.
* `GET /api/spots/{slug}`: median 925 ms and 56 SQL statements → 32 ms and 7 statements (it no
  longer builds all 50 summaries).
* UX review (screenshots at 1440×900, 1024×768 and Pixel 7, live and demo data): no blockers;
  3 majors (night dimming broke the ramp, the wind relation label vanished in multi-day runs,
  the summary scrolled away on phones) and 15 minors, all fixed except UX-14 (section 36).
  Focus ring contrast 1.79:1 → about 5.4:1.
* Keyboard: the chart is a labelled slider (arrows one step, Page Up / Down one day, Home /
  End); a sticky compact readout on phones; reduced motion honoured on the map and the page.
* Credits and licence links next to model values everywhere they appear (spot page, map panel
  and best-spots list, swell cards, opportunity page, alert emails, tides, buoy readings).

## 31. Prediction integration

* The live pipeline on 2026-10-16 (00Z cycles, 50 spots): both Open-Meteo families (10,400 and
  4,200 rows), the blend (10,400 rows), 25,000 predictions at algorithm 2.1.0 and 4,050
  archived, then detection, matching, flights and alerts (69 events) in 3.9 s.
* The timeline and `/forecast` agree on run, valid times, score and label (integration test,
  and re-run on clones of the real runs).
* Good hours are hours of light under Good-or-better steps (Nazare 2026-10-25: 10.6 h good of
  11.49 h from first to last light; it read 12 h before), and event and match hours are real hours of light (104 counted
  against 210 real before, PHYS-4).
* Interpolation is named, never denied: steps that hold a member value the blend aligned in
  time carry `interpolated_members` (5,750 of 10,400 rows of the 2026-10-16 blend).
* Raw offshore data, our breaking prediction and our rating stay separate objects in the API
  and the UI; research, forecast and local confidence stay separate fields and badges.

## 32. Test commands and results

Latest full run, 2026-10-16, on this tree (docs-only commits after `18739b4`):

| Command (see README section J) | Result |
|---|---|
| `ruff format --check .`, `ruff check .`, `mypy app` | pass: 297 files formatted, 180 files type-checked |
| `alembic upgrade head` + `alembic check` on an empty database | pass, no new operations |
| `pytest tests/unit` | 1,689 passed (45.6 s) |
| `pytest tests/integration` | 318 passed (8 min 28 s) |
| `npx tsc --noEmit`, `npx eslint .`, `npm run build` | pass |
| `npx vitest run` | 409 passed in 36 files |
| `./scripts/run-e2e.sh` (Chromium + Pixel 7) | 127 passed: 111 chromium, 16 mobile (18.4 min) |
| `pytest tests/unit/test_api_setup_doc.py tests/unit/test_surf_scoring_doc.py` | 5 passed |

From the baseline (644 / 233 / 160 / 62) the suites grew to 1,689 unit, 318 integration, 409
vitest and 127 e2e tests. Mutation checks in the package reviews: point-data 21 of 21 caught,
wind-scoring 22 of 22 after one added test, tides-obs 20 of 20 after three added tests,
map-shell 10 of 10, spot-timeline every listed sign and constant change caught. Not run here:
the `@cross-browser` tests in Firefox and WebKit (browsers not installed), real phones.

## 33. Measured performance and storage

| What | Value | How |
|---|---|---|
| Download per day, recommended setup | about 1.25 GB (before: about 10.2 GB) | per run measured live 2026-10-16; per day computed; map layers per cycle extrapolated from 9 map hours |
| Spot forecasts per set of cycles | 0.5 MB (before: about 2.8 GB) | measured |
| Open-Meteo use | 142.9 + 124.3 weighted calls per run, about 820 a day (8 % of the free tier) | measured; computed |
| Map layers | 7.82-7.98 MB per map hour with partitions, 2.69-2.72 MB without | measured |
| Database after one full live pass | 148.8 MB (12.4 MB after `init-db`) | `pg_database_size` |
| Storage steady state | about 3.3 GB for 10 forecast days; about 17 GB at 730 days | computed from row sizes |
| Peak memory per job | NDBC 98 MB to predictions 627 MB; GEFS 418 MB; map fields 568 MB | measured live |
| Timeline build on the real run | 51-66 ms median, 8 SQL queries | review |
| Crest frame p95 / basemap / colour field | 3.8 ms / 6.2 ms / median 16.8 ms | e2e, headless Chromium |
| Docker Compose (demo, 2026-10-16) | `up --build -d` 3 min 11 s with base images pulled; every service healthy within a minute; first pipeline run done 57 s after beat started | run on this machine |

## 34. External API setup required

[`API_SETUP_REQUIRED.md`](API_SETUP_REQUIRED.md) section 1 lists what works with no account
(Open-Meteo free tier, NOAA NODD, GEFS-Wave, CO-OPS, EOT20, NDBC and the other buoys, Natural
Earth, OSM tiles for light use) and what the owner must set up: decide whether the app is
commercial (then an Open-Meteo plan and `OPEN_METEO_API_KEY`), a real email provider with a
verified domain, a flight provider (Duffel or Amadeus Enterprise), optionally Twilio, a CO-OPS
application name, a tile provider only for heavy Streets traffic, and the database alert size.
Each service has purpose, variables, docs, key, free tier, licence, cost, env vars, setup and
test; a unit test checks every variable named there exists. The Open-Meteo terms, pricing and
licence quotes were re-read on 2026-10-16.

## 35. README

README sections E (running: Docker, the bulk-worker profile, local processes with both
queues, beat, frontend), F (weather data: providers, frequency, caching, storage limits,
measured downloads before and after, real-data steps, tides, observations, source
limitations), G (forecast charts), H (map visualisations, the stated wavefront exaggeration,
navigation bounds), J (testing, with this run's counts) and K (debugging: map rendering, wind
direction, stale forecasts, missing wave data, chart synchronisation, API authentication,
memory). Commands run for it on 2026-10-16: `docker compose up --build -d` (sandbox build
proxy only), `docker compose ps`, `exec … init-db`, `simulate-swell`, `pipeline`,
`run --rm … check-config`, the `bulk-worker` profile, `down -v`; locally `init-db`,
`alembic upgrade head` / `check`, uvicorn, a worker without `-Q`, beat, `npm run dev`,
`tides seed/acquire/list/prune`, `obs seed-stations`, `ml list`, `research lint`,
`check-config`, `scripts/check-health.sh`, and the real-data block pasted into zsh. Shell
blocks carry no inline comments, so they paste into zsh. Not re-run: `pip install -e
".[dev]"` into a fresh virtualenv (the Docker image's `pip install .` and the existing
virtualenv were used), `npm ci` (dependencies already installed), `npx playwright install`,
and the long `obs ingest` / `ml train` commands (run in earlier phases).

## 36. Known limitations and next steps

**Owner decisions (left unchanged on purpose):**

* Review the 2.1.0 label changes on the real run (32 % of labels; SURF_SCORING.md 3e), and
  decide `EXCLUDE_STRONG_OFFSHORE` / `LIGHT_ONSHORE_IS_NOT_ONSHORE`, Macaronis' "any wind",
  and how wind sea raised by an offshore wind should count at points and reefs (44 of 4,050
  steps at 6 spots).
* `DATA_HEALTH_DATABASE_MAX_GB` defaults to 20 GB, above the computed two-year steady state
  (about 17 GB, a lower bound); lower it for a small disk. `MAP_FIELD_STALE_AFTER_HOURS`
  defaults to 21 h, above the normal 17-20 h age of a 00/12Z map cycle, so the chip means a
  late or missed cycle (both changed by the lead after the reviews: 5 GB fired after about
  100 days and 18 h showed the chip for up to 2 h of every 12 in normal operation).
* MAPNAV-13: at the one-world desktop zoom the best-spots list covers the Philippines and
  Japan, including Cloud 9.
* UX-14: the second card "Our prediction and the model data" repeats the selected step below
  the timeline; merging it is a layout decision (its test ids are asserted by e2e).
* The spot page opens on the step covering now (or the next daylight step), not on the best
  upcoming daylight step (a one-line change).
* Commercial use of Open-Meteo (API_SETUP_REQUIRED.md 1.3).

**Not verified here:** Firefox, WebKit and real phones; Open-Meteo's commercial hosts, Windy,
Duffel, Amadeus, Resend, SendGrid and Twilio (no credentials; mock-tested); breaking heights,
quality labels and confidence classes against surf observations (none exist); the map layers'
61-hour cycle size (extrapolated from 9 measured hours); storage growth over real weeks
(computed from fresh-table row sizes, a lower bound). On the afternoon of 2026-10-16
Open-Meteo refused this sandbox's shared address for the day, so the second run of the README's
real-data block stored no spot forecast (tides, GEFS and map fields succeeded).

**Known minor issues:** "No tide prediction for this time." is stored where a tide stage
exists but its range is under 0.15 m (104 of 4,050 rescore steps); the "Why" sentence is
converted from metres already rounded, so it can differ from the summary by 0.1 ft; SMS
alerts carry no data-credit line (derived values only); `SpotForecastOut.generated_at` stays
empty on `/forecast`; the 2.0.0 runs show neutral wind bars (no favourability stored).

**Next steps, by expected impact:**

1. Nearshore observations (surf reports, cams with manual reads) to calibrate breaking
   heights, the quality score and the confidence classes; the largest error source.
2. Owner review of the 2.1.0 labels on a few spots they know, then the open wind decisions.
3. Activate the approved ML correction (v6) where it passed, after the operator step.
4. Cross-browser runs (Firefox, WebKit, iOS Safari) of the map and the timeline.
5. Refactors noted in review: merge `land-50m` into `countries-50m` (MAPNAV-12, 174 kB gzip),
   make the e2e `screenPoint` helper zoom-independent (MAPNAV-14), fold the synthetic map
   field helper into the GFS one, let `tide_stages_for` report why a stage is absent.

## 37. Definition of Done (spec 30)

Each item: **verified** (with how), **partial** or **not done**.

| # | Item | Status | How |
|---|---|---|---|
| 1 | Map bounds work | verified | e2e `map-navigation.spec.ts` at 1440×900, 1920×1080, 1280×720, 390×844, 844×390: worst 1 row outside the band, 0 past the Mercator edge; vitest `map-bounds.test.ts` |
| 2 | Map responsive | verified | minimum zoom recomputed on resize and rotation (e2e); basemap redraw 6.2 ms, colour field median 16.8 ms, 0 marker calls over 5 steps; phone bottom sheet and landscape side sheet |
| 3 | All spots accessible | verified, with a caveat | all 50 in one world copy at 1440×807 (vitest), every spot 25° inside the band; at the first desktop view the best-spots list covers Cloud 9 (MAPNAV-13, owner decision) |
| 4 | Supported remote sources | verified | live 2026-10-14 and 2026-10-16: Open-Meteo GFS and ECMWF families, GEFS-Wave, NOAA NODD map layers, CO-OPS, EOT20, NDBC; parity with direct sampling within 0.05 m Hs |
| 5 | Massive local downloads removed | verified | about 10.2 GB → about 1.25 GB a day (per run measured, per day computed); spot forecasts 2.8 GB → 0.5 MB per set of cycles; no file written (strace) |
| 6 | Cache controlled | verified | Redis 256 MB `noeviction` with a health check, response cache 200 × 2 MB, 2.43 MB used after a full pass; browser field LRU 48 / 64 MB; database growth bounded by retention, alarm size an owner decision |
| 7 | Wind animation from legitimate gridded data | verified | GFS 10 m u/v at 0.5° from NOAA NODD, decoded and checked against an independent ecCodes decode and NDBC directions; no field drawn without data |
| 8 | Wind vectors physically correct | verified | u = -V sin θ, v = -V cos θ and screen `(u, -v)` unit-tested including the sign of v; physics review on real data |
| 9 | Offshore identified | verified | V·n with n seaward from `offshore_wind_direction` + 180 (unit tests, physics review); blend direction from the mean wind vector (555 → 111 invented directions) |
| 10 | Favourable offshore improves scoring | verified | Q(0) = 1, Q(10 km/h offshore) ≥ 0.95 (unit tests); rescore of the real run |
| 11 | Strong offshore not universally ideal | verified | Q(50) = 0.32, Q(100) < Q(60); real run: offshore ≥ 45 km/h rated Very Good or better 0 of 3, offshore ≥ 30 km/h 37 → 19 |
| 12 | Waves visually distinct from wind | verified | separate crest renderer (e2e structure-tensor orientation, ink and land probes); screenshots on real fields; Chromium only |
| 13 | Wavefronts reflect direction and period | verified | e2e: 45.0° crests for a 225° swell, spacing ratio 4.00 for 8 / 16 s, motion along the travel direction |
| 14 | Primary + secondary swells where available | verified | map partitions from GFS-Wave, chooser disables missing systems; spot lanes per stored partition; live J-Bay map and chart agree (SW 224° 13 s; E 90° / 92°) |
| 15 | Readable charts | verified, one item deferred | UX review at three sizes and live data, 3 majors and 14 of 15 minors fixed; UX-14 (duplicate step card) left to the owner |
| 16 | Multi-day timeline | verified | 10-day timeline with day buttons (vitest, e2e `timeline.spec.ts`) |
| 17 | Selected-hour cursor | verified | cursor line through every row, selected pill; chart test 4 (vitest and e2e) |
| 18 | Quality timeline | verified | quality strip from the scoring engine's class (chart test 9, `test_timeline_api.py`); palette contrast test |
| 19 | Wave chart | verified | bars equal the backend's central values (chart test 5, e2e against `/timeline`) |
| 20 | Accurate partitions | verified | stored partitions only, never 0 for absent ones; live map-chart agreement at J-Bay |
| 21 | Wind bars | verified | speed and gust equal the backend's (chart test 6), favourability colour and hatching |
| 22 | Correct arrows | verified | arrows TO = FROM + 180 on charts and map (`directions.test.ts`, e2e rotation), text FROM, convention in both legends |
| 23 | Tide chart | verified | CO-OPS and EOT20 curves with datum, station and credit on the live run (screenshots); chart test 8 with synthetic tides in e2e |
| 24 | Synchronised timestamps | verified | one time axis, day boundaries within 1 px (e2e), spot zone names with DST changes named, one zone-naming rule site-wide |
| 25 | Mobile | verified, emulation only | 16 Pixel 7 e2e tests (touch swipe, tap select, bottom sheet), compact readout, screenshots; no real phone or iOS Safari |
| 26 | Confidences separate | verified | forecast-only reasons, separate badges and API fields (vitest, e2e `confidence.spec.ts`, integration tests) |
| 27 | Concise text | verified | word-budget vitest on the rendered DOM (45 / 50 / 25-word blocks) |
| 28 | Prediction engine functional | verified | live 2026-10-16: 25,000 predictions at 2.1.0, blend, detection to alerts; rescore of the real 2026-10-11 run |
| 29 | Flights, saved searches, email notifications still work | verified with demo and capture providers | regression review on a live stack (register, verify, reset, searches, demo flights, alert captured), `full-flow` and `scenario` e2e, integration suite; real providers not verified (no credentials) |
| 30 | Tests pass | verified | section 32 (1,689 unit, 318 integration, 409 vitest, 127 e2e) |
| 31 | README updated | verified | sections E-K rewritten, commands run as listed in section 35 |
| 32 | External API requirements documented | verified | `API_SETUP_REQUIRED.md` section 1 and per-service tables; `test_api_setup_doc.py` |
| 33 | Limitations disclosed | verified | section 36, README section M and the Limitations section of every package doc |

Not done anywhere in the list: none. Partial: item 3 (MAPNAV-13), item 15 (UX-14), item 25
(no real phone), item 29 (real flight, email and SMS providers), and every browser item
outside Chromium.

# Upgrade 4 (2026-10): a website on Vercel, data synced to model runs, the wave look

Branch `wf4i2/main`, from `b84dfb4` (the end of Upgrade 3), 2026-10-18 to 2026-10-23. Work
order: research on Vercel, Neon, Upstash and GitHub Actions, a design with decisions D1-D16
(recorded as built in [`decisions.md`](decisions.md), ADR-021 to ADR-037), a foundation of
shared contracts (settings, the `sync_runs` table, the sync and job interfaces), nine
packages built in parallel and reviewed adversarially (sync-core, api-serverless,
jobs-health, egress-map-serving, vercel-deploy, deps-packaging, local-vercel-stack,
status-ui, wave-look), integration round 1 (database egress and the public cache, backend
glue, the wave polish, the deployment configuration), round 2 (a second egress pass, an
end-to-end verifier, these documents).

As in Upgrade 3: everything below was measured, run or read as stated; "computed" means
arithmetic on measured values, "estimated" says what was assumed. Nothing is marked working
unless it was run. **No real Vercel deployment was made** (no Vercel account was used):
section 42 lists what only the first production deploy can show.

## 38. The owner's request

Verbatim (2026-10-18): "I want to put this on a website. Also all of the weather data should
be pulled when the forecast updates. The weather data should only update those specific
spots. Also the wave should look like this. Can you make these changes"

The attached screenshot showed a smooth wave-height field off California (deep blue for small
waves, through violet, to magenta and pink for big ones), grey land with the coastline drawn,
and many short, soft white dashes drifting in the direction the waves travel.

The owner's answers to the questions that followed:

* Hosting: "I am going to host it on vercel. Make it work with this. Also make it so i can
  test it locally before i put it on vercel."
* Map data: "Whole world, synced" (keep the worldwide map layers, fetched together with the
  spot forecasts when a new model run comes out; point forecasts only for the surf spots).
* Commercial use: "It will be free for now but eventually i will have a paywall to use it."

The lead's defaults, which the owner can change: the architecture of the design; on Neon
Free about 1 day of forecast history and 1 week of verification archive; the combined sea
as the default wave layer; new ECMWF runs sync on their own (later narrowed by the
free-database cadence, ADR-023); GEFS off on Vercel; the repository stays public; no custom
domain yet; the paywall as configuration only.

## 39. What was built, per request item

| Request | Built | Evidence (section 40, 41) | Documented in |
|---|---|---|---|
| Put it on a website, on Vercel | One Vercel project from the repository root: services `web` (Next.js) and `api` (FastAPI as one lean Python function), the `sync` daemon (`python -m app.cli sync daemon`, repo-root `Containerfile`), a daily cron watchdog, Neon Postgres and Upstash Redis from the Marketplace; the API needs no Celery (`JOB_RUNNER=daemon`), reads the client IP from `x-vercel-forwarded-for`, uses Neon's pooled URL without prepared statements; production builds migrate the database | the offline Vercel build `RESULT: ok` (API function 127.3 MiB of 500 MiB); a real deployment was not made | [`deploy-vercel.md`](deploy-vercel.md), [`ARCHITECTURE.md`](ARCHITECTURE.md) 1.1 |
| Test it locally first | `docker-compose.vercel.yml` with `scripts/vercel-local.sh` (an edge that routes like Vercel, the lean API, the Next.js build, PgBouncer like Neon's pooler, Redis, the same daemon image, Mailpit, the cron); `scripts/vercel-build-check.sh --offline`; `vercel dev -L` | smoke 19 of 19 shell checks and 6 of 6 Playwright tests with demo data and again with live data | [`local-vercel-stack.md`](local-vercel-stack.md), README E.6 |
| Pull all weather data when the forecast updates | the synced pipeline: provider metadata checked every 10 minutes; a new model run starts one sync of 12 stages under a database lease; the spot forecasts and the map layers of a run are acquired, scored and published together; a run already stored is never fetched again; Celery's beat runs the same sync (`sync.tick`) in the classic stack | after a sync a check made 0 SQL statements and only metadata requests (3,588 B); a forced re-sync wrote no data row; the daemon synced a real GFS run and its map cycle by itself (2026-10-22) | [`sync.md`](sync.md), README F.2 |
| Only those specific spots | point forecasts for the active seeded surf spots only, one Open-Meteo request per model for the 50 spots; the direct GRIB spot ingesters stay off | live: 10,264 GFS records for the 50 spots in one sync | [`sync.md`](sync.md), [`FORECAST_SOURCES.md`](FORECAST_SOURCES.md) |
| Whole world, synced | the NOAA GFS world map layers are a stage of the same sync; the map index serves the published sync's map run (`SYNC_PUBLISH_GATE`), so the map and the spots switch together; the GFS spot run and the map of a cycle wait up to 90 min for each other | live: 135 map fields for 9 map hours stored and published by the stack's sync; the map index names its publication (smoke) | [`map-layers.md`](map-layers.md) |
| The wave should look like this | a wave-height ramp anchored in metres (muted navy through slate blue, violet and magenta to pink), mid-grey land with a dark coastline, and short soft crest dashes, each a lighter tint of the field, on the crest lattice and moving at the stated phase speed; the combined sea by default, the partitions in Settings | screenshots (section 41) and the e2e physics checks (spacing ratio 4.00, orientation, motion); draw p95 3.1-3.4 ms | [`map-rendering.md`](map-rendering.md), README H |
| Free now, paywall later | the hosted and free-database profiles (`backend/app/core/profiles.py`); `COMMERCIAL_USE` refuses the free Open-Meteo API without a key; the full-retention profile for after the database upgrade; the checklist in section 44 | the guard is tested with mocked HTTP; the profiles are tested against `.env.vercel.example` and the deploy guide | [`API_SETUP_REQUIRED.md`](API_SETUP_REQUIRED.md) 1.4 and 5 |

Also built because the free plans demanded it: the database reads were cut so the
background stays inside Neon Free's 5 GB of egress (one both-family sync pass from 169 MB to
21.28 MB, section 41), a public snapshot cache in Redis computed once per publication and UTC
hour, CDN headers on every user-independent response, a `/status` sync section with the
daemon's heartbeat and a CPU meter against the daemon's 10 CPU-hour quota, and an
`OPS_ALERT_EMAILS` fallback for when the database itself is down. Migration 0008 records one
publication of the data an existing database already holds.

## 40. Verification evidence

Run by the integration verifier on the final code of round 2 (2026-10-23, this sandbox: 4
shared vCPUs, local PostgreSQL 16 and Redis, Docker 29.8.2), then the documentation tests of
this pass:

| Check | Command | Result |
|---|---|---|
| Formatting, lint, types | `ruff format --check .`, `ruff check .`, `mypy app` | 366 files formatted, all checks passed, 199 files type-checked |
| Migrations | `alembic upgrade head` + `alembic check` on a fresh database; round trip and the 0006 and 0008 migration tests | 0001-0008 applied, no new operations; 5 passed |
| Backend unit | `pytest tests/unit` | 2,040 passed, 4 skipped (the lean-install tests: 27 of 27 with `LEAN_PYTHON` set to a uv 0.11.32 lean venv) |
| Backend integration | `pytest tests/integration` | 503 passed, 3 skipped (PgBouncer mode: 3 of 3 against the local stack's PgBouncer) |
| API bundle | `scripts/check_api_bundle.py --max-mb 200` | 163.2 of 200 MB |
| Frontend | `tsc --noEmit`, `eslint . --max-warnings=0`, `vitest run` | pass; 486 tests in 40 files |
| Frontend builds | `npm run build` (proxy) and `API_ROUTING=platform` | pass; the platform build has no `/api` rewrite |
| End to end | `scripts/run-e2e.sh` (Chromium + Pixel 7) | 127 passed twice (13.2 and 12.5 min), no flaky test; wave draw p95 3.1 and 3.4 ms |
| Offline Vercel build | `scripts/vercel-build-check.sh --offline` | `RESULT: ok` in 83 s; api python3.13, 3,428 files, 127.3 MiB; web nodejs24.x functions 4.6 / 3.8 / 1.7 MiB; the cron and both routes present |
| Local Vercel-style stack | `cp .env.vercel.example .env.vercel.local`, `scripts/vercel-local.sh up`, `sync --force`, `smoke` | up in 216 s cold, nine services healthy; smoke 19/19 and 6/6 with demo data and with live data, including `/status` "published" and the Mailpit account round trip |
| Stack details | forged client-IP headers, PgBouncer path, cron tokens, a second check | forged headers overwritten (key = the gateway 172.22.0.1); `/api/spots` through PgBouncer; cron 200 / 401 / 401 / 404 without `CRON_SECRET`; a second check moved about 30 kB and changed 0 rows |
| Classic Celery stack | `docker compose up -d --build --wait` (with this sandbox's port and build overrides) | up in 154 s; beat start ran `sync.tick`: sync 1 success in 31.1 s on demo data; `/api/spots` served 50 spots |
| Data only on update | `sync check`, `sync run`, `sync check`, `sync run --force` ×3, `sync daemon --once` on a fresh database, hosted + free-database profile, real providers | second check 0 SQL statements, 8 requests / 3,588 B; a forced re-sync over stored data changed no data row and left the swell events identical; the daemon iteration made 3 start-up SELECTs and metadata requests only |
| Egress at the final code | `scripts/measure_db_egress.py` on replayed copies of a recorded live run | within 0.04 MB of round 2's figures (section 41) |
| Upgrade path | a copy of a pre-Upgrade-4 database migrated to head | before 0008: no publication; after: one publication row, the index and `/status` name it, the known runs load |
| Dependency audits | `npm audit --omit=dev`; pip-audit 2.9.0 on every extra of `uv.lock` | 0 vulnerabilities; no known vulnerabilities (after Next.js 16.4.1 and certifi 2026.10.19). Re-run at the final review (2026-10-24): GitHub had reviewed a high advisory in zod 4.6.5 (GHSA-mrhx-c7xc-m3w5) the evening before, fixed by zod 4.6.6; then no high or critical one, and one moderate (fast-uri below 3.1.9, under @hookform/resolvers and ajv; not fixed) |
| Documentation tests (this pass) | `pytest tests/unit/test_api_setup_doc.py tests/unit/test_upgrade_settings.py tests/unit/test_local_vercel_stack.py tests/unit/test_vercel_deploy.py tests/unit/test_compose_classic.py tests/unit/test_surf_scoring_doc.py tests/unit/test_free_database_cadence.py` | 77 passed (the settings table, `.env.example`, the hosted-profile block of the deploy guide, both env templates and compose files) |

Mutation checks across the packages and rounds: every listed mutation of the new tests was
caught (for example 59 detection, CLI, orchestrator and daemon mutations in the sync-core
review, 10 of 10 in the wave renderer, the eight of the egress tests and four of migration
0008).

## 41. Measurements

| What | Value | How |
|---|---|---|
| One both-family sync pass, bytes PostgreSQL sends (Neon egress) | 169 MB (package baseline) → 57 MB → 31.41 MB (round 1) → **21.24 MB** (round 2), 21.28 MB re-measured | `scripts/measure_db_egress.py` on replayed copies of a recorded live run, clock frozen, 3 SYNTHETIC saved searches |
| Demo pass with acquisition | 186.8 MB (design research) → 11.18 MB (verifier and this pass) | the same script, fresh `init-db` |
| Hourly batch of periodic jobs | 2.22 → 0.87 MB | the same, `jobs` |
| Page views, anonymous, no CDN, cold / warm | home 397.4 / 0.0 kB; map 2,513.8 / 722.4 kB (the 9 fields; the CDN keeps them a year per run); spot page 1,181.1 / 0.0 kB | the same, `pages` |
| Monthly background egress, free-database profile | **2.01 GB (40 % of Neon Free's 5 GB)**: 60 syncs × 21.28 MB + 720 hourly batches × 0.87 MB + daily jobs and start-ups; old cadence 4.32 GB; before the egress work 7.12 GB | computed from the measured unit costs |
| With visitors | quiet (100 views a day) 2.41 GB in all, fits; steady (1,000 views a day) 5.37 GB, does not fit | computed for stated example traffic |
| Neon compute | about 20 CU-hours a month in the background, at most about 37 with steady anonymous visitors (100 included); signed-in views are extra (each reads the database: `/api/auth/me` 5 statements, a dashboard about 27, measured at the final review) | arithmetic: 0.25 CU, active until 5 min after the last statement |
| Upstash commands | about 79,000 a month (daemon: 4 commands per job run, the lock and the CPU counter, 7 for data health); about 386,000 with steady visitors (500K included), plus at most about 17,000 for the per-visitor miss limit of the final review (2 commands per computed key, 12 keys an hour): about 403,000 (81 %) | measured commands per check, poll, job and view, times the cadence (corrected at the final review: the first count left out each job's CPU counter) |
| Daemon CPU, full cadence | 4.4-7.9 CPU-hours a month (10 included on Hobby) | extrapolated from measured CPU per stage (docs/sync.md) |
| A live sync in the local stack (9 map hours) | map layers: 73.3 MB in 63 requests, 135 fields, 10.6 MB stored, 38.6 s / 30.9 CPU s; GFS spots: 10,264 records, 58.9 s / 10.5 CPU s | stack logs, 2026-10-23 |
| Second check after a sync (stack) | 30,145 B received / 22,667 B sent, 0 rows changed | `/proc/net/dev` and `pg_stat_database` before and after |
| Images | sync 1.22 GB (ecCodes 2.49.0), api 409 MB, web 344 MB, pgbouncer 15.1 MB; classic backend 1.28 GB | `docker image ls` |
| Wave renderer | draw p95 3.1 / 3.4 ms (e2e); ink per dash 2.25× at 4 m vs 1 m; 2.73-3.73 dashes per 1,000 CSS px² on Pixel 7, 1.95-2.58 on desktop (budget-limited, software rendering), reference 4.05 | e2e logs; density script on screenshots |
| Inline email under the daemon | register 0.58 s with Redis up, 2.12 s with Redis blackholed | API timing with the hosted Redis timeouts |

Screenshots of the final code through the local stack's edge (real NOAA GFS-Wave 06Z +12 h
field, combined sea; desktop 1440×900 and Pixel 7; the California view at zoom 6 drew 1,494
crests with a draw p95 of 4.0-4.7 ms on the desktop) are kept with the integration's working
files, outside the repository.

## 42. What could not be verified here

The first production deploy is the test of these:

* the daemon's image build and start on Vercel (daemons do not run in previews or
  `vercel dev`), its 600 s shutdown grace, and the daemon root `./` next to the web service's
  assets;
* Vercel's CDN caching of the public responses, Services routing and cookies on a real
  domain, the client IP from `x-vercel-forwarded-for`, the build-time migration on Vercel's
  builder against Neon;
* Neon's compute suspend and wake and its CU-hour and egress accounting, Upstash's real
  command counts and TLS, the variable names the Marketplace sets today (`KV_URL` or only the
  REST variables);
* the daily cron invocation and its timing, preview deployments with Neon branches;
* real email through Resend (no key, no verified domain), the Open-Meteo commercial hosts
  (no key);
* the GitHub Actions jobs (`deploy-config`, `vercel-build`, `data-sync.yml`) on GitHub;
* the daemon's monthly CPU on Vercel's vCPU (the estimate assumes this sandbox's speed);
* the database cost of signed-in visitors on Neon itself (measured at the final review in one
  process: every signed-in view runs `/api/auth/me`, 5 statements; a dashboard about 27) and
  of uncached visitor URLs (now one key per map hour, and at most 120 misses an hour per
  client IP);
* a live two-family ECMWF acquisition in this round (Open-Meteo served its 06Z ECMWF run,
  which this source does not read); Firefox, WebKit, GPU-backed rendering and real phones.

## 43. Risks

* **Vercel Daemons and Services are Beta.** The fallbacks are documented
  (docs/deploy-vercel.md section 9): two projects, or the manual GitHub sync.
* **The daemon quota** (10 Active CPU-hours on Hobby as Upgrade 4 read it; Vercel now gives
  Hobby daemons 5, docs/sync.md "Monthly CPU estimate"): when used up, the daemons stop until
  the 1st of the next month; the site serves the last data and the watchdog emails the
  admins. The map layers are more than half of the estimate. (With Swell 2.0 the daemon is
  optional and refreshes only the map layers.)
* **Neon Free can still bind**: steady traffic (1,000 views a day) needs about 5.37 GB of
  egress; anything that reaches the database every 5 minutes keeps the compute awake (180
  CU-hours a month). Every signed-in page view reads the database (measured at the final
  review: `/api/auth/me` 5 statements, a dashboard about 27); open pages no longer poll it
  (the dashboard and `/status` re-read on focus). Requests made on purpose to miss the
  cache could use up the 5 GB of egress (about 14,000 conditions computations), after which
  Neon suspends the compute until the next billing period; since the final review one
  client IP gets at most 120 misses an hour (`PUBLIC_CACHE_MISS_LIMIT_PER_HOUR`), so it
  takes many clients.
* **Open-Meteo's free limit is per IP**, and the daemon's address may be shared: refusals
  delay a source to the next UTC day; an API key removes the risk.
* **Freshness on the free database** (Upgrade 4 cadence, `SYNC_TRIGGER_SOURCES=map`): spot
  forecasts changed twice a day; a GFS run could read stale for up to about 1.3 h before
  each sync; if NOAA's map layers failed, no sync ran (estimated, not observed live). Swell
  2.0 replaced it: `SYNC_TRIGGER_SOURCES=spots`, and the refresh endpoint follows every GFS
  and GFS-Wave run.
* **Tides after a fresh deploy**: fixed at the final review. The daemon runs every overdue
  periodic job once at start (`SYNC_DAEMON_CATCH_UP`), so the tides come within minutes, and
  data health flags tides never acquired 3 h after the catalog was loaded
  (`tides_missing:<provider>`).
* **Logs**: Hobby keeps runtime logs 1 hour; diagnosis relies on `sync_runs`, the job logs,
  `/status` and the watchdog email.
* **Young releases**: Next.js 16.4.1 is 9 days old (the oldest release with the security
  fixes); certifi is pinned past `exclude-newer` for an advisory. Dev-only advisories remain
  in vitest 3.2.7 (the fix is vitest 5).
* **Upgrading an existing database**: keep `FORECAST_PROVIDERS` as it was (the spot list and
  the map serve only runs of the configured sources) and run the migrations (0008).
* **Machine learning on the daemon**: the image ships `backend/ml_models`; a model trained at
  run time would not survive a restart (design risk, not checked on Vercel).

## 44. Owner checklist

Updated for Swell 2.0 (the spot forecasts come from the refresh endpoint, the daemon is
optional and refreshes only the map layers). The step-by-step reference is
[deploy-vercel.md](deploy-vercel.md) section 4; the costs are in
[API_SETUP_REQUIRED.md](API_SETUP_REQUIRED.md) section 1.4.

### Free now (no payments and no ads; donations are allowed on Hobby)

1. **Test on your machine** (Docker; docs/local-vercel-stack.md):
   `cp .env.vercel.example .env.vercel.local`, `scripts/vercel-local.sh up`,
   `scripts/vercel-local.sh sync --force`, `scripts/vercel-local.sh smoke`; open
   http://localhost:3100 and the Mailpit inbox at http://localhost:18025;
   `scripts/vercel-local.sh down` when done. Optionally `scripts/vercel-build-check.sh
   --offline` (Node.js needed, no Vercel account).
2. **Vercel account** on the Hobby plan ($0), signed in with GitHub. The repository stays on
   GitHub and public (free CI). *(Vercel's website.)*
3. **Import the repository**: Add New Project, this repository, **Root Directory left at the
   repository root**, build settings left at their defaults. The first deployment has no
   database yet; that is expected. *(Vercel's website.)*
4. **Neon**: Vercel Marketplace (Storage), Neon Postgres, **Free**, region **AWS us-east-1**,
   connected to Production, Preview and Development. Check that `DATABASE_URL` and
   `DATABASE_URL_UNPOOLED` appear in Environment Variables. *(Vercel's website.)*
5. **Upstash**: Vercel Marketplace, Upstash Redis, **Free**, us-east-1, connected to
   Production and Development only (a preview on the production Redis shares its rate-limit
   counters and the daemon's kick flags; docs/deploy-vercel.md section 6). Check for
   `KV_URL` (or `REDIS_URL`); if neither appears, copy the database's `rediss://` URL into a
   `REDIS_URL` variable. *(Vercel's website.)*
6. **Email: Resend and a domain.** A Resend account (free: 3,000 emails a month, 100 a day)
   and an API key. Before you have a domain: `EMAIL_FROM=Swell Travel Agent
   <onboarding@resend.dev>`. Resend then delivers only to the address of your Resend account
   ("The resend.dev domain is only available for testing purposes and can only send emails
   to the email address associated with your Resend account"): register on the site with
   that address, verify it, run `make-admin` (step 12) and set `OPS_ALERT_EMAILS` to it.
   Later, a domain you own (a registrar's price; not researched here), verified in Resend
   with its SPF, DKIM and DMARC records; then switch `EMAIL_FROM` to it. Sign-up stays open:
   visitors can create accounts, but verification, password-reset and alert emails reach
   nobody (with the onboarding@resend.dev sender, only you) until the domain is verified.
   Do not publicise the site yet.
7. **Secrets**, generated on your machine, two different values:
   `python3.13 -c "import secrets; print(secrets.token_urlsafe(48))"` (or
   `openssl rand -base64 48`) for `SECRET_KEY` and for `CRON_SECRET`.
8. **Environment variables** (Project Settings, Environment Variables; docs/deploy-vercel.md
   step 5): paste the hosted-profile block for Production and Preview; then `APP_ENV`
   (`production`; Preview `development`), `SECRET_KEY`, `CRON_SECRET` (Production),
   `COOKIE_SECURE=true`, `APP_BASE_URL`, `API_BASE_URL` and `CORS_ORIGINS`
   (`https://<project>.vercel.app` or your domain), `EMAIL_PROVIDER=resend`, `EMAIL_FROM`
   (`Swell Travel Agent <onboarding@resend.dev>` until your domain is verified, then an
   address on it), `RESEND_API_KEY`, and `OPS_ALERT_EMAILS` with your own address (your
   Resend account's while you use the testing sender; the only alert that still goes out
   when the database is down). Keep `FLIGHT_PROVIDER=demo`
   until you have a flight provider. *(Vercel's website.)*
9. **The database**: a production build migrates and seeds it (the api's build command,
   `migrate --on-build` with `MIGRATE_ON_BUILD=production`: the 50 spots, airports, the tide
   catalog). Only when that build prints `skipped` (and you do not keep the daemon, whose
   `init-db` seeds at start), seed it yourself: from `backend/` with the dev venv of README
   E.3 (it works for `init-db` and `make-admin`; for a lean install use its own venv,
   `UV_PROJECT_ENVIRONMENT=.venv-lean uv sync --locked --no-dev`), point `DATABASE_URL` and
   then `DATABASE_URL_UNPOOLED` at Neon's direct URL (two `export` lines,
   docs/deploy-vercel.md step 6) and run `.venv/bin/python -m app.cli init-db` (idempotent;
   checked here against a local PostgreSQL only). A database without spots publishes
   nothing and reports `no_spots_seeded`.
10. **Deploy**: redeploy production (variables reach new deployments only). The api build
    migrates and seeds the database; if you keep the optional map daemon, Vercel builds its
    image and starts it. *(Vercel.)*
11. **Check and schedule the refresh** (*on the deployment*):
    `https://<project>.vercel.app/api/health/ready` answers
    `{"status":"ok","checks":{"database":"ok","redis":"ok"}}`. Run the first refresh by hand,
    `curl -X POST -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/internal/refresh`:
    it answers `"status": "success"` with a version id, and `/api/forecasts/version` names
    it with `spots_updated` 50. Then do docs/deploy-vercel.md **step 7b**: schedule the
    refresh every 5 minutes (Upstash QStash on Hobby, a Vercel Cron entry on Pro) and check
    that `/admin/freshness` shows the refresh scheduler `checking` a few minutes later.
    Without the schedule the spot forecasts refresh only once a day. The watchdog answers
    200 to `curl -H "Authorization: Bearer $CRON_SECRET" https://<project>.vercel.app/api/internal/cron/daily`.
    With the optional daemon, its logs show `init-db` and the first map check, and the map
    shows the wave layer after its first map sync.
12. **Make yourself admin** after registering and verifying your email address (your Resend
    account's address while you use the testing sender, step 6):
    `.venv/bin/python -m app.cli make-admin you@example.com` with the dev venv and the same
    environment as step 9 (admins get the data-health emails and the accuracy dashboard).
13. **Keep the free tiers healthy**: point an uptime monitor at `/api/health` (the API
    without the database) or at `/`, at any rate; never at `/api/health/ready` or
    `/api/health/data?strict=true`, which wake Neon on every hit (every 15 minutes costs
    about 45-60 CU-hours a month, on top of about 20 in the background; hourly at most about
    15): on Neon Free the daily watchdog covers the database. Close `/status` when you are
    done (an open tab re-reads on focus, and every minute while a sync runs). Only if you
    keep the optional map daemon, watch the `/status` CPU meter (Hobby's 5 Active
    CPU-hours a month; if it runs high, `MAP_FIELD_PARTITIONS=off` or a shorter map
    horizon). Keep `GEFS_ENABLED=false`. Look at Neon's usage page now and then: the
    background budget of API_SETUP_REQUIRED.md 1.4 is about 1.5-2.3 GB of egress a month for
    8 component refreshes a day (up to about 3.4 GB with a third refresh per cycle) and about
    20-23 CU-hours with the hourly data-health check; Upstash about 114,000 commands a month
    for a quiet site, with the version polling and the 5-minute refresh checks. Do not add a
    schedule to `data-sync.yml` (GitHub's terms); add its repository secrets only if you
    want the manual emergency sync (docs/deploy-vercel.md section 8).

### Paywall later (before the site shows any price or checkout)

1. **Vercel Pro** ($20 a month platform fee, $20 monthly usage credit; daemons billed for
   Active CPU and memory, rate not read): Hobby is non-commercial, and "Any method of
   requesting or processing payment from visitors of the site" is commercial use.
2. **Open-Meteo plan** (Standard: 1M calls a month; confirm the price at checkout): set
   `OPEN_METEO_API_KEY`, raise `OPEN_METEO_CALLS_PER_DAY` and `OPEN_METEO_CALLS_PER_MONTH`
   to the plan's budget, then `COMMERCIAL_USE=true`. From then on the app refuses the free
   API (`/status` would report `licence` if the key were missing).
3. **Neon Launch** ($0.106 per CU-hour, $0.35 per GB-month, 500 GB egress), then the
   full-retention profile (`app/core/profiles.py`, docs/API_SETUP_REQUIRED.md section 5):
   `RETENTION_FORECAST_DAYS=10`, `RETENTION_ARCHIVE_DAYS=730`, `RETENTION_JOB_LOG_DAYS=30`,
   `SYNC_PRUNE_EACH_PASS=false`, `DATA_HEALTH_DATABASE_MAX_GB=20`,
   `SYNC_TRIGGER_SOURCES=spots,map`, `MAP_FIELD_CYCLES=00,12` and
   `PREDICTION_ARCHIVE_ENABLED=true`. The spot refreshes stay as on the free plan (every
   new GFS and GFS-Wave run through the refresh endpoint); the map layers then follow the
   00Z and 12Z cycles. (Upgrade 4's estimate of about 4.32 GB of background egress a month
   for this profile measured its old two-family sync and was not re-measured for 2.0.)
4. **Upstash** pay-as-you-go ($0.2 per 100K commands, with a budget cap) or a Fixed plan
   (from $10 a month).
5. **Redeploy** production after changing variables.
6. Optional: a faster cadence (`JOB_INTERVAL_FLOOR_MINUTES=0`, `SYNC_CHECK_MINUTES=5`) and a
   larger daemon (a `4Gi` memory request in `vercel.json`, a code change); other paid
   services only if used: Resend Pro above 3,000 emails a month, Duffel for real fares, a
   tile provider licensed for commercial use for the Streets basemap.
7. **What a paywall needs that is not built**: a payment provider (none researched) and its
   checkout and billing webhooks; plans and prices; a record of who has paid (subscription or
   one-off, in the database); access control on the API and the pages for paying accounts,
   and the decision which features stay free; the legal pages a paid service needs (terms,
   privacy, refunds; not researched). The hosting side above is configuration only.

## 45. Defects fixed at integration, and what is left

Fixed in the two integration rounds (commits on `wf4i2/main`): detection no longer cancels
and re-creates an event when run again over unchanged data; the API function's data health
no longer reports the tide constants it cannot see; the daemon's lease is 15 minutes with a
keeper; Open-Meteo's daily limit waits until 00:00 UTC; the publication hook of the public
cache is wired; `/api/system/status` and the map index are served from the public cache;
with `map` triggers and no map component, spot runs trigger; migration 0008 publishes the
data of an upgraded database; Next.js 16.4.1 and certifi 2026.10.19 for their advisories;
the regenerated OpenAPI types; the round-2 settings in the classic compose file and the
templates. This pass: `.env.example` no longer sets `SYNC_LEASE_MINUTES=45` explicitly
(that overrode the daemon's 15-minute default for anyone who copied it).

Left open:

* **Tides on a fresh hosted deployment** (section 43): done at the final review: the
  daemon's start-up catch-up and the `tides_missing:<provider>` problem.
* **Database cost of signed-in visitors** and uncached URLs: measured at the final review
  in one process (every signed-in page view runs `/api/auth/me`, 5 statements; a dashboard
  about 27), not on Neon; they decide whether Neon's compute can scale to zero. Open pages
  no longer poll the database, every `?at=` maps to one of a bounded set of cache keys, and
  a client IP gets at most `PUBLIC_CACHE_MISS_LIMIT_PER_HOUR` (120) cache misses an hour,
  then 429. Many clients together could still use up Neon Free's 5 GB of egress on purpose
  (one conditions computation sends about 358 kB), and Neon then suspends the compute until
  the next billing period (docs/map-layers.md, "Free database budget").
* **Steady traffic does not fit Neon Free**: a spot page's cached responses cost 1.16 MB to
  compute (mostly the 10-day forecast and the timeline), and scoring reads 10.85 MB per
  pass (2.61 MB of it tide predictions); both could be narrowed.
* **Vitest 3 → 5** for the dev-only advisories; Next.js 16.4.2 once it is two weeks old.
* **Commit message 41572a4** says one mutation set the publication time to now; it actually
  set it to now() minus 1 day (history is not rewritten).

---

# Swell 2.0 (2026-10, sections 46-51)

The owner's master prompt of 2026-10-26 (spec sections 1-37): the forecasts follow the
weather models automatically, one provider (Open-Meteo now, Windy once licensed), researched
spot conditions, versioned publication that open pages pick up without a reload, targeted
flight searches, admin freshness monitoring, all deployable on Vercel. The audit, the plan
and the implementation status with the per-package file lists:
[`docs/architecture_audit.md`](architecture_audit.md) (sections 1-7, and 8 for what was
built). How to run, set up and deploy it: README sections E.7 and F.2,
[`docs/deploy-vercel.md`](deploy-vercel.md), [`docs/API_SETUP_REQUIRED.md`](API_SETUP_REQUIRED.md).

## 46. Architecture changes

* **Detection**: a refresh check reads only the providers' run metadata (Open-Meteo
  `meta.json` per model domain, used 10 minutes after its availability time; Windy's
  sentinel fingerprint). The wave and the atmosphere are separate components; a component is
  new only when each of its model domains is at or past the stored run.
* **Refresh**: `GET|POST /api/internal/refresh` (Bearer `CRON_SECRET`) runs in the API
  function, called every 5 minutes by Upstash QStash on Hobby or Vercel Cron on Pro; it
  fetches only the changed component, carries the other forward with its own model time,
  scores, validates, stages and publishes a **forecast version** (`forecast_versions`), then
  runs detection, matching, flights, alerts and delivery. Partial failures publish the good
  spots and retry the others; failed spots keep their previous forecast, labelled stale.
* **Serving**: every public read is cached per version and requested with
  `?v=`; `GET /api/forecasts/version` is the small endpoint open tabs poll every 20 s.
* **Frontend**: versioned query keys; a new version is swapped in without a reload, keeping
  the selected spot and time; the "Forecast updated … · N min ago" label and an
  announcement for screen readers; stale labels everywhere a rating is shown.
* **Provider**: `WEATHER_PROVIDER=auto` (Windy when licensed and authorised to store, else
  Open-Meteo GFS + GFS-Wave; Open-Meteo is the fallback); the blend, ECMWF and GEFS are kept
  but off.
* **Scoring 2.2.0** with per-hour outputs, structured reasons, researched spot conditions
  (data/surf_spot_research), research and forecast confidence kept apart.
* **Flights and alerts**: searches only for a new or materially changed match, on request,
  and right before an alert; per-search alert lead times (2-10 days by default), short-notice
  ranking; no flight timer.
* **Monitoring**: `/admin/freshness` and admin-only email alerts (model run late, refresh
  failures, stale spots, provider credentials including flight and email keys, quotas,
  silent scheduler).
* **Hosting**: the daemon is optional (map layers only); without it the refresh endpoint and
  the daily cron run its other work.

## 47. Definition of Done (spec section 36)

| Item | Status | Evidence |
|---|---|---|
| Vercel production build succeeds | offline build verified; no real deployment | `scripts/vercel-build-check.sh --offline`: `RESULT: ok`, API function 128.2 MiB (3,469 files; limit 500 MiB), `maxDuration` 300 s (2026-10-29) |
| Hosted database works | locally verified through PgBouncer in transaction mode (as Neon's pooler); Neon itself not tried | `tests/integration/test_pgbouncer_mode.py` (3 passed against a local PgBouncer on 2026-10-29; skipped in CI without `TEST_PGBOUNCER_URL`) |
| Open-Meteo acquisition works | verified live | refreshes of 2026-10-28 and 29 (section 50) |
| Windy adapter implemented | implemented, tested with recorded and synthetic responses; no key | `tests/unit/test_windy_provider.py`, `tests/integration/test_windy_acquisition.py` |
| Windy activation instructions complete | done | `docs/WINDY_API_SETUP.md` (`tests/unit/test_windy_setup_doc.py`) |
| Windy storage disabled without authorisation | done | `WINDY_STORAGE_AUTHORIZED`; `tests/unit/test_provider_manager.py`, `test_windy_provider.py` |
| All 50 spots configured | done | `data/surf_spots/`, seeded by `init-db` (50 spots) |
| Profiles contain sourced research | done (quoted claims only; "not documented" where no source says it) | `data/surf_spots/research_conditions.json`, `docs/surf_spot_research/`, `tests/unit/test_spot_research_docs.py` |
| Scores generated; offshore scoring works | done | scoring 2.2.0, `tests/unit/test_surf_quality.py`, `test_wind.py`, `tests/integration/test_scoring_outputs.py` |
| Central acquisition | done: one refresh for all users, no per-visitor fetching | the refresh endpoint; `tests/integration/test_refresh_components.py` |
| Shared storage under the provider's licence | Open-Meteo CC BY 4.0 with the credit next to the data; Windy only with written authorisation | `docs/API_SETUP_REQUIRED.md` 2.1, 2.9 |
| Model update detection works | verified live (a natural wave-only refresh) and in tests | section 50; `tests/unit/test_sync_detect.py`, `test_open_meteo_components.py` |
| Automatic processing works | the endpoint and its stand-ins are tested; the 5-minute schedule (QStash) is documented, not deployed | `tests/integration/test_refresh_components.py`, `test_daemonless_hosted.py`, `test_internal_cron.py`; docs/deploy-vercel.md step 7b |
| Versioning works; reliable publication | done | `tests/integration/test_forecast_versions.py`, `test_refresh_components.py` (marker self-heal) |
| Cache invalidation works | done in the app; the real CDN not verified | `tests/integration/test_forecast_serving.py`, `test_public_snapshot_cache.py` |
| Active sessions detect updates; markers and charts update | done | `frontend/e2e/live-update.spec.ts` (a simulated run through the real backend, both projects), `live-update-mocked.spec.ts`, `tests/forecast-version.test.tsx` |
| Stale forecasts identified; partial failures handled | done | `tests/integration/test_forecast_versions.py` (stale spots served from their previous run), `test_refresh_components.py` (retries back off); `frontend/e2e/map-markers.spec.ts`, `tests/freshness.test.tsx` |
| New swell events detected; searches matched | done | `tests/integration/test_pipeline.py`, `frontend/e2e/full-flow.spec.ts` |
| Flight API with real credentials or a clearly labelled test adapter | test adapter ("Mock fares"); Duffel and Amadeus adapters tested with recorded responses, no credentials | `tests/unit/test_flight_providers.py` (and the flight tests), the UI's mock labels |
| Flight requests targeted and deduplicated | done | `tests/integration/test_flight_targeting.py`, `tests/unit/test_flight_targeting.py` |
| Email verification works; notifications work | done (Mailpit in the local stack: 23 messages on 2026-10-29) | `tests/integration/test_auth_api.py`, `test_email_delivery.py`, `test_free_tier_egress.py` |
| API usage monitorable | done | `/admin/freshness` usage group, `quota_near` alerts; `tests/integration/test_admin_freshness.py` |
| Frontend simple and responsive | done | the Pixel 7 project of the e2e suite |
| Tests pass | yes | section 49 |
| README updated | done | README A, C, E.7, F.2, M |
| Unverified integrations and limitations documented | done | section 51, README M |

## 48. Final deliverables (spec section 37)

| # | Deliverable | Where |
|---|---|---|
| 1 | Architecture changes | section 46; docs/architecture_audit.md section 8 |
| 2 | Files created and modified | docs/architecture_audit.md 8.2 (per package) |
| 3 | Vercel configuration | `vercel.json`, `.vercelignore`, `Containerfile`, `backend/app/core/profiles.py`; docs/deploy-vercel.md |
| 4 | Open-Meteo integration | `backend/app/services/forecasts/open_meteo.py` |
| 5 | Windy ready for credentials | `backend/app/services/forecasts/windy.py`, `provider_manager.py`; docs/WINDY_API_SETUP.md |
| 6 | Source and licensing configuration | `WEATHER_PROVIDER`, `WINDY_STORAGE_AUTHORIZED`, `COMMERCIAL_USE` (core/config.py); attribution in every forecast payload |
| 7 | Schema and migrations | `backend/alembic/versions/0009_forecast_versions.py`, `backend/app/models/version.py` |
| 8 | Fifty spot research profiles | `data/surf_spots/research_conditions.json`, `docs/surf_spot_research/` |
| 9 | Scoring engine | `backend/app/services/surf_quality/` (scoring 2.2.0); docs/SURF_SCORING.md |
| 10 | Model-update detection | `backend/app/services/sync/detect.py`, `forecasts/carry.py` |
| 11 | Centralised refresh | `backend/app/api/internal.py`, `services/sync/orchestrator.py`, `refresh.py`, `standin.py` |
| 12 | Versioning and publication | `backend/app/services/forecasts/versions.py`, `serving.py`, `services/sync/publish.py` |
| 13 | Frontend live updates | `frontend/hooks/use-forecast-version.ts`, `components/forecast-updated.tsx` |
| 14 | Map and chart integration | `frontend/app/map/page.tsx`, `components/map/`, `components/forecast/` |
| 15 | Personalised flights | `backend/app/services/flights/` (service.py, budget.py, airports.py); docs/MATCHING_AND_FLIGHTS.md |
| 16 | Email notifications | `backend/app/services/notifications/` |
| 17 | Freshness monitoring | `backend/app/services/admin_freshness.py`, `frontend/app/admin/freshness/` |
| 18 | API usage reporting | `backend/app/services/usage.py`; the dashboard's usage group |
| 19 | Test results | section 49 |
| 20 | Deployment and setup instructions | README E.7 and F.2, docs/deploy-vercel.md, docs/API_SETUP_REQUIRED.md |
| 21 | Known limitations and unresolved external dependencies | section 51 and README M |

## 49. Test results

Run on 2026-10-29 by the review-round-1 fixer on `wf6/int`, on this development
machine: the backend suites on the code of commit 5b9ce29, the e2e suite on d4fa28f (which changes only
an e2e test); the documentation commit after them changes no code.

| Check | Result |
|---|---|
| `ruff format --check .`, `ruff check .`, `mypy app` | pass (412 files formatted, 219 files type-checked) |
| `alembic upgrade head`, `downgrade -1`, `upgrade head`, `alembic check` on an empty database | pass: 0001 to 0009, no new operations |
| Upgrade of a copy of a real database (at 0006: 50 spots, 25,000 forecast rows and predictions) to head | pass: 0007, 0008 and 0009 in 1.4 s; forecast rows and predictions identical before and after (md5); no version until the first refresh |
| `pytest tests/unit` | 2,882 passed, 6 skipped (the lean-install tests) |
| `pytest tests/unit/test_lean_imports.py` with `LEAN_PYTHON` (a lean install from `uv.lock`) | 32 passed, a demo spot-only refresh in the lean install included |
| `pytest tests/integration` | 638 passed, 3 skipped (the PgBouncer tests, without `TEST_PGBOUNCER_URL`); 637 passed in the run before the alert-order fix |
| `npx tsc --noEmit`, `npx eslint . --max-warnings=0`, `npx vitest run` | pass; Vitest 604 passed (51 files) |
| `npm ci`, `npm run build` | pass |
| `./scripts/run-e2e.sh` (Chromium and Pixel 7) | 154 passed (15.6 min; the live-update spec through the real refresh: publication to the open map 4.3 s and the spot page 4.2 s with the e2e build's 2 s poll). The run before it: 152 passed, 2 failed, one test in both projects (a read count the FE-5 fix had not updated; fixed in d4fa28f) |
| `scripts/vercel-build-check.sh --offline` | `RESULT: ok`, API function 128.2 MiB (3,469 files; limit 500 MiB), `maxDuration` 300 s (2026-10-29); the daily cron is in the output config |
| `backend/.venv/bin/python scripts/check_api_bundle.py --max-mb 200` | 163.6 of 200 MB |
| `docker compose config -q` (`docker-compose.yml`, `docker-compose.vercel.yml`) | pass |
| shellcheck 0.11 on `scripts/run-e2e.sh`, `scripts/vercel-local.sh`, `scripts/vercel-build-check.sh` | clean |
| `npm audit --omit=dev` | 1 moderate advisory (fast-uri below 3.1.9, under @hookform/resolvers and ajv; docs/decisions.md ADR-037). Dev-only: Vitest 3's tinypool (2 critical) and @vitest/mocker (moderate), not upgraded in this round |

## 50. Measurements

The full table is in docs/architecture_audit.md, 8.3 (all on this development machine, none
on Vercel, Neon or Upstash). The main figures:

* A live Open-Meteo refresh of both components: 125 weighted calls, 12.1 s of refresh (38.7 s
  wall with proxy timeouts in the metadata check); a natural wave-only refresh of the 00Z
  GFS-Wave run: 75 weighted calls, 20.3 s of refresh, the wind carried with its 18Z time.
* A check with nothing new: four `meta.json` requests, 1.2-1.7 s, no data request, no
  database query.
* From publication to the new data on an open page: p50 13.8 s (16 samples, 9.7-22.9 s,
  20 s poll, local Vercel-style stack without a CDN).
* Database egress of a spot refresh: 6.3-9.5 MB from the database to the app (demo data).
* The API function: `RESULT: ok`, API function 128.2 MiB (3,469 files; limit 500 MiB), `maxDuration` 300 s (2026-10-29); the lean API imports in 1.5-1.8 s cold.
* 4,000 requests through the local edge, 64 at a time: 0 errors, p50 222-314 ms.

## 51. Not verified, limitations and external dependencies

* **Not deployed**: no real Vercel deployment was made. Unverified until the first one:
  Vercel's CDN caching of the versioned reads and of `/api/forecasts/version` (the
  publication-to-page latency was measured without a CDN), Neon (only PgBouncer in
  transaction mode was tested, locally), the QStash schedule, the Vercel daemon (the Vercel
  CLI 64.5.0 refuses a `vercel.json` with `daemons` in `vercel build`), Neon's suspend and
  wake, and real Upstash command counts.
* **The build-time seed on Vercel**: the reference data live in the repository's `data/`,
  outside the api service's root `backend/`. Whether Vercel's build of that service can read
  `../data` is not verified (`scripts/vercel-build-check.sh` builds with
  `MIGRATE_ON_BUILD=off`, so it stops before the seed). A build that cannot read it fails on
  an empty database (`"stage": "seed"`) instead of deploying a site without spots, and skips
  the seed, saying so, when the database is seeded already; then `init-db` from a checkout
  seeds it (docs/deploy-vercel.md step 6).
* **The local Vercel-style stack** was last run on 2026-10-29 01:35 UTC, before the review
  rounds (smoke 19 of 19 shell checks and 6 of 6 browser checks, demo data); the three smoke
  checks added since (the refresh endpoint and the version endpoint) and the migrate
  service's seeding build command were not run against it. Its daemon refreshes the spots
  itself, so it does not test the daemon-less hosted path; that path is covered by
  `tests/integration/test_daemonless_hosted.py` and `test_hosted_first_deploy.py`.
* **Atmosphere-only live refresh never observed**: on 2026-10-29 Open-Meteo's servers
  answered GFS 0.13 00Z and 18Z alternately for over 36 minutes; three checks saw the new run
  and three syncs skipped it safely without fetching (no charge, no version). The path is
  covered by tests with the demo provider and by the wave-only live refresh.
* **No Windy key** (the activation is configuration only, tested with recorded and synthetic
  responses), **no Duffel or Amadeus credentials** (demo fares, labelled "Mock fares").
* **Budgets are arithmetic** on unit costs measured here (docs/API_SETUP_REQUIRED.md 1.4):
  Upstash Free fits a quiet site; steady traffic with a tab open in a CDN region all month
  can pass 500K commands (pay-as-you-go about $1 a month at 540K).
* **The 60 s freshness target** depends on the scheduler's 5-minute interval and Open-Meteo's
  publication (the refresh waits 10 minutes after a run's availability time); from
  publication, open pages measured p50 13.8 s without a CDN.
* **Hobby is non-commercial** (Vercel and the free Open-Meteo API); a paywall needs Vercel
  Pro and an Open-Meteo plan first (section 44).
