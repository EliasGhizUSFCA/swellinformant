"""Overall opportunity score (0–100) combining surf, price, confidence and convenience.

The components are heuristic 0–100 indices, not probabilities. Weights per user priority
are configurable with RANKING_WEIGHTS_JSON, e.g.
``{"balanced": {"surf": 0.4, "affordability": 0.25, "confidence": 0.15, "convenience": 0.2}}``.

Two ranking-only adjustments (spec 26 and 28), documented in docs/MATCHING_AND_FLIGHTS.md:

* the surf component rewards a window whose breaking height is close to the search's
  preferred height (``wave_preferred_ft``) when one is set (:func:`height_fit`);
* swells starting SHORT_NOTICE_MIN_DAYS to SHORT_NOTICE_MAX_DAYS ahead rank higher in the
  flight-search queue and on the dashboard (:func:`ranking_score`). The stored and shown
  ``overall_score`` never includes that boost, so it does not change as time passes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.models.enums import RankingPriority

# Points added to the overall score (0-100) of a short-notice match for ordering only.
SHORT_NOTICE_BOOST = 15
# Weight of the preferred-height fit inside the surf component when a preference is set.
PREFERRED_HEIGHT_WEIGHT = 0.15

DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    RankingPriority.BALANCED: {
        "surf": 0.40,
        "affordability": 0.25,
        "confidence": 0.15,
        "convenience": 0.20,
    },
    RankingPriority.BEST_WAVES: {
        "surf": 0.60,
        "affordability": 0.15,
        "confidence": 0.15,
        "convenience": 0.10,
    },
    RankingPriority.CHEAPEST: {
        "surf": 0.25,
        "affordability": 0.50,
        "confidence": 0.10,
        "convenience": 0.15,
    },
    RankingPriority.SHORTEST: {
        "surf": 0.25,
        "affordability": 0.15,
        "confidence": 0.10,
        "convenience": 0.50,
    },
}


@lru_cache
def ranking_weights(raw: str | None = None) -> dict[str, dict[str, float]]:
    weights = {str(k): dict(v) for k, v in DEFAULT_WEIGHTS.items()}
    if raw:
        for profile, values in json.loads(raw).items():
            if profile not in weights:
                raise ValueError(f"unknown ranking profile {profile!r}")
            merged = {**weights[profile], **{k: float(v) for k, v in values.items()}}
            total = sum(merged.values())
            weights[profile] = {k: v / total for k, v in merged.items()}
    return weights


def height_fit(
    height_min_ft: float,
    height_max_ft: float,
    preferred_ft: float,
    wave_min_ft: float,
    wave_max_ft: float,
) -> float:
    """0-1: how close the window's breaking height (the middle of its likely faces) is to
    the preferred height, relative to the distance from the preference to the farther end
    of the search's accepted range (1 = exactly the preference, 0 = at that end or beyond)."""
    mid = (height_min_ft + height_max_ft) / 2.0
    span = max(preferred_ft - wave_min_ft, wave_max_ft - preferred_ft, 0.5)
    return max(0.0, 1.0 - abs(mid - preferred_ft) / span)


def surf_component(peak_score: int, avg_score: float, fit: float | None = None) -> float:
    """``0.7 × peak + 0.3 × average`` score; with a preferred height (``fit`` 0-1, see
    :func:`height_fit`) ``0.6 × peak + 0.25 × average + 15 × fit``."""
    if fit is None:
        return round(0.7 * peak_score + 0.3 * avg_score, 1)
    w = PREFERRED_HEIGHT_WEIGHT
    return round(0.6 * peak_score + (0.4 - w) * avg_score + w * 100.0 * fit, 1)


def match_surf_component(
    peak_score: int,
    avg_score: float,
    height_min_ft: float,
    height_max_ft: float,
    search: object,
) -> float:
    """The surf component of a match for ``search`` (its preferred height when set)."""
    preferred = getattr(search, "wave_preferred_ft", None)
    fit = None
    if preferred is not None:
        fit = height_fit(
            height_min_ft,
            height_max_ft,
            float(preferred),
            float(getattr(search, "wave_min_ft", 0.0)),
            float(getattr(search, "wave_max_ft", preferred)),
        )
    return surf_component(peak_score, avg_score, fit)


def is_short_notice(
    window_start: datetime, now: datetime, settings: Settings | None = None
) -> bool:
    """The swell starts SHORT_NOTICE_MIN_DAYS to SHORT_NOTICE_MAX_DAYS from ``now``."""
    s = settings or get_settings()
    ahead = window_start - now
    return (
        timedelta(days=s.short_notice_min_days) <= ahead <= timedelta(days=s.short_notice_max_days)
    )


def ranking_score(
    overall_score: int, window_start: datetime, now: datetime, settings: Settings | None = None
) -> int:
    """The ordering key of a match: its overall score plus SHORT_NOTICE_BOOST when short
    notice (never stored)."""
    boost = SHORT_NOTICE_BOOST if is_short_notice(window_start, now, settings) else 0
    return overall_score + boost


def opportunity_scores(
    priority: RankingPriority | str,
    *,
    surf: float,
    confidence: float,
    affordability: float | None,
    convenience: float | None,
) -> tuple[dict[str, float | None], int]:
    w = ranking_weights(get_settings().ranking_weights_json)[str(priority)]
    components: dict[str, float | None] = {
        "surf": round(surf, 1),
        "confidence": round(confidence, 1),
        "affordability": None if affordability is None else round(affordability, 1),
        "convenience": None if convenience is None else round(convenience, 1),
    }
    used = {k: v for k, v in components.items() if v is not None}
    total_w = sum(w[k] for k in used)
    overall = sum(w[k] * v for k, v in used.items()) / total_w if total_w else 0.0
    return components, int(round(overall))
