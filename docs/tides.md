# Tides

The spot pages, the timeline and the quality score use **predicted astronomical tides**:
NOAA CO-OPS predictions for the 8 US spots and a global harmonic tide model (EOT20)
evaluated locally for the other 42. These are predictions, never observed water levels;
weather (wind set-up, pressure, river flow) is not in them. Every height is relative to a
named datum, and one curve never mixes sources or datums. Where nothing is configured the
API says "unavailable"; demo deployments get a tide that is labelled synthetic.

Code: `backend/app/services/tides/` (`coops.py`, `harmonic_model.py`, `catalog.py`,
`ingest.py`, `stage.py`, `api.py`, `health.py`, `maintenance.py`, `cli.py`), schema
`backend/app/schemas/tides.py`, tables `tide_stations`, `tide_predictions`,
`spot_tide_stations` (migration `0006_forecast_upgrade`). Data:
`data/tide_stations.json`, `data/tides/constants_eot20.json`. Extraction and validation:
`scripts/extract_tide_constants.py`.

## Sources and licences

| Source | Used for | Licence and attribution | Account |
|---|---|---|---|
| NOAA CO-OPS Data API and Metadata API (`api.tidesandcurrents.noaa.gov`) | The 8 US spots: high and low waters, and the 30-minute curve of harmonic stations | US Government work, public domain; NOS asks for attribution ("NOAA/NOS/CO-OPS tide predictions (tidesandcurrents.noaa.gov)", <https://tidesandcurrents.noaa.gov/disclaimers.html>) | None. Each request sends `application=NOAA_COOPS_APPLICATION` (else `APP_NAME`), never an e-mail address |
| EOT20 global ocean tide model (DGFI-TUM), distributed by SEANOE | The other 42 spots, and the fallback of the US spots | CC BY 4.0. Attribution shown with every model tide: "EOT20, Hart-Davis et al. 2021, DGFI-TUM/SEANOE". Citation: Hart-Davis et al. (2021), ESSD 13, 3869-3884, <https://doi.org/10.5194/essd-13-3869-2021>; data <https://doi.org/10.17882/79489> | None; read once by the extraction script, never at run time |
| The demo forecast provider's sea level | `FORECAST_PROVIDERS=demo` only, when no prediction is stored | Synthetic (M2 + S2 shaped), labelled "Synthetic demo data" | - |

Not used: Open-Meteo `sea_level_height_msl` (free tier non-commercial; a model sea level,
not a tide prediction; compared below), WorldTides (one response per user), TPXO
(non-commercial licence), FES2022 (needs an AVISO account; `TIDE_MODEL=fes2022` is
accepted by the settings but no FES2022 constants are committed, so the job warns and keeps
using and labelling the EOT20 file), UKHO, LINZ and Marine Institute Ireland (possible
station sources for Thurso, Raglan and Mullaghmore; not integrated).

## Datums

| Source | Datum in the API (`datum`) | Meaning |
|---|---|---|
| CO-OPS | `MLLW` | Mean Lower Low Water of the station's tidal datum epoch (1983-2001 at Pillar Point, mdapi `datums.json`). For stations that publish datums, the block's `reason` also states mean sea level above MLLW (0.91 m at Pillar Point, 0.86 m at Rincon Island, 0.34 m at Kahului, 0.28 m at Haleiwa; mdapi `datums.json`, 2026-10-15). Subordinate stations 9414275 and TWC0419 publish none (`"datums": null`). |
| EOT20 | `model_msl` | The model's mean sea level at the cell. Not a chart datum: no chart datum (LAT, MLLW) is known at a model point, and none is claimed. The UI text says "relative to model mean sea level, not a chart datum". |
| Demo | none (`null`) | Synthetic; no datum. |

A station's predictions are stored in one datum; the block refuses to mix datums, and the
spot's fallback source is used only when the primary has nothing for the window (never
spliced into its curve).

## Station catalog (`data/tide_stations.json`)

Every spot has an entry: one `primary` link, US spots also a `fallback`, each with the
great-circle distance from the break (`spots.json` latitude/longitude; checked within 0.5
km by `tests/unit/test_tide_catalog.py`) and a justification. A spot could instead say
`{"unavailable": "<reason>"}`; none does. Station kinds, reference stations and positions
come from the CO-OPS station list captured 2026-10-14 (and are re-checked weekly by the
job).

| Spot | CO-OPS station | Kind | Distance | Why |
|---|---|---|---|---|
| Pipeline, Sunset Beach, Waimea Bay | 1612668 Haleiwa, Waialua Bay | subordinate (Honolulu 1612340) | 9.4, 11.3, 6.5 km | Nearest prediction station on the same North Shore coast |
| Jaws (Peahi) | 1615680 Kahului Harbor | harmonic | 18.6 km | Nearest, same north shore of Maui |
| Mavericks | 9414131 Pillar Point Harbor | harmonic | 2.0 km | Inside the point |
| Ocean Beach SF | 9414275 Ocean Beach, outer coast | subordinate (Golden Gate 9414290) | 1.7 km | Same beach; the harmonic Golden Gate station lies inside the strait, where the tide lags the open coast by about 40 minutes (validation below) |
| Trestles | TWC0419 San Clemente | subordinate (San Diego 9410170) | 4.6 km | Same open coast; the nearest harmonic station (Newport Bay Entrance) is 36.7 km away |
| Rincon | 9411270 Rincon Island, Mussel Shoals | harmonic | 4.3 km | Same open coast |

Elsewhere the tide point is the EOT20 cell nearest the break at which all 17 constituents
are defined: 1.1 km (Teahupoo) to 13.3 km (Punta Roca) away, median 7.8 km; the distances
to the spots' forecast points are recorded too (2.3-29.7 km). Twelve model links carry a
caveat, shown with the tide: Mundaka (estuary mouth), Cloudbreak and Restaurants (Mamanuca
barrier reef), Teahupoo (reef pass, very small tide), Bocas del Toro and Salsa Brava (small
Caribbean tide, islands and reef), Thurso East (Pentland Firth), Desert Point (Lombok
Strait), Pavones (Golfo Dulce), and The Box, Margaret River Main Break and North Point
Gracetown (small, mostly diurnal tide: form factor 3.5 from the constants).

`python -m app.cli tides seed` loads the catalog into `tide_stations` and
`spot_tide_stations` (idempotent: 56 stations, 58 links). `python -m app.cli init-db` and
`python -m app.cli seed` do the same after seeding the spots, and every `tides.acquire` run
does it first, so a new deployment needs no extra step. (It is not part of
`seed_reference_data`: the test session seeds reference data once and empties the tide
tables after each test.) A spot entry that no longer matches the
database (for example a spot moved by more than 0.5 km) costs only that spot its links: it
is listed under `rejected_spots` in the result and logged, and its block says "unavailable";
an unreadable station list stops the seed. Without the constants file no model point is
linked (`model_constants_error`).

## Method

### CO-OPS

`datagetter?product=predictions&time_zone=gmt&units=metric&format=json&datum=MLLW` with
`interval=hilo` (high and low waters) for every station, and `interval=30` (the station's
own 30-minute curve) for harmonic stations. From the API documentation: "Subordinate tide
prediction stations can only provide tide predictions on a high / low interval" and "must
use datum=MLLW"; the client refuses anything else for a subordinate station. CO-OPS
answers a wrong request with HTTP **200** and `{"error": {"message": ...}}`; that raises
`PermanentProviderError` (it is never read as "no tide"). Times are UTC.

A subordinate station's curve between its published events is the classical cosine
(half-period) interpolation,

    h(t) = h1 + (h2 - h1) * (1 - cos(pi * (t - t1) / (t2 - t1))) / 2

which passes through every event and is monotonic between them. Those points are stored
with `interpolated = true`, flagged per point in the API, and the block's `reason` says
the curve is interpolated here; they are never presented as CO-OPS output.

The station list (`stations.json?type=tidepredictions`, about 2 MB) and each station's
datums are re-checked at most once a week; a kind that differs from the catalog is
corrected and reported in the job summary.

### EOT20 constants (`scripts/extract_tide_constants.py`)

The SEANOE archive `85762.zip` is 2,330,678,793 bytes: `load_tides.zip` (1.12 GB) and
`ocean_tides.zip` (1.21 GB, 17 netCDF-4 files of 132.9 MB, one per constituent: 2N2, J1,
K1, K2, M2, M4, MF, MM, N2, O1, P1, Q1, S1, S2, SA, SSA, T2, 1/8 degree grid). The ocean
tide (what a coastal gauge measures) is the one used; the load tide is skipped. The
archive is never downloaded whole: the outer zip directory is read with HTTP Range
requests; `ocean_tides.zip` is deflated inside it, so its bytes (and only those) are
streamed once through zlib, each constituent file is written, CRC-checked, sampled in a
1 degree box around every spot and deleted before the next one. Run on 2026-10-14: 1.21 GB
streamed, peak disk 132.9 MB, nothing kept but a 3 MB cache of the boxes. Selecting the
cells again from that cache (`--from-cache`) reproduces the committed file exactly.

`data/tides/constants_eot20.json` (51 kB) records per spot the cell position, its
distance to the break and to the forecast point, and the amplitude (m) and Greenwich phase
lag (degrees) of each constituent, plus the archive provenance (URL, member names, sizes,
CRC-32), licence, attribution and citation. Land and unresolved cells (fill value 0 or
NaN) are never selected.

### Harmonic synthesis (`harmonic_model.py`)

    h(t) = sum_k f_k(t) A_k cos(V_k(t) + u_k(t) - G_k)

V is the equilibrium argument at Greenwich from the mean longitudes of the Moon, Sun,
lunar perigee, node and solar perigee (Meeus, Astronomical Algorithms, UTC), f and u the
nodal corrections of Schureman (1958), the FES convention EOT20 is used with. No minor
constituents are inferred; SA and SSA carry the model's seasonal cycle. It reads only the
constants file (a unit test blocks sockets). `--check-pytmd` compares it with pyTMD 3.0.9's
FES arguments and nodal factors for the same constants: worst difference 0.012 mm over 30
days at all 50 points.

High and low waters: the curve sampled every 2 minutes, each local extremum refined with
the parabola through it and its neighbours, rounded to the minute and the millimetre.

### Daily job (`tides.acquire`, 02:20 UTC, and at worker start)

For yesterday 00:00 UTC to TIDE_HORIZON_DAYS (16) + 1 day ahead, every station linked to
an active spot: CO-OPS events and curves, EOT20 curves (30 minutes) and events. Rows are
upserted on (station, time, datum, is_event); rows of the window the new set no longer has
are deleted. Measured live on 2026-10-15 (own database, all 50 spots):

| Run | CO-OPS requests | Bytes | Rows inserted / updated / removed | Time |
|---|---|---|---|---|
| First (with the station list and datums) | 16 | 2,171,649 | 52,504 / 0 / 0 | 19.2 s (whole command) |
| Second, same day | 9 | 119,363 | 0 / 52,504 / 0 | - |

56 stations x about 940 rows: 52,504 rows, 12 MB with indexes. Retention keeps future
predictions and TIDE_RETENTION_PAST_DAYS (2) of the past (`maintenance.cleanup`). One
station failing does not stop the others (the station records `last_error`); the job fails
only if every station fails. A missing constants file is reported as an error of the
`harmonic_model` provider (status `partial` while CO-OPS works).

### Tide stage (`stage.py`, for the quality score)

From the stored events: with L and H the low and high water on either side of t,
`stage = (h(t) - h_L) / (h_H - h_L)`, so every low water is 0 and every high water 1
(also the higher low of a mixed tide, which the old rolling minimum and maximum put at
0.52-0.66), with `rising` when the next event is a high. h(t) is the stored curve (linear
between 30-minute points; the event's own height at an event time; the cosine shape where
the curve has a gap). No stage where the surrounding half cycle is under 0.15 m (the small
"wiggles" of a mixed tide: 3 of 53 half cycles at Pillar Point in 14 days). Whether the
stage is scored is decided by the scoring package: only when the spot's tide research class
is moderate or high, its tide decision is not disputed and it has a preference.

## API

`GET /api/spots/{slug}/tides?start&end` (default now to now + TIDE_HORIZON_DAYS; naive
times are UTC; 400 when end <= start or the window exceeds 31 days; `Cache-Control:
public, max-age=300`) returns `TideBlockOut`, also embedded in the timeline:

* `status`: `available` (stored predictions), `demo` (synthetic, demo provider) or
  `unavailable` with `reason` ("No tide source configured for this spot." or "No tide
  predictions are stored for this period yet.").
* `station` {code, name, provider, kind (`harmonic`/`subordinate`/`model`), distance_km},
  `source` (`noaa_coops`, `eot20`, `demo`), `datum`, `units` ("m"), `attribution`,
  `fetched_at`.
* `curve`: 30-minute points covering the window plus one point beyond each end, each with
  `interpolated`; `events`: the high and low waters in the window plus the one before and
  the one after it.
* `reason` on an available block: what to read with the curve, e.g. "Model tide (EOT20),
  relative to model mean sea level, not a chart datum." plus the catalog caveat, or the
  MLLW/MSL offset and the interpolation note for CO-OPS stations.
* `preference` {value, research_class, decision, text}: the profile's tide preference
  ("Best around mid tide.", "Works on all tides.", ...) only when it is set, the tide
  research class is moderate or high and the profile decision is not "disputed";
  otherwise "Tide preference not well established." On the committed research this shows
  18 of 50 preferences (10 of them other than "all"). No tide rule is invented here.

On the spot page an available block is drawn in the timeline's tide row, and a line under
the charts (outside every fold, phones included; `TideSourceNote` in
`frontend/components/forecast/tide-row.tsx`) shows the station or model point with its
distance, the `attribution` (EOT20's CC BY 4.0 credit, or NOAA CO-OPS) and the `reason`
caveat. The selected-time summary adds "model point / station N km away" to the datum.

16 days of tide for one spot are about 56 kB of JSON.

`GET /api/spots/{slug}/observations` is described in docs/observations.md.

Health (`tide_freshness`, in the data-health report): `tides_stale:<provider>` is
"degraded" when some linked stations have predictions for less than 2 more days and
"failing" when none has a future prediction. Stations the job has not tried yet are not
counted (the job schedule check covers a new deployment). With `harmonic_model` in
TIDE_PROVIDERS, an unreadable constants file is `tides_constants:harmonic_model`,
"failing".

## Validation

Model tide at each US spot's own tide point (the EOT20 cell the app would use there)
against the CO-OPS predictions of the paired station, 14 days from 2026-10-14 00:00 UTC
(CO-OPS answers captured 2026-10-14 23:07 UTC; reproduce with
`python scripts/extract_tide_constants.py --work DIR --validate 2026-10-14`). Curve: CO-OPS
6-minute values relative to MSL, RMSE after removing the mean offset (the datum
difference). Events: each CO-OPS high or low water matched with the model's nearest one of
the same kind within 3 h.

| Spot (tide point) | Station | Curve RMSE | r | Range ratio | Events matched | Median abs. timing error | Event height RMSE |
|---|---|---|---|---|---|---|---|
| Jaws (8.1 km) | 1615680 Kahului (harmonic) | 2.1 cm | 0.9942 | 0.99 | 46 / 48 | 11.5 min | 1.6 cm |
| Mavericks (11.0 km) | 9414131 Pillar Point (harmonic) | 3.9 cm | 0.9965 | 0.98 | 54 / 54 | 5.0 min | 3.8 cm |
| Rincon (2.1 km) | 9411270 Rincon Island (harmonic) | 3.1 cm | 0.9977 | 0.98 | 50 / 50 | 6.0 min | 2.9 cm |
| Trestles (3.5 km) | 9410580 Newport Bay Entrance (harmonic, 36.7 km away) | 3.1 cm | 0.9981 | 0.97 | 50 / 50 | 3.5 min | 3.5 cm |
| Trestles | TWC0419 San Clemente (subordinate) | - | - | - | 51 / 51 | 16.0 min | 3.0 cm |
| Ocean Beach SF (10.0 km) | 9414275 Ocean Beach (subordinate) | - | - | - | 54 / 54 | 8.5 min | 6.1 cm |
| Ocean Beach SF | 9414290 Golden Gate (harmonic, inside the strait) | 13.4 cm | 0.9609 | 1.02 | 54 / 54 | 38.0 min | 5.1 cm |
| Pipeline, Waimea Bay (8.7 / 6.3 km) | 1612668 Haleiwa (subordinate) | - | - | - | 42 / 52 | 35.5 min | 5.1 cm |
| Sunset Beach (9.0 km) | 1612668 Haleiwa (subordinate) | - | - | - | 44 / 52 | 27.0 min | 7.2 cm |

The model's mean sea level and CO-OPS's MSL agreed within 4 cm at the five harmonic
stations (offsets -0.7 to +3.5 cm). On the open California and Maui coasts the model tide
is within a few centimetres and a few minutes of CO-OPS. At Haleiwa, a small mixed tide
(about 0.5 m range), 9 of the 10 unmatched CO-OPS events border a half cycle of 1-12 cm
(a "wiggle" the model draws as a stand), and timing errors are about half an hour; the CO-OPS subordinate
predictions there are themselves offsets applied to Honolulu. Inside the Golden Gate the
model (an open-coast cell) leads by about 40 minutes, which is why Ocean Beach uses its own
outer-coast station.

Unit tests pin the 72-hour version of this check (`tests/unit/test_tide_harmonic.py`,
fixtures `h72_*.json`, 2026-10-14 to 2026-10-16): r = 0.995 and RMS 4.6 cm at Pillar Point,
4.0 cm at Rincon Island, 2.0 cm at Kahului (tolerances r >= 0.99, RMS <= 6 cm, 4 cm at
Kahului). On those same 72 hours the audit's comparison of Open-Meteo `sea_level_height_msl`
at the spots' forecast points with the same stations gave RMS 5.5 cm (Pillar Point),
6.4 cm (Rincon Island), 2.4 cm (Kahului), 6.7 cm (Newport) and 18.8 cm (Golden Gate), with
mean levels 0.24-0.55 m above the stations' MSL: Open-Meteo is not used for tides.

Minor constituents: a probe with pyTMD 3.0.9 `infer_minor` (not in the app) lowered the
14-day curve RMSE from 3.9 to 3.1 cm at Pillar Point, 3.1 to 2.1 cm at Rincon Island and
3.1 to 2.5 cm at Newport, and raised it from 2.1 to 2.3 cm at Kahului; inferred minors
reached 8 cm.

A second period, checked in review on 2026-10-15 against fresh CO-OPS answers for
2026-12-20 to 2026-12-30 (6-minute MSL curve, high/low waters): Rincon Island 4.4 cm,
r = 0.9980, median timing error 7 min (39 of 39 events); Kahului 2.7 cm, 0.9953, 10 min
(39 of 39); Pillar Point 4.4 cm, 0.9978, 6 min (39 of 39). Rerunning `--check-pytmd` with
pyTMD 3.0.9 in review gave the same worst difference, 0.012 mm.

There is no comparison outside the US: no other spot has a station source in the app.
The caveats above mark where a global model is known to be weaker.

## Limitations

* Predictions only: storm surge, wave set-up, pressure and river flow are not included;
  at small-tide spots (Teahupoo, Bocas del Toro, Salsa Brava, Margaret River) they can
  match the tide.
* Global-model points are 1-13 km from the break and do not resolve reefs, passes,
  lagoons, estuaries or narrow straits (Mundaka, the Fiji reefs, Teahupoo, Bocas del Toro,
  Desert Point, Thurso East, Pavones). The block's `reason` says so for these spots; the
  heights there are a rough guide and the times may be off by more than the US numbers.
* `model_msl` is the model's mean sea level, not the local mean sea level or any chart
  datum, so model heights cannot be compared with a local tide table's numbers (only the
  shape and times).
* Subordinate CO-OPS curves are interpolated between events; the true curve can differ
  between them, most where the tide is mixed.
* No minor constituents are inferred (measured effect above).
* The validation covers 14 days in October 2026 at the 8 US spots only; the nodal
  corrections are checked against pyTMD for 30 days, not against decades of predictions.

## Settings and commands

`TIDE_PROVIDERS` (`noaa_coops,harmonic_model`; empty disables tides and the job),
`NOAA_COOPS_BASE_URL`, `NOAA_COOPS_APPLICATION` (empty = `APP_NAME`; an e-mail address is
refused and replaced), `TIDE_MODEL` (`eot20`), `TIDE_CONSTANTS_PATH`
(`data/tides/constants_eot20.json`), `TIDE_HORIZON_DAYS` (16, at most 31),
`TIDE_RETENTION_PAST_DAYS` (2).

    python -m app.cli tides seed          # catalog -> tide_stations, spot_tide_stations
                                          # (init-db and seed also do this)
    python -m app.cli tides acquire       # the daily job, now
    python -m app.cli tides list          # stations, spots, stored predictions, last error
    python -m app.cli tides prune         # retention

Re-extracting the constants (developer machine; numpy, httpx, h5py, and the optional
`tides-extract` extra for the pyTMD check; about 1.2 GB of network, under 400 MB of disk):

    pip install -e 'backend[tides-extract]' h5py
    python scripts/extract_tide_constants.py --work /tmp/eot20
    python scripts/extract_tide_constants.py --work /tmp/eot20 --check-pytmd
    python scripts/extract_tide_constants.py --work /tmp/coops --validate 2026-10-14
