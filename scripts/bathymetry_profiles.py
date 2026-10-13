#!/usr/bin/env python
"""Measured physical-profile parameters from NOAA BlueTopo bathymetry (US coastal surveys).

BlueTopo (NOAA Office of Coast Survey, National Bathymetric Source; public domain, CC0) is
published on AWS at https://noaa-ocs-nationalbathymetry-pds.s3.amazonaws.com as GeoTIFF
tiles with three layers (elevation, uncertainty, contributor) and a raster attribute table
(RAT, ``.aux.xml``) naming the survey behind every cell. Elevations are metres relative to
NAVD88, negative below the datum (https://nauticalcharts.noaa.gov/data/bluetopo_specs.html).
Hawaii is outside its extent, so only continental US spots can be measured.

For each spot (default: mavericks, ocean-beach-sf, trestles, rincon) the script

1. finds the newest tile scheme (GeoPackage) and the tiles covering a transect from the
   break (``latitude``/``longitude`` in spots.json) to its forecast point, sampled every
   TRANSECT_STEP_M, preferring the finest tile at every sample; downloads each tile and its
   RAT into ``--cache`` and checks both against the SHA-256 published in the tile scheme;
2. samples the elevation along the transect (nearest cell);
3. derives, under the rules below, the physical-profile parameters the nearshore model
   reads (backend/app/services/surf_quality/nearshore.py):

   * ``shore_normal_deg``: the azimuth (degrees, true north) of the downslope direction of
     a least-squares plane fitted to the survey cells under water (elevation < 0 m NAVD88)
     within NORMAL_RADIUS_M of the break, i.e. the direction from which a wave arrives
     normal to the local depth contours. Cells of the RAT entry "NBS Generalization" (a
     best-guess interpolation outside any survey) are excluded. Fits at 100, 500 and
     1000 m are reported to show how stable the orientation is. It is written only when
     (a) the fit has at least MIN_FIT_CELLS cells and an R^2 of at least MIN_FIT_R2,
     (b) at least MIN_MEASURED_SHARE of those cells are measured depths (RAT
     ``bathy_coverage`` 1) rather than interpolations between old soundings, and
     (c) every direction of the spot's optimal swell window lies within MAX_INCIDENCE_DEG
     of it. The nearshore model's refraction term cos(theta - beta) assumes straight,
     parallel contours, under which a swell arriving 90 deg or more from beta cannot reach
     the break; a measured beta that would block the documented best swells means the
     contours turn around a point or reef faster than the model can represent, so the
     window-centre estimate is kept. Rules (b) and (c) were added after the first run of
     this script showed a mostly interpolated fit at Mavericks and a 196 deg contour
     normal at Rincon, whose documented best swells come from 270-290 deg; the measured
     values that were not applied are kept in the provenance (``not_applied``);
   * ``controlling_depth_m``: the depth at the break (median of the cells within
     BREAK_RADIUS_M). Written only when the break cell is under water and no cell within
     SHORELINE_CLEARANCE_M of the break is at or above 0 m NAVD88: otherwise the
     coordinate marks the shoreline position of the break, not the take-off zone, and the
     depth there says nothing about where the waves break (the parameter stays unknown);
   * ``forecast_point_depth_m``: the depth at the forecast point (median of the survey
     cells within BREAK_RADIUS_M), when the point is inside the tile coverage and under
     water, and at least MIN_SURVEY_SHARE of those cells come from a survey: cells of
     "NBS Generalization" are a best-guess surface outside any survey (RAT
     ``data_assessment`` 3, uncertainties unknown), not a measurement, so a depth taken
     from them is kept in the provenance (``not_applied``) and the parameter stays unknown.
     The contributing surveys (id, year, measured or interpolated) are recorded. This rule
     was added in review, after the first committed run had labelled Trestles' 758.7 m
     (an NBS Generalization cell) "measured";

4. prints a JSON report (transect depths, fits, contributing surveys) and, with
   ``--write``, writes ``data/surf_spots/physical_profiles.json`` (the format
   backend/app/services/seed.py reads; data_quality "measured" for the derived fields and a
   ``provenance`` object: tile ids, delivery dates, checksums, method) and adds one
   ``survey_data`` source per tile used to ``research_evidence.json`` with a bathymetry
   claim. The excerpt of that claim (EVIDENCE_EXCERPT) is verbatim text of the BlueTopo
   specifications page and of every tile's TIFF ImageDescription tag; the script checks the
   tile tag and stops if it is missing.

Assumptions and limits: break coordinates in spots.json are approximate; WGS84 is used for
NAD83(2011) (they differ by about 1-2 m); depths are relative to NAVD88, not a chart datum
(BlueTopo is "not on a navigational vertical datum"); interpolated cells (RAT
``bathy_coverage`` 0) are used but their share is reported; nothing here is calibrated.

Script-only dependencies, NOT backend runtime dependencies. Install them in a separate
virtual environment (versions used for the committed output):

    python3 -m venv /path/to/bathyenv
    /path/to/bathyenv/bin/pip install numpy==2.5.4 tifffile==2026.9.20 \\
        imagecodecs==2026.10.10 pyproj==3.8.0

Run from the repository root (downloads about 50-150 MB per spot into the cache):

    /path/to/bathyenv/bin/python -I scripts/bathymetry_profiles.py --cache /tmp/bluetopo \\
        [--slugs mavericks,rincon] [--report report.json] [--write]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import urllib.request
import xml.etree.ElementTree as ET  # noqa: S405 (NOAA's own RAT files, parsed read-only)
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import tifffile
from pyproj import CRS, Geod, Proj, Transformer

BUCKET = "https://noaa-ocs-nationalbathymetry-pds.s3.amazonaws.com"
SCHEME_PREFIX = "BlueTopo/_BlueTopo_Tile_Scheme/"
SPECS_URL = "https://nauticalcharts.noaa.gov/data/bluetopo_specs.html"
PUBLISHER = "NOAA Office of Coast Survey"
DEFAULT_SLUGS = ("mavericks", "ocean-beach-sf", "trestles", "rincon")

TRANSECT_STEP_M = 20.0
BREAK_RADIUS_M = 25.0
SHORELINE_CLEARANCE_M = 100.0
NORMAL_RADIUS_M = 250.0
STABILITY_RADII_M = (100.0, 500.0, 1000.0)
MIN_FIT_CELLS = 200
MIN_FIT_R2 = 0.5
MIN_MEASURED_SHARE = 0.5
MIN_SURVEY_SHARE = 0.5
MAX_INCIDENCE_DEG = 90.0
GENERALIZATION_ID = "NBS Generalization"
REPORT_DISTANCES_M = (250.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0)
# Verbatim in every BlueTopo tile's ImageDescription tag and on SPECS_URL.
EVIDENCE_EXCERPT = (
    "This dataset is a multi-layered raster and part of NOAA Office of Coast Survey BlueTopo "
    "bathymetry...The vertical coordinate reference system for this product is NAVD88"
)
GEOD = Geod(ellps="WGS84")
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "surf_spots"


# --------------------------------------------------------------------------- download
def _get(url: str) -> bytes:
    # Tile links come from the tile scheme: only the BlueTopo bucket over https is fetched.
    if not url.startswith(f"{BUCKET}/"):
        raise ValueError(f"refusing to download from outside the BlueTopo bucket: {url}")
    req = urllib.request.Request(  # noqa: S310 (checked above: https, the BlueTopo bucket)
        url, headers={"User-Agent": "swell-travel-agent bathymetry"}
    )
    with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310
        return resp.read()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _cached(url: str, cache: Path, sha256: str | None = None) -> Path:
    """Download ``url`` into ``cache`` once; verify the published checksum when given."""
    name = url.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
        raise ValueError(f"unexpected file name in {url}")
    path = cache / name
    if not path.exists():
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(_get(url))
        tmp.rename(path)
    if sha256 and _sha256(path).lower() != sha256.lower():
        raise ValueError(f"checksum mismatch for {name}")
    return path


def newest_scheme(cache: Path) -> Path:
    listing = _get(f"{BUCKET}/?list-type=2&prefix={SCHEME_PREFIX}").decode("utf-8")
    keys = sorted(re.findall(r"<Key>([^<]+\.gpkg)</Key>", listing))
    if not keys:
        raise RuntimeError("no BlueTopo tile scheme found")
    return _cached(f"{BUCKET}/{keys[-1]}", cache)


# --------------------------------------------------------------------------- tiles
@dataclass(frozen=True)
class TileRef:
    tile: str
    geotiff_url: str
    rat_url: str
    delivered: str
    resolution_m: float
    utm: str
    geotiff_sha256: str
    rat_sha256: str


class Scheme:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.table = self.conn.execute("select table_name from gpkg_contents").fetchone()[0]

    def covering(self, lat: float, lon: float) -> list[TileRef]:
        rows = self.conn.execute(
            f"select t.tile, t.GeoTIFF_Link, t.RAT_Link, t.Delivered_Date, t.Resolution, "  # noqa: S608
            f"t.UTM, t.GeoTIFF_SHA256_Checksum, t.RAT_SHA256_Checksum from "
            f'"rtree_{self.table}_geom" r join "{self.table}" t on t.fid = r.id '
            "where r.minx <= ? and r.maxx >= ? and r.miny <= ? and r.maxy >= ? "
            "and t.GeoTIFF_Link is not null",
            (lon, lon, lat, lat),
        ).fetchall()
        refs = [
            TileRef(r[0], r[1], r[2], r[3], float(str(r[4]).rstrip("m")), str(r[5]), r[6], r[7])
            for r in rows
        ]
        return sorted(refs, key=lambda t: (t.resolution_m, t.tile))


@dataclass
class Tile:
    ref: TileRef
    elevation: np.ndarray
    contributor: np.ndarray
    x0: float
    y0: float
    dx: float
    dy: float
    epsg: int
    to_xy: Transformer
    rat: dict[int, dict[str, str]]
    description: str

    def rowcol(self, lat: float, lon: float) -> tuple[float, float, float, float]:
        x, y = self.to_xy.transform(lon, lat)
        return (self.y0 - y) / self.dy, (x - self.x0) / self.dx, x, y

    def inside(self, lat: float, lon: float) -> bool:
        r, c, _, _ = self.rowcol(lat, lon)
        return 0 <= r < self.elevation.shape[0] and 0 <= c < self.elevation.shape[1]

    def sample(self, lat: float, lon: float) -> tuple[float, int] | None:
        r, c, _, _ = self.rowcol(lat, lon)
        i, j = int(math.floor(r)), int(math.floor(c))
        if not (0 <= i < self.elevation.shape[0] and 0 <= j < self.elevation.shape[1]):
            return None
        z = float(self.elevation[i, j])
        if not math.isfinite(z):
            return None
        return z, int(self.contributor[i, j])

    def disc(self, lat: float, lon: float, radius_m: float) -> dict[str, np.ndarray]:
        """Cells whose centre lies within ``radius_m``: offsets (m, east/north), z, ids."""
        r, c, x, y = self.rowcol(lat, lon)
        n_r, n_c = int(radius_m / self.dy) + 2, int(radius_m / self.dx) + 2
        r0, r1 = max(0, int(r) - n_r), min(self.elevation.shape[0], int(r) + n_r + 1)
        c0, c1 = max(0, int(c) - n_c), min(self.elevation.shape[1], int(c) + n_c + 1)
        rr, cc = np.mgrid[r0:r1, c0:c1]
        ex = self.x0 + (cc + 0.5) * self.dx - x
        ny = self.y0 - (rr + 0.5) * self.dy - y
        z = self.elevation[r0:r1, c0:c1]
        ids = self.contributor[r0:r1, c0:c1]
        m = (ex**2 + ny**2 <= radius_m**2) & np.isfinite(z)
        return {"dx": ex[m], "dy": ny[m], "z": z[m], "ids": ids[m].astype(np.int64)}


def _geokey(keys: tuple[int, ...], key: int) -> int | None:
    for i in range(4, len(keys), 4):
        if keys[i] == key:
            return int(keys[i + 3])
    return None


def parse_rat(path: Path) -> dict[int, dict[str, str]]:
    tree = ET.parse(path)  # noqa: S314 (NOAA RAT downloaded with a verified checksum)
    rat = tree.find(".//GDALRasterAttributeTable")
    if rat is None:
        return {}
    names = [str(f.findtext("Name")) for f in rat.findall("FieldDefn")]
    out: dict[int, dict[str, str]] = {}
    for row in rat.findall("Row"):
        values = dict(zip(names, (f.text or "" for f in row.findall("F")), strict=False))
        out[int(float(values.get("value", "0")))] = values
    return out


def load_tile(ref: TileRef, cache: Path) -> Tile:
    tif = _cached(ref.geotiff_url, cache, ref.geotiff_sha256)
    rat_path = _cached(ref.rat_url, cache, ref.rat_sha256) if ref.rat_url else None
    with tifffile.TiffFile(tif) as tf:
        page = tf.pages[0]
        arr = page.asarray()
        sx, sy, _ = page.tags["ModelPixelScaleTag"].value
        tie = page.tags["ModelTiepointTag"].value
        keys = page.tags["GeoKeyDirectoryTag"].value
        description = str(page.tags["ImageDescription"].value)
        gdal_meta = str(page.tags["GDAL_METADATA"].value) if "GDAL_METADATA" in page.tags else ""
    epsg = _geokey(keys, 3072)
    if epsg is None:
        raise ValueError(f"{ref.tile}: no projected CRS in the GeoKeys")
    if "navd88" not in gdal_meta.lower() and "NAVD88" not in description:
        raise ValueError(f"{ref.tile}: vertical datum is not NAVD88")
    if arr.ndim != 3 or arr.shape[-1] < 3:
        raise ValueError(f"{ref.tile}: expected three interleaved layers, got {arr.shape}")
    return Tile(
        ref=ref,
        elevation=arr[..., 0].astype(np.float64),
        contributor=np.nan_to_num(arr[..., 2], nan=-1).astype(np.int64),
        x0=float(tie[3]),
        y0=float(tie[4]),
        dx=float(sx),
        dy=float(sy),
        epsg=epsg,
        to_xy=Transformer.from_crs(4326, epsg, always_xy=True),
        rat=parse_rat(rat_path) if rat_path else {},
        description=description,
    )


class TileStore:
    def __init__(self, scheme: Scheme, cache: Path) -> None:
        self.scheme, self.cache = scheme, cache
        self.tiles: dict[str, Tile] = {}

    def finest_at(self, lat: float, lon: float) -> Tile | None:
        """The finest-resolution tile with a valid cell at (lat, lon), or the finest at all."""
        first: Tile | None = None
        for ref in self.scheme.covering(lat, lon):
            tile = self.tiles.get(ref.tile)
            if tile is None:
                tile = self.tiles[ref.tile] = load_tile(ref, self.cache)
            if not tile.inside(lat, lon):
                continue
            first = first or tile
            if tile.sample(lat, lon) is not None:
                return tile
        return first


# --------------------------------------------------------------------------- analysis
@dataclass
class Fit:
    radius_m: float
    cells: int
    survey_cells: int
    measured_share: float
    azimuth_deg: float | None
    slope: float | None
    r2: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "radius_m": self.radius_m,
            "cells": self.cells,
            "survey_cells": self.survey_cells,
            "measured_share": round(self.measured_share, 3),
            "azimuth_deg": None if self.azimuth_deg is None else round(self.azimuth_deg, 1),
            "slope": None if self.slope is None else round(self.slope, 4),
            "r2": None if self.r2 is None else round(self.r2, 3),
        }


def _measured(tile: Tile, ids: np.ndarray) -> np.ndarray:
    return np.array([tile.rat.get(int(i), {}).get("bathy_coverage") == "1" for i in ids])


def _survey(tile: Tile, ids: np.ndarray) -> np.ndarray:
    def ok(i: int) -> bool:
        row = tile.rat.get(int(i))
        return row is not None and row.get("source_survey_id") != GENERALIZATION_ID

    return np.array([ok(int(i)) for i in ids], dtype=bool)


def plane_fit(tile: Tile, lat: float, lon: float, radius_m: float) -> Fit:
    """Downslope azimuth of a plane fitted to the underwater survey cells near a point."""
    d = tile.disc(lat, lon, radius_m)
    under = d["z"] < 0.0
    survey = _survey(tile, d["ids"]) if d["ids"].size else np.zeros(0, dtype=bool)
    m = under & survey
    n = int(m.sum())
    measured = float(_measured(tile, d["ids"][m]).mean()) if n else 0.0
    if n < 3:
        return Fit(radius_m, int(under.sum()), n, measured, None, None, None)
    a = np.c_[np.ones(n), d["dx"][m], d["dy"][m]]
    z = d["z"][m]
    coef, *_ = np.linalg.lstsq(a, z, rcond=None)
    gx, gy = float(coef[1]), float(coef[2])
    pred = a @ coef
    ss_tot = float(((z - z.mean()) ** 2).sum())
    r2 = 1.0 - float(((z - pred) ** 2).sum()) / ss_tot if ss_tot > 0 else 0.0
    grid_az = math.degrees(math.atan2(-gx, -gy)) % 360.0  # downslope, grid north
    # Grid north -> true north: add the meridian convergence (pyproj sign convention:
    # the angle from true north to grid north, clockwise positive).
    conv = Proj(CRS.from_epsg(tile.epsg)).get_factors(lon, lat).meridian_convergence
    return Fit(
        radius_m, int(under.sum()), n, measured, (grid_az + conv) % 360.0, math.hypot(gx, gy), r2
    )


def disc_median(tile: Tile, lat: float, lon: float, radius_m: float) -> float | None:
    d = tile.disc(lat, lon, radius_m)
    return float(np.median(d["z"])) if d["z"].size else None


def survey_depth(
    tile: Tile, lat: float, lon: float, radius_m: float
) -> tuple[float | None, float, float]:
    """(median elevation of the survey cells, share of survey cells, share of measured
    cells among them) within ``radius_m``; NBS Generalization cells are not survey cells."""
    d = tile.disc(lat, lon, radius_m)
    if not d["z"].size:
        return None, 0.0, 0.0
    survey = _survey(tile, d["ids"])
    share = float(survey.mean())
    if not survey.any():
        return None, share, 0.0
    measured = float(_measured(tile, d["ids"][survey]).mean())
    return float(np.median(d["z"][survey])), share, measured


def nearest_exposed_m(tile: Tile, lat: float, lon: float, radius_m: float) -> float | None:
    """Distance (m) to the nearest cell at or above 0 m NAVD88 within ``radius_m``."""
    d = tile.disc(lat, lon, radius_m)
    m = d["z"] >= 0.0
    if not m.any():
        return None
    return float(np.sqrt(d["dx"][m] ** 2 + d["dy"][m] ** 2).min())


def contributors(tile: Tile, lat: float, lon: float, radius_m: float) -> list[dict[str, Any]]:
    d = tile.disc(lat, lon, radius_m)
    ids, counts = np.unique(d["ids"], return_counts=True)
    out = []
    for i, n in sorted(zip(ids.tolist(), counts.tolist(), strict=True), key=lambda t: -t[1]):
        row = tile.rat.get(int(i), {})
        out.append(
            {
                "cells": int(n),
                "survey": row.get("source_survey_id"),
                "institution": row.get("source_institution"),
                "start": row.get("survey_date_start"),
                "end": row.get("survey_date_end"),
                "measured": row.get("bathy_coverage") == "1",
            }
        )
    return out


def _angle(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _window_directions(lo: float, hi: float, step: float = 1.0) -> list[float]:
    width = (hi - lo) % 360.0
    n = max(1, int(width / step))
    return [(lo + width * k / n) % 360.0 for k in range(n + 1)]


def shore_normal_rejection(fit: Fit, spot: dict[str, Any]) -> str | None:
    """Why a fitted contour normal is not used as the shore normal (None = it is used)."""
    if fit.azimuth_deg is None or fit.survey_cells < MIN_FIT_CELLS:
        return f"fewer than {MIN_FIT_CELLS} underwater survey cells near the break"
    if (fit.r2 or 0.0) < MIN_FIT_R2:
        return f"the plane fit explains too little of the depth (R2 {fit.r2:.2f} < {MIN_FIT_R2})"
    if fit.measured_share < MIN_MEASURED_SHARE:
        return (
            f"only {100 * fit.measured_share:.0f} % of the fitted cells are measured depths "
            f"(below {100 * MIN_MEASURED_SHARE:.0f} %); the rest are interpolated between "
            "soundings"
        )
    lo, hi = float(spot["optimal_swell_direction_min"]), float(spot["optimal_swell_direction_max"])
    worst = max(_angle(d, fit.azimuth_deg) for d in _window_directions(lo, hi))
    if worst >= MAX_INCIDENCE_DEG:
        return (
            f"the documented optimal swell window ({lo:.0f}-{hi:.0f} deg) reaches "
            f"{worst:.0f} deg from it, and the model's straight-contour refraction would treat "
            "those swells as unable to reach the break (the contours turn around the point "
            "faster than the model represents)"
        )
    return None


@dataclass
class SpotResult:
    slug: str
    report: dict[str, Any]
    profile: dict[str, Any] = field(default_factory=dict)
    tiles: dict[str, Tile] = field(default_factory=dict)


def analyse(spot: dict[str, Any], store: TileStore) -> SpotResult:
    lat, lon = float(spot["latitude"]), float(spot["longitude"])
    flat, flon = float(spot["forecast_latitude"]), float(spot["forecast_longitude"])
    az, _back, dist = GEOD.inv(lon, lat, flon, flat)
    n = max(2, int(dist / TRANSECT_STEP_M) + 1)
    pts = [(lat, lon)] + [(p[1], p[0]) for p in GEOD.npts(lon, lat, flon, flat, n - 2)]
    pts.append((flat, flon))
    used: dict[str, Tile] = {}
    samples: list[dict[str, Any]] = []
    for k, (plat, plon) in enumerate(pts):
        tile = store.finest_at(plat, plon)
        s = tile.sample(plat, plon) if tile is not None else None
        d_m = dist * k / (len(pts) - 1)
        if tile is not None and s is not None:
            used.setdefault(tile.ref.tile, tile)
        samples.append(
            {
                "distance_m": round(d_m, 1),
                "elevation_m": None if s is None else round(s[0], 2),
                "tile": None if tile is None else tile.ref.tile,
            }
        )
    valid = [s for s in samples if s["elevation_m"] is not None]
    report: dict[str, Any] = {
        "slug": spot["slug"],
        "break": [lat, lon],
        "forecast_point": [flat, flon],
        "transect_azimuth_deg": round(az % 360.0, 1),
        "transect_length_m": round(dist, 1),
        "samples": len(samples),
        "samples_with_data": len(valid),
        "depth_at_distance_m": {
            str(int(target)): next(
                (s["elevation_m"] for s in samples if s["distance_m"] >= target), None
            )
            for target in REPORT_DISTANCES_M
            if target < dist
        },
        "shallowest_offshore_of_1km_m": max(
            (s["elevation_m"] for s in valid if s["distance_m"] >= 1000.0), default=None
        ),
    }
    profile: dict[str, Any] = {}
    quality: dict[str, str] = {}
    notes: list[str] = []
    not_applied: dict[str, dict[str, Any]] = {}
    prov_tiles: dict[str, dict[str, Any]] = {}

    def remember(tile: Tile, role: str) -> None:
        entry = prov_tiles.setdefault(
            tile.ref.tile,
            {
                "tile": tile.ref.tile,
                "delivered": tile.ref.delivered,
                "resolution_m": tile.ref.resolution_m,
                "geotiff": tile.ref.geotiff_url,
                "geotiff_sha256": tile.ref.geotiff_sha256,
                "rat": tile.ref.rat_url,
                "roles": [],
            },
        )
        if role not in entry["roles"]:
            entry["roles"].append(role)
        used.setdefault(tile.ref.tile, tile)

    # --- the break ---------------------------------------------------------------------
    btile = store.finest_at(lat, lon)
    if btile is None:
        report["break_status"] = "outside BlueTopo coverage"
    else:
        remember(btile, "break")
        z = disc_median(btile, lat, lon, BREAK_RADIUS_M)
        exposed = nearest_exposed_m(btile, lat, lon, 250.0)
        fits = {r: plane_fit(btile, lat, lon, r) for r in (NORMAL_RADIUS_M, *STABILITY_RADII_M)}
        main = fits[NORMAL_RADIUS_M]
        report.update(
            {
                "break_tile": btile.ref.tile,
                "break_elevation_m": None if z is None else round(z, 2),
                "nearest_exposed_cell_m": None if exposed is None else round(exposed, 1),
                "fits": [fits[r].as_dict() for r in sorted(fits)],
                "break_contributors": contributors(btile, lat, lon, NORMAL_RADIUS_M)[:6],
            }
        )
        fit_text = (
            f"the downslope azimuth of a plane fitted to {main.survey_cells} underwater survey "
            f"cells within {NORMAL_RADIUS_M:.0f} m of the break; R2 {main.r2 or 0.0:.2f}, "
            f"{100 * main.measured_share:.0f} % measured depths, the rest interpolated within "
            "the surveys"
        )
        reason = shore_normal_rejection(main, spot)
        if reason is None and main.azimuth_deg is not None:
            profile["shore_normal_deg"] = round(main.azimuth_deg, 1)
            quality["shore_normal_deg"] = "measured"
            notes.append(f"Shore normal {main.azimuth_deg:.1f} deg: {fit_text}.")
        else:
            if main.azimuth_deg is not None:
                not_applied["shore_normal_deg"] = {
                    "value": round(main.azimuth_deg, 1),
                    "reason": reason,
                }
                notes.append(
                    f"The contours near the break face {main.azimuth_deg:.0f} deg ({fit_text}). "
                    f"This is not used as the shore normal: {reason}."
                )
            else:
                notes.append(f"Shore normal not measured: {reason}.")
        if z is not None and z < 0.0 and (exposed is None or exposed > SHORELINE_CLEARANCE_M):
            profile["controlling_depth_m"] = round(-z, 1)
            quality["controlling_depth_m"] = "measured"
            notes.append(f"Depth at the break {-z:.1f} m below NAVD88.")
        else:
            where = (
                f"the break coordinate is {z:+.1f} m NAVD88"
                if z is not None
                else "the break coordinate has no BlueTopo value"
            )
            if exposed is not None:
                where += f", {exposed:.0f} m from the nearest cell at or above 0 m"
            notes.append(
                f"Controlling depth not set: {where}, so the coordinate marks the shoreline "
                "position of the break, not where the waves break."
            )
            report["controlling_depth_status"] = where
    # --- the forecast point -----------------------------------------------------------
    ftile = store.finest_at(flat, flon)
    if ftile is not None and ftile.sample(flat, flon) is not None:
        remember(ftile, "forecast_point")
        fz = disc_median(ftile, flat, flon, BREAK_RADIUS_M)
        sz, survey_share, measured_share = survey_depth(ftile, flat, flon, BREAK_RADIUS_M)
        fp_contributors = contributors(ftile, flat, flon, BREAK_RADIUS_M)
        report["forecast_point_tile"] = ftile.ref.tile
        report["forecast_point_elevation_m"] = None if fz is None else round(fz, 2)
        report["forecast_point_survey_share"] = round(survey_share, 3)
        report["forecast_point_contributors"] = fp_contributors[:6]
        if fz is not None and fz < 0.0 and survey_share < MIN_SURVEY_SHARE:
            reason = (
                f"{100 * (1 - survey_share):.0f} % of the cells within {BREAK_RADIUS_M:.0f} m "
                f"are {GENERALIZATION_ID} (a best-guess surface outside any survey, "
                "uncertainty unknown), not survey depths"
            )
            not_applied["forecast_point_depth_m"] = {"value": round(-fz, 1), "reason": reason}
            notes.append(
                f"Forecast point depth not set: BlueTopo gives {-fz:.1f} m below NAVD88 there, "
                f"but {reason}."
            )
        elif sz is not None and sz < 0.0:
            profile["forecast_point_depth_m"] = round(-sz, 1)
            quality["forecast_point_depth_m"] = "measured"
            if measured_share >= 1.0:
                kind = "all cells measured depths"
            elif measured_share <= 0.0:
                kind = "cells interpolated between the survey's soundings"
            else:
                kind = (
                    f"{100 * measured_share:.0f} % of the cells measured depths, the rest "
                    "interpolated within the survey"
                )
            names = ", ".join(
                f"{c['survey']} ({(c['start'] or '')[:4]})"
                for c in fp_contributors
                if c["survey"] and c["survey"] != GENERALIZATION_ID
            )
            notes.append(f"Forecast point depth {-sz:.1f} m below NAVD88 (survey {names}; {kind}).")
    else:
        report["forecast_point_status"] = "outside BlueTopo coverage"
        notes.append("Forecast point outside BlueTopo coverage: its depth stays unknown.")
    for name in sorted(used):
        if name not in prov_tiles:
            remember(used[name], "transect")
    report["tiles"] = sorted(prov_tiles)
    if profile:
        profile["data_quality"] = quality
        profile["notes"] = (
            "Measured from NOAA BlueTopo (scripts/bathymetry_profiles.py; depths relative to "
            "NAVD88). " + " ".join(notes)
        )
        profile["provenance"] = {
            "source": "NOAA Office of Coast Survey BlueTopo",
            "method": (
                f"plane fit within {NORMAL_RADIUS_M:.0f} m (underwater survey cells), break "
                f"depth = median within {BREAK_RADIUS_M:.0f} m when no cell at or above 0 m "
                f"lies within {SHORELINE_CLEARANCE_M:.0f} m, forecast-point depth = median "
                f"of the survey cells within {BREAK_RADIUS_M:.0f} m when at least "
                f"{100 * MIN_SURVEY_SHARE:.0f} % of the cells there are survey cells (not "
                f"{GENERALIZATION_ID})"
            ),
            "vertical_datum": "NAVD88",
            "tiles": [prov_tiles[t] for t in sorted(prov_tiles)],
            "break_elevation_m": report.get("break_elevation_m"),
            "nearest_exposed_cell_m": report.get("nearest_exposed_cell_m"),
            "fits": report.get("fits", []),
            "forecast_point_survey_share": report.get("forecast_point_survey_share"),
            "forecast_point_contributors": report.get("forecast_point_contributors", []),
            "not_applied": not_applied,
        }
    return SpotResult(spot["slug"], report, profile, {t: used[t] for t in prov_tiles})


# --------------------------------------------------------------------------- outputs
def write_profiles(results: list[SpotResult], path: Path) -> None:
    current: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    for r in results:
        if r.profile:
            current[r.slug] = r.profile
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def evidence_source(r: SpotResult, tile: Tile, accessed: str) -> dict[str, Any]:
    roles = r.profile.get("provenance", {}).get("tiles", [])
    role = next((t["roles"] for t in roles if t["tile"] == tile.ref.tile), ["transect"])
    year = int(tile.ref.delivered[:4])
    rep = r.report
    parts = [f"BlueTopo tile {tile.ref.tile} ({tile.ref.resolution_m:g} m cells)"]
    if "break" in role:
        fit = next((f for f in rep["fits"] if f["radius_m"] == NORMAL_RADIUS_M), None)
        parts.append(
            f"covers the break: elevation {rep['break_elevation_m']:+.1f} m NAVD88 at the break "
            "coordinate"
        )
        if fit and fit["azimuth_deg"] is not None:
            parts.append(
                f"the depth contours within {NORMAL_RADIUS_M:.0f} m face {fit['azimuth_deg']:.0f}"
                f" deg (plane fit, R2 {fit['r2']:.2f}, {100 * fit['measured_share']:.0f} % of "
                "cells measured depths)"
            )
        surveys = [
            f"{c['survey']} ({c['start'][:4]})"
            for c in rep["break_contributors"]
            if c["survey"] and c["survey"] != GENERALIZATION_ID
        ][:3]
        if surveys:
            parts.append("main surveys " + ", ".join(surveys))
    if "forecast_point" in role and r.profile.get("forecast_point_depth_m") is not None:
        parts.append(
            f"depth at the forecast point {r.profile['forecast_point_depth_m']:.1f} m below NAVD88"
        )
    elif "forecast_point" in role and rep.get("forecast_point_elevation_m") is not None:
        parts.append(
            f"the forecast point lies in {GENERALIZATION_ID} cells (elevation "
            f"{rep['forecast_point_elevation_m']:+.1f} m NAVD88, a best-guess surface outside "
            "any survey, not a measured depth)"
        )
    if role == ["transect"]:
        parts.append("covers part of the transect from the break to the forecast point")
    claim = "; ".join(parts) + "."
    return {
        "key": f"bluetopo-{tile.ref.tile.lower()}",
        "title": f"BlueTopo bathymetry tile {tile.ref.tile} (delivered {tile.ref.delivered[:10]})",
        "url": tile.ref.geotiff_url,
        "publisher": PUBLISHER,
        "source_type": "survey_data",
        "published_year": year,
        "accessed_at": accessed,
        "retrieval": "fetched",
        "notes": (
            "Read directly: GeoTIFF elevation and contributor layers and the raster attribute "
            f"table, checksums verified against the BlueTopo tile scheme; depths sampled by "
            f"scripts/bathymetry_profiles.py. The excerpt is verbatim in the tile's TIFF "
            f"ImageDescription tag and on {SPECS_URL}."
        ),
        "claims": [
            {
                "variable": "bathymetry",
                "claim": claim,
                "value": None,
                "excerpt": EVIDENCE_EXCERPT,
                "agrees": None,
            }
        ],
    }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def excerpt_in(text: str) -> bool:
    body = _norm(text)
    pos = 0
    for part in EVIDENCE_EXCERPT.split("..."):
        i = body.find(_norm(part).strip(" .,;:"), pos)
        if i < 0:
            return False
        pos = i + len(part)
    return True


def write_evidence(results: list[SpotResult], path: Path, accessed: str) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    for r in results:
        if not r.tiles:
            continue
        entry = data["spots"][r.slug]
        keep = [s for s in entry["sources"] if not s["key"].startswith("bluetopo-")]
        new = [evidence_source(r, r.tiles[t], accessed) for t in sorted(r.tiles)]
        entry["sources"] = keep + new
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cache", required=True, type=Path, help="download directory")
    ap.add_argument("--slugs", default=",".join(DEFAULT_SLUGS))
    ap.add_argument("--report", type=Path, default=None, help="write the JSON report here")
    ap.add_argument("--write", action="store_true", help="update the data files")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)
    args.cache.mkdir(parents=True, exist_ok=True)
    spots = {s["slug"]: s for s in json.loads((args.data_dir / "spots.json").read_text())}
    scheme = Scheme(newest_scheme(args.cache))
    store = TileStore(scheme, args.cache)
    results = []
    for slug in args.slugs.split(","):
        result = analyse(spots[slug], store)
        for tile in result.tiles.values():
            if not excerpt_in(tile.description):
                raise SystemExit(f"{tile.ref.tile}: evidence excerpt not found in the tile")
        results.append(result)
        print(json.dumps(result.report, indent=1), file=sys.stderr)
    out = {
        "scheme": scheme.path.name,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "spots": [{**r.report, "profile": r.profile} for r in results],
    }
    if args.report:
        args.report.write_text(json.dumps(out, indent=1) + "\n")
    if args.write:
        accessed = datetime.now(UTC).date().isoformat()
        write_profiles(results, args.data_dir / "physical_profiles.json")
        write_evidence(results, args.data_dir / "research_evidence.json", accessed)
    print(json.dumps({r.slug: r.profile for r in results}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
