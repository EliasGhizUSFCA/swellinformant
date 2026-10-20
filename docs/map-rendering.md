# Map rendering: wind particles, the wave colour field and crest dashes

How the map draws its animated layers from the model fields: wind particles (the Wind layer,
and the optional wind overlay on the wave layers), the wave-height colour field, and short
crest dashes on the wave layers (Wave Height, Wave Energy, Wave Period). Every formula,
constant and scale factor used on screen is listed here. The data side (which NOAA records,
the 0.5° encoding, the run and valid-time contract) is in [map-layers.md](map-layers.md).

The dashes are **stylised crests from the forecast model, not individual waves**: they are
drawn from the model's significant height, period and direction of each wave system at the
selected valid time. Their spacing and speed are scaled to be visible (stated below and in
the legend), and their fading in and out and the short trail behind each dash are display
choices. Neither layer is an
observation, and neither is for navigation.

The look follows the owner's reference screenshot (Upgrade 4, decision D11: a smooth blue →
violet → magenta/pink wave-height field, grey land with the coastline, many short soft white
dashes drifting with the waves) with our own ramp, dash shapes and code: no third-party map
code, icons or palettes are used.

Code:

| Part | File |
|---|---|
| Wave physics, scales, dash sizes and styles (pure) | `frontend/lib/map-waves.ts` |
| Colour ramps (wave-height ramp in metres, shared ramp) | `frontend/lib/map-colors.ts` |
| Dash renderer (lattice, cells, dash lives, mask, animator) | `frontend/components/map/wavefronts.ts` |
| Wind particles | `frontend/components/map/particles.ts` |
| 8-bit-safe trail renderer | `frontend/components/map/trails.ts` |
| Canvases, panes, data attributes | `frontend/components/map/map-canvas-layers.tsx` |
| Basemap (land, coastline, borders, isobars, labels) | `frontend/components/map/basemap-draw.ts` |
| Field download, checks and cache | `frontend/lib/map-fields.ts`, `frontend/hooks/map-fields.ts` |
| Legend and settings | `frontend/components/map/legend-drawer.tsx`, `frontend/components/map/settings-popover.tsx`, `frontend/lib/map-settings.ts` |

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
| `fieldPane` (210) | colour field | painted at half resolution (2 CSS px per pixel), drawn onto a canvas of one pixel per CSS pixel with `imageSmoothingQuality = "high"` |
| `wavePane` (220) | crest dashes | wave layers only |
| `particlePane` (225) | wind particles | Wind layer, or a wave layer with the wind overlay |
| `basemapPane` (230) | Natural Earth land, coastline, borders, isobars, labels | dark basemap draws land |

* **Dark basemap, wave layers**: land is opaque grey (`LAND_COLOR = #3a3a3c`), drawn above the
  colour field and the dashes, so it also masks them at the coast. The coastline is the land
  rings stroked at `rgba(255, 255, 255, 0.35)`, 0.8 px (`COASTLINE_COLOR`,
  `COASTLINE_WIDTH_PX`). The grey is lighter than the deepest ocean colour (#0c1846) and
  neutral, so the land never reads as sea.
* **Dark basemap, Wind layer**: land is translucent (`rgba(58, 58, 60, 0.55)`, `LAND_ALPHA =
  0.55`), so the wind colour and particles over land stay visible (offshore flow at a coast
  matters for surf). Borders, isobars and labels are unchanged.
* **Streets basemap**: no land is drawn over the tiles, so the wave colour field is cut at the
  Natural Earth coastline (`destination-out` with the land rings) and the dashes are masked to
  the ocean (section 4.7). The wind colour is not cut (it is defined over land).
* The basemap rings are not closed with `closePath()`: `fill()` closes every subpath and the
  Natural Earth rings repeat their first point. One `closePath()` per ring (2,558 rings) cost
  214 ms of a 234 ms redraw at zoom 2 (audit MAPNAV-5); without it the e2e runs measured
  **5.1 to 9.4 ms** for the dark basemap at zoom 2, 1440 x 900 (`data-draw-ms` on
  `canvas.map-basemap-canvas`, guard < 30 ms; 6.2 ms in the final run).

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
  A particle that respawns leaves its old trail behind in a second slot, where it fades out
  with its age as it would on the dimming renderer, instead of vanishing at once.

The particle canvas reports `data-color-type` and `data-trails` ("fade" or "explicit"). E2E:
the alpha histogram after 5 s has no spike at 6 with either renderer (Chromium, plus Chromium
forced to an 8-bit canvas; the same test is tagged `@cross-browser` for Firefox and WebKit,
which this sandbox cannot run).

### 3.4 Reduced motion

With `prefers-reduced-motion: reduce` the particles are replaced by still arrows on a 46 px
grid, 7 to 18 px long (8 px per px-per-frame of flow), pointing downwind, drawn once.

## 4. Wave layers: colour field and crest dashes

### 4.1 Wave systems

| System | Variables | Dash (shape, colour) | Length factor | Width factor | Cell |
|---|---|---|---|---|---|
| Combined sea (peak), **the default** | `hs`, `tp`, `wave_dir` | solid, white `#ffffff` | 1 | 1 | 18 px |
| Primary swell | `swell1_hs/_tp/_dir` | solid, white `#ffffff` | 1 | 1 | 18 px |
| Secondary swell | `swell2_*` | long and split (a gap of 0.2 of its length in the middle), amber `#ffd59a` | 1.4 | 0.8 | 18 px |
| Wind sea | `windsea_*` | short and thin, pale cyan `#b8f0ff` | 0.55 | 0.7 | 20 px |

Systems are told apart by dash **shape** (solid, long split, short) and colour, never colour
alone; the legend and the settings chooser show a swatch of each one's dashes. Every piece
of every dash is at least twice as long as wide (unit test for 0.5 to 8 m), so its
orientation still reads. The combined
sea is drawn by default (`DEFAULT_WAVE_SYSTEMS = ["combined"]`, closest to the reference);
the primary, secondary and wind sea partitions are selectable in Settings when the model run
stores them (the field index lists them with `group: "partition"`; the client downloads a
partition only when it is chosen). The combined sea is an alternative to the partitions,
never drawn with them, and is labelled "Combined sea (peak)", never "primary swell". A system
the run does not have is disabled in the chooser ("Not available for this model run"); when
none of the chosen systems exists the combined sea is drawn. A missing partition is no data:
nothing is drawn for it and nothing is invented. Drawing order is wind sea, secondary,
primary (on top); the dash budget goes the other way (section 4.8).

The display settings live under the `localStorage` key `swell.map.settings.v2`. The key
moved from v1 with the dash renderer: a v1 object is read once into v2 with every field
(layer, units, basemap, animation, overlays) except `waveSystems`, which takes the new
default, and the v1 key is then removed (`readSettings`, `migrateV1`).

The wave model numbers partitions by height per cell, so neighbouring cells can hold
different swells. Where the four grid corners of a direction disagree by more than
`MAX_CORNER_SPREAD_DEG = 60`, the point has no direction (no averaged direction that no wave
has), and no dash is drawn there.

### 4.2 Physics

Linear wave theory, `omega^2 = g k tanh(k h)`, in deep water (`tanh(k h) -> 1` for `h > L/2`,
which holds where the global model's 0.25° to 0.5° cells are):

* wavelength `L = g T^2 / (2 pi)`, with `g = 9.80665 m/s^2`: 99.9 m at 8 s, 156.1 m at 10 s,
  224.8 m at 12 s, 399.6 m at 16 s;
* phase speed `c = L / T = g T / (2 pi)`: 15.6 m/s at 10 s. Crest lines are lines of constant
  phase and move at c;
* group speed `cg = c / 2` (energy of a wave group); the dashes make no group-speed claim.

T is the period the model gives for that system (`tp` is the peak period of the combined sea;
the partition periods are called "period" because NCEP's SWPER/WVPER are mean periods,
audit WAVEVIZ-10).

### 4.3 Crest spacing and its exaggeration

Dashes lie on crest lines `s` apart. On screen `s = clamp(S x L, 8, 64)` px with **S = 0.12 px
per metre of wavelength** (`crestSpacingPx`): 12.0 px at 8 s, 18.7 px at 10 s, 27.0 px at
12 s, 48.0 px at 16 s; periods under 6.5 s are drawn at 8 px and over 18.5 s at 64 px. The
spacing does not change with zoom, so it is an exaggeration of the true wavelength at that map
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
`L / (metres per pixel)` is drawn (8 to 256 px). Because the Mercator scale changes with
latitude, E does too (half the equator's value at 60°), so the legend states E for the zoom
and the latitude of the view centre, for example "Crest spacing ×4,700 at the map centre;
time runs 4× faster than real." (`scaleStatement`; E is rounded to the nearest 100 from
1,000, to a whole number from 10, else to one decimal). Where the spacing is clamped (periods
under 6.5 s or over 18.5 s) the drawn spacing is 8 or 64 px and E does not apply exactly.

### 4.4 Motion and its time scale

**Time runs `TIME_SCALE = tau = 4` times faster than real.** A crest line passes any fixed point
of the map once every `T / tau` seconds (2.5 s for a 10 s swell), at every zoom and also where
the spacing is clamped. Each dash moves with its crest line along the travel direction p at

`v = s x tau / T` px/s, which unclamped equals `S x c x tau` (0.75 T px/s: 6.0 px/s at 8 s,
7.5 at 10 s, 9.0 at 12 s, 12.0 at 16 s).

In screen distance this is `E x tau` times the true crest speed at that map scale; the "4×"
is the time scale, the spacing factor E comes on top. The animation clock only advances
while the layer animates (pauses on pan and zoom, resumes with the same clock), at most
`MAX_STEP_MS = 100` per frame, and frames are drawn at most 30 per second
(`FRAME_INTERVAL_MS`). Every dash position is a function of the clock alone: identical at 60
and 120 Hz (unit test).

### 4.5 The wave-height colour field

The Wave Height layer paints offshore significant wave height with its own ramp, anchored in
metres (`WAVE_HEIGHT_RAMP`, `lib/map-colors.ts`), interpolated linearly in metres (in sRGB)
between anchors and saturating at the end colours: below 0 m #0c1846, above 10 m #f9d0e0.
CIE L* (sRGB, D65) rises strictly, so a larger sea is always lighter (the scale also reads in
grey) and neighbouring anchors differ by ΔE*ab ≥ 5.4:

| Hs (m) | 0 | 0.5 | 1 | 1.5 | 2 | 2.5 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Colour | #0c1846 | #132e78 | #1b48a6 | #2a5cc2 | #4363cf | #5a62d6 | #7360d6 | #925ad0 | #ad55c4 | #c556b4 | #d860a6 | #e6729f | #f19ebd | #f9d0e0 |
| L* | 10.4 | 21.8 | 33.2 | 41.3 | 45.1 | 46.7 | 48.1 | 49.5 | 51.1 | 53.8 | 57.8 | 62.8 | 74.1 | 87.3 |

Deep navy and blue for small seas, violet around 3 to 4 m, magenta from 5 m, pink above 8 m.
(A first draft had L* flat at 45 to 46 between 2 and 5 m and was rejected.) `metresToStops`
places the anchors on the layer's display range (0 to 10 m from the field index); a wider
range moves the stops, never the colour of a given height. The legend ticks are 0, 1, 2, 3,
4, 6, 8 and 10 m, or 0, 5, 10, 15, 20, 25 and 30 ft. Wave Period, Wave Energy and Wind Speed
keep the shared ramp (`RAMP`). The field is painted at half resolution and drawn onto the
canvas with `imageSmoothingQuality = "high"` (section 2).

### 4.6 Crest dashes

* **Lattice**: per view, values are sampled every `LATTICE_PX = 8` px on a grid fixed in world
  pixels: per system the travel vector p (TOWARD = FROM + 180°), spacing, speed, height and a
  validity flag. A node is invalid without data (NaN height, period or direction), with
  `Hs < MIN_DRAW_HS_M = 0.2 m`, where the direction corners disagree (section 4.1) or over land
  (4.7). Sampling between nodes is bilinear and needs all four nodes valid.
* **Cells**: one dash per cell of a grid fixed in world pixels, `DASH_CELL_PX` on screen (18 px
  for the combined sea, the primary and the secondary swell, 20 for the wind sea) at every zoom,
  so the density on screen is the same at every zoom (the cell is adjusted so that a whole
  number of cells spans the world: 17.96 px at zoom 2). The cells cover the view plus a margin
  of 3 crest spacings and a dash length (how far a dash travels in its life), so dashes born
  just outside the view drift into it.
* **Life**: a cell's dash lives `DASH_LIFE_PASSAGES = 3` crest passages, `3 T / tau` seconds
  (7.5 s at 10 s), then the cell's next dash is born. A cell's first dash is placed from the
  clock alone: life number `floor(t / life + phase)` with a hashed phase per cell, so the
  result never depends on the frame rate or on when the map was opened; later lives follow
  one another. A new forecast step keeps every live dash, which ends its life with the values
  it was born with; the next dash of the cell is born from the new fields.
* **Birth on the crest lattice**: a dash is born at the first of `BIRTH_TRIES = 4` hashed places
  in its cell where its system has data (a cell that is partly land, sea ice or a partition
  swap still gets its dash), sampled from the model grid itself. Its centre is then moved
  along p (by at most s / 2) onto the nearest crest line of the lattice `p . W = n s + v t`
  (W in world px of the view's continuous pixel plane, not wrapped to one world copy, so there
  is no seam at the antimeridian). It then moves along p at v: every dash of a uniform sea stays
  on those crest lines, so rows of dashes are one crest spacing apart and move with the crests
  (unit tests: centres on multiples of s within 1 %, row spacing within 3 % for 8, 10 and 16 s,
  displacement v Δt within 2 %).
* **Orientation**: along the crest, perpendicular to the travel vector sampled where the dash
  is now (a quarter turn clockwise of p on screen): vertical dashes for a swell from the west,
  NW-SE dashes for a swell from the SW (unit tests within 1°).
* **Visibility**: a dash is drawn only where its centre and both ends sample all-valid lattice
  nodes, so no dash sits on land or where the model has no data.
* **Life fade (a display choice)**: opacity × `smoothstep(age / 0.2) x smoothstep((1 - age) /
  0.3)`: in over the first 20 % of the life, out over the last 30 %. It only keeps dashes from
  popping in and out; it is not a wave group and makes no group-speed claim.
* **Size and opacity from the local Hs** (m): length `clamp(5 + 1.5 Hs, 6, 14)` px times the
  system's length factor (1; 1.4 secondary; 0.55 wind sea); core width `clamp(1.8 + 0.3 Hs,
  1.8, 3.4)` px times its width factor (1; 0.8; 0.7); opacity `clamp(0.40 + 0.09 Hs, 0.40,
  0.90)` (D11's formula) times the life fade. Higher seas draw longer, wider, brighter dashes
  (e2e: more than twice the ink at 4 m as at 1 m).
* **Soft edge**: each dash is two round-capped strokes of the same segment, bottom to top: a
  halo 1.7 x the core width at 0.45 x its opacity, and the core (`DASH_PASSES`). A split dash
  (secondary) is two segments with a gap of 0.2 of the length between the core's caps.
* **Motion trail (a display choice)**: under each dash of the combined sea, the primary swell
  and the wind sea (`FamilyStyle.trail`; the secondary swell's split dashes have none, so their
  shape stays distinct under the primary's trails), the band its crest swept over in the
  last `TRAIL_SECONDS = 1` s of animation (0.25 s of real time at tau = 4), against the travel
  direction: the dash moves away from its trail, so a still frame also shows where the waves
  go. Length `min(v x 1 s, 0.5 s, 16 px)` (`trailLength`; `TRAIL_MAX_SHARE = 0.5` of the crest
  spacing keeps rows of dashes apart, `TRAIL_MAX_PX = 16` holds when zoomed in to true scale,
  where crests move fast): 6.0 px at 8 s, 7.5 px at 10 s, 12.0 px at 16 s at the default
  spacing. While animating it reaches back no further than the dash's birth (`v x age`), so a
  young dash's trail grows in; the still frame (reduced motion) draws it whole. It is as wide
  along the crest as the dash with its round caps (length + core width) and fades backwards in
  `TRAIL_STEPS`: the whole band at 0.25 x the dash's opacity and its front half at 0.25 x
  again, butt-capped and stacked, so the front half shows at 1 - 0.75^2 = 0.44 x and the back
  half at 0.25 x. The trail is drawn only where its back corners and the ends and middle of
  each step's centre line sample all-valid lattice nodes (else the dash is drawn without one),
  and from 3.5 px. It carries no wave data of its own: it is how the dash moved.
* **Batching**: core strokes are batched by system, 5 core-width levels (1.2, 1.75, 2.3, 2.85,
  3.4 px) and 8 opacity levels (0.06 to 0.90); each batch's path is built once and stroked
  once per pass, so a dash costs two path vertices (a split dash four). Trails are batched by
  system, 7 trail-length levels (4, 6, ..., 16 px: the length is rounded to the nearest) and the
  dash's opacity level, with one path per step: two vertices per step, so a solid dash with its
  trail costs 6 vertices. Per system, the trails are stroked first (butt caps), then the halos
  and the cores (round caps).

**Departures from D11's first numbers.** D11 proposed dashes `clamp(6 + 3 Hs, 6, 22)` px long,
`clamp(1.0 + 0.3 Hs, 1.0, 2.6)` px wide at opacity `clamp(0.40 + 0.09 Hs, 0.40, 0.90)` with one
halo (2.4 x the width at 0.28 x the opacity) on 26 / 34 / 30 px cells, and a secondary dash 0.85
x as long with a gap of 0.3 on the sparser 34 px cells. Drawn on the real NOAA GFS-Wave fields
of 2026-10-16 00Z off California (zoom 6, Hs about 2 m) and in the Southern Ocean (Hs 5 to 7 m),
and compared with the owner's screenshot, those dashes read as long thin hairlines (22 px at 5
m), sparser than the reference, and a single halo at 0.28 showed either nothing or a hard ring.
The values above (shorter, wider, softer, denser) were chosen by that comparison. The secondary
dash is longer and thinner than proposed because its split halves came out as round dots at 1 to
2 m (the e2e orientation test found no peak for a secondary swell from the south; with these
values that peak holds 0.19 of the gradient energy against the test's 0.15, the primary's 0.41).
Every physical statement (orientation, spacing, speed, time scale, masking) is unchanged, and
the vertex budget is kept by stroking each batch's path once per pass.

**The trail (2026-10-20).** D11 had no trail. Next to the reference, whose dashes are short
bright crest edges with a soft fading smear behind them, the bare dashes read as crisp thin
grains; the two-step trail gives the soft, drifting look. It was fitted to D11's checks rather
than the other way round: with trails on every system and both halos (2.6 x at 0.22 and 1.7 x
at 0.45) the e2e frame draw p95 rose from 2.4-3.6 ms to 4.7-6.7 ms on this shared machine
(above D11's 5 ms), the secondary swell's orientation peak fell to 0.15 (the test needs more),
and the ink ratio 4 m / 1 m to 2.03. So the outer halo, the largest stroke of a dash and most
of its raster time, was dropped (D11 itself had one halo); the opacity went back to D11's
`0.40 + 0.09 Hs` (ink 2.17); the secondary's split dashes have no trail (peak 0.20); the
frame keeps to 9,000 vertices (section 4.8); and the bands went from 0.30 to 0.25 x the dash's
opacity, which lifts the orientation coherence of a uniform swell from 0.60 / 0.56 (desktop /
Pixel 7) to 0.64 / 0.60 against the test's 0.5 (a trail's sides run across the crest). An
attempt to draw a dash and its trail as one dash-patterned stroke (two vertices a dash) cost
more, not less: Chrome dashes the path to record it (6.8-7.9 ms). Screenshots: section 7.

### 4.7 Ocean mask (both basemaps)

Dashes are kept to the ocean in three ways:

1. Lattice nodes without data are invalid (land and sea ice are no data in the wave model),
   and so are lattice nodes over Natural Earth land (rasterised at lattice resolution: a node is
   land when more than 200/255 of its 8 px cell is land). A dash needs its centre and both ends
   on valid nodes (section 4.6), so dashes stop within about one lattice cell of the coast in
   the dash canvas itself.
2. **Dark basemap**: the opaque land is drawn above the dash pane (section 2), so it clips the
   dashes exactly at the Natural Earth coastline.
3. **Streets basemap** (no land drawn over the tiles): every frame is composited
   (`destination-in`) with an ocean mask built once per view: where any system has data (the
   lattice validity, soft edged) and not inside the Natural Earth land rings (crisp). This
   full-canvas composite costs about 4 ms a frame with software rendering, so it is skipped on
   the dark basemap where step 2 already clips (`data-ocean-mask` = 1 or 0).

E2E land probes read the dash canvas itself: alpha 0 in central Australia (25°S 135°E), the
central United States (40°N 100°W), the Sahara (23°N 10°E), central Africa and Kazakhstan, and
30 km inland of Perth at zoom 7, on both basemaps, with dashes over the open ocean.

### 4.8 Reduced motion and draw budget

* Reduced motion: one still frame (the dashes of the clock at 0) at full opacity for their
  height (no fade), plus an arrowhead on every `ARROW_EVERY = 6`th dash of the primary swell or
  the combined sea: a white chevron (1.4 px, opacity 0.85) `ARROW_PX = 5` px long and 7 px
  wide, just ahead of the dash's halo, pointing where the waves travel; the trails are drawn
  whole. Nothing animates (e2e: the canvas hash is unchanged over 1 s, with more than 3
  arrowheads).
* Budget: at most `MAX_DASHES = 4,000` dashes per frame, `PHONE_MAX_DASHES = 1,500` below
  640 px wide (`maxDashes`), and at most `MAX_FRAME_VERTICES = 9,000` vertices (under D11's
  12,000; enforced in the renderer since the trails: a frame stops placing dashes when the next
  one, at most 8 vertices, could pass it; about 1,500 trailed dashes, which keeps the busiest
  desktop view, the whole world at 1440 x 900, within D11's 5 ms, section 7; phone views stay
  well under it). The top system (primary, else combined) is planned first and may use 60 %
  (`BUDGET_SHARE`) of the dashes and of the vertices left, the next 60 % of the rest, the last
  one all of it. Cells are visited in a fixed hashed order, so a frame over budget leaves out an even, random share of
  the cells, never a region. The number of cells visited only shrinks when a budget is hit and
  grows back by one cell a frame while under 75 % of both, so the cut is steady. Targets (D11,
  e2e at 1440 x 900): draw p95 ≤ 5 ms and ≤ 12,000 vertices.

### 4.9 Data attributes (tests and diagnostics)

`canvas.map-wave-canvas`: `data-style` (`dashes`), `data-mode` (animated, static, off),
`data-families` (systems drawn, in drawing order), `data-crests` (dashes drawn),
`data-dash-trails` (dashes drawn with a trail), `data-vertices`, `data-strokes`, `data-drawn`,
`data-arrows` (still mode), `data-exaggeration` (E rounded), `data-view` (origin, size, world px), `data-ocean-mask`, `data-draw-ms`,
`data-draw-ms-p95` (last 120 frames), `data-frames` (frames drawn) and `data-lattice-ms`. The
e2e frame-cost test (`e2e/map-performance.spec.ts`, run without the Playwright trace recorder)
reads the p95 once more than 160 frames are drawn.
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
  the viewer's time zone with its abbreviation, and the lead: "+30 h ... (forecast, not
  observed)", or at the cycle time itself "model analysis (not observed)".

## 6. Units

Wind speed in km/h (default), mph, kn or m/s: one preference for the site, stored under the
`localStorage` key `swell.windUnit` through the one implementation in `frontend/lib/units.ts`
(`storedWindUnit` / `writeWindUnit` / `useWindUnit`), which the spot charts and the map
settings share; the map listens for the `storage` event and the `swell:wind-unit` event of
the same tab. Heights
follow the account's ft/m preference. The legend title states the unit of the colour layer.

## 7. Measured (e2e, synthetic fields, headless Chromium, 1440 x 900, shared 4-CPU machine)

Full e2e run with the trailed crest dashes (2026-10-20, chromium and Pixel 7 projects, 127
tests passed; the machine is shared with another agent's test runs, so times vary from run to
run). The dashes without trails (2026-10-19 run) and the traced-crest renderer (2026-10-15) are
given for comparison.

| Check | Trailed dashes (2026-10-20) | Dashes, no trail (2026-10-19) | Traced crests (2026-10-15) |
|---|---|---|---|
| Orientation, uniform swell from 225° at 12 s (structure tensor) | 44.9°, coherence 0.64 (Pixel 7: 45.0°, 0.60) | 45.0°, 0.80 (0.78) | 45.0°, 0.98 |
| Motion along the travel direction | 7.0 px in 0.81 s (expected about 7.3); Pixel 7: 6.0 px in 0.60 s (about 5.4) | 5.0 px in 0.61 s | 5.0 px in 0.61 s |
| Crest spacing 8 s / 16 s (ink autocorrelation) | 12 px / 48 px (ratio 4.00; D11 allows 4.0 ± 0.2) | 12 / 48 px | 12 / 48 px |
| Ink, Hs 1 m / 4 m | 30,266 / 67,118 (2.22 x; D11 asks > 2 x) | 32,237 / 70,395 (2.18 x) | 10,117 / 35,411 |
| Orientation peaks, swell1 from 270° + swell2 from 180° | 92.5° (0.38 of the energy) and 177.5° (0.20; threshold 0.15); 87.5° alone without swell2 | 92.5° (0.41) and 2.5° (0.19) | 92.5° and 2.5° |
| Frame draw time p95 once settled (combined sea, whole world, no trace recorder) | 4.7 ms, 1,549 dashes, 8,676 vertices (D11: ≤ 5 ms, ≤ 12,000; the 9,000-vertex budget cuts this view) | 2.6 ms, 2,111 dashes, 4,208 vertices | 3.8 ms, 9,531 vertices |
| Main-thread cost per animation frame | 2.87 ms script (32.0 ms task) | 2.06 ms script (37.6 ms task) | 2.7 ms script (33.6 ms task) |
| Basemap redraw at zoom 2 | 4.4 ms | 4.1 ms | 6.2 ms |
| Colour-field redraw at 1440 x 900 (median of 6 pans) | 14.9 ms (10.7 to 24.8) | 18.0 ms | 16.8 ms |
| Particle canvas after 5 s: alpha 5 / 6 / 7 counts, fade renderer (float16) | 11,431 / 9,652 / 7,903 (no spike at 6) | 11,546 / 9,305 / 8,376 | 11,290 / 8,922 / 8,647 |
| Same, explicit renderer (8-bit canvas) | 3,721 / 5,534 / 3,577 | 3,284 / 5,323 / 3,697 | 4,071 / 6,423 / 4,012 |
| Field cache after 48 downloads | array buffers 23.0 -> 56.3 MB (cache at most 49.9 MB decoded) | 22.9 -> 56.2 MB | 23.5 -> 56.7 MB |

Wind particles in the same run: 1.25 ms script (20.8 ms task) per frame; idle map 0.06 ms
(0.31 ms task). "Task" includes the software rasterisation of headless Chromium, which a
GPU-backed canvas does off the main thread; no browser with a GPU was available here.

The frame draw p95 is the measure closest to its limit, and the noisiest: the same build gave
3.8 ms in a map-only e2e run an hour earlier, and the dashes without trails measured 2.4 to
3.6 ms in interleaved runs on the development server when the trails were being fitted (with
trails, both halos and no vertex budget: 4.7 to 6.7 ms). `MAX_FRAME_VERTICES` is the knob: a
lower budget thins busy desktop views evenly and lowers the cost in proportion.

Coherence is lower than for long traced crests because a short dash has round ends and its
trail has sides, whose edges run across the crest as well as along it. The 0.5 threshold of
the e2e check is unchanged.

**Visual QA on real fields.** NOAA GFS-Wave + GFS fields of the 2026-10-16 00Z run (the 9
stored hours of a clone of the `swell_u3i_live_run` database), served by this branch's backend
to a production build, screenshots at 1440 x 900 and in Pixel 7 emulation, compared with the
owner's reference screenshot. Dashes drawn (with a trail), and the 120-frame draw p95 with the
median frame in brackets, once settled (SwiftShader GL, machine shared):

| View | Desktop 1440 x 900 | Pixel 7 |
|---|---|---|
| California, zoom 6, centre 36.5°N 123°W (Hs about 2 m: blue) | 1,507 (1,463), 8,866 vertices: 5.0 ms (2.7) | 545 (522): 1.3 ms (0.9) |
| California, zoom 5 (violet to magenta offshore) | 1,516 (1,450): 3.2 ms (2.0) | |
| Gulf of Alaska storm, zoom 5 (Hs 4 to 7 m: violet, magenta, pink) | 1,552 (1,462): 4.2 ms (2.9) | 777 (719): 2.1 ms (1.6) |
| Pacific, zoom 2 (Pixel 7: its whole-world view) | 1,460 (1,341): 5.9 ms (3.5) | 323 (279): 1.4 ms (1.1) |
| California, zoom 6, primary + secondary + wind sea | 1,689 (1,093; the secondary has no trail), 8,820 vertices: 4.7 ms (3.4) | |
| California, zoom 6, reduced motion | still: 1,522 dashes with full trails, 254 arrowheads | |

Without trails the same views drew 1,735 (California, zoom 6), 2,192 (Alaska) and 3,914
(partitions) dashes at 2.1, 8.3 and 10.2 ms: the vertex budget now cuts the busy desktop views
by about 12 % (California) to 30 % (Alaska), evenly, and keeps their cost down; phone views are
not cut.

The field reads as a smooth navy-blue-violet-magenta-pink surface, the land is grey with a
light coastline, and many short, soft white dashes lie across the swell with a bright leading
edge and a fading smear behind, drifting with it: the character of the reference, drawn with
our own ramp, shapes and code. The California coast that day had about 2 m of sea, so it is
blue on our ramp where the reference (a bigger day) is magenta; the colours follow the metre
anchors, not the reference. The reference, a phone screenshot, is denser (about 3.8 blobs per
1,000 CSS px squared, each about 30 CSS px squared above the local background, against our
1.8 and about 40 on a desktop at zoom 6, counted the same way on the screenshots): ours are
fewer and larger, within the frame budget. The screenshots are attached to the Upgrade 4 package report, not committed.

## 8. Limitations

* The dashes are stylised crests from the model's height, period and direction per system: no
  wave rays, refraction, shoaling or depth effects (deep water is assumed), no real wave
  groups, and the cells, births, the life fade and the trails are a drawing device. A trail is
  the dash's own path over the last second of animation, not a wake and not a measure of
  anything. The 0.5° fields smooth fronts and coasts.
* Wind-sea dashes often sit on rows at the 8 px minimum spacing (periods under 6.5 s).
* In a uniform sea, dashes of neighbouring cells line up on the same crest rows, which shows
  the crest spacing; in a real sea, where period and direction vary, rows are short.
* Firefox and WebKit were not run here (their browsers are not installed in this sandbox):
  the 8-bit particle renderer is verified in unit tests and in Chromium forced to an 8-bit
  canvas.
* Where the swell turns fast (around storms, at partition swaps of the peak direction) no dash
  is drawn rather than one along a direction no wave has, so such areas show gaps.
* The dash sizes and density were tuned by eye on one real run (GFS-Wave 2026-10-16 00Z) and
  synthetic fields against one reference screenshot. A busy desktop view (the whole world, a
  storm at zoom 5, California at zoom 6 at 1440 x 900) reaches the 9,000-vertex budget, about
  1,500 trailed dashes, and leaves out an even share of the cells, so it is somewhat sparser
  than the cell grid (section 7); phone views stay under it.
* Without a GPU (headless Chromium here) the dash layer keeps the main thread busy with
  software rasterisation of its soft strokes (about 38 ms of task time per frame).
* With the combined sea drawn (the default), the partition fields are not downloaded, so the
  hover readout lists partitions only when they are chosen in Settings.
