"""Overall opportunity score (0–100) combining surf, price, confidence and convenience.

The components are heuristic 0–100 indices, not probabilities. Weights per user priority
are configurable with RANKING_WEIGHTS_JSON, e.g.
``{"balanced": {"surf": 0.4, "affordability": 0.25, "confidence": 0.15, "convenience": 0.2}}``.
"""

from __future__ import annotations

import json
from functools import lru_cache

from app.core.config import get_settings
from app.models.enums import RankingPriority

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


def surf_component(peak_score: int, avg_score: float) -> float:
    return round(0.7 * peak_score + 0.3 * avg_score, 1)


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
