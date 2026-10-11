# Surf forecast model research

**Swell Travel Agent: forecasting breaking-wave quality 5–10 days ahead at 50 surf spots**

Compiled 2026-10-10 against repository HEAD `0ab835d`, plus the working-tree change to `nearshore.py` noted in Section 16.

**How to read this document**

- `[n]` refers to the numbered Source register at the end.
- "Sandbox" means computed or checked directly in the research environment: S3 listings, decoded GRIB2, Python re-computation.
- "Search extract" means confirmed only through a search-engine extract.
- "Mirror" means confirmed through a GitHub-hosted copy, such as a reference list, BibTeX file or vendored documentation.
- "Not re-verified" marks a number that must not be used as a design constant.

**Access conditions during the research**

- Reachable: public S3 and GCS buckets, PyPI and GitHub.
- Blocked: most publishers and DOI resolvers, plus NDBC, NOMADS, Open-Meteo, CO-OPS, NCEI, CDIP, CEDA and Zenodo [3].
- As a result, data and code claims are mostly first-hand, while many literature claims rest on abstracts or metadata.

---

## 1. Research objective

The app scores 50 spots hourly and alerts users, with flights, to swells 5–10 days ahead. The research answers five questions:

1. What forecast inputs exist, how they depend on each other, and which are reachable.
2. How to turn offshore wave forecasts into breaking (face) height at a specific break, with explicit formulas.
3. What skill to expect at 5–10 days, and how to express uncertainty honestly.
4. Where ML helps, and where it must not be deployed.
5. How to validate everything without leakage.

Three kinds of evidence are kept apart throughout:

- **Offshore significant wave height (Hs).** Model Hs verified against buoys and altimeters. This evidence is plentiful [23][24][26][68].
- **Observed surf height.** Scarce. The only long record found is Oahu's North Shore [48][49].
- **Laboratory and beach breaker formulas** [33][35][36].

No reviewed source validates a 5–10-day surf-quality forecast against observed surf. Nothing in the app has been validated at its 50 spots.

## 2. Current forecasting architecture

Files read: `forecasts/{open_meteo,windy,families,blend,grib,archive,registry,base}.py`, `surf_quality/{breaking,nearshore,scoring,uncertainty,confidence,predict}.py`, `core/config.py`, `docs/SURF_SCORING.md` and `docs/FORECAST_SOURCES.md`.

**Providers.** `FORECAST_PROVIDERS` defaults to `demo`. The live options are:
- `open_meteo`: wave model `ncep_gfswave025`, atmosphere `gfs_seamless`.
- `windy`: Point Forecast API with `gfsWave` and `gfs`; paid key.

Each Open-Meteo batch of 10 spots makes three requests:

| # | Endpoint | Model | Location | Variables |
|---|---|---|---|---|
| 1 | `/v1/marine` | `ncep_gfswave025` | offshore forecast point | Hs, `wave_period`, `wave_direction`, wind sea, primary swell (secondary optional) |
| 2 | `/v1/gfs` | `gfs_seamless` | **break coordinates** | 10 m wind, gusts, MSL pressure |
| 3 | `/v1/marine` | default (no `models`) | forecast point | `sea_level_height_msl`, the tide proxy |

The run time comes from `meta.json`, falling back to a nominal GFS cycle with a 5.0 h lag.

`registry.py` accepts `noaa_gfs` and `ecmwf_ifs` and lazily imports `forecasts.noaa` and `forecasts.ecmwf`. **Neither module exists**, so selecting either provider would fail at import time. `grib.py` is complete:
- parses NCEP `.idx` and ECMWF JSON `.index` files;
- fetches by HTTP Range and decodes with ecCodes;
- samples the nearest valid sea cell, never interpolating across land.

**Families and blend.** `families.py` defines these families:
- `ncep_gfs`: NOAA GFS-Wave, Open-Meteo `ncep_gfswave*` and Windy `gfsWave`.
- `ncep_gefs`, related to `ncep_gfs`.
- `ecmwf_ifs`, related to `ecmwf_aifs` and `mfwam`.
- `dwd_icon`.

Its heuristic prior error correlations are 0.6 between related families and 0.3 between independent ones.

`blend.py` (`blue_correlated_v1`) keeps one member per family. Weights are w = Σ⁻¹·1/(1ᵀ·Σ⁻¹·1), with Σ_ij = ρ_ij·σ_i·σ_j. Hs is combined as a geometric mean. Partitions come from the structure member, scaled by the Hs ratio, and are never matched by index. Disagreement flags fire at:
- an Hs ratio above 1.4;
- ΔTp above 3 s;
- Δdirection above 30°;
- Δwind above 15 km/h.

**Breaking height.** `nearshore.py` (`linear_wave_v2`) runs these steps:
1. De-shoal when the forecast-point depth is known, using Fenton–McKee plus a Newton step.
2. Compute a directional factor R: a wrapped Gaussian (σ = 10°, 15° or 30°) against the shore normal β and a shadow table, or a 15° taper outside the swell window.
3. Apply Komar–Gaughan per partition, multiplied by a focusing prior Kf(T).
4. Combine by energy flux: Hb = (Σ Hb_i^2.5)^0.4.
5. Optionally cap at γ·d, with γ = 0.78 by default.

Sets are 1.27·Hb. When `spot.calibration` holds a, b and c, `calibrated_power_law` is used instead. Version 1 is kept for backtests.

**No `data/surf_spots/physical_profiles.json` exists.** All 50 spots therefore use derived profiles:
- β is the centre of the optimal window ("estimated").
- Kf_ref is `height_factor` ("estimated").
- Depth is unknown, so deep water is assumed.
- There is no shadow table.

**Score, uncertainty and confidence**
- `scoring.py` (2.0.0) weights the components: direction 0.20, period 0.20, size 0.20, wind 0.30, tide 0.05, consistency 0.05. Size, period and wind gates follow.
- `uncertainty.py` computes σ_ln(Hb)² = (0.8·σ_lnHs)² + (0.4·σ_T/T)² + σ_m², with:
  - σ_lnHs² = σ_f² + 0.15²;
  - σ_f equal to GEFS spread/mean, or else `LEAD_TABLE` (0.074/0.168/0.232/0.305/0.387 at days 1/3/5/7/10);
  - σ_m = 0.30.
- The 80% range is Hb·exp(∓1.2816σ).
- Confidence = 100·exp(−σ_f/0.4)·(0.6 + 0.4·completeness), ×0.8 when stale.
- Run-to-run agreement is a diagnostic only.
- `archive.py` stores issued predictions immutably every 3 h to 240 h.
- GEFS, ECMWF, observation, verification and ML settings exist but are off.

**Discrepancies found**
1. `docs/SURF_SCORING.md` still describes algorithm 1.0.0 and confidence = 25 + 75·exp(−lead/9).
2. `LEAD_TABLE` and the docstring claim ("spread matched … so k = 1") reproduce only when Hs ≤ 0.2 m is excluded. Unfiltered, the log error is 0.080/0.192/0.276/0.352/0.430 against a relative spread of 0.098/0.181/0.245/0.313/0.392. The spread is 6–13% too small on days 3–10 [2].
3. Wind is requested at break coordinates. On the GFS 0.25° grid, 13/50 of these have a land nearest cell, and 33/50 have land in the bilinear stencil [1].
4. `gfs_seamless` blends HRRR over the US [13], so the run label `ncep_gfs025` is not pure GFS at the 8 US spots.
5. Request 3's sea level is CMEMS `total_sea_level` via Open-Meteo's default model, not GFS [14].
6. Puerto Escondido's forecast point (15.72, −97.12) is missing in NOAA's global.0p25 grid [1]. Open-Meteo returned data for all 50 spots on 2026-10-09 (FORECAST_SOURCES.md), so it presumably uses another cell (unverified).

**Update after implementation (2026-10-11).** Status of the points above:
- `forecasts/noaa.py`, `forecasts/ecmwf.py` and `forecasts/ensemble.py` now exist. Each was verified live on 2026-10-11 for all 50 spots with 0 errors:
  - NOAA GFS-Wave + GFS: 4,050 records, 1.91 GB.
  - ECMWF IFS: 3,250 records, 847 MB.
  - GEFS-Wave statistics: 2,050 rows, 201 MB.
  The blend and the predictions were then built from these runs (`docs/FORECAST_SOURCES.md`).
- Discrepancies 1 and 2 are fixed. `SURF_SCORING.md` describes 2.0.0. `LEAD_TABLE` now holds the unfiltered errors (0.080/0.192/0.276/0.352/0.430), and the spread scale is k = 1.05.
- Discrepancies 3 and 4 are avoided by the direct providers:
  - Wind comes from the nearest GFS cell with LAND < 0.5 (ECMWF: lsm < 0.5) to the break.
  - The NOAA path is pure GFS, with no HRRR blending.
  The Open-Meteo path is unchanged and documented.
- Discrepancy 6: the direct NOAA provider uses the native `global.0p16` grid. Puerto Escondido gets a valid cell 7.7 km from its forecast point (15.67 N, 97.17 W), and ECMWF gets one 14.3 km away.
- First live comparison of GFS and ECMWF: 33% of spot-hours were flagged over 10 days. In the first 48 h, Hs disagreement concentrated at sheltered Southern California spots (Rincon, Trestles), where ECMWF had a stronger local wind sea. This is the case the blend's widened range is meant for.

## 3. Primary scientific sources

**Verified:** F = full text or code read; A = abstract or search extract; M = metadata only; S = secondary transcription.

| Ref | Source | Kind | Evidence target | Used for | Verified |
|---|---|---|---|---|---|
| [15] | WAVEWATCH III manual and code | Official documentation | Offshore and shelf waves | Dispersion, partitions, breaking and friction constants, GRIB semantics | F |
| [44] | SWAN technical documentation v40.81 | Official documentation | Nearshore | Breaker-index ranges; diffraction limits | F |
| [23] | Campos et al. 2024 | Peer-reviewed | Offshore Hs | Correlation against lead | A |
| [24] | Campos et al. 2020 | Peer-reviewed | Offshore Hs | Ensemble mean against control; tail bias | A |
| [25] | Campos et al. 2025 | Peer-reviewed | Offshore Hs | Week-2 probability method | A |
| [26] | Saetra & Bidlot 2004 | Peer-reviewed | Offshore Hs, Tp | Joint Hs–Tp ensemble value | A |
| [27] [28] | Bidlot et al. 2002; Bidlot & Holt 2006 | Peer-reviewed; WMO report | Offshore Hs | Buoy verification standard | A |
| [29] | Mentaschi et al. 2013 | Peer-reviewed | Metrics | NRMSE bias; HH | A, plus F via [22] |
| [32] | Fenton & McKee 1990 | Peer-reviewed | Theory | Explicit wavenumber | Mirror, re-computed |
| [33] | Komar & Gaughan 1972 | ICCE proceedings | Breaking (lab and field) | Breaker formula | A |
| [34]–[37] | Larson et al. 2010; Weggel 1972; Goda 2010; Ruessink 2003 / Zhang 2021 | Peer-reviewed | Breaking (lab or beach) | Refraction at breaking; breaker indices | M/S |
| [39] [40] | Longuet-Higgins 1952; Battjes 1974 | Peer-reviewed; ICCE | Statistics; breaking | Rayleigh ratios; surf similarity | M/A |
| [41] [42] | Magne et al. 2007; Hope et al. 2026 (preprint) | Peer-reviewed; preprint | Canyons | Direction- and period-dependent focusing | M/S |
| [43] [45]–[47] | SWAN 1999; XBeach; FUNWAVE-TVD; CDIP MOP | Peer-reviewed and manuals | Nearshore models | Capabilities | F (manuals), M |
| [48] | Caldwell & Aucan 2007 | Peer-reviewed | **Observed surf** (Oahu) | Buoy to H1/10 surf | S |
| [49] | Goddard–Caldwell NODC 0001754/0001753 | NOAA data records | **Observed surf** (Oahu) | Hawaii scale; observation error | Catalogue mirror |
| [51] | Mead & Black 2001; Hutt et al. 2001 | Peer-reviewed | Break geometry | Components; peel angles | S |
| [58] [60] [62] [68] | Zhang 2023; Minuzzi & Farina 2025; James 2017; Hahner 2026 | Preprints | ML (offshore and nearshore) | Verified ML numbers | F |
| [65]–[67] | Browne 2006, 2007; Carney 2005 | Peer-reviewed | **Surf / nearshore** ML | Closest surf analogues | M |
| [80]–[83] [87] [88] | Hamill; Ferro; Gneiting & Raftery; Gneiting et al.; Taillardat; Gibbs & Candès | Peer-reviewed | Probabilistic verification and calibration | EMOS, QRF, conformal, scores | M/A |

## 4. Physical wave modeling theory

Linear (Airy) theory, valid up to roughly the break point [15].

**Dispersion and group velocity** [15]

```
omega = 2·pi/T,   omega^2 = g·k·tanh(k·h)
L0 = g·T^2/(2·pi),   Cg0 = g·T/(4·pi)
n = 1/2 + k·h/sinh(2·k·h),   Cg = n·omega/k
```

**Explicit wavenumber** [32]

```
k ≈ k0/[tanh((k0·h)^(3/4))]^(2/3),   k0 = omega^2/g
```

- The paper states "always better than 1.7%". The sandbox found a maximum error of 1.63% in k.
- Eckart's k0/sqrt(tanh(k0·h)) errs by up to 4.98% (sandbox).
- Use Fenton–McKee plus one Newton step.

**Shoaling**

```
E = rho·g·H^2/8,   E·Cg·cos(alpha) = const,   Ks = sqrt(Cg0/Cg)
```

- The minimum Ks is 0.913, at k·h ≈ 1.20.
- Sandbox values: 0.913 (14 s at 50 m), 0.918 (18 s at 100 m), 0.916 (22 s at 100 m).
- So a model point 50–200 m deep under-reads long-swell deep-water height by up to about 9%. De-shoal first.
- GFS-Wave's output point for 51201 has a model depth of 472.7 m [1].

**Refraction** (straight parallel contours, Snell: sin(alpha)/C = const, with shallow-water breaking):

```
Hb ∝ (H0^2·T·cos(alpha0))^(2/5)   =>   Hb(alpha0)/Hb(0) ≈ cos(alpha0)^0.4
```

Sandbox check against an iterative solve (γ = 0.78, H0 = 2 m, T = 14 s):

| α0 | 20° | 40° | 60° | 75° |
|---|---:|---:|---:|---:|
| Iterative solve | 0.977 | 0.902 | 0.762 | 0.583 |
| cos(α0)^0.4 | 0.975 | 0.899 | 0.758 | 0.582 |

A direct (non-iterative) formula exists [34]. WW3 geometric optics is exact only for slowly varying depth; diffraction, scattering and interference are neglected [15].
- **Scripps Canyon:** height changes about 5× over half a wavelength, yet geometric optics "can provide reasonable results" [15][41].
- **Nazaré** (preprint): focusing comes from refraction and reflection on the canyon's northern rim. Waves longer than about 7 s are mostly reflected, and focusing is best from 275–315° [42].

**Breaker indices**
- McCowan Hmax/d = 0.78. WW3's default Battjes–Janssen-type breaking uses γ = 0.73 with α = 1; the Miche form is k·Hmax = γ_M·tanh(k·d). The reef-flat value is 0.55 [15].
- The 0.73 is attributed inconsistently. SWAN calls it the mean of Battjes & Stive's data (range 0.6–0.83), and says Battjes & Janssen used 0.8. Kaminsky & Kraus found 0.6–1.59 (mean 0.79) [44].
- Weggel [35] (S): γb = b − a·Hb/(g·T^2), with a = 43.75·(1 − e^(−19m)) and b = 1.56/(1 + e^(−19.5m)), valid for m < ~0.1.
- Baldock (XBeach): Hb = (0.88/k)·tanh(γ·k·h/0.88) [45]. Variable-γ forms are in [37][44].
- Random-wave γ (Hmax/d) is not the regular-wave Hb/hb. Do not mix them.

**Komar–Gaughan** [33]

```
Hb = 0.39·g^(1/5)·(T·H0^2)^(2/5)   ≡   Hb/H0 = 0.563·(H0/L0)^(-1/5)
```

- Fitted to 3 lab and 1 field dataset.
- Shallow-water linear theory with γ = 0.78 gives a coefficient of 0.346. So 0.39 is 12.8% higher, and 10.9–11.6% above an iterative full-Cg solve (sandbox).
- Examples: 1 m at 12 s gives 1.66 m; 2 m at 16 s gives 3.25 m; 4 m at 18 s gives 5.93 m.
- Assumes regular waves on a plane beach. With Hs as input, it gives roughly a significant breaker height.

**Bottom friction** [15][44]

```
S_bot = 2·Gamma·(n − 1/2)/(g·d)·N,   Gamma = −0.038 m²/s³ (swell), −0.067 (wind sea, WW3 default)
H ← H·exp(−Σ |Gamma|·(n − 1/2)/(g·d_j·Cg_j)·Δx_j)
```

For 12 s swell over 100 km of flat shelf, the factor is 0.743 at 30 m, 0.904 at 50 m and 0.977 at 80 m (sandbox).

**Definitions**
- Hm0 = 4·sqrt(m0). NDBC WVHT is computed this way, though described as H1/3 [69].
- Tm01 = m0/m1; Tm02 = sqrt(m0/m2) (NDBC APD); Tp = 1/fp [69].
- Buoy directional spectrum: D(θ) = (1/π)[1/2 + r1·cos(θ−α1) + r2·cos 2(θ−α2)] [71].
- Rayleigh: H1/10 = 1.271·H1/3, and P(H > H1/3) = 0.135 [39].
- Surf similarity: ξ = m/sqrt(H/L0) [40] (thresholds not re-verified).

**Partitions** [15][30]
- The spectrum is split by watershed.
- Wind sea is the energy where 1.7·U10·cos(θ − θw) > c, with fraction ≥ 0.333.
- Swells are **ranked by Hs**, so "swell 1" is just the largest swell.

## 5. Offshore versus breaking wave height

| Quantity | Definition | Source |
|---|---|---|
| Model Hs (HTSGW, swh) | 4·sqrt(m0) at a cell or point | GFS-Wave, GEFS-Wave, ecWAM [1][2][16] |
| Partition Hs | 4·sqrt(m0) of one watershed partition, height-ranked | [15] |
| Buoy Hs | Hm0, or time-domain H1/3 (AODN WHTH) | [69][73][74] |
| Breaking height Hb | Significant breaker face, trough to crest | Formulas [33]; not observed at the spots |
| Reported surf | Observer scale: Hawaii ≈ face/2; daily value ≈ H1/10 | [49] |

**Chain:** model Hs → de-shoal → direction and shadow → breaker formula → focusing → flux combination → tide and depth cap → reporting convention. Error enters at every step.

**Oahu evidence** [49] (catalogue mirror)
- Hawaii Scale Feet understate breakers by "as much as one-half".
- Scaled photographs give face ≈ 2 × Hawaii scale, within 10–20%.
- The daily value is the top of the range, about H1/10.
- Observations track shoaling-only breaker estimates from buoy 51001 within 10–15%.

**Caldwell & Aucan** [48] multiply a shoaling-only Hb (in feet) by Kr(Hb) for high-refraction reefs. Every copy of the coefficients is garbled. This reconstruction is consistent with the stated 2.145 cap, since it peaks at 20.7 ft (Kr = 2.138):

```
Kr = −0.0003·Hb^3 + 0.0099·Hb^2 − 0.025·Hb + 1.0747   (Hb ≤ 21 ft, else 2.145)
```

It gives Kr = 1.16, 1.52, 1.92 and 2.14 at 5, 10, 15 and 20 ft. It is **unconfirmed**; do not ship it.

**Grid-cell sensitivity** (single times, not statistics)
- **46026, 2026-10-09 00Z:** the nearest GFS global.0p16 cell gave 1.30 m, adjacent cells 1.60–1.83 m, the GFS point output 1.46 m and ECMWF 1.83 m [1][16]. A 1.41× spread in Hs becomes about 1.31× in Hb, because Hb ∝ H^0.8.
- **Waimea, GEFS f168:** 1.89 m in the nearest cell and 1.70 m one cell east [2].

**Conclusion.** Every Hb the app reports is an uncalibrated estimate. Label it as one until per-spot observations exist.

## 6. GFS and wave-model integration

**GFS atmosphere** [1][7]
- **Version.** Operational on 2026-10-10: v16 series, FV3 C768 (~13 km), 127 layers. v16.3.0 became effective 2022-11-29 [4], with patches to v16.3.33 [7]. Evidence:
  - the `atmos/` layout;
  - a 743-record f006 `.idx`;
  - `generatingProcessIdentifier` = 96.
- **v17.**
  - PNS 26-29 (seen only via a secondary summary) proposes October 2026 and says "directory structure and file naming will change" [5][6].
  - dev/gfs.v17 configures C1152 (~9 km), coupled S2SW, Thompson microphysics and an unstructured `uglo_15km` wave mesh.
  - PR #5298 (merged 2026-10-06) removes the 0.5° and 1.0° atmospheric products [7].
- **Fields** in `pgrb2.0p25` (1440×721; latitude 90 to −90, longitude 0 to 359.75):
  - `UGRD/VGRD:10 m above ground` (0/2/2, 0/2/3);
  - `GUST:surface` (0/2/22);
  - `PRMSL:mean sea level` (Pa);
  - `LAND:surface` (0/1).
- **Cadence:** hourly to f120, then 3-hourly to f384 (209 files plus `.anl`); four cycles a day.
- **Latency** (S3 LastModified):
  - f000 +3.54–3.80 h; f120 +4.07–4.37 h; f240 about +4.60 h; f384 +5.20–5.29 h.
  - Uploads are out of order: 42 and 44 of 120 hourly `.idx` files arrived before their predecessor.
- **GUST** is a PBL-mixing diagnostic [11]. It is still below the 10 m wind in 8.6% of sea cells [1].

**GFS-Wave** (WAVEWATCH III) [1][7][15]
- **Configuration:**
  - native grids `gnh_10m`, `gsh_15m` and `aoc_9km`, a mosaic [31];
  - **one-way** forcing from FV3 winds, with ice and RTOFS currents;
  - restarts from GDAS;
  - **no wave data assimilation**, so its errors are GFS wind errors carried through WW3;
  - an untagged WW3, WWVER 7.00.
- **Native files:** `global.0p16` (2160×406, 52.5N–15S), `gsouth.0p25` (1440×277, 10.5S–79.5S) and `arctic.9km`.
- **Interpolated files** (no added resolution): `global.0p25`, `atlocn`, `epacif` and `wcoast`.
- **Contents:** 209 steps and 19 fields: WIND, WDIR, UGRD, VGRD, HTSGW, PERPW, DIRPW, WVHGT, SWELL 1–3, WVPER, SWPER 1–3, WVDIR, SWDIR 1–3.
- **Field meanings:**
  - PERPW = 1/fp, i.e. Tp, although ecCodes calls it a mean period;
  - WVPER and SWPER are partition peak periods;
  - DIRPW is the peak direction;
  - GRIB directions are nautical "from".
- **Stations:** 4,036, each with `.bull`, `.cbull` and `.spec`. A `.spec` file is 7.75 MB: 385 hourly records and 50 frequencies (0.035–0.964 Hz) × 36 directions, given "going-to" in radians.
  - `.bull` directions are "going-to".
  - `.cbull` directions are "from", with Hs in feet.

**GEFS-Wave** [2][15]
- c00 plus p01–p30, with mean, spread and prob files: 105 steps (3-hourly to f240, then 6-hourly), 0.25°.
- **Swapped names:** IMWF holds Tm02, and MWSPER holds Tm01.
- The mean and spread files have no SWDIR. Their two unlabelled SWELL records are zero-filled member means.
- The bulletins' first three P(Hs>) columns are headed 1/2/3 m but hold P(Hs > 0.6/1/2 m).
- Only 8 of the 50 spots are within 100 km of a bulletin point.

**ECMWF** [16][17][18][68]
- **HRES wave (ecWAM):** 00Z to 360 h, 06Z to 144 h. Thirteen parameters: swh, pp1d (Tp), mwp and mp2 (mean periods), and period-band Hs from h1012 to h2530 (10–12 s up to 25–30 s) [17]. No partitions.
- **ENS `waef`:** 50 perturbed members, about 550 MB per step file. A NOAA script comment says the ENS wave control moved to stream=wave/type=fc at IFS 50r1 [21].
- **AIFS wave:** `aifs-single` and `aifs-ens`, 6-hourly to 360 h, since 2026-05-12.
- **Licence:** CC-BY-4.0 plus the ECMWF Terms [3].
- ecWAM is IFS-forced and has altimeter-assimilation code [18].

**Open-Meteo** [13][14]
- `ncep_gfswave025` = `global.0p25`; `ncep_gfswave016` = `global.0p16`.
- `wave_period` is PERPW (Tp) for GFS, `mwp` for `ecmwf_wam025` and `VTM10` for MFWAM. Swell period is Tp for GFS but Tm01 for MFWAM.
- `marine_best_match` = MFWAM (CMEMS product [19]) + DWD EWAM + Météo-France currents and SST. ECMWF is added only when the MFWAM run is at least 26 h old.

**Dependency diagram**

```
                 shared global observing system (all centres assimilate overlapping observations)
                          |                                              |
            NOAA GFS atmosphere (FV3)                         ECMWF IFS atmosphere
             |            |            \                          |                \
 Open-Meteo gfs_seamless  |       GEFS atmosphere (31)          ecWAM ("ECMWF WAM",     AIFS (trained on ecWAM
 (+HRRR, US only)         v            v                        altimeter DA code)      hindcast, ERA5, ORAS6)
                  GFS-Wave (WW3)   GEFS-Wave (WW3, same          |-- HRES wave           |-- aifs-single wave
                  one-way, no DA)  physics, ensemble forcing)    |-- ENS waef (50 pf)    |-- aifs-ens waef
                   |                 |-- noaa-gefs-pds           |-- Open-Meteo ecmwf_wam025
                   |                 |-- Open-Meteo ncep_gefswave025
                   |-- NOAA S3 gridded + station .bull/.spec
                   |-- Open-Meteo ncep_gfswave025 / 016
                   |-- Windy gfsWave
 GEFSv12 wave reforecast (WW3 v7.12, GEFSv12 winds) .... related to GEFS-Wave, not identical
 MFWAM (CMEMS; Open-Meteo best_match core) ............. forcing claimed ECMWF IFS, NOT re-verified
 DWD GWAM/EWAM ......................................... forcing not verified
 ERA5 .................................................. ECMWF reanalysis; not independent of ECMWF or altimeters
```

**What the diagram implies**
1. **Not independent, one family:** NOAA GFS-Wave, Windy `gfsWave` and Open-Meteo `ncep_gfswave025`/`016`. Their agreement is an integrity check, not evidence. `families.py` already treats them this way.
2. **Related, not independent:** GEFS-Wave shares WW3 physics with GFS-Wave, and its forcing shares the GFS model core.
3. **Independent of NOAA:** ECMWF ecWAM (HRES and ENS) is the only verified independent chain.
4. **ECMWF-related:** AIFS. Treat MFWAM as ECMWF-related until verified; DWD is unknown.
5. **No chain is fully independent.** Estimate error covariance from verification.

## 7. Nearshore transformation methods

| Model | Type | Can | Cannot | Needs | Role here |
|---|---|---|---|---|---|
| WW3 / GFS-Wave [15] | Phase-averaged spectral; geometric optics | Global and shelf propagation; partitions; spectra at 4,036 points; optional BJ breaking, JONSWAP friction, unresolved-obstacle schemes (Tolman 2003; UOST, Mentaschi et al. 2018), shoreline reflection "from a fraction of a percent to about 50%" | Resolve reefs, canyons or headlands at 1/6–1/4° (about 18–28 km); diffraction; publish directional spread in gridded files | Global winds | Offshore input |
| SWAN [43][44] | Phase-averaged coastal spectral | Refraction, shoaling, depth breaking (BJ with α = 1, γ = 0.73, or variable γ), friction | Proper diffraction near harbours or reflecting obstacles (phase-decoupled); phase-resolved focusing | Surveyed bathymetry; boundary spectra | Offline transfer tables |
| XBeach [45] | Stationary, surfbeat, non-hydrostatic | Surf-zone hydrodynamics and morphology; wave-resolving diffraction | Cheap diffraction (wave-resolving mode is far costlier) | High-resolution bathymetry | Research only |
| FUNWAVE-TVD [46] | Fully nonlinear Boussinesq | Phase-resolved breaking (NSWE switch at H/h > 0.8; Cbrk1 = 0.65, Cbrk2 = 0.15), wetting–drying, MPI | Hourly operation for 50 spots | Very fine grids (lab benchmark dx = 0.025 m) | Offline focusing studies |
| CDIP MOP [47] | Precomputed spectral transforms to about 10 m depth | Operational precedent | Run outside California without new setup | Per-site transforms | Design pattern |

None of these can run hourly for 50 spots without surveyed bathymetry. Instead, precompute offline a per-spot T(f, θ) table from SWAN or backward ray tracing. It replaces max(cos(θ − β), 0)·T_dir(θ)·Kf in Section 12, so the online cost stays a dot product.

## 8. Available datasets

The "Sandbox" column is reachability from the research environment. Production reachability is untested.

| Dataset | Exact access path | Sandbox | Notes |
|---|---|---|---|
| GFS 0.25° | `https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF` + `.idx` | Yes | From 2021-01-01; `atmos/` from cycle 2021-03-22 12Z; SNS topic `arn:aws:sns:us-east-1:123901341784:NewGFSObject` (Lambda/SQS only) [1][3] |
| GFS mirror | `https://storage.googleapis.com/global-forecast-system/` (same paths) | Yes | Fallback [1][12] |
| GFS sflux | `…/atmos/gfs.tHHz.sfluxgrbfFFF.grib2` (Gaussian N768) | Yes | 10 m U/V, LAND; no GUST or PRMSL; only 2 of 50 points touch land [1] |
| GFS BUFR | `…/atmos/gfs.tCCz.bufrsnd.tar.gz` | Yes | The only BUFR product found [1] |
| GFS-Wave gridded | `…/gfs.YYYYMMDD/HH/wave/gridded/gfswave.tHHz.{global.0p16,gsouth.0p25,arctic.9km,global.0p25,…}.fFFF.grib2` + `.idx` | Yes | Use native grids first [1] |
| GFS-Wave stations | `…/wave/station/bulls.tHHz/gfswave.{ID}.{bull,cbull,spec}`; `spec_tar.gz` (1.73 GB) | Yes | 4,036 points; 5 buoy points reportedly to be removed [5] |
| GEFS-Wave | `https://noaa-gefs-pds.s3.amazonaws.com/gefs.YYYYMMDD/HH/wave/gridded/gefs.wave.tHHz.{c00,p01..p30,mean,spread,prob}.global.0p25.fFFF.grib2`; `…/wave/station/gefs.wave.tHHz.bull_tar` | Yes | 00Z bulletins posted at 06:55 UTC [2] |
| ECMWF IFS / AIFS waves | `https://ecmwf-forecasts.s3.amazonaws.com/{YYYYMMDD}/{HH}z/ifs/0p25/{wave,waef}/…-{step}h-{wave-fc,waef-ef,waef-ep}.grib2`; `…/aifs-single/0p25/wave/`; `…/aifs-ens/0p25/waef/` + JSON `.index` | Yes | CC-BY-4.0; S3 SlowDown throttling [16] |
| GEFSv12 wave reforecast | `https://noaa-nws-gefswaves-reforecast-pds.s3.amazonaws.com/GEFSv12/reforecast/{YYYY}/{YYYYMMDD}/{gridded,station}/gefs.wave.{YYYYMMDD}.{c00,p01..}.{tab,spec}.nc` | Yes | 2000–2019; 658 buoys; hourly `tab.nc`; no partitions [20] |
| NCEI GFS | `https://www.ncei.noaa.gov/data/global-forecast-system/access/grid-004-0.5-degree/forecast/YYYYMM/YYYYMMDD/gfs_4_YYYYMMDD_HHMM_FFF.grb2` | No | "Spotty at best" [9][12] |
| NCAR RDA d084001 | `data.rda.ucar.edu/d084001/YYYY/YYYYMMDD/gfs.0p25.YYYYMMDDHH.fFFF.grib2` | No | No `.idx` [10][12] |
| NOMADS grib filter | `https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?file=…&lev_10_m_above_ground=on&var_UGRD=on&var_VGRD=on&subregion=&leftlon=…&dir=%2Fgfs.YYYYMMDD%2FHH%2Fatmos` | No | A subregion cannot cross 180° [8] |
| Open-Meteo | `https://marine-api.open-meteo.com/v1/marine`; `https://api.open-meteo.com/v1/gfs` | No | Used by the repo; verified live elsewhere on 2026-10-09 |
| IMOS altimeter | `https://imos-data.s3.ap-southeast-2.amazonaws.com/IMOS/SRS/Surface-Waves/Wave-Wind-Altimetry-DM00/{MISSION}/…_FV02_{LLL}{N|S}-{LLL}E-DM00.nc` | Yes | 1° tiles; SWH_KU_CAL + flags; CC BY 4.0; Jason-3 to 2026-06-21 [72] |
| AODN wave buoys | `s3://aodn-cloud-optimised/wave_buoy_realtime_nonqc.parquet/`, `…/wave_buoy_delayed_qc.parquet/` (use `%2520` for `%20`) | Yes | All 8 Australian spots; QLD/WA real-time WHTH only [73] |
| Met Office buoys | `https://met-office-marine-observations-data.s3.eu-west-2.amazonaws.com/{issue}_{start}_{end}/{station}_buoy_{uuid}.csv` | Yes | Rolling 168 h; swell/wind-sea Hm0; CC BY-SA 4.0 [74] |
| Sofar Spotters | `https://sofar-spotter-archive.s3.us-west-2.amazonaws.com/` | Yes | 2019 to March 2022; own agreement [75] |
| ARCO-ERA5 | `gs://gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3` | Yes | Reanalysis [76] |
| NDBC | `https://www.ndbc.noaa.gov/data/realtime2/{ID}.{txt,spec,data_spec,swdir,swdir2,swr1,swr2}`; `…/view_text_file.php?filename={id}{h,w,d,i,j,k}{YYYY}.txt.gz&dir=data/historical/{type}/`; `https://dods.ndbc.noaa.gov/thredds/fileServer/data/stdmet/{id}/{id}h{YYYY}.nc` | No | Real-time ≈ 45 days [69][70] |
| CDIP | `https://thredds.cdip.ucsd.edu/thredds/fileServer/cdip/archive/{stn}p1/{stn}p1_historic.nc` | No | [22] |
| CO-OPS | `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?` | No | 8 US spots; 31 days per request for water_level [79] |
| CMEMS in situ waves | doi:10.17882/70345 | No | About 1990 to October 2022 [78] |
| ESA Sea State CCI v4 | doi:10.5285/82273fe59b784e7c914e1bd3049e3483 | No | About 6 km along-track [77] |
| Goddard–Caldwell | `https://ncei.noaa.gov/archive/accession/0001754` | No | Only observed-surf series (Pipeline, Sunset, Waimea) [49] |

**Missing or unusable:**
- No NDBC, CDIP or CO-OPS mirror bucket; S3 `ndbc` holds only a 2020 GeoJSON.
- Sentinel-3 data in `meeo-s3` end 2024-08-07.
- The PO.DAAC bucket returns 403.
- Only 1 of 600 sampled ISD marine files has wave fields [3].
- CoastSnap measures shoreline position, not surf height [53].

## 9. Data-quality limitations

**Land masks**
- 33/50 break coordinates and 14/50 forecast points have land in the GFS 0.25° stencil.
- Sea-only and all-cell 10 m wind speeds differ by up to 0.82 m/s, at Burleigh Heads.
- Puerto Escondido is missing in global.0p25 [1].

**Wind fields**
- GUST ≥ wind fails in about 9% of sea cells.
- The median gust/wind ratio is 1.14 over sea and 1.44 over land, so never interpolate gust across land.
- PRMSL and MSLET agree at sea, but differ by up to about 16 hPa over high terrain [1].

**Timing and versions**
- Out-of-order uploads: define "cycle ready" as every required `.idx` being present [1].
- GFSv17 will change paths and the wave mesh [5][7].
- ECMWF's 06Z/18Z waves moved from `scwv` to `wave` around 2026-05-12 [16].

**Partitions and periods**
- Partition index is a height rank. A 16.5 s system was partition 2 in one cell and partition 3 in the next [1], so index matching mixes swells.
- GFS periods are peak periods.
- GEFS IMWF is Tm02 and MWSPER is Tm01.
- ECMWF pp1d is Tp; mwp and mp2 are mean periods.
- Open-Meteo `wave_period` changes meaning by model [14][15][16].

**Directions**
- GRIB and `.cbull` are "from".
- `.bull` and `.spec` are "going-to", and `.spec` is in radians [1].
- AODN SSWMD is magnetic [73].
- NDBC `.spec` gives SwD as compass strings [70].

**Ensemble files**
- GEFS bulletin P(Hs>) columns are mislabelled, and Tp spread is shown in "(m)".
- GEFS mean and spread SWELL fields are zero-filled and carry no direction [2].
- ECMWF `waef` has no control member, and its probability files exist only at 240 h and 360 h [16].

**Buoys**
- NDBC uses `MM` in real-time files and per-column 9-fills in historical ones (PRES is both 999.0 and 9999.0).
- ndbc-api's blanket mask drops a valid WDIR of 99°, and it requests `.swden` where NDBC serves `.data_spec` [69][70].
- AODN QLD and WA real-time data carry time-domain WHTH only.
- Cape Naturaliste reports about twice a day.
- The Apollo Bay and Storm Bay Spotters stopped on 2026-09-21 [73].
- Met Office keeps only 7 days [74].

**Representativeness**
- The 56006 bulletin point is about 19 km from the buoy [1][73].
- The model depth at 51201 is 472.7 m [1].
- One coastal Sentinel-6A tile held only about 3,455 records in 5.5 years [72].

**Reference data**
- ERA5 assimilates altimetry, so it is not independent truth [76].
- The reforecast is not the operational configuration: 5 members, 03Z, WW3 v7.12 [20].
- Goddard–Caldwell is visual and subjective [49].

## 10. Candidate ML algorithms

| Family | Problem | Evidence (verification level) | Fit for this app |
|---|---|---|---|
| Linear / ridge MOS per lead day and region | Bias of Hs, Tp, direction | Engineering baseline | **First**: the bar to beat |
| EMOS / NGR (truncated, log-normal), min-CRPS | Calibrated distribution from mean and spread | [83][84][85] | **Second** |
| Quantile regression forest | Nonparametric quantiles | [86]; beat EMOS for temperature and wind [87] | Third, given enough buoy pairs |
| LightGBM / CatBoost quantile | Tabular point residuals | [54] (M); XGBoost lost to TrajGRU on gridded fields [58] | Practical for tabular data; not proven superior |
| MLP / LSTM / CNN residual ensembles | Point Hs residual to 16 days | Mixed results [60]; [57] (M) | Only with reforecast training and chronological tests |
| Spectral CNN residual (SWRL Net) | Short-range correction from buoy history | "Up to 24-hours" [55] | **Not** for 5–10 days |
| TrajGRU spatio-temporal net | Gridded correction to 240 h | [58], scored against ERA5 | Too heavy for 50 points |
| Transformer / attention | Hs or spectral correction | [59][61] (M) | Watch list |
| SWAN surrogates (MLP/SVM, RF, MDA–RBF) | Fast nearshore transfer | [62]; [63] (M); [64] | Later, for a few spots with bathymetry |
| ANN from WW3 to surf; mixture density networks | Surf height | [65][66][67] (M) | Needs labels; template for P(surf in range) |
| Conformal (CQR, EnbPI, ACI) | Coverage under drift | [88][89] | Wrapper on EMOS or QRF |
| CNN on camera images | Surf-quality **labels** | [52] | Measurement, not forecast |
| AIFS Waves | Extra ensemble member and features | [68][16] | Ingest; do not train |

## 11. Published validation results

| Study | Target | Model | Metric | Value | Split / sample |
|---|---|---|---|---|---|
| Campos et al. 2024 [23] | Offshore Hs (buoys, altimeters) | WW3 forced by GEFSv12 | Correlation | 0.95–0.96 day 1; 0.86–0.88 within week 1; below 0.70 after day 10; bias ≈ 0 | 20-year reforecast; details not seen (A) |
| Campos et al. 2020 [24] | Offshore Hs, U10 (altimeters) | NCEP wave ensemble | Scatter error | Mean substantially below control; extremes low beyond day 5 | 2017; 33.2 M matchups |
| Saetra & Bidlot 2004 [26] | Hs, Tp | ECMWF wave EPS | Relative economic value | Beats control and poor-man's ensembles; larger gain for joint Hs–Tp | Day 5 |
| Hahner et al. 2026 [68] | Offshore SWH (buoys, altimeters) | AIFS Waves vs ECMWF operational | Error reduction | About 10% (about 1 day of lead) | May–Aug 2024; authors' own evaluation |
| Zhang et al. 2023 [58] | Gridded SWH against **ERA5** | TrajGRU on ECMWF-IFS | MAE / RMSE change | MAE −12.97% to −46.24% (spring), −13.79% to −38.95% (winter); at 240 h, MAE −13% (spring), RMSE −17% (winter) | Train 1–2 months, test the next month |
| Minuzzi & Farina 2025 [60] | Buoy Hs (6 buoys) | NN residuals on reforecast control | RMSE, NN/NOAA (m) | Itajaí 0.56/0.56; Santos 0.38/0.55; Fortaleza 0.21/0.22; Rio Grande 0.58/0.57; Recife 0.31/0.38; Vitória 0.56/0.50; MAPE better at 3, worse at 3 | Train 2013 to event; one ~16-day test per buoy |
| James et al. 2017 [62] | Nearshore Hs against **SWAN** | MLP surrogate | RMSE; runtime | 9 cm (8.0–10.2); over 90% of periods correct; 0.12 s vs 583 s | 10 shuffled 90/10 splits |
| Atkin et al. 2023 [52] | Break and crest points (camera) | CNN | mAP | 0.794 (about 1 M images) | One site; measurement |
| Komar & Gaughan 1972 [33] | Breaker height (lab and field) | Airy theory with flux | Fit | k = 0.39; much better than Munk | 3 lab + 1 field datasets |
| NODC 0001754/0001753 [49] | **Observed surf**, Oahu | Shoaling-only from 51001; photos | Agreement | Within 10–15%; face ≈ 2 × Hawaii scale within 10–20% | 1981–2002; 1998–2004 |
| Taillardat et al. 2016 [87] | Temperature, wind | QRF vs EMOS | CRPS | QRF better | 35 members, 3–54 h |
| Research analysis [2] | GEFS-Wave Hs against **own f000** | 31-member mean and spread | RMSE (m) d1/3/5/7/10; spread/RMSE; coverage; BSS | 0.093/0.236/0.353/0.480/0.631; 1.36/1.04/1.03/1.00/0.94; ±1.645σ coverage 0.90–0.91; day-7 BSS 0.56 (>2 m), 0.35 (>3 m) | 51 cycles, Aug–Oct 2026; not peer reviewed |
| Research demo [1][73] | Buoy 56006 WHTH | GFS-Wave `.bull` | Bias / RMSE / CC (m) | d0 −0.31/0.42/0.94; d5 −0.18/0.54/0.80; d7 −0.29/0.61/0.76; d9 −0.16/1.15/0.11 | 46 cycles; n = 76–89; illustrative |

**Not re-verified, so excluded from design:** the numbers in [54][55][56][57][59][61][63][65][66], Goda's coefficients of variation [36], Zhang 2021's gains [37] and Stringari & Power's fractions [38].

## 12. Recommended baseline algorithm

This is the physics baseline every ML model must beat: `linear_wave_v2` as implemented, plus the input changes below. Every constant is an assumption until calibrated.

**Inputs**
- Per partition i: Hs_i, peak period T_i, and θ_i (nautical "from", true north). Take them from GFS-Wave native `global.0p16`, or `gsouth.0p25` / `arctic.9km`, with `global.0p25` as a fallback [1].
- Per spot: shore normal β; transmission T_dir[36]; extraction depth h_m; focusing Kf_ref, κ and T*; optionally d_ctrl and γ_r.
- Optional static quality descriptors, scored separately from size: bathymetric class and peel-angle limits by skill [50][51].

**Steps**

0. **Normalise.** Convert directions to "from" and tag each period with `period_kind`. Match partitions by direction (±15–30°) and period (±1.5 s), never by index.
1. **De-shoal.** If h_m < L0/2:

   ```
   k from Fenton–McKee + Newton [32];   Ks = sqrt(Cg0/Cg(h_m));   H0_i = Hs_i/Ks
   ```

   If h_m is unknown, assume deep water and flag it.
2. **Direction and shadowing.**

   ```
   R_i = Σ_θ D(θ; θ_i, σ_i)·max(cos(θ − β), 0)·T_dir(θ)·Δθ
   D = normalised wrapped Gaussian
   σ_i = 10° (T ≥ 13 s), 15° (9–13 s), 30° (wind sea or T < 9 s)
   ```

   These widths are assumptions: GFS-Wave publishes no directional spread [15].
3. **Breaker height per partition** [33].

   ```
   Hb_i = 0.39·g^0.2·(T_i·H0_i^2·R_i)^0.4·Kf(T_i)
   Kf(T) = Kf_ref·[1 + κ·max(0, T − T*)/T*]    (κ = 0 for open beaches)
   ```

4. **Energy-flux combination** (since Hb^2.5 ∝ H0²·T):

   ```
   Hb = (Σ_i Hb_i^2.5)^0.4
   ```

5. **Optional caps.**
   - Depth: Hb ≤ γ_r·(d_ctrl + η_tide), with γ_r between 0.55 (reef) and 0.78 (McCowan) [15].
   - Friction on wide shallow shelves only: H ← H·exp(−Σ |Γ|·(n − 1/2)/(g·d_j·Cg_j)·Δx_j), with |Γ| = 0.038 m²/s³ [15][44].

**Spectral variant.** Use it where a GFS-Wave `.spec` station is near and similarly exposed (for example 51201 for the North Shore):

```
X  = 16·Σ_f Σ_θ E(f,θ)·f^(−1)·max(cos(θ − β), 0)·T_dir(θ)·Δf·Δθ
Hb = 0.39·g^0.2·X^0.4·Kf
```

For a delta spectrum at normal incidence X = Hs²·Tp, which makes a good test. Add 180° to `.spec` directions, and de-shoal from the model depth.

**Reporting**
- Typical = Hb, the significant face.
- Sets ≈ 1.27·Hb, a Rayleigh upper bound [39].
- Hawaii scale ≈ face/2 [49].
- State the definition next to the method label.
- Show users P(score ≥ their threshold) with the 80% range. One lab study found that uncertainty estimates improved non-experts' decisions [93].

**Uncertainty**

```
σ_ln(Hb)^2 = (0.8·σ_lnH)^2 + (0.4·σ_T/T)^2 + σ_m^2
σ_lnH^2    = (k_L·spread/mean)^2 + σ0^2
```

- k_L = 1.0–1.1, because unfiltered log error exceeds spread by 6–13% [2].
- σ0 = 0.15–0.20, and σ_m = 0.30 (both heuristics).
- Without an ensemble, σ_lnH = 0.080/0.192/0.276/0.352/0.430 at days 1/3/5/7/10. Label this "against model analysis; a lower bound".
- At day 7, σ_ln(Hb) ≈ 0.39, giving p10–p90 of about ×0.60 to ×1.65 [2][33].
- Preferred: Monte Carlo with about 200 draws through `breaking.py` and `scoring.py`, giving p10/p50/p90 and P(score ≥ threshold).

**Calibration**, once observations exist:

```
ln H_obs = ln a + b·ln H0 + c·ln T + d·ln R (+ e·tide)
```

Shrink toward b = 0.8, c = 0.4 and d = 0.4, with a = 0.39·g^0.2·Kf. Use one target convention per spot.

**Unit tests**
- Fenton–McKee plus Newton within 1e-10.
- Ks → 1 in deep water, minimum 0.913.
- cos^0.4 within 1% up to 75°.
- Two equal partitions give 2^0.4 × one.
- X = Hs²·Tp for a delta spectrum.
- Kr cubic continuous at its cap.

**Gaps against the code:** estimated profiles, no spectral variant, `global.0p25` input, filtered `LEAD_TABLE`.

## 13. Recommended machine learning approach

At 5–10 days, error is dominated by wind-forcing error for storms that have not formed yet, and buoy history carries little of that. ML should therefore **post-process physics and ML ensembles, with lead time as a feature**. It should not replace them. Expect gains of about 10%, or about 1 day of lead [68]. Do not expect the 13–46% of [58], which was scored against ERA5 on adjacent-month tests.

**Stages**
0. **Archive and harness.** Persist every forecast row (cycle, valid time, lead, provider, raw variables), exempt from `RETENTION_FORECAST_DAYS`. Verify against AODN, Met Office and IMOS now [72][73][74], and NDBC/CDIP where deployment allows.
1. **Linear MOS plus performance-weighted multi-model averaging.** Members: GFS-Wave, GEFS mean, ECMWF HRES, AIFS single and AIFS ENS mean. Fit per lead day and region, with Hs in log space.
2. **EMOS.** Log-normal or truncated normal, fitted by minimum CRPS [83][84][85], using `scoringrules` [90].
3. **Residual quantile models.** LightGBM/CatBoost quantiles 0.1/0.5/0.9, or QRF [86]. Compare them with EMOS by CRPS [87].
   - Features: lead; hs, tr, fp, th1p, sth1p; GEFS mean and spread; inter-model disagreement; AIFS h1012–h2530; month sin/cos; exposure descriptors.
   - **No station-ID features.**
   - Train on reforecast station tables using all members [20]; validate on 2021+ operational data [1][2].
4. **Conformal wrapper.** CQR or ACI per lead bucket [88][89].
5. **Surf level.** Collect reports and camera labels now [52]; Browne et al. used daily visual observations [66]. After one or two seasons, fit the Section 12 calibration with robust regression.

**When NOT to deploy ML**
- Buoy-driven autoregressive forecasters for 5–10 days. The evidence for them covers 24 h or less [55].
- Any model that fails to beat Stage 1 on **every lead day** on a chronological holdout, with block-bootstrap intervals excluding zero.
- Results from shuffled splits [62], reanalysis truth [58], single events [60] or a single season [68], until re-evaluated.
- Breaking-height or score ML before surf labels exist.
- SWAN surrogates for all 50 spots: each needs bathymetry and inherits SWAN error [62].
- Immediately after GFSv17 or any model upgrade, until re-baselined [5][7].
- Spots with no buoy of similar exposure, unless leave-one-region-out tests pass.
- Whenever longer leads score better than shorter ones (leakage).
- Buckets with fewer than 200 pairs (the repo's `MIN_VERIFICATION_PAIRS` heuristic).

## 14. Experimental methodology

**Chronological splits**
- Train on reforecast 2000–2016, validate on 2017–2019, test on operational data from 2021 [20][1].
- `atmos/` and `wave/` start at cycle 2021-03-22 12Z; Herbie's 2021-03-23 cutoff misses two cycles [1][12].
- Leave gaps of at least the maximum lead plus 3 days (19 days for the reforecast).

**Rolling-origin backtests.** Expanding window, refitted monthly and scored on the next month; EMOS on rolling 40–60-day windows. Report every fold.

**Leakage rules**
1. Use only data available at issue time:
   - GFS f000 at about +3.6 h and f240 at about +4.6 h [1];
   - ECMWF at 7–9 h [16];
   - GEFS bulletins at about +6.9 h [2].
2. Fit scaling and decompositions on training data only.
3. Never use analyses, observations or reanalysis after issue time as features.
4. Do not score altimeter-assimilated products against ERA5 [76].
5. Do not count same-family sources as independent (Section 6).
6. Bootstrap by blocks of cycles, because consecutive cycles share valid times; never split randomly.
7. Match swells by direction and period, not by index.

**Lead stratification.** Bins at days 0–1, 1–3, 3–5, 5–7 and 7–10, plus per-day curves. Compare models only on identical samples.

**Geographic holdout.** Leave-one-buoy-out and leave-one-region-out folds. Report transfer to spot points separately, and stratify by basin, season and Hs tercile.

**References.** Raw model; persistence; climatology (reforecast or ERA5 day-of-year); Stage 1 MOS.

**Observation matching**
- Compare Hm0 with WSSH, `hm_0` or WVHT, and flag WHTH.
- Correct SSWMD to true north and convert `.bull` directions.
- Record the buoy–model distance.
- Altimeter collocation [22][72]: within 35 km and 30 min; depth ≥ 80 m; ≥ 50 km from the coast; IMOS flags 1–2; pooled by basin.

**Events and surf**
- Match swell events with ±12 h arrival tolerance and site-specific thresholds; Hs ≥ 3 m occurred in 60% of Cape Naturaliste observations [73].
- Validate Pipeline, Sunset and Waimea against Goddard–Caldwell, allowing for its H1/10 definition [49].

**Model upgrades** are regime breaks. Start statistics afresh after GFSv17.

## 15. Model evaluation metrics

**Deterministic** (WW3-tools implementation of [29]; m = model, o = observation) [22]:

```
bias = mean(m) − mean(o)            NBias = bias/|mean(o)|
RMSE = sqrt(mean((m−o)^2))          NRMSE = sqrt(Σ(m−o)^2/Σo^2)
SCrmse = sqrt(RMSE^2 − bias^2)      SI = sqrt(Σ((m−mean m)−(o−mean o))^2/Σo^2)
HH = sqrt(Σ(m−o)^2/Σ(m·o))          CC, N, bias/RMSE above the observed 95th percentile
```

- NRMSE flatters negatively biased runs [29], so never rank models on RMSE alone.
- State which SI definition is used.
- Use circular errors for direction, and Tm01/Tm02 or partition-matched Tp for period.
- Skill score: SS = 1 − MSE_f/MSE_ref.

**Probabilistic**
- CRPS = ∫(F(x) − 1{x ≥ y})² dx, which is strictly proper [82]. Closed forms are in [90].
- Fair ensemble CRPS [81][92], needed to compare a 5-member ensemble with one of 31–50:

  ```
  CRPS_fair = (1/M)·Σ|x_i − y| − (1/(2M(M−1)))·Σ_{i≠j}|x_i − x_j|
  ```

- Brier score BS = mean((p − o)²), with BSS = 1 − BS/BS_ref at the alert thresholds.
- Reliability diagrams.
- PIT and rank histograms [80]: flat does not mean reliable, and observation error and independence matter.
- 50/80/90% coverage, with width.
- Interval score IS = (u − l) + (2/α)(l − y)·1{y < l} + (2/α)(y − u)·1{y > u} [82].
- Circular CRPS for directions [91].

**Events** [92]

```
POD = H/(H+M)   FAR = F/(H+F)   CSI = H/(H+M+F)   frequency bias = (H+F)/(H+M)
ETS = (H − Hr)/(H + M + F − Hr),   Hr = (H+M)(H+F)/N
```

Add Peirce and Heidke, and always report the base rate. NOAA's week-2 verification code (BSS, CRPSS, ROC, reliability against climatology and persistence) is a usable template, though it scores window probabilities [21][25].

**Surf level.** Spearman correlation against reported quality, and ROC AUC for "Good or better". Report every metric with block-bootstrap intervals and sample sizes.

## 16. Expected computational requirements

| Feed | Size | Per cycle |
|---|---|---|
| GFS U10, V10, GUST, PRMSL (byte ranges) | 3.53 MB per step (of 534.8 MB) | About 568 MB to f240; about 738 MB to f384 [1] |
| GFS-Wave global.0p25 | About 11.5 MB per step; one field 0.3–1.0 MB | Byte ranges [1] |
| GFS-Wave `.spec` | 7.75 MB per station | Stations near spots only [1] |
| GEFS-Wave mean and spread (HTSGW, PERPW, WIND) | About 5 MB per step | About 125 MB (6-hourly, days 4–10); about 400 MB to f240 [2] |
| GEFS-Wave member partitions | About 3.5–3.9 MB per member per step | About 3 GB per day (31 members, days 4–10) [2] |
| ECMWF `waef` | About 550 MB per step; one member's swh 0.78–0.86 MB | Byte ranges only [16] |
| AIFS single / ENS pf | About 8–9 MB / about 451 MB per step | Back off on SlowDown [16] |

**One-off.** Reforecast `tab.nc` is about 8.7 MB per member-day (19 MB on Wednesdays). That is roughly 65 GB for c00 over 2000–2019, and about 0.5 TB for all members (derived from file sizes). `spec.nc` is 0.52–1.13 GB per member-day, and the gridded GRIB2 about 1.75 GB per member-day with `.idx` files [20].

**CPU** (measured in the sandbox, Python 3.13)
- `nearshore.estimate` with four partitions takes about 0.37 ms per call with the loop version of `directional_factor`. With the NumPy-cached version now in the working tree, it takes about 0.11 ms.
- So a 200-draw Monte Carlo over 50 spots × 241 hourly steps takes about 900 s, or about 270 s, on one core.
- Limiting it to daylight hours in the 5–10-day window should cut that roughly fourfold (estimated).
- EMOS is trivial. GBDT or QRF on a few million rows is a single-node CPU job (an engineering estimate, not measured).
- SWAN took 583 s per Monterey Bay forecast (112 s on 8 cores) [62]. FUNWAVE needs MPI and fine grids [46]. Both are offline only.

**Storage.** The archive holds 81 rows per spot per run (3-hourly to 240 h). At 50 spots × 4 runs a day, that is about 16,200 rows per day per source, or about 5.9 M per year.

## 17. Known limitations

- **No surf truth at 47 of 50 spots.** Goddard–Caldwell covers only the 3 Oahu spots, and its NCEI host is blocked here [49]. All breaking heights are uncalibrated.
- **Spot physics is estimated.** No profile file exists.
- **Skill at 5–10 days is limited.** Correlation is 0.86–0.88 within week 1 [23]. Day-7 RMSE is about 0.48 m even against the model's own analysis, a lower bound [2]. Extremes are underestimated beyond day 5 [24].
- **Coastal wind.** A 13 km grid cannot resolve sea breezes, terrain channelling or dawn offshores. This is inferred from grid spacing; the supporting literature could not be verified. Yet wind carries the largest score weight (0.30).
- **Breaker physics.** Komar–Gaughan assumes regular waves on a plane beach, and its coefficient is 11–13% above linear theory [33]. γ scatters from 0.6 to 1.59 [44]. Caldwell–Aucan is unconfirmed [48]. Focusing is a prior [41][42].
- **Heuristic uncertainty.** σ0, σ_m and the 1.27 sets ratio are heuristics. The spread-skill sample is 7 weeks against the model's own analysis [2].
- **Tide** comes from CMEMS via Open-Meteo [14]. Size ignores tide without a documented controlling depth.
- **Literature access.** Many sources are metadata or search extract only (see the register).
- **Pending changes.** GFSv17 will change paths and physics [5][7]. The NOAA and ECMWF provider modules are missing.
- **Production reachability** of NDBC, CDIP, CO-OPS and NCEI is untested [3].

## 18. Future improvements

1. **Profiles.** Curate `physical_profiles.json`: β from coastline geometry, shadow tables by ray-casting, extraction depth, and focusing priors with provenance.
2. **Ingesters.** Implement `noaa.py` and `ecmwf.py`:
   - native `global.0p16`;
   - an all-`.idx` readiness check;
   - canaries on the 743-record `.idx` list and process ID 96;
   - version-keyed path templates [1][7].
3. **Wind.** Fetch wind at the offshore point or with a sea-only stencil; consider sflux U/V [1]; label HRRR blending.
4. **Spectra.** Ingest `.spec` and add the spectral variant.
5. **Ensembles.** Add ECMWF ENS and AIFS ENS, verifying each before combining [16][68].
6. **Uncertainty.** Replace `LEAD_TABLE` with the unfiltered values and set k_L ≈ 1.05, then refit both from buoy pairs.
7. **Calibration ladder.** Promote EMOS, then QRF/GBDT, then conformal, each only if it beats its predecessor on every lead day.
8. **Transfer tables.** Precompute offline T(f, θ) tables where bathymetry exists [43][47].
9. **Surf labels.** Collect in-app reports and camera labels [52]; get the Goddard–Caldwell series where NCEI is reachable [49].
10. **Verification dashboard.** Per lead and region (Section 15).
11. **Re-verification.** Check the originals for Caldwell–Aucan [48], Campos 2024 tables [23], Goda [36], Stringari & Power [38], Browne [65][66] and the PNS PDFs [5].
12. **Docs.** Update `docs/SURF_SCORING.md` to algorithm 2.0.0 and the spread-based confidence index.

---

## Appendix: Source register

Each entry gives the title; author or organisation; year or version; URL or DOI; then **Finding**, **Relevance**, **Limitations** and **Access**.

### A. Data, model documentation and code

**[1] NOAA NODD GFS bucket `noaa-gfs-bdp-pds` and GCP mirror `global-forecast-system`.**
- **Source:** NOAA/NCEP. Cycles 2026-10-09/10; archive samples 2021–2025; 46 bulletin cycles. https://noaa-gfs-bdp-pds.s3.amazonaws.com/ ; https://storage.googleapis.com/global-forecast-system/
- **Finding:** v16 layout, fields, grids, latency, land-mask statistics, GUST behaviour; GFS-Wave grids, fields, 4,036 stations, `.spec` format, directions.
- **Relevance:** Ingestion design.
- **Limitations:** A few cycles; one valid time for the field statistics.
- **Access:** Opened directly (listings, range GETs, ecCodes 2.49).

**[2] NOAA NODD GEFS bucket `noaa-gefs-pds` (GEFS-Wave).**
- **Source:** NOAA/NCEP EMC. Cycles 2026-08-20 to 2026-10-09. https://noaa-gefs-pds.s3.amazonaws.com/
- **Finding:** File layout, swapped period names, zero-filled mean SWELL, mislabelled bulletins; 51-cycle spread-skill analysis.
- **Relevance:** Ensemble uncertainty.
- **Limitations:** Verified against its own f000; 7 weeks.
- **Access:** Opened directly; analysis re-run.

**[3] AWS Open Data Registry dataset YAMLs, and sandbox reachability tests.**
- **Source:** awslabs/open-data-registry, dcba4f3 (2026-10-08). https://github.com/awslabs/open-data-registry
- **Finding:** GFS bucket and SNS topic; the v16.3.0 note; ECMWF CC-BY-4.0 plus Terms (doi 10.21957/open-data); blocked hosts; missing mirrors.
- **Relevance:** Access and licences.
- **Limitations:** The registry website is blocked; mirror absence cannot be proven.
- **Access:** Opened directly (git clone, curl).

**[4] Service Change Notice 22-104: GFS v16.3.0.**
- **Source:** NOAA NWS, 2022. https://www.weather.gov/media/notification/pdf2/scn22-104_gfs.v16.3.0.pdf
- **Finding:** Effective 2022-11-29.
- **Relevance:** Current baseline.
- **Limitations:** Not opened.
- **Access:** Link in [3].

**[5] PNS 26-29 (Science for GFSv17) and PNS 26-30 (GFSv17 product removal).**
- **Source:** NOAA NWS/EMC, April 2026 (per [6]). https://www.weather.gov/media/notification/pdf_2026/pns26-29_Science_for_GFSv17.pdf ; https://www.weather.gov/media/notification/pdf_2026/pns26-30_CommentsGFSv17_ProductRemoval.pdf
- **Finding:** v17 proposed for October 2026; paths and names change; 5 buoy points removed.
- **Relevance:** Ingestion will break.
- **Limitations:** PDFs not opened.
- **Access:** Via [6]; a v17 PNS is corroborated by [7].

**[6] GitHub issues h0ffmann/ww3-gpu #85 and #86.**
- **Source:** GitHub user h0ffmann, 2026-10-09. https://github.com/h0ffmann/ww3-gpu/issues/85 ; https://github.com/h0ffmann/ww3-gpu/issues/86
- **Finding:** Quote the PNS URLs, dates and content.
- **Relevance:** Only route to the PNS.
- **Limitations:** Secondary, unofficial.
- **Access:** Opened directly.

**[7] NOAA-EMC global-workflow.**
- **Source:** NOAA EMC, 2026. PR #5298; issues #5211, #5258, #4981; OMD-gfs.v16.3.34; dev/gfs.v17; develop 01b3e81; gefs_v12.3.22-0. https://github.com/NOAA-EMC/global-workflow
- **Finding:** 0.5°/1° products removed; v17 configuration; v16 wave configuration (one-way, no wave DA); v16.3.33; GEFS bulletin thresholds.
- **Relevance:** Forcing and the v17 transition.
- **Limitations:** Development code.
- **Access:** Opened directly.

**[8] Configuring SNAP with NOAA GFS meteorological data (NOMADS grib filter).**
- **Source:** MET Norway, metno/snap b92aa93. https://github.com/metno/snap/blob/b92aa93eebbc8fcf74e958fa6186371539886a1f/src/naccident/examples/gfs/README.md
- **Finding:** Filter syntax; no crossing of 180°.
- **Relevance:** Where NOMADS is reachable.
- **Limitations:** Third-party; not run.
- **Access:** Opened directly.

**[9] NCEI Global Forecast System product page.**
- **Source:** NOAA NCEI. https://www.ncei.noaa.gov/products/weather-climate-models/global-forecast
- **Finding:** Archive path template.
- **Relevance:** Long archive.
- **Limitations:** Not opened.
- **Access:** Via [12].

**[10] NCEP GFS 0.25° Historical Archive (RDA d084001).**
- **Source:** NCEP / NCAR RDA. https://rda.ucar.edu/datasets/d084001/
- **Finding:** File path; no index files.
- **Relevance:** Pre-2021 data.
- **Limitations:** Not opened; DOI and cadence not verified.
- **Access:** Via [12].

**[11] UPP CALGUST.f.**
- **Source:** NOAA-EMC UPP, develop (history to 2025-06-30). https://github.com/NOAA-EMC/UPP/blob/develop/sorc/ncep_post.fd/CALGUST.f
- **Finding:** Gust = max of U10 + (U_k − U10)·(1 − min(0.5, dz/2000)) to the PBL top.
- **Relevance:** Defines GUST.
- **Limitations:** develop may differ from operations.
- **Access:** Opened directly.

**[12] Herbie GFS template.**
- **Source:** B. K. Blaylock, main branch 2026-10-10 (DOI 10.5281/zenodo.4567540, not resolved). https://github.com/blaylockbk/Herbie/blob/main/src/herbie/models/gfs.py
- **Finding:** URL templates; the 2021-03-23 switch; NCEI "spotty at best".
- **Relevance:** Byte-range reference.
- **Limitations:** Community code; misses two cycles.
- **Access:** Opened directly.

**[13] Open-Meteo GFS & HRRR API documentation.**
- **Source:** Open-Meteo, website main 2026-10-10. https://open-meteo.com/en/docs/gfs-api (source: https://github.com/open-meteo/open-meteo-website/blob/main/src/routes/en/docs/gfs-api/+page.svelte)
- **Finding:** HRRR "only for the United States"; GFS 0.11°; GFS013 falls back to GFS025.
- **Relevance:** `gfs_seamless`.
- **Limitations:** Read from source.
- **Access:** Opened directly (GitHub).

**[14] Open-Meteo source code.**
- **Source:** Open-Meteo (P. Zippenfenig et al.), f625df2 (2026-10-09). https://github.com/open-meteo/open-meteo
- **Finding:** `best_match` composition; per-model period mapping; GEFS-wave 31 members to 240 h; CMEMS sea level.
- **Relevance:** What the repo receives.
- **Limitations:** Code may change.
- **Access:** Opened directly.

**[15] WAVEWATCH III source, manual and regression tests.**
- **Source:** WW3DG / NOAA-EMC. Manual v6.07 (Tech. Note 333, 2019); develop 03adda9; production/GFS.v16. https://github.com/NOAA-EMC/WW3
- **Finding:** Dispersion; geometric-optics limits; breaking γ = 0.73/0.78/0.55; JONSWAP Γ; obstacles; partitioning; GRIB semantics; bulletin rules.
- **Relevance:** NOAA wave physics.
- **Limitations:** Not surf validation; 0.73 attribution conflicts with [44].
- **Access:** Opened directly.

**[16] ECMWF open data on AWS (`ecmwf-forecasts`) and the ecmwf-opendata README.**
- **Source:** ECMWF. Cycles 2026-10-09; README d2f7c9f. https://ecmwf-forecasts.s3.amazonaws.com/ ; https://github.com/ecmwf/ecmwf-opendata
- **Finding:** HRES wave steps and parameters; `waef` 50 pf; probability files at 240/360 h only; AIFS waves from 2026-05-12; stream change; 7–9 h availability.
- **Relevance:** The independent model.
- **Limitations:** Throttling.
- **Access:** Opened directly.

**[17] ENS-WAM dataset page (Set IV).**
- **Source:** ECMWF. https://www.ecmwf.int/en/forecasts/datasets/set-iv
- **Finding:** h1012 (140114) is Hs for 10–12 s; IDs 140115–140119 cover the bands to 25–30 s.
- **Relevance:** Groundswell energy.
- **Limitations:** Not opened.
- **Access:** Search extract.

**[18] ecWAM source.**
- **Source:** ECMWF, 7ca19c1 (2026-08-31). https://github.com/ecmwf-ifs/ecwam
- **Finding:** IFS coupling; altimeter assimilation path.
- **Relevance:** Independence.
- **Limitations:** Capability only.
- **Access:** Opened directly.

**[19] Global Ocean Waves Analysis and Forecast (MFWAM, GLOBAL_ANALYSISFORECAST_WAV_001_027).**
- **Source:** Copernicus Marine / Météo-France, dataset _202411. https://data.marine.copernicus.eu/product/GLOBAL_ANALYSISFORECAST_WAV_001_027
- **Finding:** 1/12°, 3-hourly; core of Open-Meteo `best_match`.
- **Relevance:** Dependence on ECMWF.
- **Limitations:** Forcing not verified.
- **Access:** Via [14].

**[20] NOAA Wave Ensemble Reforecast (GEFSv12 WW3).**
- **Source:** NOAA NWS/NCEP OPC and EMC, AOML, CIMAS. Description PDF 2024-02-24. https://noaa-nws-gefswaves-reforecast-pds.s3.amazonaws.com/Description_of_reforecast_data.pdf
- **Finding:** WW3 v7.12; 03Z; 5 or 11 members; 16 or 35 days; 658 buoys; hourly `tab.nc` without partitions; file sizes.
- **Relevance:** Training data.
- **Limitations:** Not the operational configuration.
- **Access:** Opened directly.

**[21] NOAA-EMC/gefswaves_reforecast repository.**
- **Source:** NOAA EMC/OPC (R. M. Campos), 591a71c. https://github.com/NOAA-EMC/gefswaves_reforecast
- **Finding:** Week-2 window/tail settings and thresholds; verification code; IFS 50r1 control-stream note.
- **Relevance:** Template.
- **Limitations:** Scripts, not documentation.
- **Access:** Opened directly.

**[22] WW3-tools.**
- **Source:** NOAA-EMC (R. M. Campos et al.), 70af784. https://github.com/NOAA-EMC/WW3-tools
- **Finding:** Metric formulas; altimeter QC defaults; NDBC/CDIP/CMEMS URL patterns.
- **Relevance:** Verification code.
- **Limitations:** Research code.
- **Access:** Opened directly.

### B. Offshore verification literature

**[23] Development and Validation of NOAA's 20-Year Global Wave Ensemble Reforecast.**
- **Source:** Campos, R. M. et al. Weather and Forecasting 39(11):1651–1672, 2024. doi:10.1175/WAF-D-24-0043.1
- **Finding:** Correlation 0.95–0.96 on day 1, 0.86–0.88 in week 1, below 0.70 after day 10.
- **Relevance:** Skill curve.
- **Limitations:** No tables seen.
- **Access:** Search extract; authors via [21].

**[24] Global assessments of the NCEP Ensemble Forecast System using altimeter data.**
- **Source:** Campos, R. M., Alves, J.-H. G. M., Penny, S. G., Krasnopolsky, V. Ocean Dynamics 70:405–419, 2020. doi:10.1007/s10236-019-01329-4
- **Finding:** The mean beats the control; extremes low beyond day 5.
- **Relevance:** Tail bias.
- **Limitations:** Predecessor system.
- **Access:** Search extract.

**[25] Probabilistic Wave Forecast for Week 2 and beyond Based on NOAA's GEFS.**
- **Source:** Campos, R. M., Figurskey, D., Mehra, A., Sienkiewicz, J. Weather and Forecasting 40(8):1511–1527, 2025. doi:10.1175/WAF-D-24-0154.1
- **Finding:** Skilful for waves up to 9 m.
- **Relevance:** Probability method.
- **Limitations:** Week-2 hazards only.
- **Access:** Search extract.

**[26] Potential Benefits of Using Probabilistic Forecasts for Waves and Marine Winds Based on the ECMWF EPS.**
- **Source:** Saetra, Ø., Bidlot, J.-R. Weather and Forecasting 19(4):673–689, 2004. https://journals.ametsoc.org/view/journals/wefo/19/4/1520-0434_2004_019_0673_pboupf_2_0_co_2.xml
- **Finding:** The ensemble beats the control; larger gain for joint Hs–Tp.
- **Relevance:** Score members jointly.
- **Limitations:** Old system.
- **Access:** Search extract.

**[27] Intercomparison of the performance of operational ocean wave forecasting systems with buoy data.**
- **Source:** Bidlot, J.-R. et al. Weather and Forecasting 17:287–310, 2002. https://data-ww3.ifremer.fr/BIB/Bidlot_etal_WF2002.pdf
- **Finding:** Buoy exchange since 1995 among five centres.
- **Relevance:** Method.
- **Limitations:** Not opened.
- **Access:** Search extract.

**[28] Verification of operational global and regional wave forecasting systems against measurements from moored buoys (JCOMM TR-30).**
- **Source:** Bidlot, J.-R., Holt, M. W. WMO/TD-1333, 2006. https://repository.oceanbestpractices.org/handle/11329/101
- **Finding:** Exchange procedures; 17 centres by 2012.
- **Relevance:** Standard.
- **Limitations:** Not opened.
- **Access:** Search extract.

**[29] Problems in RMSE-based wave model validations.**
- **Source:** Mentaschi, L., Besio, G., Cassola, F., Mazzino, A. Ocean Modelling 72:53–58, 2013. doi:10.1016/j.ocemod.2013.08.003
- **Finding:** NRMSE flatters negative bias; HH index.
- **Relevance:** Metrics.
- **Limitations:** Not opened.
- **Access:** Search extract; equations via [22].

**[30] Automated Analysis of Ocean Surface Directional Wave Spectra.**
- **Source:** Hanson, J. L., Phillips, O. M. J. Atmos. Oceanic Technol. 18, 2001. doi:10.1175/1520-0426(2001)018<0277:AAOOSD>2.0.CO;2
- **Finding:** Partitioning and wind-sea fraction.
- **Relevance:** Partitions.
- **Limitations:** DOI not resolved.
- **Access:** Mirror (WW3 bib).

**[31] A mosaic approach to wind wave modeling.**
- **Source:** Tolman, H. L. Ocean Modelling 25:35–47, 2008. doi:10.1016/j.ocemod.2008.06.005
- **Finding:** Multi-grid WW3.
- **Relevance:** Native grids.
- **Limitations:** DOI not resolved.
- **Access:** Mirror (WW3 bib).

### C. Nearshore physics

**[32] On calculating the lengths of water waves.**
- **Source:** Fenton, J. D., McKee, W. D. Coastal Engineering 14:499–513, 1990. doi:10.1016/0378-3839(90)90032-R
- **Finding:** Explicit k, within 1.7%.
- **Relevance:** De-shoaling.
- **Limitations:** Not read.
- **Access:** Mirror; re-computed in the sandbox.

**[33] Airy wave theory and breaker height prediction.**
- **Source:** Komar, P. D., Gaughan, M. K. Proc. 13th ICCE, 1972. doi:10.9753/icce.v13.20
- **Finding:** Hb = 0.39·g^0.2·(T·H0²)^0.4.
- **Relevance:** Core formula.
- **Limitations:** Regular waves, plane beach.
- **Access:** ICCE OAI-PMH mirror.

**[34] Direct Formula to Compute Wave Height and Angle at Incipient Breaking.**
- **Source:** Larson, M., Hoan, L. X., Hanson, H. JWPCOE 136:119–122, 2010. doi:10.1061/(ASCE)WW.1943-5460.0000030
- **Finding:** Non-iterative breaker angle and height.
- **Relevance:** Refraction.
- **Limitations:** Details not verified.
- **Access:** Mirror.

**[35] Maximum breaker height.**
- **Source:** Weggel, J. R. ASCE J. Waterways, Harbors and Coastal Eng. Div. 98(WW4):529–548, 1972. doi:10.1061/AWHCAR.0000166
- **Finding:** Slope-dependent γb.
- **Relevance:** Breaker index.
- **Limitations:** Secondary transcription.
- **Access:** Mirror.

**[36] Reanalysis of regular and random breaking wave statistics.**
- **Source:** Goda, Y. Coastal Engineering Journal 52:71–106, 2010. doi:10.1142/S0578563410002129
- **Finding:** Breaker indices for regular and random waves.
- **Relevance:** Caution on mixing them.
- **Limitations:** Numbers not verified.
- **Access:** Mirror.

**[37] Ruessink, Walstra & Southgate (2003); Zhang et al. (2021), Parameterization of nearshore wave breaker index.**
- **Source:** Coastal Engineering 48:139–149; Coastal Engineering 168:103914. doi:10.1016/j.coastaleng.2021.103914
- **Finding:** Variable γ.
- **Relevance:** γ is not universal.
- **Limitations:** Gains not verified.
- **Access:** Mirror.

**[38] The fraction of broken waves in natural surf zones.**
- **Source:** Stringari, C. E., Power, H. E. JGR Oceans, 2019. doi:10.1029/2019JC015213
- **Finding:** Field study of breaking fraction.
- **Relevance:** Tide effect.
- **Limitations:** Numbers not verified.
- **Access:** Mirror.

**[39] On the statistical distribution of the heights of sea waves.**
- **Source:** Longuet-Higgins, M. S. J. Marine Research 11(3):245–266, 1952. https://elischolar.library.yale.edu/journal_of_marine_research/774
- **Finding:** Rayleigh statistics; 1.271 ratio.
- **Relevance:** Sets.
- **Limitations:** Offshore theory.
- **Access:** Mirror; computed.

**[40] Surf similarity.**
- **Source:** Battjes, J. A. Proc. 14th ICCE, 1974. doi:10.9753/icce.v14.26
- **Finding:** One parameter governs breaker type.
- **Relevance:** Descriptor.
- **Limitations:** Thresholds not verified.
- **Access:** ICCE mirror.

**[41] Evolution of surface gravity waves over a submarine canyon.**
- **Source:** Magne, R. et al. JGR 112(C1), 2007. doi:10.1029/2005JC003035
- **Finding:** About 5× height change at Scripps Canyon.
- **Relevance:** Canyon factors.
- **Limitations:** Details not verified.
- **Access:** Mirror (WW3 manual).

**[42] Focusing of swell at the Nazaré submarine canyon (preprint).**
- **Source:** Hope, G. et al. EGUsphere 2026. doi:10.5194/egusphere-2026-2778
- **Finding:** Rim focusing; best from 275–315°.
- **Relevance:** Nazaré prior.
- **Limitations:** Not peer reviewed.
- **Access:** Secondary summary.

**[43] A third-generation wave model for coastal regions, Parts 1–2 (SWAN).**
- **Source:** Booij, N., Ris, R. C., Holthuijsen, L. H. (and Ris et al.). JGR 104(C4):7649–7681, 1999. doi:10.1029/98JC02622
- **Finding:** The SWAN model.
- **Relevance:** Transfer tables.
- **Limitations:** Validation not verified.
- **Access:** Mirror.

**[44] SWAN Scientific and Technical Documentation v40.81.**
- **Source:** SWAN team, TU Delft (NOAA-EMC/nwps f838b2e). https://github.com/NOAA-EMC/nwps/blob/f838b2ec986b3bed61b95058be1106dc1c5331b1/sorc/swan.fd/swantech.tex
- **Finding:** γ ranges; JONSWAP constants; diffraction limits.
- **Relevance:** Constants.
- **Limitations:** Older version.
- **Access:** Opened directly.

**[45] Modelling storm impacts on beaches, dunes and barrier islands; XBeach manual.**
- **Source:** Roelvink, J. A. et al. Coastal Engineering 56:1133–1152, 2009. doi:10.1016/j.coastaleng.2009.08.006 ; https://github.com/openearth/xbeach-docs
- **Finding:** Model modes; diffraction cost; Baldock Hb.
- **Relevance:** Capability.
- **Limitations:** Storm focus.
- **Access:** Opened directly.

**[46] A high-order adaptive time-stepping TVD solver for Boussinesq modeling of breaking waves and coastal inundation; FUNWAVE-TVD 3.0 manual.**
- **Source:** Shi, F. et al. Ocean Modelling 43–44:36–51, 2012. doi:10.1016/j.ocemod.2011.12.004 ; https://github.com/fengyanshi/FUNWAVE-TVD
- **Finding:** Breaking switches; MPI.
- **Relevance:** Offline reference.
- **Limitations:** Expensive.
- **Access:** Manual opened.

**[47] The California coastal wave monitoring and prediction system.**
- **Source:** O'Reilly, W. C. et al. Coastal Engineering 116:118–132, 2016. doi:10.1016/j.coastaleng.2016.06.005
- **Finding:** Precomputed MOP transforms.
- **Relevance:** Precedent.
- **Limitations:** Not read.
- **Access:** Mirror.

### D. Observed surf and surf-break science

**[48] An Empirical Method for Estimating Surf Heights from Deepwater Significant Wave Heights and Peak Periods in Coastal Zones with Narrow Shelves, Steep Bottom Slopes, and High Refraction.**
- **Source:** Caldwell, P. C., Aucan, J. P. J. Coastal Research 23(5), 2007. doi:10.2112/04-0397R.1 (not resolved)
- **Finding:** H1/10 ≈ shoaling-only Hb × Kr(Hb).
- **Relevance:** Buoy-to-surf link.
- **Limitations:** Coefficients unconfirmed; one coast.
- **Access:** Secondary mirror.

**[49] Goddard–Caldwell Oahu Surf Observations (NODC 0001754); Scaled surf photographs (NODC 0001753); 2020 edition.**
- **Source:** Caldwell, P., Goddard, L. / NOAA NODC–NCEI. http://accession.nodc.noaa.gov/0001754 ; http://accession.nodc.noaa.gov/0001753 ; https://ncei.noaa.gov/archive/accession/0001754
- **Finding:** Hawaii scale ≈ face/2; daily value ≈ H1/10; 10–15% against 51001.
- **Relevance:** Only surf truth.
- **Limitations:** Visual; blocked here.
- **Access:** Catalogue mirror; search extract.

**[50] Research-Based Surfing Literature for Coastal Management and the Science of Surfing — A Review.**
- **Source:** Scarfe, B. E., Healy, T. R., Rennie, H. G. J. Coastal Research 25(3):539–557, 2009. doi:10.2112/07-0958.1
- **Finding:** Height, peel and intensity are distinct.
- **Relevance:** Score design.
- **Limitations:** Numbers not verified.
- **Access:** Mirror.

**[51] Mead & Black, Field studies leading to the bathymetric classification of world-class surfing breaks; Hutt, Black & Mead, Classification of surf breaks in relation to surfing skill.**
- **Source:** J. Coastal Research SI 29, 2001. https://ref.coastalrestorationtrust.org.nz/documents/field-studies-leading-to-the-bathymetric-classification-of-world-class-surfing-breaks/
- **Finding:** Seven components; peel angles 90° to 27°.
- **Relevance:** Spot descriptors.
- **Limitations:** Secondary.
- **Access:** Mirror.

**[52] Deep learning object detection application to surfing wave quality.**
- **Source:** Atkin, E. A., Davies-Campbell, J., McIntosh, R. Coastal Engineering Proceedings 37, 2023. doi:10.9753/icce.v37.papers.25
- **Finding:** mAP 0.794.
- **Relevance:** Labels.
- **Limitations:** One site.
- **Access:** ICCE mirror.

**[53] Shoreline change mapping using crowd-sourced smartphone images (CoastSnap).**
- **Source:** Harley, M. D. et al. Coastal Engineering 150:175–189, 2019. doi:10.1016/j.coastaleng.2019.04.003
- **Finding:** Shoreline, not surf height.
- **Relevance:** Excluded as truth.
- **Limitations:** Not opened.
- **Access:** Search extract.

### E. Machine learning

**[54] Using random forest and gradient boosting trees to improve wave forecast at a specific location.**
- **Source:** Callens, A. et al. Applied Ocean Research 104:102339, 2020. doi:10.1016/j.apor.2020.102339
- **Finding:** Tree-ensemble correction at one site.
- **Relevance:** Point correction.
- **Limitations:** Numbers not verified.
- **Access:** Mirror.

**[55] SWRL Net: A Spectral, Residual Deep Learning Model for Improving Short-Term Wave Forecasts.**
- **Source:** Mooneyham, J., Crosby, S. C., Kumar, N., Hutchinson, B. Weather and Forecasting 35(6):2445–2460, 2020. doi:10.1175/WAF-D-19-0254.1
- **Finding:** "Up to 24-hours"; trained on hindcasts.
- **Relevance:** Short-range only.
- **Limitations:** Percentages not verified.
- **Access:** Mirror.

**[56] Improving NCEP's global-scale wave ensemble averages using neural networks.**
- **Source:** Campos, R. M. et al. Ocean Modelling, article 101617, 2020. doi:10.1016/j.ocemod.2020.101617
- **Finding:** NN in place of the ensemble mean.
- **Relevance:** Post-processing.
- **Limitations:** Numbers not verified.
- **Access:** Reference list in [60].

**[57] Predicting significant wave height with artificial neural networks in the South Atlantic Ocean: a hybrid approach.**
- **Source:** Pinto, P. M. G. M. et al. Ocean Dynamics, 2023.
- **Finding:** LSTM residuals.
- **Relevance:** Multi-day correction.
- **Limitations:** DOI and numbers not verified.
- **Access:** Reference list in [60].

**[58] A Deep-Learning Real-Time Bias Correction Method for Significant Wave Height Forecasts in the Western North Pacific.**
- **Source:** Zhang, W. et al. arXiv:2311.15001, 2023. https://arxiv.org/abs/2311.15001
- **Finding:** TrajGRU against ERA5; beat XGBoost.
- **Relevance:** Long-lead correction.
- **Limitations:** Reanalysis truth.
- **Access:** Full text (arXiv bulk bucket).

**[59] A Transformer-Based Regression Scheme for Forecasting Significant Wave Heights in Oceans.**
- **Source:** Pokhrel, P. et al. IEEE J. Oceanic Eng. 47(4):1010–1023, 2022.
- **Finding:** Transformer SWH.
- **Relevance:** Watch list.
- **Limitations:** Not verified.
- **Access:** Reference list in [60].

**[60] Artificial neural networks ensemble methodology to predict significant wave height.**
- **Source:** Minuzzi, F. C., Farina, L. arXiv:2509.14020, 2025. https://arxiv.org/abs/2509.14020
- **Finding:** Mixed results at 6 buoys.
- **Relevance:** Honest long-lead result.
- **Limitations:** One event per buoy.
- **Access:** Full text.

**[61] Site-Specific Post-Processing of Spectral Wave Forecast by Learning From Buoy Measurements.**
- **Source:** Filoche, A. et al. JGR: Machine Learning and Computation, 2026. doi:10.1029/2026JH001454
- **Finding:** Metadata only.
- **Relevance:** Spectral correction.
- **Limitations:** Unseen.
- **Access:** Mirror.

**[62] A Machine Learning Framework to Forecast Wave Conditions.**
- **Source:** James, S. C., Zhang, Y., O'Donncha, F. arXiv:1709.08725, 2017. https://arxiv.org/abs/1709.08725
- **Finding:** SWAN surrogate; 9 cm RMSE; 0.12 s.
- **Relevance:** Surrogates.
- **Limitations:** Truth is SWAN; shuffled splits.
- **Access:** Full text.

**[63] Using machine learning to derive spatial wave data: A case study for a marine energy site.**
- **Source:** Chen, J., Pillai, A. C., …, Ashton, I. Environmental Modelling & Software, article 105066, 2021. doi:10.1016/j.envsoft.2021.105066
- **Finding:** Metadata only.
- **Relevance:** Surrogates.
- **Limitations:** Unseen.
- **Access:** Mirror.

**[64] A hybrid efficient method to downscale wave climate to coastal areas.**
- **Source:** Camus, P., Mendez, F. J., Medina, R. Coastal Engineering 58(9):851–862, 2011. doi:10.1016/j.coastaleng.2011.05.007
- **Finding:** MDA plus RBF downscaling.
- **Relevance:** Template.
- **Limitations:** Numerical validation.
- **Access:** Mirror (abstract).

**[65] Empirical Estimation of Nearshore Waves From a Global Deep-Water Wave Model.**
- **Source:** Browne, M. et al. IEEE GRSL 3(4):462–466, 2006. https://ieeexplore.ieee.org/document/1715295/
- **Finding:** WW3-to-nearshore ANN.
- **Relevance:** Surf analogue.
- **Limitations:** Metrics not verified.
- **Access:** Mirror.

**[66] Near-shore swell estimation from a global wind-wave model: Spectral process, linear, and artificial neural network models.**
- **Source:** Browne, M. et al. Coastal Engineering 54(5):445–460, 2007.
- **Finding:** SWAN, linear and ANN compared.
- **Relevance:** Surf analogue.
- **Limitations:** Not verified.
- **Access:** Reference lists in [60] and [62].

**[67] Predicting probability distributions for surf height using an ensemble of mixture density networks.**
- **Source:** Carney, M., Cunningham, P., Dowling, J., Lee, C. Proc. ICML 2005, 113–120. doi:10.1145/1102351.1102366
- **Finding:** Probabilistic surf height.
- **Relevance:** P(range).
- **Limitations:** Unseen.
- **Access:** Mirror.

**[68] Representing the Surface Ocean in ECMWF's Data-Driven Forecasting System AIFS.**
- **Source:** Hahner, S., Zampieri, L. et al. arXiv:2604.25559, 2026. https://arxiv.org/abs/2604.25559
- **Finding:** About 10% lower SWH error (about 1 day of lead).
- **Relevance:** Size of ML gains.
- **Limitations:** One 4-month evaluation.
- **Access:** Full text.

### F. Observations and tools

**[69] NDBC Measurement Descriptions; wave calculation FAQ; real-time data FAQ.**
- **Source:** NOAA NDBC. https://www.ndbc.noaa.gov/faq/measdes.shtml ; https://www.ndbc.noaa.gov/faq/wavecalc.shtml ; https://www.ndbc.noaa.gov/faq/rt_data_access.shtml
- **Finding:** WVHT = 4·sqrt(m0); fill values; 45 days of real-time data.
- **Relevance:** Adapter.
- **Limitations:** Blocked.
- **Access:** Search extract.

**[70] ndbc-api.**
- **Source:** CDJellen, 2026-10-10. https://github.com/CDJellen/ndbc-api
- **Finding:** URL builders; masking and `.swden` pitfalls.
- **Relevance:** Parser.
- **Limitations:** Third-party.
- **Access:** Opened directly.

**[71] wavespectra.**
- **Source:** wavespectra developers, de30b55. https://github.com/wavespectra/wavespectra
- **Finding:** NDBC D(θ) reconstruction.
- **Relevance:** Observed partitions.
- **Limitations:** Library.
- **Access:** Opened directly.

**[72] IMOS multi-mission altimeter database; Ribal & Young, 33 years of globally calibrated wave height and wind speed data.**
- **Source:** IMOS/AODN; Scientific Data 6:77, 2019. doi:10.1038/s41597-019-0083-9 ; https://imos-data.s3.ap-southeast-2.amazonaws.com/IMOS/SRS/Surface-Waves/Wave-Wind-Altimetry-DM00/
- **Finding:** Calibrated 1° tiles; CC BY 4.0.
- **Relevance:** Reachable truth.
- **Limitations:** Sparse at the coast; delayed.
- **Access:** Data opened; paper via search extract.

**[73] AODN cloud-optimised wave buoy datasets.**
- **Source:** AODN/IMOS and partners, to 2026-10-10. https://aodn-cloud-optimised.s3.ap-southeast-2.amazonaws.com/wave_buoy_realtime_nonqc.parquet/
- **Finding:** WSSH against WHTH; site status; 56006 pairs.
- **Relevance:** Australian truth.
- **Limitations:** WHTH-only feeds; stale Spotters.
- **Access:** Opened directly.

**[74] Met Office UK Marine Observations.**
- **Source:** Met Office, rolling 7 days. https://met-office-marine-observations-data.s3.eu-west-2.amazonaws.com/
- **Finding:** Hourly Hm0 with swell/wind-sea split; CC BY-SA 4.0.
- **Relevance:** NE Atlantic.
- **Limitations:** 7-day retention.
- **Access:** Opened directly.

**[75] Sofar Spotter Archive.**
- **Source:** Sofar Ocean, 2019–2022. https://sofar-spotter-archive.s3.us-west-2.amazonaws.com/
- **Finding:** 871 drifters; 6.39 M records.
- **Relevance:** Bias checks.
- **Limitations:** Own licence.
- **Access:** Opened directly.

**[76] ARCO-ERA5.**
- **Source:** Google Research / Copernicus ERA5. https://storage.googleapis.com/gcp-public-data-arco-era5/ar/full_37-1h-0p25deg-chunk-1.zarr-v3/.zmetadata
- **Finding:** Hourly wave fields, including partitions.
- **Relevance:** Climatology.
- **Limitations:** Not independent.
- **Access:** Opened directly.

**[77] ESA Sea State CCI L3 v4.**
- **Source:** ESA CCI (Ifremer)/CEDA, 2026. doi:10.5285/82273fe59b784e7c914e1bd3049e3483
- **Finding:** About 6 km along-track Hs.
- **Relevance:** Cross-check.
- **Limitations:** Not downloaded.
- **Access:** Search extract.

**[78] Copernicus Marine In Situ Global Ocean Wave Observations Multi-Year Product.**
- **Source:** CMEMS In Situ TAC (SEANOE). doi:10.17882/70345
- **Finding:** Buoy waves, about 1990 to October 2022.
- **Relevance:** Historical truth.
- **Limitations:** Blocked.
- **Access:** Search extract; URL in [22].

**[79] noaa_coops; NOAA CO-OPS web services.**
- **Source:** GClunies, a90ef48; NOAA NOS. https://github.com/GClunies/noaa_coops ; https://tidesandcurrents.noaa.gov/web_services_info.html
- **Finding:** Endpoints and request limits.
- **Relevance:** US tides.
- **Limitations:** US only.
- **Access:** Opened directly plus search extract.

### G. Probabilistic verification, calibration and communication

**[80] Interpretation of Rank Histograms for Verifying Ensemble Forecasts.**
- **Source:** Hamill, T. M. Monthly Weather Review 129(3):550–560, 2001. https://doi.org/10/dkkvh3
- **Finding:** Flat does not mean reliable.
- **Relevance:** Reliability checks.
- **Limitations:** Not opened.
- **Access:** Via xskillscore; search extract.

**[81] Fair scores for ensemble forecasts.**
- **Source:** Ferro, C. A. T. QJRMS 140:1917–1923, 2014. doi:10.1002/qj.2270
- **Finding:** Fair CRPS and Brier score.
- **Relevance:** Different ensemble sizes.
- **Limitations:** Not opened.
- **Access:** Search extract.

**[82] Strictly Proper Scoring Rules, Prediction, and Estimation.**
- **Source:** Gneiting, T., Raftery, A. E. JASA 102(477):359–378, 2007. doi:10.1198/016214506000001437
- **Finding:** CRPS and interval-score theory.
- **Relevance:** Scores.
- **Limitations:** Not opened.
- **Access:** Via scoringRules and MAPIE.

**[83] Calibrated Probabilistic Forecasting Using Ensemble Model Output Statistics and Minimum CRPS Estimation.**
- **Source:** Gneiting, T. et al. Monthly Weather Review 133:1098–1118, 2005. doi:10.1175/MWR2904.1
- **Finding:** EMOS.
- **Relevance:** Calibration.
- **Limitations:** Weather variables.
- **Access:** Via package bibliographies.

**[84] Log-Normal Distribution Based EMOS Models for Probabilistic Wind-Speed Forecasting.**
- **Source:** Baran, S., Lerch, S. QJRMS 141:2289–2299, 2015. doi:10.1002/qj.2521
- **Finding:** Log-normal EMOS.
- **Relevance:** Positive Hs.
- **Limitations:** Wind.
- **Access:** Via scoringRules.

**[85] Heteroscedastic Censored and Truncated Regression with crch.**
- **Source:** Messner, J. W., Mayr, G. J., Zeileis, A. R Journal 8(1):173–181, 2016. doi:10.32614/RJ-2016-012
- **Finding:** Truncated NGR.
- **Relevance:** Reference implementation.
- **Limitations:** R.
- **Access:** Opened directly.

**[86] Quantile Regression Forests; quantile-forest 1.4.2.**
- **Source:** Meinshausen, N. JMLR 7:983–999, 2006. http://www.jmlr.org/papers/volume7/meinshausen06a/meinshausen06a.pdf ; https://github.com/zillow/quantile-forest
- **Finding:** Forest quantiles.
- **Relevance:** QRF.
- **Limitations:** Poor extrapolation.
- **Access:** Package opened.

**[87] Calibrated Ensemble Forecasts Using Quantile Regression Forests and Ensemble Model Output Statistics.**
- **Source:** Taillardat, M., Mestre, O., Zamo, M., Naveau, P. Monthly Weather Review 144(6):2375–2393, 2016. doi:10.1175/MWR-D-15-0260.1
- **Finding:** QRF beat EMOS.
- **Relevance:** Upgrade path.
- **Limitations:** Not waves.
- **Access:** Via scoringRules; search extract.

**[88] Adaptive Conformal Inference Under Distribution Shift.**
- **Source:** Gibbs, I., Candès, E. NeurIPS 34, 2021. https://proceedings.neurips.cc/paper/2021/file/0d441de75945e5acbc865406fc9a2559-Paper.pdf
- **Finding:** Coverage under drift.
- **Relevance:** Intervals.
- **Limitations:** Marginal coverage only.
- **Access:** Via MAPIE; search extract.

**[89] MAPIE 1.5.0.**
- **Source:** scikit-learn-contrib (Cordier, T. et al.), 2026. https://github.com/scikit-learn-contrib/MAPIE
- **Finding:** CQR, EnbPI, ACI.
- **Relevance:** Wrapper.
- **Limitations:** Marginal coverage.
- **Access:** Opened directly.

**[90] Evaluating Probabilistic Forecasts with scoringRules; Python scoringrules 0.11.0.**
- **Source:** Jordan, A., Krüger, F., Lerch, S. J. Statistical Software 90(12), 2019. doi:10.18637/jss.v090.i12
- **Finding:** Closed-form CRPS.
- **Relevance:** Losses.
- **Limitations:** Library.
- **Access:** Opened directly.

**[91] The Continuous Ranked Probability Score for Circular Variables and its Application to Mesoscale Forecast Ensemble Verification.**
- **Source:** Grimit, E. P. et al. QJRMS 132:2925–2942, 2006. doi:10.1256/qj.05.235
- **Finding:** Circular CRPS.
- **Relevance:** Directions.
- **Limitations:** Not opened.
- **Access:** Via scoringRules.

**[92] xskillscore; WWRP/WGNE JWGFVR "Forecast Verification: Issues, Methods and FAQ".**
- **Source:** xarray-contrib, 42bbeba; CAWCR. https://github.com/xarray-contrib/xskillscore ; https://www.cawcr.gov.au/projects/verification/
- **Finding:** Fair CRPS, reliability, contingency scores.
- **Relevance:** Implementations.
- **Limitations:** CAWCR page not opened.
- **Access:** Code opened; search extract.

**[93] Uncertainty forecasts improve weather-related decisions and attenuate the effects of forecast error.**
- **Source:** Joslyn, S. L., LeClerc, J. E. J. Experimental Psychology: Applied 18(1):126–140, 2012. https://www.sci.utah.edu/~kpotter/Library/Papers/joslyn:2012:UFID/index.html
- **Finding:** Uncertainty estimates improved lay decisions.
- **Relevance:** Communication.
- **Limitations:** Not surf.
- **Access:** Search extract.
