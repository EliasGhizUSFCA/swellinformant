# Map rendering: wind particles, the wave colour field and crest dashes

How the map draws its animated layers from the model fields: wind particles (the Wind layer,
and the optional wind overlay on the wave layers), the wave-height colour field, and short
crest dashes on the wave layers (Wave Height, Wave Energy, Wave Period). Every formula,
constant and scale factor used on screen is listed here. The data side (which NOAA records,
the 0.5° encoding, the run and valid-time contract) is in [map-layers.md](map-layers.md).

The dashes are **stylised crests from the forecast model, not individual waves**: they are
drawn from the model's significant height, period and direction of each wave system at the
selected valid time. Their spacing and speed are scaled to be visible (stated below and in
the legend), and their fading in and out and the smear behind each dash are display
choices. Neither layer is an observation, and neither is for navigation.

The look follows the owner's reference screenshot (Upgrade 4, decision D11, polished at
integration): a smooth, slightly dusky wave-height field (slate blue for small seas, violet
to magenta-purple for typical 2-3 m seas, pink for big ones), mid-grey land with a dark
coastline, and many short, soft blobs, each a lighter tint of the colour under it, drifting
with the waves. It is drawn with our own ramp, shapes and code: no third-party map code,
icons or palettes are used, and the screenshot is a style reference only.

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

* **Dark basemap, wave layers**: land is opaque mid grey (`LAND_COLOR = #808080`, L* 53.6,
  neutral: the owner's reference's land, sampled at every land point of the screenshot),
  drawn above the colour field and the dashes, so it also masks them at the coast.
  The coastline is the land rings stroked dark, `rgba(24, 24, 28, 0.8)` at 1 px
  (`COASTLINE_COLOR`, `COASTLINE_WIDTH_PX`), and country borders are a faint dark line
  (`BORDER_COLOR = rgba(30, 30, 34, 0.35)`, 0.7 px). The grey has no hue, so the land never
  reads as sea; it is lighter than seas up to about 5.5 m (L* 12 to 51.5 up to 5 m) and
  darker than bigger ones. Country and city labels are near-white (`#e8e8e8`, `#f4f4f4`) on a dark halo
  (`rgba(0, 0, 0, 0.6)`, 3 px), and the city dot (`#ededed`) has a thin dark ring, so they
  stay legible on the grey land and on the sea. (Upgrade 4 first drew #3a3a3c land with a
  light coastline, then #737376, L* 48.5, darker than the reference side by side; the
  reference has #808080 land outlined dark.)
* **Dark basemap, Wind layer**: land is translucent (`rgba(128, 128, 128, 0.55)`, `LAND_ALPHA =
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

| System | Variables | Dash (shape, tint) | Length factor | Depth factor | Cell |
|---|---|---|---|---|---|
| Combined sea (peak), **the default** | `hs`, `tp`, `wave_dir` | soft blob, a light tint of the field | 1 | 1 | 12 px |
| Primary swell | `swell1_hs/_tp/_dir` | soft blob, a light tint of the field | 1 | 1 | 12 px |
| Secondary swell | `swell2_*` | long thin bar split by a gap of 0.2 of its length, amber (`#ffcf85`, 75 % into the tint) | 2.5 | 1 (bar width) | 13 px |
| Wind sea | `windsea_*` | short blob, cyan (`#9cecff`, 75 % into the tint) | 0.65 | 0.8 | 18 px |

Systems are told apart by dash **shape** (soft blob, long split bar, short blob) and colour,
never colour alone; the legend and the settings chooser show a swatch of each one's dashes
on a patch of a typical (2.5 m) sea. A blob of a 1 to 3 m sea of 10 s or more reaches at
least as far along its travel as along its crest, as the reference's blobs do, and each
piece of a split bar is at least twice as long as it is wide (unit tests for 0.5 to 8 m and
6 to 18 s). A blob's crisp leading edge runs along the crest and its smear fades behind it,
so its travel reads from the bright front (e2e: the direction of the brightness falloff,
section 7); a bar's orientation reads from its long edges. The combined
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
between anchors and saturating at the end colours: below 0 m #151d42, above 10 m #f3c4dc.
CIE L* (sRGB, D65) rises strictly, by at least 2 per anchor, so a larger sea is always
lighter (the scale also reads in grey); neighbouring anchors differ by ΔE*ab ≥ 7.6; the hue
turns steadily from blue (h ≈ -68°) to pink (h ≈ -15°); and CIE chroma stays at or under 62
(muted, not electric):

| Hs (m) | 0 | 0.5 | 1 | 1.5 | 2 | 2.5 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Colour | #151d42 | #212f68 | #344790 | #4a4ea6 | #614fb0 | #7a4eb2 | #8f4db0 | #a64fab | #b856a8 | #c862a9 | #d572ae | #df86b8 | #e9a3c7 | #f3c4dc |
| L* | 12.2 | 21.4 | 32.4 | 37.2 | 40.0 | 42.5 | 44.6 | 47.8 | 51.5 | 56.0 | 61.0 | 66.7 | 74.6 | 83.8 |

Muted navy for calm seas, slate blue at 1 m, blue-violet at 1.5 m, violet at 2 m,
magenta-purple at 2.5 to 3 m, magenta at 4 to 5 m, pink at 6 to 8 m and pale pink at 10 m.
The first Upgrade 4 ramp (saturated blue up to 2.5 m, #4363cf at 2 m) was re-anchored at
integration: a typical 2 to 3 m sea read electric blue where the reference is violet to
magenta. `metresToStops` places the anchors on the layer's display range (0 to 10 m from
the field index); a wider range moves the stops, never the colour of a given height. The
legend ticks are 0, 1, 2, 3, 4, 6, 8 and 10 m, or 0, 5, 10, 15, 20, 25 and 30 ft. Wave Period,
Wave Energy and Wind Speed keep the shared ramp (`RAMP`). The field is painted at half
resolution and drawn onto the canvas with `imageSmoothingQuality = "high"` (section 2).

### 4.6 Crest dashes

* **Lattice**: per view, values are sampled every `LATTICE_PX = 8` px on a grid fixed in world
  pixels: per system the travel vector p (TOWARD = FROM + 180°), height and a validity flag
  (and the largest crest spacing), and for the dash tint the level of the colour field at the
  node. A node is invalid without data (NaN height, period or direction), with
  `Hs < MIN_DRAW_HS_M = 0.2 m`, where the direction corners disagree (section 4.1) or over land
  (4.7). Sampling between nodes is bilinear and needs all four nodes valid.
* **Cells**: one dash per cell of a grid fixed in world pixels, `DASH_CELL_PX` on screen (12 px
  for the combined sea and the primary swell, 13 for the secondary, 18 for the wind sea) at
  every zoom, so the density on screen is the same at every zoom (the cell is adjusted so that
  a whole number of cells spans the world: 11.97 px at zoom 2). The cells cover the view plus
  a margin of 3 crest spacings and a dash length (how far a dash travels in its life), so
  dashes born just outside the view drift into it. (18 px cells until integration: the
  reference has about 4 blobs per 1,000 CSS px², section 7.)
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
  on those crest lines, so in an even swell (the test fixtures) rows of dashes are one crest
  spacing apart and move with the crests (unit tests: leading edges on multiples of s within
  1 %, row spacing within 3 % for 8, 10 and 16 s, displacement v Δt within 2 %). In served
  model fields the rows do not form (section 8): the phase p · W is counted from the world
  origin, so neighbours whose period differs by about 0.1 s or whose direction differs by a
  fraction of a degree snap to unrelated rows, and the dashes sit at random phases, as in the
  reference.
* **Orientation**: along the crest, perpendicular to the travel vector sampled where the dash
  is now (a quarter turn clockwise of p on screen): vertical dashes for a swell from the west,
  NW-SE dashes for a swell from the SW (unit tests within 1°).
* **The blob** (combined sea, primary swell, wind sea): one soft blob per dash, a short piece
  of a crest line smeared back along the way it came. Its crisp, bright **leading edge lies on
  the crest line** (that is the point on the lattice above) and runs along the crest; its
  body is the band that crest swept over in the last `SMEAR_SECONDS = 0.55` s of animation,
  behind it (against the travel), fading towards the trailing side like a motion blur. Its
  opacity across the depth (0 = the leading edge, 1 = the back) follows `BLOB_PROFILE`, a
  linear gradient through 0.8 at 0, 1 at 0.06, 0.9 at 0.55, 0.5 at 0.85 and 0 at 1 (unit test:
  the front half carries more than 1.5 times the back half). The life fade and its opacity
  multiply it. The blob is a rectangle, not sheared like the reference's parallelograms: the
  smear runs along the travel, which is perpendicular to the crest, and a sheared blob would
  show a travel direction the model does not have.
* **Size and opacity from the local Hs** (m): length along the crest `clamp(4 + 0.7 Hs, 4.5,
  7.2)` px (`DASH_BASE_PX`, `DASH_PX_PER_M`, `DASH_MIN_PX`, `DASH_MAX_PX`) times the
  system's length factor (1; 2.5 secondary; 0.65 wind sea); depth along the travel `min(2 +
  v x 0.55 s, 0.5 s, 1.6 x length)` px (`BLOB_HEAD_PX`, `SMEAR_SECONDS`, `SMEAR_MAX_SHARE`
  of the crest spacing, so the blobs of one crest never reach the next, and
  `BLOB_MAX_DEPTH_SHARE` of the length) times the depth factor (1; 0.8 wind sea). The smear
  is speed x time, so the depth does not grow with the height. Examples at the default
  spacing (along the crest x along the travel): 1 m, 8 s: 4.7 x 5.3 px; 1.5 m, 12 s: 5.05 x
  6.9 px; 2 m, 10 s: 5.4 x 6.1 px; 3 m, 12 s: 6.1 x 6.9 px; 5 m, 12 s: 7.2 x 6.9 px; 5 m, 16
  s: 7.2 x 8.6 px. Like the reference's blobs (about 5 CSS px along the crest by 7 along the
  travel, the smear on the long axis for about 80 % of them, measured on the owner's
  screenshot), a blob of a 1 to 3 m sea of 10 s or more is at least as deep as it is long: a
  short crest piece streaking forward, not a tile lying across the swell (until the final
  review the blobs were 1.5 times longer along the crest than deep, rotated 90° from the
  reference). While animating a blob's depth reaches back no further than its birth,
  `min(depth, 2 + v x age)`, so a young blob's smear grows in; the still frame (reduced
  motion) draws it whole. Opacity `clamp(0.40 + 0.14 Hs, 0.40, 0.95)` times the life fade,
  drawn rounded to `ALPHA_STEP = 0.95 / 23 = 0.041` (24 levels; about 6 grey levels of the
  tint, finer than the fade moves in two frames). D11's formula was `clamp(0.40 + 0.09 Hs,
  0.40, 0.90)`; it is steeper since the final review because the depth no longer grows with
  the height, so that higher seas still draw more than twice the ink per dash at 4 m as at 1
  m (e2e, measured 2.14 to 2.22 times; the review's proposed 0.45 + 0.13 Hs with blobs of
  4.2 + 0.55 Hs px measured 1.97 and 2.02 times), and more in all. Higher seas draw longer,
  brighter blobs, never shallower ones.
* **The secondary swell's bar**: a thin bar centred on the crest line, `clamp(1.8 + 0.3 Hs,
  1.8, 3.4)` px wide (`dashWidth`) and 2.5 x the length long (as long as before the blobs
  were made shorter, within 4.5 %), split by a transparent gap of 0.2 of its length in the
  middle; no smear, so its shape stays distinct under the primary's blobs (its two crisp
  long edges also carry its orientation, e2e section 7).
* **Tint**: each dash is a lighter tint of the colour field under it (the colour layer of
  the wave layer on screen: Wave Height, Wave Energy or Wave Period), never pure white:
  every channel of the field colour is lifted by `TINT_LIFT = 95` (of 255) but keeps at
  least `TINT_HEADROOM = 0.02` of its way to 255 (`dashTint`). A blob over a violet 2 m sea
  (#614fb0) is tinted (192, 174, 253), over a pink 8 m sea (#df86b8) (254, 229, 254).
  Measured on the owner's reference, the heads of its blobs are the field colour plus about
  70 to 90 in every channel alike (a lift, not a mix with white, which would grey them). The
  lift is therefore about the same in every channel up to 3 m (blue at least three quarters
  of red's and green's, unit test), so a blob keeps the hue of its sea: until the final
  review `TINT_LIFT = 150` and `TINT_HEADROOM = 0.1` lifted red and green about twice as
  much as blue (measured (76, 79, 41) over a 1.5 m sea), which greyed the blobs. Drawn at
  the dash opacity a head is lifted by about 51 per channel at 1 m, 58 at 1.5 m, 71 at 2.5 m
  and 78 to 90 at 3 to 4 m (measured on the real fields of the final review: (58, 57, 51)
  over a 1.5 m sea, (76, 78, 65) over a 3 to 4 m sea, against the reference's (74, 79, 71)
  over its magenta and (89, 90, 83) over its blue sea): 3 to 4 m heads match the
  reference's, 1 to 1.5 m heads are about a third fainter, because D11's ink rule keeps low
  seas fainter. The field colour comes from the lattice: each node holds the level of the
  colour scale under it in `TINT_LEVELS = 32` steps (`fieldPositionAt` in
  `lib/map-render.ts`, the same bilinear value the colour canvas paints), and a dash takes
  its nearest node's level. The secondary swell and the wind sea mix 75 % of their own
  colour into the tint (`tintMix`: amber `#ffcf85`, cyan `#9cecff`), so the systems differ
  by colour as well as shape. Without a colour field (tests) a dash takes its system's own
  colour.
* **Visibility**: a dash is drawn only where its centre and its two ends sample all-valid
  lattice nodes, and a blob also its two back corners (else only its 2 px head is drawn, if
  that fits), so no dash sits on land or where the model has no data.
* **Life fade (a display choice)**: opacity × `smoothstep(age / 0.2) x smoothstep((1 - age) /
  0.3)`: in over the first 20 % of the life, out over the last 30 %. It only keeps dashes from
  popping in and out; it is not a wave group and makes no group-speed claim.
* **Drawing (the cheaper path)**: every dash is ONE `fillRect` of the unit rectangle under its
  own canvas transform (`setTransform`): the transform maps the rectangle onto the dash (x
  along the crest times its length, y against the travel times its depth, the origin on its
  leading edge), and the fill is a linear gradient in the rectangle's own coordinates, so one
  gradient per tint serves every blob of that tint whatever its size and angle. A split bar's
  gap is a hard-stop gradient along its length (one quad too). Dashes are batched per system,
  tint level and opacity level: a batch sets its fill style and opacity once, then two canvas
  calls per dash. No path is built or stroked. Gradients and colours are cached per context.
  Drawing order: per system (wind sea, secondary, primary or combined on top), per tint.
* **Frame cost** (none of this changes what is drawn): the cells are made in their hashed
  visiting order, so a frame walks them in memory order; a cell whose dash cannot be drawn in
  the view during its life (no data at its birth place, or a path from birth to the end of
  its life that stays outside the view and the 10 px drawing margin) is skipped until that
  life ends, and its next life is placed exactly as if it had been visited (unit test: an
  animation with skipping equals one that visits every cell every frame, and still frames in
  between change nothing); each lattice cell carries an "all four nodes valid" flag, so every
  validity probe is one read; a dash too faint by its life fade alone (fade x 0.95 < 0.04) is
  left out before any sampling; the hot single-use helpers (`dashAt`, `sampleLattice`,
  `tintLevelAt`, `batchOf`) are called with `/*#__NOINLINE__*/`, because the production
  minifier otherwise inlines each as a function expression created anew for every cell or
  dash (thousands of closures a frame and more garbage-collection pauses); and the fill
  style of a system and tint level is made once a frame (its batches differ only in
  opacity). Section 7 has the measurements.

**Why filled quads (integration, 2026-10-22).** The wave-look package drew each dash as a
round-capped core stroke with a halo and a two-step butt-capped trail (6 path vertices),
which read as a stack of 2 to 3 hard bars ("barcode"), in pure white, and its 6,000-vertex
frame budget held desktop views to about 1,000 dashes (1.3 per 1,000 CSS px² in the
package's own count, against 3.75 for the reference in that count). Candidate draw paths
were measured on this machine (headless Chromium 141, software rendering, 1440 x 900, JS
time per frame for N random dashes, median of 130 frames; wall = time per animation frame):

| Path | N | JS per frame | Wall per frame |
|---|---|---|---|
| Strokes as before (core, halo, two trail steps) | 4,500 | 2.8 ms | 82 ms |
| Pre-rendered sprite, `drawImage` under `setTransform` (ImageBitmap or canvas) | 4,500 | 27 to 29 ms (1.8 ms at 1,000, 5.1 ms at 3,000: the cost jumps between 3,000 and 4,500 images, most likely the canvas flushing its recording inside the calls) | 37 to 43 ms |
| Batched `Path2D` quads, 3 nested per dash, 24 fills | 4,500 | 65 ms | 113 ms |
| `fillRect` under `setTransform`, sprite as a `CanvasPattern` | 4,500 | 2.4 to 2.5 ms | 44 to 46 ms (131 ms at 2x pixels) |
| `fillRect` under `setTransform`, linear gradient per tint (chosen) | 4,500 | 2.1 ms | 19 to 20 ms (35 ms at 2x pixels) |
| same, 6,000 dashes, opacity set per dash / per batch | 6,000 | 2.9 / 2.6 ms | 22 to 25 ms |
| WebGL2, one instanced draw (SwiftShader here) | 6,000 | 0.3 ms | 121 ms |

The gradient quad is the cheapest overall (the pattern and the sprite cost far more to
rasterise in software, and WebGL runs on SwiftShader here); its floor is the two canvas calls
per dash, `setTransform` and `fillRect`, about 0.4 µs together on this machine.

### 4.7 Ocean mask (both basemaps)

Dashes are kept to the ocean in three ways:

1. Lattice nodes without data are invalid (land and sea ice are no data in the wave model),
   and so are lattice nodes over Natural Earth land (rasterised at lattice resolution: a node is
   land when more than 200/255 of its 8 px cell is land). A dash needs its centre, both ends
   and (a blob) both back corners on valid nodes (section 4.6), so dashes stop within about
   one lattice cell of the coast in the dash canvas itself.
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
  height (no fade) and at full depth, plus an arrowhead on every `ARROW_EVERY = 6`th dash of
  the primary swell or the combined sea: a white chevron (1.4 px, opacity 0.85) `ARROW_PX = 5`
  px long and 7 px wide, 1.5 px ahead of the blob's leading edge, pointing where the waves
  travel. Nothing animates (e2e: the canvas hash is unchanged over 1 s, with more than 3
  arrowheads).
* Budgets: at most `MAX_DASHES = 8,000` dashes per frame, `PHONE_MAX_DASHES = 3,000` below
  640 px wide (`maxDashes`), and a frame budget in filled quads (one per dash) that adapts to
  the device (`QuadBudget`, `components/map/wavefronts.ts`): it starts at `MAX_FRAME_QUADS =
  7,000`; every 20 animated frames, when their median draw time is over `DRAW_TARGET_MS = 2.5`
  ms, the cap drops to the quads those frames drew x 0.95 x 2.5 / median, never below
  `MIN_FRAME_QUADS = 1,500`; when the median is under 80 % of the target and the cap limited
  those frames (they drew at least 90 % of it), it grows by 15 % (less than 1 / 0.8, so it
  settles instead of swinging), up to 7,000. The first 15 frames after each start (pan, zoom,
  new mode), which place every dash afresh, are left out. A median of 2.5 ms keeps D11's
  frame draw p95 of 5 ms: on this machine, in the QA views the cap limited, the p95 ran 1.2
  to 2.2 times the median (section 7). The median, not a high percentile, drives it, so a
  garbage collection or a busy moment of the page does not thin the map for good. A fast
  device keeps the full density; this shared, software-rendered test machine draws about
  1,500 to 2,600 blobs on busy 1440 x 900 views, depending on its load (section 7). Phone
  views of the combined sea stay under the cap (700 to 1,430 blobs on a Pixel 7); with all
  three partitions a phone view (about 1,950 to 2,650 dashes) can reach it too.
* The top system (primary, else combined) is planned first and may use 60 % (`BUDGET_SHARE`)
  of the dashes left under the smaller of the two budgets (one quad per dash), the next 60 %
  of the rest, the last one all of it. Cells are visited in a fixed hashed order, so a frame
  over budget leaves out an even, random share of the cells, never a region. The number of
  cells visited only shrinks when the budget is hit and grows back by one cell a frame while
  under 75 % of its share, so the cut is steady; when the adaptive cap grows, every system may
  visit all its cells again (the next frame finds the new cut). Targets (D11, e2e at
  1440 x 900): draw p95 ≤ 5 ms and ≤ 12,000 stroked path vertices (now only the arrowheads of
  reduced motion), plus quads ≤ the cap ≤ 7,000.

### 4.9 Data attributes (tests and diagnostics)

`canvas.map-wave-canvas`: `data-style` (`dashes`), `data-mode` (animated, static, off),
`data-families` (systems drawn, in drawing order), `data-crests` (dashes drawn),
`data-quads` (filled quads drawn, one per dash), `data-quad-cap` (the adaptive cap of the
frame), `data-dash-trails` (blobs drawn deeper than their head, i.e. with their smear),
`data-vertices` (stroked path vertices: the reduced-motion arrowheads, 3 each),
`data-strokes` (canvas draw calls), `data-cells` (dash cells visited, idle ones included),
`data-drawn`, `data-arrows` (still mode),
`data-exaggeration` (E rounded), `data-view` (origin, size, world px), `data-ocean-mask`,
`data-draw-ms`, `data-draw-ms-p50` and `data-draw-ms-p95` (last 120 frames), `data-frames`
(frames drawn) and `data-lattice-ms`. The e2e frame-cost test (`e2e/map-performance.spec.ts`,
run without the Playwright trace recorder) reads the p95 once more than 160 frames are drawn.
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

## 7. Measured (e2e, synthetic fields, headless Chromium 141, 1440 x 900, shared 4-CPU machine)

Full e2e run of the integrated crest blobs (2026-10-22, chromium and Pixel 7 projects, 127
tests passed, 6 skipped; the machine is shared with other agents' builds and test runs, so
times vary from run to run). The wave-look package's 6,000-vertex trailed dashes
(2026-10-21) and the traced-crest renderer (2026-10-15) are given for comparison; the runs
in between are in this file's history.

| Check | Blobs, adaptive quad budget (2026-10-22) | Trailed dashes, 6,000 vertices (2026-10-21) | Traced crests (2026-10-15) |
|---|---|---|---|
| Orientation, uniform swell from 225° at 12 s (structure tensor) | 45.0°, coherence 0.66 (Pixel 7: 45.0°, 0.60) | 44.9°, coherence 0.60 (Pixel 7: 45.1°, 0.60) | 45.0°, 0.98 |
| Motion along the travel direction | 5.0 px in 0.54 s (expected about 4.8); Pixel 7: 5.0 px in 0.55 s (about 5.0) | 8.0 px in 0.88 s (expected about 8.0); Pixel 7: 8.5 px in 0.99 s (about 9.0) | 5.0 px in 0.61 s |
| Crest spacing 8 s / 16 s (ink autocorrelation) | 12 px / 48 px (ratio 4.00; D11 allows 4.0 ± 0.2) | 12 / 48 px | 12 / 48 px |
| Ink, Hs 1 m / 4 m | 31,801 / 74,971 in total (2.36 x); 7.59 / 17.07 per dash drawn (2.25 x; D11 asks > 2 x) | 20,862 / 48,019 in total (2.30 x; D11 asks > 2 x) | 10,117 / 35,411 |
| Orientation peaks, swell1 from 270° + swell2 from 180° | 92.5° (0.34 of the energy) and 2.5° (0.23; threshold 0.15); 92.5° alone without swell2 | 87.5° (0.37 of the energy) and 177.5° (0.20; threshold 0.15); 87.5° alone without swell2 | 92.5° and 2.5° |
| Frame draw p95 once settled (combined sea, whole world, no trace recorder) | 2.8 ms, 2,287 dashes in 2,287 quads (adaptive cap 2,300), 0 vertices (D11: ≤ 5 ms, ≤ 12,000) | 3.3 ms, 1,040 dashes, 5,804 vertices | 3.8 ms, 9,531 vertices |
| Main-thread cost per animation frame | 1.28 ms script (6.92 ms task) | 2.51 ms script (36.5 ms task) | 2.7 ms script (33.6 ms task) |
| Basemap redraw at zoom 2 | 6.4 ms | 8.7 ms | 6.2 ms |
| Colour-field redraw at 1440 x 900 (median of 6 pans) | 10.9 ms (10.4 to 22.6) | 23.3 ms (16.4 to 24.2) | 16.8 ms |
| Particle canvas after 5 s: alpha 5 / 6 / 7 counts, fade renderer (float16) | 11,522 / 9,578 / 8,227 (no spike at 6) | 11,738 / 9,748 / 7,765 (no spike at 6) | 11,290 / 8,922 / 8,647 |
| Same, explicit renderer (8-bit canvas) | 3,481 / 5,974 / 3,818 | 3,898 / 5,132 / 4,150 | 4,071 / 6,423 / 4,012 |
| Field cache after 48 downloads | array buffers 24.0 -> 57.2 MB (cache at most 49.9 MB decoded) | array buffers 23.0 -> 56.2 MB (cache at most 49.9 MB decoded) | 23.5 -> 56.7 MB |

Wind particles in the same run: 0.78 ms script (15.27 ms task) per frame; idle map 0.04 ms
(0.22 ms task). "Task" includes the software rasterisation of headless Chromium, which a
GPU-backed canvas does off the main thread; no browser with a GPU was available here. The
task time of a wave frame fell from about 36 ms to about 6 to 9 ms although the frame draws
more than twice as many dashes: a gradient-filled quad rasterises far more cheaply than the
stroked halos and trails it replaces (section 4.6).

The ink check compares the ink per dash drawn (`data-crests`, read with the pixels), and
asserts that the 4 m sea also draws more ink in all: both seas draw one dash per cell, but
the adaptive budget can thin one frame more than the other on a busy machine (the 4 m blobs
cost more to fill), which made the total 1.89 x in one full run under load, against 2.21 to
2.34 x in seven quieter runs.

The frame draw p95 of the e2e check (the whole world at 1440 x 900) was 2.8, 3.9, 4.2 and
4.7 ms in the full and map-only e2e runs of this round's code, the budget settling at 2,287,
2,451, 2,390 and 3,459 dashes; the 6,000-vertex trailed dashes measured 3.2 to 5.0 ms at
about 1,040 dashes. `DRAW_TARGET_MS` is the knob: the budget keeps the median near or under
it on any device, and the p95 follows at 1.2 to 2.2 times the median here.

Coherence is lower than for long traced crests because a blob's smear has sides that run
across the crest as well as along it.

**Final review (2026-10-24), the blobs reshaped to the reference's proportions** (map-layers
wave checks against this round's code, the same machine): a swell from 225° at 12 s has its
line orientation at 45.0° (the crisp fronts on the crests) with a coherence of 0.29 (Pixel
7: 0.20), since a blob is now about as deep as it is long, so the e2e check reads the travel
instead: the brightness falloff (sum of g |g| over the wave canvas, the sharp front
outweighing the soft back) points to -45.9° (Pixel 7: -44.3°, expected -45°, within 15°),
with a lean of 0.185 (0.283). Motion: 5.0 px in 0.62 s (expected about 5.6); Pixel 7: 6.0 px
in 0.58 s (about 5.2). Spacing 12 / 48 px. Ink per dash, 1 m / 4 m: 8.37 / 18.12 (2.17 x),
8.41 / 18.66 and 8.42 / 18.70 (2.22 x) in three runs, 8.47 / 18.10 (2.14 x) in the full e2e
run of the final review (127 passed). A primary swell from the W and a secondary from the S:
the amber bars lie at 0.0° (coherence 0.52 of their own pixels) and the blobs travel at 0.6°
(east); without the secondary, no amber pixel and the blobs travel at 0.4°. The coherence
threshold of 0.5 was replaced by these travel checks.

**Visual QA on real fields.** NOAA GFS-Wave + GFS fields of the 2026-10-16 00Z run (the 9
stored hours of a clone of the `swell_u3i_live_run` database, with the real Open-Meteo spot
runs of that day), served by this branch's backend to its production build, screenshots of
the analysis hour at 1440 x 900 and in Pixel 7 emulation (412 x 839 CSS px at 2.625 device px
per CSS px), compared with the owner's reference screenshot. Final pass of this round's code
(2026-10-23): Hs over the desktop view (10th / 50th / 90th percentile of its ocean cells in
the served field, and its maximum; the Pixel 7 views see the middle of it), dashes drawn
(with their smear in brackets), the adaptive cap when it limited the view, and the 120-frame
draw median / p95 once settled (headless Chromium, software rendering, machine shared):

| View | Hs in the desktop view, m | Desktop 1440 x 900 | Pixel 7 |
|---|---|---|---|
| California, zoom 6, centre 36.5°N 123°W (blue-violet) | 1.2 / 1.5 / 2.0 (max 3.7) | 2,037 (2,022), cap 2,118: 2.3 / 2.8 ms | 1,207 (1,190), under the cap: 1.1 / 1.5 ms |
| California, zoom 5 (blue-violet inshore, violet to magenta-purple offshore) | 1.3 / 1.7 / 3.5 (max 4.9) | 2,436 (2,413), cap 2,451: 2.2 / 3.2 ms | 1,143 (1,125): 1.2 / 1.5 ms |
| Gulf of Alaska, zoom 5 (magenta-purple to magenta) | 1.5 / 3.3 / 4.3 (max 5.1) | 1,885 (1,870), cap 1,941: 2.2 / 3.2 ms | 1,434 (1,421): 1.2 / 1.6 ms |
| Pacific, zoom 2 (Pixel 7: its whole-world view) | 0.6 / 2.2 / 4.8 (max 12.2) | 2,057 (2,025), cap 2,090: 2.3 / 2.7 ms | 700 (668): 1.1 / 2.0 ms |
| California, zoom 6, primary + secondary + wind sea | | 1,961 (1,488; the secondary's bars have no smear), cap 2,002: 2.4 / 2.9 ms | 2,642 (1,667), under the cap: 2.2 / 2.8 ms |
| California, zoom 6, reduced motion | | still: 4,211 dashes at full depth, 702 arrowheads; the one still frame took 50 ms | 1,328, 222 arrowheads: 26 ms |
| California, zoom 6, legend open | | 2,280, cap 2,331: 2.1 / 2.6 ms | 1,198: 1.2 / 1.4 ms |
| California, zoom 6, settings open (three partitions) | | 1,827, cap 1,847: 2.5 / 3.1 ms | 1,959, cap 1,984: 2.1 / 4.7 ms |

The 2026-10-22 pass of the same views drew 2,152 to 2,581 blobs on the desktop (caps 2,206
to 2,670) and the same Pixel 7 counts within 3 %; other passes of 2026-10-23 settled as low
as 1,476 (Pacific, zoom 2). The desktop cap follows the machine's load, which other agents'
builds and tests share. The reduced-motion frame is drawn once per view (after each pan or
zoom) and does not feed the adaptive budget, so on a page that never animated it is cut
only at 7,000 quads: here it drew every cell.

**Density.** Counted the same way on every screenshot (`density.mjs` of the QA tools, not
committed): open-ocean regions clear of labels, markers and controls; a blob is a connected
patch at least 25 grey levels above the local mean over 31 CSS px. Blobs per 1,000 CSS px²,
with the median blob area and the share of the region covered (final pass, 2026-10-23):

| View (region) | Desktop, this machine | Desktop, budget as on a 3 x faster device (2026-10-22) | Pixel 7 |
|---|---|---|---|
| Owner's reference (phone screenshot, open ocean, 167 x 342 CSS px) | 4.05 (26.4 CSS px², 11.6 %) | | |
| California, zoom 6 (desktop 600 x 400, phone 300 x 300 CSS px) | 2.20 (28 CSS px², 6.4 %) | 3.49 (27 CSS px², 11.0 %) | 3.49 (27.6 CSS px², 11.1 %) |
| California, zoom 5 | 2.26 (30, 8.0 %) | | 3.06 (27.4, 9.6 %) |
| Gulf of Alaska, zoom 5 | 1.92 (37, 7.2 %) | 3.14 (36, 13.3 %) | 3.65 (37.2, 16.1 %) |
| Pacific, zoom 2 (desktop 500 x 300) | 2.01 (30, 7.3 %) | 2.72 (31, 10.3 %) | 2.65 (29.9, 10.1 %) |

The 2026-10-22 pass counted 2.58, 2.31, 2.25 and 1.95 on the desktop and 3.49, 3.03, 3.73
and 2.73 on the Pixel 7 in the same regions. The "3 x faster" column ran the same build with
`performance.now` slowed three times in the page (a QA-only init script, not in the app), so
the budget saw every frame three times cheaper, as a device three times faster than this one
would, and kept its cap at 7,000: 3,866 (California), 4,080 (Alaska) and 3,284 (Pacific)
dashes, the full cell grid. Its frames took about three times the 1.1 to 1.3 ms median they
reported, so the column shows the density such a device gets, not a cost on this one.
**Item 4 (at least about 3 per 1,000 CSS px²)**: met on the phone (2.65 to 3.73; 2.65 to 2.73
in its whole-world view), and on a desktop only where the device can afford the full grid
(3.1 to 3.5 off California and Alaska with the 3 x budget). On this software-rendered machine
a busy 1440 x 900 view cannot keep the draw p95 under 5 ms at that density, so the adaptive
budget holds it at 1.92 to 2.58 there. The wave-look package, counted with its own (earlier)
tool, had 1.3 on the desktop and 2.3 on the phone against 3.75 for the reference in that
count.

**Where a desktop frame's time goes (2026-10-23).** With the same real fields in the
frame-cost harness (no budget; California, zoom 6, at 1440 x 807, the map's size under the
header), the full grid is about 3,958 blobs out of 13,818 cells visited, drawn in a median of
3.0 to 3.3 ms in seven runs (0.76 to 0.83 µs a blob). About 0.4 µs of that is the two canvas
calls per blob (`setTransform` and `fillRect`, CPU profile; section 4.6), the floor of this
draw path; the rest is the cell walk and the per-dash sampling. Most visited cells are idle
(over land, or with a path that never enters the view) and cost little: in the app the same
view visited 3.6 cells per blob drawn (8,862 for 2,454, `data-cells`). In the app's passes a
blob cost 0.9 to 1.2 µs (a 2.2 ms median for 2,454 blobs, 2.3 ms for 2,037), so the 2.5 ms
target holds about 1,500 to 2,600 blobs here, short of the full grid. Making each system and
tint level's fill style once a frame and keeping `tintLevelAt` and `batchOf` out of line
(section 4.6) changed the draw time by no more than the run-to-run noise in an interleaved
comparison with the harness (California, zoom 6: 3.1, 3.2 and 3.1 ms before, 3.1, 3.0 and 3.3
ms after; Pacific, zoom 2: 2.6, 2.5 and 2.6 ms before, 2.4, 2.4 and 2.5 ms after).

The field reads as a smooth, dusky blue-violet-magenta-pink surface, the land is mid grey
with a dark coastline and light labels, and many short, soft blobs, each a lighter tint of
the sea under it, streak along the swell with a crisp leading edge and a fading smear
behind, drifting with it: the character of the reference, drawn with our own ramp, shapes
and code.
The California view that hour had 1.2 to 2.0 m of sea (median 1.5 m), so it is blue-violet
on our ramp where the reference (a bigger day) is magenta; the Gulf of Alaska view (median
3.3 m) shows the reference's magenta-purple. The colours follow the metre anchors, not the
reference. The screenshots are attached to the integration report, not committed.

**Final review (2026-10-24), the same clone and hour, the reshaped blobs and tint.** Measured
on the screenshots with the review's tools (not committed): a blob's full extent along its
travel x along its crest (medians, CSS px), the share of blobs longer along the travel, and
how consistently their bright heads point one way (resultant length, 1 = all alike):

| Screenshot (region) | Travel x crest | Longer along travel | Heads alike | Head lift over the field, per channel |
|---|---|---|---|---|
| Owner's reference, offshore magenta / nearshore blue | 7.2 x 5.1 / 7.0 x 5.3 | 82 % / 90 % | 0.86 / 0.90 | (74, 79, 71) / (89, 90, 83) |
| California, zoom 6, desktop, before / after | 6.6 x 6.8 / 6.7 x 5.7 | 52 % / 93 % | 0.34 / 0.74 | (76, 79, 41) / (58, 57, 51) |
| California, zoom 6, Pixel 7, before / after | 8.4 x 6.1 / 6.4 x 4.9 | 61 % / 97 % | 0.62 / 0.80 | (77, 78, 42) / (58, 57, 52) |
| California, zoom 5, desktop (3 to 4 m), before / after | 5.9 x 8.2 / 6.2 x 6.7 | 29 % / 35 % | 0.69 / 0.79 | (62, 100, 48) / (76, 78, 65) |

The blobs of the typical (1.5 m) sea now streak along their travel as the reference's do,
and keep the hue of the sea; a 3 to 4 m blob is about as long along its crest (6.1 to 6.8
px) as its smear is deep (6.1 px at 10 s, 6.9 at 12 s), as the length grows with the height
and the smear does not. In this pass the desktop drew 1,725
dashes at California zoom 6 and 5 (cap 1,725: 2.1 / 4.9 ms) and the Pixel 7 1,188 and 1,145
(1.8 / 4.0 and 1.5 / 2.2 ms); the reduced-motion still frame drew 4,406 dashes in 105 ms on
the desktop and 1,295 in 55 ms on the Pixel 7, while the machine also ran this review's other
checks.

## 8. Limitations

* The dashes are stylised crests from the model's height, period and direction per system: no
  wave rays, refraction, shoaling or depth effects (deep water is assumed), no real wave
  groups, and the cells, births, the life fade and the smears are a drawing device. A smear
  is the band the crest swept over in the last 0.55 s of animation, not a wake and not a
  measure of anything. The 0.5° fields smooth fronts and coasts.
* The blobs are rectangles, not the reference's sheared parallelograms (section 4.6).
* Wind-sea dashes often sit on rows at the 8 px minimum spacing (periods under 6.5 s).
* Crest rows form only in a uniform sea (the test fixtures): there the dashes of neighbouring
  cells line up one crest spacing apart. In served model fields they do not form at all: the
  lattice phase p · W is counted from the world origin (thousands of px at zoom 6), so
  neighbours whose period differs by about 0.1 s, or whose direction differs by a fraction of
  a degree (directions are stored in 1.5° steps), snap to unrelated rows, and the dashes sit
  at random phases, as in the reference (whose blobs show no crest rows either). The legend
  says rows line up only in an even swell.
* The blobs' motion blur is softer than the reference's: the front third of a blob is about
  1.5 times as bright as its back third, the reference's about 1.8 to 2.0 times (measured on
  the real fields of the final review), and low seas (1 to 1.5 m) are about a third fainter
  than the reference's heads, because D11's ink rule keeps them fainter (section 4.6).
* Firefox and WebKit were not run here (their browsers are not installed in this sandbox):
  the 8-bit particle renderer is verified in unit tests and in Chromium forced to an 8-bit
  canvas.
* Where the swell turns fast (around storms, at partition swaps of the peak direction) no dash
  is drawn rather than one along a direction no wave has, so such areas show gaps.
* The dash sizes, tints and density were tuned by eye on one real run (GFS-Wave 2026-10-16
  00Z) and synthetic fields against one reference screenshot. On this shared,
  software-rendered machine the adaptive budget holds busy 1440 x 900 views to about 1,500
  to 2,600 blobs, depending on the machine's load (1.92 to 2.58 per 1,000 CSS px² counted,
  against 4.05 on the reference); the full grid (3.1 to 3.5 off California and Alaska) needs
  a device about three times faster.
  No device with a GPU was measured.
* The secondary swell's thin split bars are subtle under the primary's blobs (the e2e check
  of two orientations passes at 0.23 of the energy for the second peak against a threshold
  of 0.15).
* The dashes take their tint from the layer on screen, so on Wave Period and Wave Energy
  they follow the shared ramp, not the height ramp.
* Without a GPU (headless Chromium here) the dash layer still keeps the main thread busy
  with software rasterisation (about 6 to 9 ms of task time per frame in this round's e2e
  runs), and the reduced-motion still frame of a busy desktop view (about 4,200 dashes,
  cut only at 7,000 quads) took 50 to 60 ms to draw.
* With the combined sea drawn (the default), the partition fields are not downloaded, so the
  hover readout lists partitions only when they are chosen in Settings.
