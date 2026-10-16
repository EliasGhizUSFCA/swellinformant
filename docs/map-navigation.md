# Map navigation and the map shell

How the world map (`/map`) and the spot page's mini map move, zoom and label their data. The
ocean layers drawn on them (colour fields, wind particles, wave crests, basemap) are described
in `docs/map-rendering.md` and `docs/map-layers.md`.

Files: `frontend/lib/map-bounds.ts` (limits), `frontend/components/map/forecast-map.tsx` (the
forecast map, its markers and flights), `frontend/components/map/surf-map.tsx` (the mini map and
the navigation pieces both maps share), `frontend/components/map/map-freshness.tsx` (freshness
chip, model run label), `frontend/app/map/page.tsx` (layout), `frontend/lib/map-time.ts`
(timeline and run labels).

## Limits

| What | Value | Why |
|---|---|---|
| Latitude band | 84°N to 78.5°S, hard edge (`maxBounds`, `maxBoundsViscosity: 1`, `bounceAtZoomLimits: false`) | The GFS-Wave grid's extent on a stored live cycle, inside the Mercator edge at ±85.05°. Every spot is at least 25° inside it. Before, one 600 px drag north left 805 of 807 rows blank. |
| Longitude | effectively unbounded (±100,000°) | The basemap, fields and tiles repeat in every world copy, so panning across the date line is continuous. |
| Minimum zoom | `minZoomFor(w, h) = max(1, log2(w / 256), log2(h / (256 × 0.8347)))`, re-measured when the map's container resizes | The smallest zoom at which no longitude shows twice and the band fills the height: 2.49 at 1440×807, 2.91 at 1920×987, 1.78 at 390×735. A fixed zoom 2 showed 1.88 world copies at 1920 px, with coasts drawn twice and markers on one of them. |
| First view | centre 8°N 10°E, zoom `max(2, minZoom)` | One world wide on desktop, with the spots (43°S to 59°N) midway between the layer tabs and the timeline. The container is measured before the map is created, so it opens at that zoom without a jump. |
| Maximum zoom | 8 on the dark basemap, 19 (OSM) or 18 (CARTO) with tiles | Natural Earth 1:50m outlines turn angular beyond 8. |
| Zoom snapping | none on the forecast map (`zoomSnap: 0`); whole levels on the mini map | The one-world zoom is usually fractional; whole-level snapping rounded 2.91 up to 3, so zooming out never returned to it. A wheel notch is still about one level, `+`/`−` one level. Raster tiles at fractional zooms are scaled by the browser (slightly soft on the optional Streets basemap). |

## World copies and the date line

Leaflet draws vector markers once, at their literal longitude. Both maps place every marker,
the user's position and every flight target in the world copy nearest the view centre
(`nearestCopy(lon, centreLng) = lon + 360 · round((centreLng − lon) / 360)`). The centre used for
placement changes only when some spot (or the user's position) crosses the centre ± 180°
(`wrapKey`), so a pan re-renders the marker layer a few times per world, not every frame.

`worldCopyJump` is off: with nearest-copy markers it moved the map by a world during a drag while
the markers followed only on the next render (7,483 px off for a whole drag in the audit). The
centre longitude therefore grows on endless panning; once it passes ±540° the view is re-centred
on the same meridian without animation, after the markers have moved to their new copy in the
same task (no frame shows them a world away).

Flights go the short way: Cloudbreak (177.2°E) to Pipeline (158.1°W) crosses about 25° of
longitude, not 335°.

## Markers and flights

* `SpotMarker` is memoised with stable props. A new forecast step re-styles only the markers
  whose colour class changes; zooming and panning touch none (before: 50 `setLatLng`,
  `setStyle`, `on` and `off` calls per page render, 1,000 of each over five timeline steps).
  The page re-renders on zoom only when it crosses the city-label zoom (4).
* Leaflet's SVG renderer keeps a 50 % margin around the view (default 10 %), so markers do not
  pop in late during long inertial pans.
* The map flies to a spot only when the selected slug changes. A filter that hides and re-shows
  the selected spot, or a new forecast step, never moves the map. With `prefers-reduced-motion`
  the flights (to a spot, to the user's position) jump without animation.
* On phones the spot panel is a bottom sheet up to 72 % of the map's height; a selected spot
  lands in the middle of the strip between the layer tabs and the sheet. On short, wide screens
  (640 px wide or more and 500 px high or less: phones in landscape) a bottom sheet would leave
  about 30 px of map, so the panel is a 360 px side sheet below Settings instead, the locate
  button and timeline move left of it, and the spot lands in the middle of the map left of the
  sheet, between the tabs and the timeline.
* Phones in landscape and tablets (640 px wide and up, below 1024 px) put the best-spots bar
  beside the locate button. On touch screens with a short map (480 px high or less) the zoom
  buttons are left out: pinch zooms, and they would sit under Settings.

## Time and freshness on the map

* The timeline bubble gives the selected time in the viewer's zone with the zone's name
  ("8am Sun PDT") and stays over the thumb. Beside it, on the side with more room: the model run
  and the step's lead time after it, "GFS 18Z Oct 11 · +6 h", or "analysis" at the run's own time
  (model output still, not an observation). Phones show the lead time only; the full source and
  run are in its tooltip and the legend.
* Once the newest run behind the ocean layers (`FieldIndexOut.stale`, `age_hours`, limit
  `MAP_FIELD_STALE_AFTER_HOURS`, default 18 h) or behind the spot markers
  (`MapSnapshotOut.stale`, `newest_run_issued_at`) is out of date, a status chip under the layer
  tabs reads "Forecast out of date — model run 54 h old". The layers and markers keep drawing
  that run.
* The best-spots list is "Best spots now" on the current step and "Best spots · 9am Sun PDT" on
  any other; the time is never cut off (it wraps to a second line where a long zone name does not
  fit). Its count reads "50 of 50 spots" ("Showing … " and "updating" are screen-reader text; the
  pin turns into a spinner while the list refreshes). Marker tooltips give our rating out of 10
  ("Good 6.5/10 · 4–6 ft surf (our prediction)").
* The spot panel names its time in the spot's own zone ("Thu, Oct 15, 5:00 AM GMT+2 at the
  break"). Once the user has moved the timeline off "now", its link opens the full forecast at
  that time, `/spots/{slug}?t=<UTC ISO>`; at "now" the spot page opens on its own default step.

## Tests

* `frontend/tests/map-bounds.test.ts`: the band and `BAND_HEIGHT`, `minZoomFor` at eight
  container sizes (one limit tight), every spot inside the band with 25° to spare, all 50 spots
  in one world copy at 1440×807, `nearestCopy`, `wrapCentre` and `wrapKey` across the date line.
* `frontend/tests/map-markers.test.tsx` (jsdom, real Leaflet): markers in their nearest copy and
  re-placed only on a crossing, the marker layer re-rendering only then, memoisation (no Leaflet
  call for equal states, one `setStyle` for a colour change), the silent re-centre, flights the
  short way and only on a new selection, the jump under reduced motion, the bottom-sheet and
  side-sheet offsets.
* `frontend/tests/map-spot-list.test.tsx`: the best-spots title and count text.
* `frontend/tests/map-freshness.test.tsx`, `frontend/tests/map-time.test.ts`: the stale chip,
  the run and lead-time labels, `?t=`.
* `frontend/e2e/map-navigation.spec.ts` (production build): the audit's section 6.1 list at
  1440×900, 1920×1080, 1280×720, 390×844 and 844×390 (the phone tests also run in the Pixel 7
  project): bounds after drags, wheel zoom-outs, touch drags, pinches and rotation; markers at
  the date line and after inertial pans; the Cloudbreak to Pipeline flight; marker positions
  during a drag; the user's position marker in its nearest copy; marker call counts; the filter
  round trip; the freshness chip and run label; the phone bottom sheet and the landscape side
  sheet; the mini map. `e2e/map-helpers.ts` reads Leaflet's live map through a probe
  installed before the page loads (`installMapProbe`, `liveView`, `screenPoint`).

## Open decisions

* At the one-world zoom on a 1440×900 screen the desktop best-spots list (top right) covers the
  Philippines and Japan, including the Cloud 9 marker; no centre longitude avoids every overlay
  at that width (MAPNAV-13). Computed with the controls' boxes: centres between about 93°W and
  51°W (latitude 15-20°N) avoid it at 1440×807, none does at 1280×627. Options: an
  Americas-centred first view, the list starting collapsed below about 1,700 px, or leaving it.
* The first view is Atlantic-centred (10°E), which puts the date line at the screen edges; a
  Pacific-centred view is equally reachable.
