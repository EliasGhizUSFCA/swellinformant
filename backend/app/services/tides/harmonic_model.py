"""Tide predictions from harmonic constants (EOT20 by default), evaluated locally.

The constants file (TIDE_CONSTANTS_PATH, default data/tides/constants_eot20.json, written by
scripts/extract_tide_constants.py) holds, for each spot's tide point (a cell of the global
EOT20 ocean tide model), the amplitude A (metres) and Greenwich phase lag G (degrees) of 17
constituents. The predicted astronomical tide is

    h(t) = sum_k f_k(t) * A_k * cos(V_k(t) + u_k(t) - G_k)

with V the equilibrium argument at Greenwich and f, u the nodal corrections of Schureman
(1958), "Manual of Harmonic Analysis and Prediction of Tides" (US Coast and Geodetic Survey
Special Publication 98): the inclination I of the lunar orbit to the equator and the angles
nu, xi, nu' and 2nu'' from the longitude of the Moon's node N (pages 156 and 37-45), f from
equations 73-78, 227 and 235. These are the corrections FES-type models such as EOT20 are
used with (pyTMD's "FES" set); scripts/extract_tide_constants.py --check-pytmd compares this
module with pyTMD 3.0.9. Mean longitudes of the Moon (s), Sun (h), lunar perigee (p), node
(N) and solar perigee (p1) are the polynomials of Meeus, "Astronomical Algorithms" (2nd ed.,
chapters 22 and 47), evaluated in UTC (TT - UTC, about 69 s, would move the Moon's mean
longitude by about 0.01 deg, under 3 s of M2 timing).

Heights are relative to the model's mean sea level (datum "model_msl"), never a chart
datum: no datum offset is known at a model point. No minor constituents are inferred, and
long-period constituents (SA, SSA) carry the model's seasonal cycle. Nothing here reads the
network; the file is the only input.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

SOURCE = "eot20"
DATUM = "model_msl"
REPO_ROOT = Path(__file__).resolve().parents[4]

# Equilibrium argument V = a*T + b*s + c*h + d*p + e*N + g*p1 + offset (degrees), T the
# hour angle of the mean Sun at Greenwich (Schureman, Table 2), and the nodal group.
_ARGUMENTS: dict[str, tuple[int, int, int, int, int, int, float, str]] = {
    "M2": (2, -2, 2, 0, 0, 0, 0.0, "m2"),
    "S2": (2, 0, 0, 0, 0, 0, 0.0, "none"),
    "N2": (2, -3, 2, 1, 0, 0, 0.0, "m2"),
    "2N2": (2, -4, 2, 2, 0, 0, 0.0, "m2"),
    "K2": (2, 0, 2, 0, 0, 0, 0.0, "k2"),
    "T2": (2, 0, -1, 0, 0, 1, 0.0, "none"),
    "K1": (1, 0, 1, 0, 0, 0, -90.0, "k1"),
    "O1": (1, -2, 1, 0, 0, 0, 90.0, "o1"),
    "P1": (1, 0, -1, 0, 0, 0, 90.0, "none"),
    "Q1": (1, -3, 1, 1, 0, 0, 90.0, "o1"),
    "J1": (1, 1, 1, -1, 0, 0, -90.0, "j1"),
    "S1": (1, 0, 0, 0, 0, 0, 0.0, "none"),
    "M4": (4, -4, 4, 0, 0, 0, 0.0, "m4"),
    "MF": (0, 2, 0, 0, 0, 0, 0.0, "mf"),
    "MM": (0, 1, 0, -1, 0, 0, 0.0, "mm"),
    # Doodson 056.554 (h - p1), the convention pyTMD reads EOT20 with.
    "SA": (0, 0, 1, 0, 0, -1, 0.0, "none"),
    "SSA": (0, 0, 2, 0, 0, 0, 0.0, "none"),
}
CONSTITUENTS = tuple(_ARGUMENTS)


class ConstantsError(ValueError):
    pass


@dataclass(frozen=True)
class SpotConstants:
    """The harmonic constants at one spot's tide point."""

    slug: str
    latitude: float
    longitude: float
    # Great-circle distance from the break to the tide point (km).
    distance_km: float
    names: tuple[str, ...]
    amplitudes: tuple[float, ...]  # metres
    phases: tuple[float, ...]  # degrees, Greenwich phase lag
    source: str = SOURCE
    datum: str = DATUM
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


def resolve_constants_path(path: str | Path | None = None) -> Path:
    """An absolute path as given; a relative one from the working directory, else from the
    repository root (development) or / (the Docker image keeps data/ in /data)."""
    if path is None:
        # Imported here so this module needs only numpy (the extraction script imports it).
        from app.core.config import get_settings

        path = get_settings().tide_constants_path
    raw = Path(path)
    if raw.is_absolute():
        return raw
    for base in (Path.cwd(), REPO_ROOT, Path("/")):
        if (base / raw).is_file():
            return base / raw
    return REPO_ROOT / raw


def _read(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads(path.read_text()))
    except FileNotFoundError as exc:
        raise ConstantsError(f"tide constants file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConstantsError(f"tide constants file is not valid JSON: {path}") from exc


@lru_cache(maxsize=4)
def _load_cached(path: str, mtime: float) -> tuple[dict[str, Any], dict[str, SpotConstants]]:
    raw = _read(Path(path))
    points = raw.get("points")
    if not isinstance(points, dict):
        raise ConstantsError(f"{path}: no 'points' object")
    out: dict[str, SpotConstants] = {}
    for slug, point in points.items():
        if not point.get("available"):
            continue
        constants = point.get("constants") or {}
        unknown = sorted(set(constants) - set(_ARGUMENTS))
        if unknown:
            raise ConstantsError(f"{path}: {slug}: unknown constituents {unknown}")
        names = tuple(c for c in CONSTITUENTS if c in constants)
        if "M2" not in names:
            raise ConstantsError(f"{path}: {slug}: no M2 constant")
        amplitudes = tuple(float(constants[c][0]) for c in names)
        phases = tuple(float(constants[c][1]) for c in names)
        if any(not np.isfinite(a) or a < 0 or a > 10 for a in amplitudes):
            raise ConstantsError(f"{path}: {slug}: amplitude out of range")
        out[slug] = SpotConstants(
            slug=slug,
            latitude=float(point["latitude"]),
            longitude=float(point["longitude"]),
            distance_km=float(point["distance_km"]),
            names=names,
            amplitudes=amplitudes,
            phases=phases,
            source=str(raw.get("source", SOURCE)),
            datum=str(raw.get("datum", DATUM)),
            extra={k: v for k, v in point.items() if k not in ("constants",)},
        )
    meta = {k: v for k, v in raw.items() if k != "points"}
    meta["unavailable"] = {
        slug: point.get("reason") for slug, point in points.items() if not point.get("available")
    }
    return meta, out


def load_model(path: str | Path | None = None) -> tuple[dict[str, Any], dict[str, SpotConstants]]:
    """(file metadata: model, licence, attribution, ...; constants by spot slug)."""
    resolved = resolve_constants_path(path)
    try:
        mtime = resolved.stat().st_mtime
    except FileNotFoundError as exc:
        raise ConstantsError(f"tide constants file not found: {resolved}") from exc
    return _load_cached(str(resolved), mtime)


def load_constants(path: str | Path | None = None) -> dict[str, SpotConstants]:
    return load_model(path)[1]


def _posix_seconds(times: Sequence[datetime]) -> np.ndarray:
    for t in times:
        if t.tzinfo is None:
            raise ValueError("times must be timezone-aware")
    return np.array([t.timestamp() for t in times], dtype=np.float64)


def astronomical_arguments(times: Sequence[datetime]) -> dict[str, np.ndarray]:
    """T, s, h, p, N, p1 in degrees at ``times`` (UTC)."""
    seconds = _posix_seconds(times)
    # Julian centuries since J2000.0 (2000-01-01 12:00 UTC = POSIX 946728000).
    c = (seconds - 946728000.0) / (86400.0 * 36525.0)
    s = 218.3164477 + 481267.88123421 * c - 0.0015786 * c**2 + c**3 / 538841.0
    h = 280.46646 + 36000.76983 * c + 0.0003032 * c**2
    p = 83.3532465 + 4069.0137287 * c - 0.0103200 * c**2 - c**3 / 80053.0
    n = 125.04452 - 1934.136261 * c + 0.0020708 * c**2 + c**3 / 450000.0
    p1 = 282.93768193 + 1.71945766 * c + 0.00045688 * c**2
    # Hour angle of the mean Sun at Greenwich: 180 degrees at 00:00 UTC (POSIX days are
    # exactly 86400 s, so the seconds of the UTC day are the remainder).
    tau = 180.0 + 15.0 * np.mod(seconds, 86400.0) / 3600.0
    return {"T": tau, "s": s, "h": h, "p": p, "N": n, "p1": p1}


def nodal_corrections(n_deg: np.ndarray) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    """Nodal factors f and angles u (degrees) per nodal group, from the node longitude N."""
    n = np.radians(n_deg)
    # Schureman p. 156: cos I = cos(w) cos(i) - sin(w) sin(i) cos(N), w = 23.452, i = 5.145.
    inc = np.arccos(0.913694997 - 0.035692561 * np.cos(n))
    at1 = np.arctan(1.01883 * np.tan(n / 2.0))
    at2 = np.arctan(0.64412 * np.tan(n / 2.0))
    xi = n - at1 - at2
    xi = np.arctan2(np.sin(xi), np.cos(xi))
    nu = at1 - at2
    sin_i, sin_2i = np.sin(inc), np.sin(2.0 * inc)
    # nu' (K1) and 2nu'' (K2), Schureman equations 224 and 232.
    nu_p = np.arctan2(sin_2i * np.sin(nu), sin_2i * np.cos(nu) + 0.3347)
    two_nu_s = np.arctan2(sin_i**2 * np.sin(2.0 * nu), sin_i**2 * np.cos(2.0 * nu) + 0.0727)
    f_m2 = np.cos(inc / 2.0) ** 4 / 0.9154
    f = {
        "none": np.ones_like(n),
        "m2": f_m2,
        "m4": f_m2**2,
        "o1": sin_i * np.cos(inc / 2.0) ** 2 / 0.3800,
        "j1": sin_2i / 0.7214,
        "mf": sin_i**2 / 0.1578,
        "mm": (2.0 / 3.0 - sin_i**2) / 0.5021,
        "k1": np.sqrt(0.8965 * sin_2i**2 + 0.6001 * sin_2i * np.cos(nu) + 0.1006),
        "k2": np.sqrt(19.0444 * sin_i**4 + 2.7702 * sin_i**2 * np.cos(2.0 * nu) + 0.0981),
    }
    u = {
        "none": np.zeros_like(n),
        "m2": 2.0 * xi - 2.0 * nu,
        "m4": 4.0 * xi - 4.0 * nu,
        "o1": 2.0 * xi - nu,
        "j1": -nu,
        "mf": -2.0 * xi,
        "mm": np.zeros_like(n),
        "k1": -nu_p,
        "k2": -two_nu_s,
    }
    return f, {k: np.degrees(v) for k, v in u.items()}


def predict(constants: SpotConstants, times: Sequence[datetime]) -> np.ndarray:
    """Predicted tide (metres relative to the model's mean sea level) at ``times``."""
    if not times:
        return np.zeros(0)
    args = astronomical_arguments(times)
    f, u = nodal_corrections(args["N"])
    height = np.zeros(len(times))
    for name, amplitude, phase in zip(
        constants.names, constants.amplitudes, constants.phases, strict=True
    ):
        a, b, c, d, e, g, offset, group = _ARGUMENTS[name]
        v = (
            a * args["T"]
            + b * args["s"]
            + c * args["h"]
            + d * args["p"]
            + e * args["N"]
            + g * args["p1"]
            + offset
        )
        height += f[group] * amplitude * np.cos(np.radians(v + u[group] - phase))
    return height


def regular_times(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    """Times every ``step`` from ``start`` rounded up to a multiple of ``step`` (counted from
    midnight UTC) up to and including ``end``."""
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    seconds = step.total_seconds()
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    k = int(np.ceil((start - midnight).total_seconds() / seconds - 1e-9))
    t = midnight + k * step
    out: list[datetime] = []
    while t <= end:
        out.append(t)
        t += step
    return out


def find_extrema(
    times: Sequence[datetime], heights: np.ndarray
) -> list[tuple[datetime, float, str]]:
    """High and low waters of a regularly sampled curve: (time, height, "high" | "low").

    Each sample that is a local maximum or minimum is refined with the parabola through it
    and its two neighbours (vertex time and height), then rounded to the minute and the
    millimetre. Maxima and minima of a smooth curve alternate, so the result does too.
    """
    out: list[tuple[datetime, float, str]] = []
    if len(times) < 3:
        return out
    step = (times[1] - times[0]).total_seconds()
    h = np.asarray(heights, dtype=np.float64)
    for k in range(1, len(h) - 1):
        a, b, c = h[k - 1], h[k], h[k + 1]
        if b > a and b >= c:
            kind = "high"
        elif b < a and b <= c:
            kind = "low"
        else:
            continue
        denom = a - 2.0 * b + c
        offset = 0.0 if denom == 0 else float(np.clip(0.5 * (a - c) / denom, -0.5, 0.5))
        vertex = b - 0.25 * (a - c) * offset
        t = times[k] + timedelta(seconds=offset * step)
        t = (t + timedelta(seconds=30)).replace(second=0, microsecond=0)
        out.append((t, round(float(vertex), 3), kind))
    return out


def predict_events(
    constants: SpotConstants,
    start: datetime,
    end: datetime,
    step: timedelta = timedelta(minutes=2),
) -> list[tuple[datetime, float, str]]:
    """High and low waters in [start, end] from a curve sampled every ``step``."""
    times = regular_times(start - step, end + step, step)
    events = find_extrema(times, predict(constants, times))
    return [e for e in events if start <= e[0] <= end]
