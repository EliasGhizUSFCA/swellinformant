"""Offshore wind physics and the wind fit of the quality score (algorithm 2.1.0).

Conventions (the same everywhere in the app):

* a wind direction ``theta`` is the nautical bearing the wind blows FROM (0 = north,
  clockwise). Its velocity vector (where the air goes) has u = -V sin(theta) (east) and
  v = -V cos(theta) (north), the inverse of ``geo.wind_from_uv``;
* **offshore** means the wind blows from the land toward the ocean. Each spot's researched
  ``offshore_wind_direction`` is the FROM bearing of a straight-offshore wind;
* the **seaward unit normal** n points from the land to the ocean, at bearing
  ``offshore_wind_direction + 180``: n = (sin phi, cos phi) with phi that bearing.

Then ``V_off = V . n`` is positive for an offshore component and negative for an onshore one,
and ``V_cross = abs(V x n)`` is the size of the cross-shore component. For a west-facing
beach (offshore from the east, 90 deg) an east wind gives V . n = +V, a west wind -V and north
or south winds 0.

This normal is NOT the nearshore model's shore normal ``beta`` (``Profile.shore_normal_deg``,
the centre of the optimal swell window unless measured). At points and wrapping reefs the two
are far apart (Chicama: beta + 180 = 30 deg, researched offshore 150 deg), so the wind normal
is always built from ``offshore_wind_direction``.

The wind fit, continuous in speed and direction, with no floor:

    V_on    = max(0, -V_off)
    V_e     = hypot(V_on, cross_weight * V_cross)
    Q_chop  = 1 / (1 + (V_e / v_half)^p)                       onshore and cross-shore chop
    Q_strong= 1 / (1 + (max(0, V_off - tolerance) / soft)^2)   strong offshore
    Q       = Q_chop * Q_strong

Calm and light offshore wind score 1. Spot shelter scales the wind before the components are
taken (``V`` -> ``(1 - s(theta)) V``) and a researched tolerance replaces the default. Every
number in :class:`WindParams` is a HEURISTIC default (not fitted to observations: none exist
for these spots); the strength classes follow the Met Office Beaufort scale. See
docs/SURF_SCORING.md, section 3b.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal

from app.services.geo import window_distance

if TYPE_CHECKING:
    from app.services.surf_quality.models import SpotParams

Provenance = Literal["research", "beaufort", "heuristic"]
WindStrength = Literal["glassy", "light", "moderate", "strong"]
WindFavourability = Literal["favourable", "workable", "unfavourable"]
WindProfileQuality = Literal["research", "heuristic default"]

# The Beaufort scale in its km/h form: forces 0-1 (calm, light air: "calm (glassy)" and "calm
# (rippled)" seas) up to 5 km/h, force 2 (light breeze) 6-11, forces 3-4 (gentle and moderate
# breeze) 12-28, force 5 (fresh breeze) and above from 29. The Met Office table gives the same
# forces in knots (force 2 4-6 kt, forces 3-4 7-16 kt, force 5 17-21 kt).
GLASSY_MAX_KMH = 5.0
LIGHT_BELOW_KMH = 12.0
MODERATE_BELOW_KMH = 29.0
# Favourability classes of the wind fit (heuristic; the cut-offs the explanation text uses).
FAVOURABLE_MIN_Q = 0.75
WORKABLE_MIN_Q = 0.4
# Relation sectors: psi = the angle between the wind and straight offshore, arccos(V_off / V).
# Upper bounds (inclusive), symmetric about cross-shore (90 deg): offshore up to 45,
# cross-offshore up to 75, cross-shore up to 105, cross-onshore up to 135, onshore beyond
# (heuristic widths; 2.0.0 used 45 / 80 / 110 / 135, which put winds 90-110 deg from offshore,
# with an onshore component, in cross-shore).
OFFSHORE_MAX_DEG = 45.0
CROSS_OFFSHORE_MAX_DEG = 75.0
CROSS_SHORE_MAX_DEG = 105.0
CROSS_ONSHORE_MAX_DEG = 135.0
# Shelter fades out over this many degrees outside a sheltered sector, like the swell-window
# taper of the nearshore model: s = factor * exp(-2 (d / 15)^2) (heuristic).
SHELTER_TAPER_DEG = 15.0
# Gust definitions documented per source (schemas.spots.GustKind); "unknown" is never scored.
# Used by the score (WIND_GUST_SCORING=scored) to decide whether a gust may enter the fit.
SCORABLE_GUST_KINDS = frozenset({"gfs_instantaneous", "ecmwf_max_3h", "ecmwf_max_since_pp"})
WIND_PROFILE_QUALITY: tuple[WindProfileQuality, ...] = ("research", "heuristic default")


@dataclass(frozen=True)
class WindParams:
    """Defaults of the wind model. All HEURISTIC: reasoned from practitioner guidance (strong
    offshore from about 15-20 kt; onshore or cross-shore chop above about 10 kt), never fitted.
    """

    # Onshore-equivalent speed that halves Q_chop (about 6.7 kt).
    v_half_kmh: float = 12.5
    # Steepness of the chop curve.
    p: float = 3.0
    # Share of the cross-shore component that counts as onshore-equivalent chop.
    cross_weight: float = 0.5
    # Offshore speed up to which offshore wind does not hurt (about 15 kt).
    offshore_tolerance_kmh: float = 28.0
    # How quickly offshore wind beyond the tolerance hurts.
    soft_kmh: float = 15.0
    # Share of the gust excess (gust - mean) added to the speed when gusts are scored.
    gust_weight: float = 0.25


DEFAULT_PARAMS = WindParams()
PARAM_PROVENANCE: dict[str, Provenance] = {f.name: "heuristic" for f in fields(WindParams)}


@dataclass(frozen=True)
class WindComponents:
    v_offshore_kmh: float  # V . n: > 0 offshore, < 0 onshore
    v_cross_kmh: float  # abs(V x n), >= 0


@dataclass(frozen=True)
class ShelterSector:
    """Winds FROM the clockwise sector [from_min, from_max] are reduced by ``factor`` (0 = no
    shelter, 1 = none of the wind reaches the break), fading out outside the sector."""

    from_min: float
    from_max: float
    factor: float
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class WindProfile:
    """Optional per-spot wind knowledge (``spot_physical_profiles.wind_profile``). Filled only
    from research claims read directly (``data_quality`` "research", with their evidence keys);
    every field left empty falls back to :class:`WindParams`."""

    offshore_tolerance_kmh: float | None = None
    # Evidence keys of the tolerance and the extra offshore directions.
    sources: tuple[str, ...] = ()
    shelter: tuple[ShelterSector, ...] = ()
    # Further straight-offshore FROM bearings (breaks with several wave faces).
    extra_offshore_deg: tuple[float, ...] = ()
    data_quality: WindProfileQuality = "heuristic default"
    notes: str | None = None


@dataclass(frozen=True)
class WindFit:
    """The wind fit of one hour and how it was obtained (stored as ``wind_detail``)."""

    q: float
    v_offshore_kmh: float | None
    v_cross_kmh: float | None
    strength: WindStrength
    favourability: WindFavourability
    gust_used: bool
    shelter_factor: float
    params: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "q": round(self.q, 3),
            "v_offshore_kmh": None
            if self.v_offshore_kmh is None
            else round(self.v_offshore_kmh, 1),
            "v_cross_kmh": None if self.v_cross_kmh is None else round(self.v_cross_kmh, 1),
            "strength": self.strength,
            "favourability": self.favourability,
            "gust_used": self.gust_used,
            "params": self.params,
        }


# --------------------------------------------------------------------------- vectors
def wind_vector(speed_kmh: float, from_deg: float) -> tuple[float, float]:
    """(u, v) of a wind of ``speed_kmh`` blowing FROM ``from_deg``: where the air goes."""
    theta = math.radians(from_deg)
    return -speed_kmh * math.sin(theta), -speed_kmh * math.cos(theta)


def seaward_normal(offshore_deg: float) -> tuple[float, float]:
    """Unit vector (east, north) from the land to the ocean: bearing offshore + 180."""
    phi = math.radians((offshore_deg + 180.0) % 360.0)
    return math.sin(phi), math.cos(phi)


def wind_components(speed_kmh: float, from_deg: float, offshore_deg: float) -> WindComponents:
    """``V . n`` and ``abs(V x n)`` from the explicit vectors (see the module docstring)."""
    u, v = wind_vector(speed_kmh, from_deg)
    nx, ny = seaward_normal(offshore_deg)
    return WindComponents(v_offshore_kmh=u * nx + v * ny, v_cross_kmh=abs(u * ny - v * nx))


def offshore_angle_deg(speed_kmh: float, from_deg: float, offshore_deg: float) -> float:
    """psi in [0, 180]: the angle between the wind and straight offshore, arccos(V_off / V)
    (atan2 of the components, robust at every angle). 0 for a calm wind."""
    c = wind_components(speed_kmh, from_deg, offshore_deg)
    return round(math.degrees(math.atan2(c.v_cross_kmh, c.v_offshore_kmh)), 9)


# --------------------------------------------------------------------------- labels
def wind_relation(speed_kmh: float | None, from_deg: float | None, offshore_deg: float) -> str:
    """One of glassy, offshore, cross-offshore, cross-shore, cross-onshore, onshore, or
    unknown without data. The strings (and their String(16) column) are those of 2.0.0."""
    if speed_kmh is None:
        return "unknown"
    if speed_kmh <= GLASSY_MAX_KMH:
        return "glassy"  # includes a calm wind, whose direction is None
    if from_deg is None:
        return "unknown"
    psi = offshore_angle_deg(1.0, from_deg, offshore_deg)
    if psi <= OFFSHORE_MAX_DEG:
        return "offshore"
    if psi <= CROSS_OFFSHORE_MAX_DEG:
        return "cross-offshore"
    if psi <= CROSS_SHORE_MAX_DEG:
        return "cross-shore"
    if psi <= CROSS_ONSHORE_MAX_DEG:
        return "cross-onshore"
    return "onshore"


def wind_strength(speed_kmh: float) -> WindStrength:
    """Speed class from the Beaufort scale (km/h): glassy <= 5, light < 12, moderate < 29."""
    if speed_kmh <= GLASSY_MAX_KMH:
        return "glassy"
    if speed_kmh < LIGHT_BELOW_KMH:
        return "light"
    if speed_kmh < MODERATE_BELOW_KMH:
        return "moderate"
    return "strong"


def wind_favourability(q: float) -> WindFavourability:
    """Heuristic class of the wind fit: favourable >= 0.75, workable >= 0.4."""
    if q >= FAVOURABLE_MIN_Q:
        return "favourable"
    if q >= WORKABLE_MIN_Q:
        return "workable"
    return "unfavourable"


# --------------------------------------------------------------------------- the fit
def shelter_factor(from_deg: float | None, profile: WindProfile | None) -> float:
    """s(theta) in [0, 1]: the largest shelter of any sector, tapered outside it."""
    if from_deg is None or profile is None or not profile.shelter:
        return 0.0
    best = 0.0
    for sector in profile.shelter:
        d = window_distance(from_deg, sector.from_min, sector.from_max)
        s = sector.factor * math.exp(-2.0 * (d / SHELTER_TAPER_DEG) ** 2)
        best = max(best, s)
    return best


def effective_speed(speed_kmh: float, gust_kmh: float | None, params: WindParams) -> float:
    """The speed the fit uses: the mean plus ``gust_weight`` of the gust excess, if a gust is
    given (the score passes one only when gusts are scored, see SCORABLE_GUST_KINDS)."""
    if gust_kmh is None:
        return speed_kmh
    return speed_kmh + params.gust_weight * max(0.0, gust_kmh - speed_kmh)


def _q_for_direction(
    speed_kmh: float, from_deg: float, offshore_deg: float, tolerance: float, params: WindParams
) -> tuple[float, WindComponents]:
    c = wind_components(speed_kmh, from_deg, offshore_deg)
    v_on = max(0.0, -c.v_offshore_kmh)
    v_e = math.hypot(v_on, params.cross_weight * c.v_cross_kmh)
    q_chop = 1.0 / (1.0 + (v_e / params.v_half_kmh) ** params.p)
    excess = max(0.0, c.v_offshore_kmh - tolerance)
    q_strong = 1.0 / (1.0 + (excess / params.soft_kmh) ** 2)
    return q_chop * q_strong, c


def wind_fit(
    speed_kmh: float,
    from_deg: float | None,
    spot: SpotParams,
    gust_kmh: float | None = None,
    params: WindParams = DEFAULT_PARAMS,
) -> WindFit:
    """Q_wind and its breakdown for one hour at ``spot`` (its ``offshore_wind_direction`` and
    optional ``wind_profile``; see the module docstring).

    A wind with no direction is calm when its speed is 0 (Q = 1). Above that, an unknown
    direction is scored as a pure cross-shore wind (V_off = 0, V_cross = V), the middle of
    the range, and its components are reported as None.
    """
    profile: WindProfile | None = spot.wind_profile
    offshore_deg = float(spot.offshore_wind_direction)
    s = shelter_factor(from_deg, profile)
    v = (1.0 - s) * max(0.0, effective_speed(speed_kmh, gust_kmh, params))
    tolerance = params.offshore_tolerance_kmh
    tolerance_prov: dict[str, Any] = {"value": tolerance, "provenance": "heuristic"}
    if profile is not None and profile.offshore_tolerance_kmh is not None:
        tolerance = profile.offshore_tolerance_kmh
        tolerance_prov = {
            "value": tolerance,
            "provenance": "research" if profile.data_quality == "research" else "heuristic",
            "sources": list(profile.sources),
        }
    used_offshore = offshore_deg
    v_off: float | None
    v_cross: float | None
    if v == 0.0:
        q, v_off, v_cross = 1.0, 0.0, 0.0
    elif from_deg is None:
        v_e = params.cross_weight * v
        q = 1.0 / (1.0 + (v_e / params.v_half_kmh) ** params.p)
        v_off = v_cross = None
    else:
        q, comps = _q_for_direction(v, from_deg, offshore_deg, tolerance, params)
        v_off, v_cross = comps.v_offshore_kmh, comps.v_cross_kmh
        for extra in profile.extra_offshore_deg if profile is not None else ():
            q_extra, _ = _q_for_direction(v, from_deg, extra, tolerance, params)
            if q_extra > q:
                q, used_offshore = q_extra, extra
    detail: dict[str, dict[str, Any]] = {
        "offshore_wind_direction": {"value": used_offshore, "provenance": "research"},
        "glassy_max_kmh": {"value": GLASSY_MAX_KMH, "provenance": "beaufort"},
        "v_half_kmh": {"value": params.v_half_kmh, "provenance": PARAM_PROVENANCE["v_half_kmh"]},
        "p": {"value": params.p, "provenance": PARAM_PROVENANCE["p"]},
        "cross_weight": {
            "value": params.cross_weight,
            "provenance": PARAM_PROVENANCE["cross_weight"],
        },
        "offshore_tolerance_kmh": tolerance_prov,
        "soft_kmh": {"value": params.soft_kmh, "provenance": PARAM_PROVENANCE["soft_kmh"]},
    }
    if gust_kmh is not None:
        detail["gust_weight"] = {
            "value": params.gust_weight,
            "provenance": PARAM_PROVENANCE["gust_weight"],
        }
    if s > 0.0 and profile is not None:
        detail["shelter_factor"] = {
            # The direction of the sheltered sector comes from research; its strength is a
            # heuristic encoding of words such as "some shelter".
            "value": round(s, 3),
            "provenance": "heuristic",
            "sources": sorted({k for sector in profile.shelter for k in sector.sources}),
        }
    return WindFit(
        q=q,
        v_offshore_kmh=v_off,
        v_cross_kmh=v_cross,
        strength=wind_strength(speed_kmh),
        favourability=wind_favourability(q),
        gust_used=gust_kmh is not None,
        shelter_factor=s,
        params=detail,
    )


def q_wind(
    speed_kmh: float,
    from_deg: float | None,
    spot: SpotParams,
    gust_kmh: float | None = None,
    params: WindParams = DEFAULT_PARAMS,
) -> float:
    """Q_wind in [0, 1] (the ``q`` of :func:`wind_fit`): Q_chop x Q_strong, shelter applied,
    no floor, Q(0) = 1."""
    return wind_fit(speed_kmh, from_deg, spot, gust_kmh, params).q


# --------------------------------------------------------------------------- profiles
WIND_PROFILE_KEYS = frozenset(
    {"offshore_tolerance_kmh", "sources", "shelter", "extra_offshore_deg", "data_quality", "notes"}
)
SHELTER_KEYS = frozenset({"from_min", "from_max", "factor", "sources"})


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_keys(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(k, str) and k for k in value)


def validate_wind_profile(
    raw: Any, evidence: Mapping[str, str] | None = None, where: str = "wind_profile"
) -> list[str]:
    """Every problem with a ``wind_profile`` object (empty = valid).

    ``evidence``: evidence key -> retrieval of the spot's research sources. When given, every
    key a "research" profile cites must exist and have been read directly ("fetched").
    """
    if not isinstance(raw, dict):
        return [f"{where} must be an object"]
    problems: list[str] = []
    unknown = sorted(set(raw) - WIND_PROFILE_KEYS)
    if unknown:
        problems.append(f"{where}: unknown keys {unknown}")
    quality = raw.get("data_quality")
    if quality not in WIND_PROFILE_QUALITY:
        problems.append(f"{where}.data_quality must be one of {list(WIND_PROFILE_QUALITY)}")
    research = quality == "research"
    cited: list[str] = []
    tolerance = raw.get("offshore_tolerance_kmh")
    if tolerance is not None and (not _is_number(tolerance) or not 0 < tolerance <= 150):
        problems.append(f"{where}.offshore_tolerance_kmh must be a number in (0, 150] or null")
    sources = raw.get("sources", [])
    if not _is_keys(sources):
        problems.append(f"{where}.sources must be a list of evidence keys")
        sources = []
    cited += sources
    extra = raw.get("extra_offshore_deg", [])
    if not isinstance(extra, list) or not all(_is_number(d) and 0 <= d < 360 for d in extra):
        problems.append(f"{where}.extra_offshore_deg must be a list of degrees in [0, 360)")
        extra = []
    if research and (tolerance is not None or extra) and not sources:
        problems.append(f"{where}: a research tolerance or extra offshore direction needs sources")
    shelter = raw.get("shelter", [])
    if not isinstance(shelter, list):
        problems.append(f"{where}.shelter must be a list")
        shelter = []
    for i, sector in enumerate(shelter):
        at = f"{where}.shelter[{i}]"
        if not isinstance(sector, dict):
            problems.append(f"{at} must be an object")
            continue
        bad = sorted(set(sector) - SHELTER_KEYS)
        missing = sorted(SHELTER_KEYS - {"sources"} - set(sector))
        if bad:
            problems.append(f"{at}: unknown keys {bad}")
        if missing:
            problems.append(f"{at}: missing keys {missing}")
            continue
        for name in ("from_min", "from_max"):
            if not _is_number(sector[name]) or not 0 <= sector[name] < 360:
                problems.append(f"{at}.{name} must be in [0, 360)")
        if not _is_number(sector["factor"]) or not 0 <= sector["factor"] <= 1:
            problems.append(f"{at}.factor must be in [0, 1]")
        keys = sector.get("sources", [])
        if not _is_keys(keys):
            problems.append(f"{at}.sources must be a list of evidence keys")
            keys = []
        if research and not keys:
            problems.append(f"{at}: a research shelter sector needs sources")
        cited += keys
    if research and evidence is not None:
        for key in sorted(set(cited)):
            retrieval = evidence.get(key)
            if retrieval is None:
                problems.append(f"{where}: unknown evidence key {key!r}")
            elif retrieval != "fetched":
                problems.append(f"{where}: evidence {key!r} was not read directly ({retrieval})")
    return problems


def wind_profile_from_dict(raw: Mapping[str, Any] | None) -> WindProfile | None:
    """A WindProfile from its stored JSON (validated by the seed), or None."""
    if not raw:
        return None
    shelter = tuple(
        ShelterSector(
            from_min=float(s["from_min"]),
            from_max=float(s["from_max"]),
            factor=float(s["factor"]),
            sources=tuple(s.get("sources") or ()),
        )
        for s in raw.get("shelter") or ()
    )
    tolerance = raw.get("offshore_tolerance_kmh")
    quality = raw.get("data_quality")
    return WindProfile(
        offshore_tolerance_kmh=None if tolerance is None else float(tolerance),
        sources=tuple(raw.get("sources") or ()),
        shelter=shelter,
        extra_offshore_deg=tuple(float(d) for d in raw.get("extra_offshore_deg") or ()),
        data_quality="research" if quality == "research" else "heuristic default",
        notes=raw.get("notes"),
    )


def cited_keys(raw: Mapping[str, Any]) -> list[str]:
    """Every evidence key a stored wind profile cites."""
    keys: list[str] = list(raw.get("sources") or [])
    for sector in raw.get("shelter") or []:
        if isinstance(sector, Mapping):
            keys += list(sector.get("sources") or [])
    return sorted(set(keys))


def documented_defaults() -> Sequence[tuple[str, float, Provenance]]:
    """(name, default, provenance) of every WindParams field, for the documentation check."""
    return [
        (f.name, getattr(DEFAULT_PARAMS, f.name), PARAM_PROVENANCE[f.name])
        for f in fields(WindParams)
    ]
