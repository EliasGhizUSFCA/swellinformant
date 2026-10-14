# Map rendering: wind particles and wave crests

How the map draws its two animated layers from the model fields: wind particles (the Wind
layer, and the optional wind overlay on the wave layers) and wave crests (the Wave Height,
Wave Energy and Wave Period layers). Every formula, constant and scale factor used on screen
is listed here. The data side (which NOAA records, the 0.5° encoding, the run and valid-time
contract) is in [map-layers.md](map-layers.md).

The crests are **stylised crests from the forecast model, not individual waves**: they are
drawn from the model's significant height, period and direction of each wave system at the
selected valid time. Their spacing and speed are scaled to be visible (stated below and in
the legend). Neither layer is an observation, and neither is for navigation.

Code:

| Part | File |
|---|---|
| Wave physics, scales and styles (pure) | `frontend/lib/map-waves.ts` |
| Crest renderer (lattice, packets, tracing, mask, animator) | `frontend/components/map/wavefronts.ts` |
| Wind particles | `frontend/components/map/particles.ts` |
| 8-bit-safe trail renderer | `frontend/components/map/trails.ts` |
| Canvases, panes, data attributes | `frontend/components/map/map-canvas-layers.tsx` |
| Basemap (land, borders, isobars, labels) | `frontend/components/map/basemap-draw.ts` |
| Field download, checks and cache | `frontend/lib/map-fields.ts`, `frontend/hooks/map-fields.ts` |
| Legend and settings | `frontend/components/map/legend-drawer.tsx`, `frontend/components/map/settings-popover.tsx` |

## 1. Conventions

* **Directions are nautical degrees FROM** (0 = from the north, clockwise), as the API serves
  them (`wave_dir`, `swell1_dir`, `swell2_dir`, `windsea_dir`). Wind is served as components
  `wind_u` (eastward) and `wind_v` (northward) of the velocity the air moves with, so for a
  wind of speed V from θ: `u = -V sin θ`, `v = -V cos θ`.
* **Motion and arrows point where the wind or waves travel TO**; readouts and text say where
  they come FROM ("from WSW (250°)"). The legend states this (`ARROW_CONVENTION_TEXT`,
  `lib/directions.ts`), the same convention as the spot charts.
* **Screen vectors are (x east, y down).** Web Mercator is conformal, so a bearing keeps its
  angle on screen at every latitude. The screen vector of a wind is `(u, -v)` and the travel
  vector of waves from F is `(-sin F, cos F)`, with no `cos(lat)` factor on either component.
  A swell from the SW (225°) travels NE: `(0.707, -0.707)`; from the west (270°): `(1, 0)`;
  from the north (0°): `(0, 1)` (down the screen).
* Directions are never averaged as numbers: grids are interpolated as unit vectors
  (`sampleDirection`), so 350° and 10° give 0°, never 180°.

## 2. Panes and land

| Pane (z-index) | Canvas | Notes |
|---|---|---|
| tiles (200) | Leaflet tiles | Streets basemap only |
| `fieldPane` (210) | colour field | half resolution (2 CSS px per pixel), smoothed |
| `wavePane` (220) | wave crests | wave layers only |
| `particlePane` (225) | wind particles | Wind layer, or a wave layer with the wind overlay |
| `basemapPane` (230) | Natural Earth land, borders, isobars, labels | dark basemap draws land |

* **Dark basemap, wave layers**: land is opaque (`#2b2b2b`), drawn above the colour field and
  the crests, so it also masks them at the coast.
* **Dark basemap, Wind layer**: land is translucent (`LAND_ALPHA = 0.55`), so the wind colour
  and particles over land stay visible (offshore flow at a coast matters for surf). Borders,
  isobars and labels are unchanged.
* **Streets basemap**: no land is drawn over the tiles, so the wave colour field is cut at the
  Natural Earth coastline (`destination-out` with the land rings) and the crests are masked to
  the ocean (section 4.6). The wind colour is not cut (it is defined over land).
* The basemap rings are not closed with `closePath()`: `fill()` closes every subpath and the
  Natural Earth rings repeat their first point. One `closePath()` per ring (2,558 rings) cost
  214 ms of a 234 ms redraw at zoom 2 (audit MAPNAV-5); without it the e2e run measured
  **5.1 ms** for the dark basemap at zoom 2, 1440 x 900 (`data-draw-ms` on
  `canvas.map-basemap-canvas`, guard < 30 ms).

## 3. Wind particles

### 3.1 Velocity and scale

`velocity(x, y) = (u, -v) x WIND_PARTICLE_SPEED` CSS px per 1/60 s, with u and v sampled
bilinearly at the pixel's latitude and longitude (`sampleGrid`, at least half the bilinear
weight on cells with data). `WIND_PARTICLE_SPEED = 0.2`, i.e. **12 CSS px per second per m/s**
at every zoom: 10 m/s moves 120 px/s. This is a visual speed, not the ground speed; the legend
says so ("at a visual speed (not ground speed)").

The colour field paints the wind speed as `|(bilinear u, bilinear v)|`, the speed of the
interpolated vector, which is also the value of the hover readout. (Interpolating the node
speeds instead gave up to 2.9 m/s more where the wind turns between nodes; audit WINDVIZ-10.)

### 3.2 Time-based motion

Each `requestAnimationFrame` advances by the elapsed time `dt` (ms), capped at
`MAX_DT_MS = 50` after a stall; the first frame after a (re)start advances by 0. A particle
moves `velocity x dt / FRAME_MS` with `FRAME_MS = 1000 / 60`, so it covers the same distance
per second at 60, 120 or 144 Hz (unit test: 120 px ± 1 in 1 s for 10 m/s). Particle lifetime
is 50 to 110 frames of 1/60 s (0.83 to 1.83 s), in ms of animation time. Count:
`min(3500, area / 650)` on desktops, `min(600, area / 1100)` below 640 px wide; the wind
overlay on a wave layer uses `OVERLAY_DENSITY = 0.45` of that, so the crests stay readable.

A new forecast step (`setFlow`) keeps every particle's position and trail: no reseed and no
cleared canvas during playback (WINDVIZ-11). A pan or zoom pauses the animation and reseeds
when the map settles. A particle that reaches a point without data or leaves the view is
respawned elsewhere (the wind field has no land mask).

### 3.3 Trails on every browser

Two renderers with the same fade curve, `alpha(age) = HEAD_ALPHA x FADE^(age / FRAME_MS)`,
`HEAD_ALPHA = 0.82`, `FADE = 0.92` (per 1/60 s):

* **Fade** (half-float canvas, `colorType: "float16"`, Chromium 137+): each frame dims the
  previous one by `FADE^(dt / FRAME_MS)` (`destination-in`) and draws the newest segments.
* **Explicit** (every 8-bit canvas: Firefox, Safari, all iOS browsers): on 8-bit alpha the
  dimming stops at `round(6 x 0.92) = 6`, a permanent 6/255 white veil (WINDVIZ-1). `trails.ts`
  instead keeps each particle's recent positions (one point per `SAMPLE_MS = 32` ms, the head
  follows the particle), clears the canvas every frame and strokes each trail with the same
  fade, quantised into age buckets ending at 60, 140, 250, 390 and `TRAIL_MS = 560` ms (alpha
  0.05 at 560 ms). A still particle leaves no ink, so nothing remains once the particles stop.

The particle canvas reports `data-color-type` and `data-trails` ("fade" or "explicit"). E2E:
the alpha histogram after 5 s has no spike at 6 with either renderer (Chromium, plus Chromium
forced to an 8-bit canvas; the same test is tagged `@cross-browser` for Firefox and WebKit,
which this sandbox cannot run).

### 3.4 Reduced motion

With `prefers-reduced-motion: reduce` the particles are replaced by still arrows on a 46 px
grid, 7 to 18 px long (8 px per px-per-frame of flow), pointing downwind, drawn once.

## 4. Wave crests

### 4.1 Wave systems

| System | Variables | Style (dash, colour) | Crests per set | Crest half-length | Set cell |
|---|---|---|---|---|---|
| Primary swell | `swell1_hs/_tp/_dir` | solid, `#f4f7fb`, glow + trough | 6 | 1.8 spacings | 6 spacings |
| Secondary swell | `swell2_*` | dashed `[7, 5]`, `#ffc46b` | 5 | 1.5 | 5.5 |
| Wind sea | `windsea_*` | dotted `[2, 4]` round caps, `#7fe3ff` | 4 | 1.4 | 4.5 |
| Combined sea (peak) | `hs`, `tp`, `wave_dir` | solid, white, glow + trough | 6 | 1.8 | 6 |

Systems are told apart by dash pattern **and** colour (never colour alone), and the legend
shows a swatch of each one drawn. Partitions exist only when the model run stores them (the
field index lists them with `group: "partition"`); the client requests them only then. The
settings chooser (shown only when the index lists partitions) disables a system the run does
not have ("Not available for this model run"). The default choice is primary + secondary;
when none of the chosen systems exists the combined sea is drawn, labelled "Combined sea
(peak)", never "primary swell". A missing partition is no data: nothing is drawn for it and
nothing is invented. Drawing order is wind sea, secondary, primary (on top); the vertex budget
goes the other way (section 4.7).

The wave model numbers partitions by height per cell, so neighbouring cells can hold
different swells. Where the four grid corners of a direction disagree by more than
`MAX_CORNER_SPREAD_DEG = 60`, the point has no direction (no averaged direction that no wave
has), and no crest crosses it.

### 4.2 Physics

Linear wave theory, `omega^2 = g k tanh(k h)`, in deep water (`tanh(k h) -> 1` for `h > L/2`,
which holds where the global model's 0.25° to 0.5° cells are):

* wavelength `L = g T^2 / (2 pi)`, with `g = 9.80665 m/s^2`: 99.9 m at 8 s, 156.1 m at 10 s,
  224.8 m at 12 s, 399.6 m at 16 s;
* phase speed `c = L / T = g T / (2 pi)`: 15.6 m/s at 10 s. Crest lines are lines of constant
  phase and move at c;
* group speed `cg = c / 2`: the energy of a wave group (a set) moves at half the crest speed.

T is the period the model gives for that system (`tp` is the peak period of the combined sea;
the partition periods are called "period" because NCEP's SWPER/WVPER are mean periods,
audit WAVEVIZ-10).

### 4.3 Spacing and its exaggeration

On screen the crest spacing is `s = clamp(S x L, 8, 64)` px with **S = 0.12 px per metre of
wavelength** (`crestSpacingPx`): 12.0 px at 8 s, 18.7 px at 10 s, 27.0 px at 12 s, 48.0 px
at 16 s; periods under 6.5 s are drawn at 8 px and over 18.5 s at 64 px. The spacing does not
change with zoom, so the drawn spacing is an exaggeration of the true wavelength at that map
scale by

`E = S x (metres per pixel) = 0.12 x 40,075,016.686 x cos(lat) / (256 x 2^zoom)`

| Zoom | E at the equator | at 45° |
|---|---|---|
| 2 | 4,696 | 3,321 |
| 4 | 1,174 | 830 |
| 6 | 293.5 | 207.5 |
| 7 | 146.8 | 103.8 |
| 8 | 73.4 | 51.9 |
| 10 | 18.3 | 13.0 |

E is at least 1: zoomed in so far that `S x metres per pixel < 1`, the true wavelength
`L / (metres per pixel)` is drawn (8 to 256 px). The legend states E for the zoom and the
latitude of the view centre, for example "Crest spacing ×4,700 at this zoom; time runs 4×
faster than real." (`scaleStatement`; E is rounded to the nearest 100 from 1,000, to a whole
number from 10, else to one decimal).

### 4.4 Motion and its time scale

**Time runs `TIME_SCALE = tau = 4` times faster than real.** A crest passes any fixed point of
the map once every `T / tau` seconds (2.5 s for a 10 s swell), at every zoom and also where
the spacing is clamped. The crest speed on screen is therefore

`v = s x tau / T` px/s, which unclamped equals `S x c x tau` (0.75 T px/s: 6.0 px/s at 8 s,
7.5 at 10 s, 9.0 at 12 s, 12.0 at 16 s).

In screen distance this is `E x tau` times the true crest speed at that map scale; the
"4×" is the time scale, the spacing factor E comes on top. The crests are drawn at the
phase speed; each set's envelope (section 4.5) moves at `v / 2`, the deep-water group
speed, so crests appear at the back of a set, cross it and fade at its front, as real wave
groups do. The animation clock only advances while the layer animates (pauses on pan and
zoom, resumes with the same clock), at most `MAX_STEP_MS = 100` per frame, and frames are
drawn at most 30 per second (`FRAME_INTERVAL_MS`): the motion is time-based, identical at 60
and 120 Hz (unit test).

### 4.5 Sets (packets) and crest shape

* **Lattice**: per view, values are sampled every `LATTICE_PX = 8` px on a grid fixed in world
  pixels: per system the travel vector, spacing, speed, height and a validity flag. A node is
  invalid without data (NaN height, period or direction), with `Hs < MIN_DRAW_HS_M = 0.2 m`,
  where the direction corners disagree (section 4.1) or over land (4.6). Sampling between
  nodes is bilinear and needs all four nodes valid.
* **Sets** sit one per cell of a jittered grid in world pixels (cell = 6 / 5.5 / 4.5 crest
  spacings of the view's median spacing, 40 to 320 px, quantised to quarter octaves so a small
  change of period between steps keeps the same sets). Anchored in world pixels, they stay in
  place geographically when the map pans (unit test: a 100 px pan gives identical crests) and
  when the timeline steps.
* **Crest positions**: the crests of a set lie on the lattice `p . W = n s + v t` (W in world
  px, p the travel vector), the same lines for every set of a uniform sea, so neighbouring sets
  agree. The envelope is flat over its middle half (`ENVELOPE_FLAT = 0.5`) with a cosine
  taper outside, `crests x s` long; it travels the cell once per cycle at `v / 2` and fades in
  and out over the first and last `CYCLE_FADE = 15 %` of the cycle. Crests under weight 0.04
  are not drawn.
* **Tracing**: each crest is traced from its centre along the local crest-tangent field
  (perpendicular to the travel vector) in `TRACE_STEP_PX = 8` px midpoint steps, so it curves
  where the field turns and is straight where it is uniform. It stops at an invalid lattice
  cell or where it would turn more than `MAX_TURN_DEG = 25` in one step.
* **Height**: line width `clamp(0.75 + 0.55 Hs, 0.75, 3)` px and opacity
  `clamp(0.30 + 0.12 Hs, 0.30, 0.90)` (Hs in m), times the envelope weight. Opacity falls off
  toward the crest ends (full over the inner half of each half-crest, 0.6 to 78 %, 0.28 to the
  tip), so crests have no hard tips. Solid systems add a soft glow (3.2 x the width, 0.22 x the
  opacity, over the inner half of each half-crest) and a faint dark trough line (`#000814`,
  1.6 x the width, 0.24 x the opacity, 80 % of the crest length) half a spacing behind each
  crest, so the bands read as crests and troughs. Dashed and dotted systems get a dark halo
  under their dashes (1.6 px wider, 0.55 x the opacity, same dash pattern), so they stay
  visible on bright colours. Strokes are batched by system, kind (crest, glow, trough,
  halo), 5 width levels and 8 opacity levels.
* **Reuse**: crests move a fraction of a pixel per frame, so each crest's traced shape is kept
  per set and lattice crest number and drawn shifted along the travel vector; it is traced
  again once it has moved `RETRACE_PX = 2` px. A new lattice (pan, zoom, new fields) traces
  afresh.
* **Vertices**: a traced vertex is kept only where the crest has turned more than
  `SIMPLIFY_DEG = 1.5` since the last kept one, at the taper boundaries and at the ends (less
  than 0.35 px from the traced curve over a 100 px chord); where the field is straight the
  trace step doubles up to 32 px.

### 4.6 Ocean mask (both basemaps)

Crests are kept to the ocean in three ways:

1. Lattice nodes without data are invalid (land and sea ice are no data in the wave model),
   and so are lattice nodes over Natural Earth land (rasterised at lattice resolution: a node is
   land when more than 200/255 of its 8 px cell is land). No crest crosses an invalid cell, so
   crests stop within one lattice cell (8 px) of the coast in the crest canvas itself.
2. **Dark basemap**: the opaque land is drawn above the crest pane (section 2), so it clips
   the crests exactly at the Natural Earth coastline.
3. **Streets basemap** (no land drawn over the tiles): every frame is composited
   (`destination-in`) with an ocean mask built once per view: where any system has data (the
   lattice validity, soft edged) and not inside the Natural Earth land rings (crisp). This
   full-canvas composite costs about 4 ms a frame with software rendering, so it is skipped on
   the dark basemap where step 2 already clips (`data-ocean-mask` = 1 or 0).

E2E land probes read the crest canvas itself: alpha 0 in central Australia (25°S 135°E), the
central United States (40°N 100°W), the Sahara (23°N 10°E), central Africa and Kazakhstan, and
30 km inland of Perth at zoom 7, on both basemaps, with crests over the open ocean.

### 4.7 Reduced motion and draw budget

* Reduced motion: the same sets drawn once, still, with the full envelope, plus an arrowhead
  (7 px, white) on the leading crest of each primary or combined set pointing where the waves
  travel. Nothing animates (e2e: the canvas hash is unchanged over 1 s).
* Budget: at most `VERTEX_BUDGET = 20,000` vertices per frame for a 1440 x 900 view (scaled
  with the view area between 0.5 and 1.6 times), `PHONE_VERTEX_BUDGET = 6,000` below 640 px
  wide. The top system (primary, else combined) is planned first and may use 60 % of what is
  left, the next 60 % of the rest, the last one all of it. Sets are visited in a fixed random
  order (a hash per set), so a frame over budget leaves out an even, random share of the sets,
  never a region. The number of sets drawn only shrinks when the budget is hit and grows back
  by one set a frame while under 75 % of it, so the cut is steady (no set flickers at the
  margin); the vertices per crest come from earlier frames of the same system. A set that
  could overrun the frame's total is skipped.

### 4.8 Data attributes (tests and diagnostics)

`canvas.map-wave-canvas`: `data-mode` (animated, static, off), `data-families` (systems
drawn, in drawing order), `data-crests`, `data-vertices`, `data-strokes`, `data-drawn`,
`data-arrows` (still mode), `data-exaggeration` (E rounded), `data-view` (origin, size, world
px), `data-ocean-mask`, `data-draw-ms`, `data-draw-ms-p95` (last 120 frames) and
`data-lattice-ms`.
`canvas.map-particle-canvas`: `data-mode`, `data-color-type`, `data-trails`, `data-arrows`.

## 5. Field client and freshness

* Each field response carries `X-Field-Meta`. The client rejects a field whose `variable`,
  `valid_time` (compared as instants) or `run_key` differs from the request, and reads a 404 the
  same way: the field is never shown under the requested time (`FieldMismatchError`), and the
  field index is re-read (at most once every 30 s).
* The field index is re-read every 5 minutes (`FIELD_INDEX_REFETCH_MS`) and when the window
  regains focus (stale after 60 s), so a map left open picks up a new model run.
* Decoded fields are cached per (run, variable, valid time) in an LRU of
  `FIELD_STORE_CAPACITY = 48` fields and at most 64 MB: 12 wave fields per step (3 combined +
  9 partitions) for the step on screen and both neighbours, plus pressure and the wind overlay
  (the previous 24 evicted the step on screen, audit WAVEVIZ-7).
* Partitions are optional: one that fails to load is simply not drawn; the core variables of
  the layer are required.
* The legend shows the source, model cycle (UTC), the valid time of the fields on screen in
  the viewer's time zone with its abbreviation, the lead ("+30 h", or "analysis"), and
  "(forecast, not observed)".

## 6. Units

Wind speed in km/h (default), mph, kn or m/s: one preference for the site, stored under the
`localStorage` key `swell.windUnit` (the spot charts read and write the same key; the map
listens for the `storage` event and the `swell:wind-unit` event of the same tab). Heights
follow the account's ft/m preference. The legend title states the unit of the colour layer.

## 7. Measured (e2e, synthetic fields, headless Chromium, 1440 x 900, shared 4-CPU machine)

| Check | Result |
|---|---|
| Crest orientation, uniform swell from 225° at 12 s (structure tensor) | 45.0°, coherence 0.97 |
| Crest motion along the travel direction in 0.65 s | 5.0 px (expected about 5.9) |
| Crest spacing 8 s / 16 s | 12 px / 48 px (ratio 4.00) |
| Ink, Hs 1 m / 4 m | 8,456 / 30,571 |
| Orientation peaks, swell1 from 270° + swell2 from 180° | 92.5° and 177.5° (one peak, 92.5°, without swell2) |
| Crest frame draw time, p95 | 6.9 ms, 13,656 vertices |
| Basemap redraw at zoom 2 | 5.1 ms |
| Particle canvas after 5 s: alpha 5 / 6 / 7 counts, fade renderer (float16) | 11,859 / 9,483 / 9,000 (no spike at 6) |
| Same, explicit renderer (8-bit canvas) | 2,405 / 4,080 / 3,171 (no spike at 6) |

Main-thread cost per animation frame in headless Chromium (software raster) on a quiet
machine: crests 11.6 ms task (1.2 ms script) with the per-frame ocean-mask composite, 7.4 ms
without it (the dark basemap skips it, section 4.6); idle map 0.3 ms. Under the full e2e run on the shared machine the same layer took
39 ms task (3.8 ms script) and the wind particles 24 ms task (1.7 ms script). These include
software rasterisation that a GPU-backed canvas does off the main thread; no browser with a
GPU was available here.

## 8. Limitations

* The crests are stylised from the model's height, period and direction per system: no wave
  rays, refraction, shoaling or depth effects (deep water is assumed), no real wave groups,
  and the sets' positions are a drawing device. The 0.5° fields smooth fronts and coasts.
* Wind-sea crests often sit at the 8 px minimum spacing (periods under 6.5 s).
* Firefox and WebKit were not run here (their browsers are not installed in this sandbox):
  the 8-bit renderer is verified in unit tests and in Chromium forced to an 8-bit canvas.
