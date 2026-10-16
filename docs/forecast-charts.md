# Spot forecast charts (the unified timeline)

The spot page (`/spots/{slug}`) shows the whole forecast horizon on **one time axis**: a
summary of the selected time, one button per day, and six chart rows that share one scale and
one label column, so a day boundary or the selected step is at the same x in every row. This
replaces the three separate Recharts charts (quality, swell, wind), which had no shared
selection, no shared x layout and, for 42 of the 50 spots, no axis labels at all (audit CH-1
to CH-4).

Code: `frontend/components/forecast/*` (rows, cursor, summary, selection store),
`frontend/lib/timeline.ts` (scale, ticks, days, nights, steps; pure functions),
`backend/app/services/timeline.py` and `backend/app/api/timeline.py` (the data).

## 1. Layout, top to bottom

| Part | Test id | What it shows |
|---|---|---|
| Selected-time summary | `selected-summary` | "Thursday, Oct 15 · 7:00 AM SAST", the step length ("3-hour step"), surf quality (class, x.x/10 and a rank glyph), breaking waves (typical–sets, the central value and the likely range), the wave systems (the one that drives the surf first), wind (speed, FROM direction, relation, strength and favourability when rated, gust on its own line), tide (height, rising or falling, datum, and the next high or low water with its height and time), and forecast confidence with confidence at the break as a separate badge. "Why this forecast?" expands below. |
| Day buttons | `day-nav`, `day-YYYY-MM-DD` | One button per local day with steps: weekday, date, the day's best daylight class and score, duration-weighted good hours. `aria-pressed` marks the selected step's day; a button selects the day's best daylight step and scrolls the timeline to the day's start. Previous / next day buttons. |
| Header | `timeline-header` | Day names with local dates (they stick to the left edge while the day is in view), hour ticks, the selected time pill, the "Now" marker. |
| Surf quality | `quality-strip` | Our rating per step, coloured on the ordinal ramp, with the class written in each run of equal steps ("Very Good", or "VG" where narrow). |
| Surf | `wave-bars` | Our breaking-height estimate per step (bar = central value), the likely range behind it, the sets tick, each day's peak value; optional offshore Hs (model). |
| Swell | `swell-partitions` | One lane per wave system the model sent (primary, secondary, third swell, wind sea), an arrow per step. |
| Wind | `wind-row` | Direction arrows, speed bars with gust ticks, the relation to this break written underneath. |
| Tide | `tide-row` | The predicted tide curve with high and low waters, or "Tide data unavailable". |
| Legend | `quality-chart-legend` | Every mark, the arrow convention, the tide's station, source and datum, and the spot's tide preference. Collapsed under "How to read these charts" on phones. |
| Table | `timeline-table` | "Table of every forecast step": all values as an HTML table (mounted when opened). |

The timeline root keeps the old chart's test id `quality-chart`; the swell and wind rows'
plots keep `swell-chart` and `wind-chart`, and the pointer surface inside the slider keeps
`hour-slider` (the ids of the charts and the hour slider it replaced). Below the timeline,
"Our prediction and the model data" (`forecast-hour`, with `forecast-breakdown`,
`our-prediction`, `raw-model`, `uncertainty-range`) shows the selected step's full breakdown
from `/forecast`, and "Compare forecast models" keeps the model comparison chart.

## 2. Data: `GET /api/spots/{slug}/timeline?days=10`

Contract: `backend/app/schemas/timeline.py` (`SpotTimelineOut`). The page fetches it once
(`useSpotTimeline`, stale time 2 minutes); selecting, hovering or changing units never sends
another request (for selection, asserted in `e2e/timeline.spec.ts`).

* Same authoritative run and window as `/forecast` (`latest_runs_by_spot`, from 3 h ago to
  `days` ahead), so a step's score and label equal the forecast point's
  (`tests/integration/test_timeline_api.py`).
* `steps[]` are the run's stored valid times at the model's own spacing. `step_hours` is the
  time to the run's next stored valid time (the last step: the run's next stored time
  beyond the window; at the end of the run's horizon, the spacing of the steps before it; a
  lone step, the gap from the run's stored time before it). `steps_meta.segments` lists
  where the spacing changes; `interpolation` is always `"none"`.
* `days[]`: local midnight to local midnight from `timesteps.local_day_bounds` (23 or 25 hours
  on clock-change days), sunrise and sunset from `surf_quality.sun`, first and last light =
  sunrise − / sunset + `TWILIGHT_MARGIN`, polar state, the best daylight step and
  duration-weighted Good-or-better hours.
* Raw model values (`offshore`, `partitions`), our breaking-height prediction (`breaking`) and
  our rating (`quality`) are separate objects. Partitions appear only where the model stored
  them; `drives_surf` marks the partition with the largest predicted breaking height
  (`breaking.dominant_component`), which can be the wind sea.
* `wind`: from `components.wind_detail` when the run was scored with it (strength,
  favourability, V·n components); older rows carry the fit and relation only, and the rest is
  `null`. `gust_below_mean` flags a source whose gust is lower than its mean wind.
* `tide` and `observations` blocks come from `services/tides/api.py`; a step's `tide` is read
  from the block's curve (never from model sea level), linearly between the curve's two
  neighbouring points (30-60 minutes apart; none when they are more than 3 hours apart), with
  rising or falling from the next high or low water. The blocks are filled by the tides
  package (NOAA CO-OPS stations and EOT20 model points, docs/tides.md); a spot without a tide
  source or stored predictions says "unavailable" with the reason.
* Caching: `Cache-Control: public, max-age=300` and a weak `ETag` over the run key, algorithm
  version, tide fetch time and preference text, calibration, first and last step, staleness,
  window, newest observation and every step's rating, central and sets height, confidence
  classes and wind favourability (a re-score after new research evidence keeps the run key
  and algorithm version but changes these); `If-None-Match` returns `304`.

Units are fixed in the response (`units`): breaking heights in feet, offshore heights and
tide in metres, wind in km/h. The client converts: heights follow the account's ft/m setting,
wind follows the shared `swell.windUnit` preference (km/h, mph, kn, m/s): one implementation
in `frontend/lib/units.ts` (`readWindUnit` / `writeWindUnit` / `useWindUnit`, same-tab event
`swell:wind-unit`, every storage access in try/catch), used by the charts and the map
settings alike.

## 3. Time axis

* **One scale**: x = (t − t0) / 1 h × px per hour, t0 = the first step's left edge. Every row
  draws on it; the label column (`--tl-gutter`, 92 px, 66 px on phones) is sticky.
* **Zoom**: about 72 hours in view on wide screens and about 24 hours on phones (frame
  narrower than 640 px), clamped to 6–28 px per hour; the rest of the horizon scrolls
  horizontally inside the frame (never the page).
* **Local time**: every label is in the spot's IANA zone, with the zone's own abbreviation
  where one exists ("SAST", "PDT", "WITA", "AEDT") and the GMT offset beside it in the header
  ("SAST (GMT+2)"), or the offset alone ("GMT-5" for Lima). The browser's zone is never
  used.
* **Hour ticks** are generated from the axis and the zone (`hourTicks`), never from the sample
  times: 12 AM / 6 AM / 12 PM / 6 PM, every 3 hours when wide (≥ 22 px/h), every 12 hours when
  narrow (< 8 px/h). Half- and quarter-hour zones get their own instants (Kathmandu's 12 AM is
  18:15 UTC).
* **Days**: separators at each local midnight from the API's `days[].start_utc`, drawn in
  every row; a 25-hour fall-back day is 25 hours wide.
* **Night**: shaded from each day's `last_light` to the next day's `first_light` (the API's
  sun times, not the per-step daylight flags), before the first day's first light and after
  the last day's last light; a polar night is shaded all day, a polar day not at all. Night
  steps are dimmed, never removed.
* **Now**: a dotted line at the current time (refreshed each minute) with a "Now" label; when
  the selected step covers the current time the selected pill reads "Now · 5:00 PM".

## 4. Steps and the interpolation policy

* A step is drawn over the times for which it is the nearest valid step: from halfway to the
  previous valid time to halfway to the next (`stepExtents`). Bars, quality segments and
  partition cells are therefore as wide as the step: 1-hour steps are thin, 3-hour steps three
  times as wide, 6-hour steps six times; the transition step of a 3 h → 6 h run is 4.5 h wide.
  Nothing is drawn at a time the API did not send, and no hourly values are implied.
* A selected or hovered time snaps to the **nearest valid step** (ties to the earlier one).
  Summary, tooltip and table name the step length ("3-hour step"). The header and the
  toolbar say which spacing the run uses ("3-hour steps, from Sat 2:00 AM 6-hour steps · not
  interpolated").
* Model values are never interpolated in the client. The only line drawn between data points
  is the tide curve: a monotone cubic through the tide block's own 30–60 minute points (it
  never overshoots them), which depicts the astronomical tide prediction those points sample.
  High and low waters are the block's events, not maxima picked from the drawn curve.
* Arrows need about 13 px; when hourly steps are narrower, every n-th arrow is drawn (no
  value is ever moved or averaged).

## 5. Rows and conventions

**Surf quality.** The class is the API's (our spot-specific scoring engine,
`quality.label`), never derived from wave height in the client. Colours are one ordinal ocean
ramp, deeper = better (`--q-*` in `app/globals.css`, `QUALITY_COLORS` in `lib/quality.ts`):
every class has at least 3:1 contrast against the card, lightness is strictly ordered, and
the labels on the colours have 4.5:1 (`tests/quality-palette.test.ts`). It replaces the
earlier blue / yellow / green / teal / orange / purple legend, whose "fair" had 1.92:1 on
white and whose top classes were nearly the same lightness (CH-13). Colour never carries the
class alone: the strip is labelled, the summary adds a rank glyph (one to six rising bars),
and the tooltip and table spell the class out. A matching dark-surface ramp
(`QUALITY_COLORS_DARK`, brighter = better) is used on dark surfaces: the forecast map's
markers on the dark basemap (`colorForScore(score, "dark")`).

**Surf (breaking waves).** Our estimate of breaking face height at the break, not a model
output:

| Mark | Field | Meaning |
|---|---|---|
| Bar | `breaking.central_ft` | The central estimate (the bar height). |
| Pale band | `breaking.p10_ft`–`p90_ft` | The likely range of an assumed error model (heuristic, "not yet verified"; `uncertainty_kind` says how it was made). It is not a calibrated probability interval. Where it reaches above the axis it is clipped and marked with a small chevron; its value is in the tooltip, summary and table. |
| Dark tick | `breaking.sets_ft` | The height of the sets. |
| Summary "typical–sets" | `typical_ft`–`sets_ft` | The range a surfer would quote ("5–7 ft"). |
| Dashed level (toggle "Offshore Hs (model)") | `offshore.hs_m` | The wave model's significant wave height offshore, a different quantity from breaking height. |
| Hollow diamond (with the toggle) | `observations.items[]` with `kind = "instrument"` | A measured buoy height, from the observations block, at its own time. None are shown while that block is unavailable. |

The axis fits the bars and set heights over the whole horizon (it does not rescale while
scrolling); each day's largest daylight central value is written above its bar.

**Swell.** One lane per partition the API sent anywhere in the response. Arrows point where
the waves travel (FROM + 180°), the same convention as the map's particles and wavefronts
(`lib/directions.ts`, `arrowRotationDeg`, `ARROW_CONVENTION_TEXT`); text always gives the
FROM direction ("SW 225°"). Arrow length grows with the square root of the partition's
offshore height relative to the largest in the response. The partition that drives the surf
(`drives_surf`) is solid, the others outlined, so the emphasis does not depend on colour. The
summary lists every system with height (in the viewer's unit), period and direction, the
driving one first and tagged "drives the surf".

**Wind.** Bar height = mean speed (`wind.speed_kmh`), dark tick = gust (`wind.gust_kmh`,
labelled "gust" in the legend, tooltip and summary; no gust, no tick). Bars are coloured by
the API's favourability (favourable / workable / unfavourable) with a hatch pattern for
workable (sparse) and unfavourable (dense); runs scored before the wind model carry no
favourability, so their bars are neutral and the legend says "favourability not rated for
this run". The client never derives favourability from the relation. The relation to this
break (offshore, cross-offshore, …, computed by the backend from the spot's researched
offshore direction, never from "wind from the east") is written under the bars for each run
of equal relation. Arrows point where the wind blows to, as on the map.

**Tide.** The predicted tide from the tide block: curve, highs (filled) and lows (hollow)
with their local times, heights relative to the block's datum (labelled in the gutter and the
legend: MLLW, MSL, LAT or "model mean sea level"), station, distance and source in the
legend. Status `demo` is labelled "Demo tide (synthetic)". The spot's researched preference
is the block's `preference.text`, which is "Tide preference not well established." when the
research is weak, disputed or missing; the client never invents a tide rule. Without a
source the row reads "Tide data unavailable · <reason>" and draws no curve, and the summary
says "Tide data unavailable". With real predictions (`status` "available") a line under the
charts, outside every fold, shows the station or model point with its distance, the
source's attribution (EOT20's CC BY 4.0 credit or NOAA CO-OPS) and the block's caveat
(`reason`: model mean sea level is not a chart datum, or the CO-OPS datum and interpolation
note). The header line under "Surf forecast" lists every model run with its cycle, when it
was fetched, and the sources' credit and licence links (`provenance.source.attribution_links`).

## 6. Selected-time summary and "Why this forecast?"

The summary always describes the selected step. The page opens on the step covering the
current time when it is daylight, otherwise on the next daylight step; `?t=<ISO time>` opens
on the step nearest to that time. (The week's best step is already shown just above the
timeline, under "Best surf, next 7 days", with a "Show in timeline" button.)

"Why this forecast?" is built only from the step's `why` record: the scoring engine's own
explanation sentence, the factor fits (0–1, shown with the app's Favorable / Workable /
Limiting words), the gates below 1 that held the score back ("Held back by swell period
(score × 0.85)"), the points lost per factor when the run stored them, whether tide was part
of the rating, and the 0–100 score (a ranking index, not a probability). Forecast confidence
and confidence at the break are separate badges; research confidence stays in "About this
break". None of them is a probability.

## 7. Interaction and rendering

* **One selection** for every row, the summary, the day buttons and the breakdown below:
  `TimelineSelectionProvider` holds a tiny external store (`selectedT`, `hoverT`, `hoverX`)
  read with `useSyncExternalStore` and a selector, so a component re-renders only when its
  slice changes.
* The rows never subscribe. They are created once per layout (data, scale, units) and passed
  unchanged into the slider element; the selected step's marks get a `data-selected`
  attribute from a store subscription, not from a re-render. Only the cursor layer (selected
  column and line through all rows, the time pill, the tide marker, the hover line), the
  tooltip, the summary and the day buttons re-render. `tests/forecast-timeline.test.tsx`
  counts row renders with React's Profiler: 0 during hover, click, keys and day buttons.
* **Pointer**: hover moves a dashed cursor and shows a tooltip (time, step length, quality,
  surf, the driving swell, wind, gust, tide) kept inside the frame; click or drag selects.
  **Touch**: a swipe scrolls the timeline; a tap selects (selection happens on `click`, so a
  scroll gesture never selects).
* **Keyboard**: the frame is a `role="slider"` (value = step index, `aria-valuetext` such as
  "Thu 7:00 AM, Very Good, 5–7 ft"); ArrowLeft / ArrowRight move one API step, Page Up / Page
  Down one day (the nearest step at the same time of day), Home / End the first / last step
  of the selected local day. The selected step is scrolled into view (smoothly, or instantly
  with `prefers-reduced-motion`).
* **Deep link**: a chosen step is written to `?t=` with `history.replaceState` (debounced,
  no navigation); the default selection is not written.
* **Errors**: a failed request shows "The forecast timeline could not be loaded." with a retry
  button; `available: false` shows the API's message; the rest of the page still works.

## 8. Accessibility

Labelled slider with spoken value text and key help (`aria-describedby`); every row has a
text name; the table view carries every value; the quality class is written in the strip and
summary, the rank glyph and wind patterns make classes readable without colour; contrast is
tested; focus rings are visible; reduced motion disables smooth scrolling; all controls are
real buttons (`aria-pressed` for the day buttons, the wind unit and the offshore toggle).

## 9. Model comparison

"Compare forecast models" (collapsed) keeps the Recharts comparison of the raw model series.
Its day ticks now come from the axis and the zone (`localMidnights`), so every spot gets
labels on 3-hourly data (CH-1), and the tooltip time is in the spot's zone with its name.

## 10. Measurements

Measured on this machine (4 CPUs) on 2026-10-14; see the package report for commands.

| What | Value | How |
|---|---|---|
| Real J-Bay blend run, `/timeline` vs `/forecast` | 132,905 B vs 262,304 B (JSON) | `services/timeline.spot_timeline` on a clone of the `swell_live2` capture (78 3-hourly steps) |
| Demo J-Bay (hourly, 243 steps), `/timeline` vs `/forecast` | 375,075 B vs 702,528 B (JSON); 15,046 B gzip through the Next.js proxy vs 26,260 B gzip | `curl` against the demo backend and the proxy (`Accept-Encoding: gzip`) |
| Conditional request with the ETag | 304, 0 B | `curl -H "If-None-Match: …"` |
| `/timeline` response time, demo J-Bay | 94–169 ms (5 requests, while other tests ran) | `curl -w %{time_total}` |
| DOM size of the timeline (demo, hourly, 10 days) | about 3,750 elements desktop, 3,390 on a phone | `document.querySelectorAll` in Chromium |
| Row renders on hover / select / keys / day buttons | 0 | React Profiler counter, `tests/forecast-timeline.test.tsx` |
| Hover, click and ArrowRight to the next frame (production build, demo J-Bay, hourly, 10 days) | median 25 / 29 / 32 ms on a 1440 px desktop, 20 / 25 / 24 ms in Pixel 7 emulation | 25 synthetic events each in headless Chromium, handler + render + one animation frame, `requestAnimationFrame` + `setTimeout` after each event |
| Requests caused by selecting steps | 0 | request log in `e2e/timeline.spec.ts` |

## 11. Tests (spec 27, chart items 1-15)

| # | Where |
|---|---|
| 1 rows load | `forecast-timeline.test.tsx` "1."; `e2e/timeline.spec.ts` "every row loads" |
| 2 hour labels | `timeline-lib.test.ts` (Johannesburg 22/04/10/16 UTC, Honolulu, Kathmandu, all spot zones); `forecast-timeline.test.tsx` "2." |
| 3 day boundaries align | `forecast-timeline.test.tsx` "3."; e2e bounding boxes within 1 px at 1280 px and on Pixel 7 |
| 4 selection updates all rows | `forecast-timeline.test.tsx` "4."; e2e "selecting a time …" |
| 5 wave height = backend | `forecast-timeline.test.tsx` "5 and 6."; e2e values vs `/timeline` |
| 6 wind speed = backend | same, including a null gust |
| 7 wind arrows | `directions.test.ts`; `forecast-timeline.test.tsx` "7."; e2e arrow rotation = FROM + 180 |
| 8 tide aligned | `forecast-timeline.test.tsx` "8." (synthetic tide, H/L at scale(t), datum; unavailable state); e2e synthetic tide |
| 9 quality from the model | `forecast-timeline.test.tsx` "9."; backend `test_timeline_api.py` (same score and label as `/forecast`) |
| 10 tooltips | `forecast-timeline.test.tsx` "10."; e2e hover |
| 11 mobile scrolling | e2e `@mobile` (touch swipe via CDP, tap select, no page scroll) |
| 12 day/night shading | `timeline-lib.test.ts`, `forecast-timeline.test.tsx` "12."; backend sun tests |
| 13 time zones | `timeline-lib.test.ts` (TZ=America/New_York, Lisbon 25-hour day, Fiji); `forecast-timeline.test.tsx` "13." |
| 14 missing data | `forecast-timeline.test.tsx` "14."; e2e failed and empty timeline |
| 15 no fabricated times | `timeline-lib.test.ts`, `forecast-timeline.test.tsx` "15." (mark count, API times only, 6 h twice as wide as 3 h); e2e mixed cadence; backend segments |

The e2e suite runs on demo forecasts (hourly) and on route-mocked payloads derived from them
(a synthetic tide, a thinned 3 h / 6 h cadence). It says nothing about live tides or live
observations, which are not configured in this package.

## 12. Limitations

* Real tide and observation blocks are covered by the backend's tests and by synthetic
  frontend payloads only; the e2e backend runs the demo provider, so the e2e suite draws the
  demo tide and no buoy markers.
* Runs scored before the wind model (algorithm 2.0.0) have no wind strength or
  favourability, so wind bars are neutral for them.
* Zone abbreviations come from the browser's Intl data (several English locales are tried);
  where none exists, the GMT offset is shown.
* The wave axis spans the whole horizon, so a large swell late in the forecast makes the
  first days' bars shorter; the likely range is clipped at the top of the axis.
