"""The main favourable and unfavourable conditions of one forecast hour (algorithm 2.2.0).

Short plain phrases built from the component fits of the score, never from anything the
score did not use:

* a factor whose fit is at least 0.75 (the "good" cut-off of the explanation text and of
  the wind classes) can be favourable, one below 0.4 unfavourable, and so is a size or
  period whose limiting-factor gate is below 1 (it holds the whole score down); fits in
  between are not a main condition either way;
* the swell phrases describe the partition that drives the surf (the largest share of the
  breaking energy flux), named as the explanation names it ("swell", "secondary swell",
  "wind sea");
* the wind phrase uses the relation and strength classes of ``wind.py`` (directions are
  FROM bearings, so "offshore wind" blows from the land); an onshore or cross-onshore wind
  that is merely "workable" is listed as unfavourable, a light one included (spec 19:
  light onshore is often marginal), and a strong wind is never listed as favourable, even
  when the wind fit is still high (strong offshore may be less favourable);
* the tide is mentioned only when it was scored (an established preference with moderate
  or high research confidence); a spot without one gets no tide phrase, so no preference
  is ever implied;
* each list keeps at most MAX_REASONS phrases, ordered by the points the factor added to
  the score (favourable) or cost it, its gate included (unfavourable).

The cut-offs are the documented heuristic ones; the phrases are descriptions, not
calibrated judgements.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

GOOD_FIT = 0.75
POOR_FIT = 0.4
MAX_REASONS = 3
# The gate that belongs to each factor (its points lost count with the factor's).
FACTOR_GATES = {"wave_size": "size_gate", "swell_period": "period_gate", "wind": "wind_gate"}
ONSHORE_RELATIONS = frozenset({"cross-onshore", "onshore"})
BREAK_WORDS = {
    "reef": "reef",
    "point": "point",
    "beach": "beach",
    "slab": "slab",
    "rivermouth": "rivermouth",
}


def _whole(value: float) -> int:
    """Halves rounded up, as the explanation and the charts round."""
    return math.floor(value + 0.5)


def _feet(value: float) -> str:
    return f"{value:g}"


def condition_reasons(
    fits: Mapping[str, float | None],
    *,
    used_weights: Mapping[str, float],
    points_lost: Mapping[str, float],
    gates: Mapping[str, float] | None = None,
    swell_kind: str,
    swell_period_s: float | None,
    swell_blocked: bool,
    break_type: str | None,
    face_min_ft: float,
    face_max_ft: float,
    face_mid_ft: float,
    size_min_ft: float,
    size_max_ft: float,
    wind_relation: str,
    wind_strength: str | None,
    wind_favourability: str | None,
    wind_speed_kmh: float | None,
) -> tuple[list[str], list[str]]:
    """(favourable, unfavourable) phrases of one hour (see the module docstring).

    ``fits``: the factor fits 0-1 the score used (None or absent = not scored);
    ``used_weights`` and ``points_lost``: the stored ``weights_used`` and ``contributions``;
    ``gates``: the gate values (``size_gate``, ``period_gate``; absent = 1);
    ``swell_*``: the driving partition (``swell_blocked``: its direction is outside the
    break's swell window); ``face_*``: the predicted breaking face heights and ``size_*`` the
    spot's working range, in feet.
    """
    good: list[tuple[float, str]] = []
    bad: list[tuple[float, str]] = []

    def gained(factor: str) -> float:
        fit = fits.get(factor)
        return 100.0 * used_weights.get(factor, 0.0) * (fit or 0.0)

    def lost(factor: str) -> float:
        gate = FACTOR_GATES.get(factor)
        return points_lost.get(factor, 0.0) + (points_lost.get(gate, 0.0) if gate else 0.0)

    def add(factor: str, favourable: str | None, unfavourable: str | None) -> None:
        fit = fits.get(factor)
        if fit is None:
            return
        gate = FACTOR_GATES.get(factor) if factor != "wind" else None
        held_down = gate is not None and (gates or {}).get(gate, 1.0) < 1.0
        if fit >= GOOD_FIT and favourable:
            good.append((gained(factor), favourable))
        elif (fit < POOR_FIT or held_down) and unfavourable:
            bad.append((lost(factor), unfavourable))

    add(
        "swell_direction",
        f"well-aligned {swell_kind}",
        f"{swell_kind} mostly blocked at this break"
        if swell_blocked
        else f"{swell_kind} poorly aligned with the break",
    )
    period = None if swell_period_s is None else _whole(swell_period_s)
    where = BREAK_WORDS.get(break_type or "", "break")
    add(
        "swell_period",
        None if period is None else f"good swell period ({period} s)",
        None if period is None else f"period too short for this {where} ({period} s)",
    )
    faces = f"{_feet(face_min_ft)}–{_feet(face_max_ft)} ft"
    add(
        "wave_size",
        f"good size for this break ({faces})",
        f"too big for this break ({faces})"
        if face_mid_ft > size_max_ft
        else f"too small ({faces})"
        if face_mid_ft < size_min_ft
        else None,
    )
    if fits.get("wind") is not None:
        favourable, unfavourable = wind_phrases(
            wind_relation, wind_strength, wind_favourability, wind_speed_kmh
        )
        if favourable:
            good.append((gained("wind"), favourable))
        elif unfavourable:
            bad.append((lost("wind"), unfavourable))
    add("tide", "favourable tide stage", "unfavourable tide stage")
    add("consistency", None, "mostly wind sea, little organised swell")
    return _top(good), _top(bad)


def wind_phrases(
    relation: str, strength: str | None, favourability: str | None, speed_kmh: float | None
) -> tuple[str | None, str | None]:
    """(favourable, unfavourable) phrase of the wind, at most one of them: glassy and
    favourable winds are favourable ("light offshore wind"), except a strong one (spec 19:
    strong offshore may be less favourable, so it is not listed as a good condition);
    unfavourable winds, and workable onshore or cross-onshore ones, are unfavourable
    ("onshore wind 25 km/h")."""
    if speed_kmh is None or favourability is None or relation == "unknown":
        return None, None
    if relation == "glassy":
        return "glassy, near-calm wind", None
    if favourability == "favourable":
        if strength == "strong":
            return None, None
        return (f"{strength} {relation} wind" if strength else f"{relation} wind"), None
    if favourability == "unfavourable" or relation in ONSHORE_RELATIONS:
        prefix = f"{strength} " if strength in ("light", "strong") else ""
        return None, f"{prefix}{relation} wind {_whole(speed_kmh)} km/h"
    return None, None


def _top(items: list[tuple[float, str]]) -> list[str]:
    ordered = sorted(enumerate(items), key=lambda pair: (-round(pair[1][0], 6), pair[0]))
    out: list[str] = []
    for _, (_, phrase) in ordered:
        if phrase not in out:
            out.append(phrase)
    return out[:MAX_REASONS]
