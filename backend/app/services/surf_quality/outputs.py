"""Per-hour outputs of the quality model for the API (algorithm 2.2.0, spec 18-20).

Read from a stored prediction only (no database access):

* ``favourable`` / ``unfavourable``: the hour's main conditions (``reasons.py``), stored with
  the score; empty for rows scored before 2.2.0;
* ``offshore_class``: the offshore-wind classification, the strength and relation classes
  of ``wind.py`` in one phrase ("light offshore", "strong onshore", "glassy"); None without
  wind data;
* ``model_version``: the scoring model version the hour was scored with (the stored
  ``algorithm_version``, tagged when SCORING_WEIGHTS_JSON changed the weights).

The two confidences stay separate (spec 20) and come with the other confidence fields of
``forecast_confidence.api_fields``: ``forecast_confidence`` (the forecast class at read
time, labelled heuristic) and ``research_confidence`` (the spot's research class the hour
was scored with). The combined "local confidence" is deprecated and used for no decision.
"""

from __future__ import annotations

from typing import Any

WIND_RELATIONS = frozenset(
    {"offshore", "cross-offshore", "cross-shore", "cross-onshore", "onshore"}
)


def offshore_class(relation: str | None, strength: str | None) -> str | None:
    """The offshore-wind class from the stored relation and strength ("light offshore",
    "strong onshore"); "glassy" for a near-calm wind; the relation alone for rows without a
    strength class (scored before 2.1.0); None without wind data."""
    if relation == "glassy":
        return "glassy"
    if relation not in WIND_RELATIONS:
        return None
    return f"{strength} {relation}" if strength else relation


def hour_outputs(prediction: Any) -> dict[str, Any]:
    """``favourable``, ``unfavourable``, ``offshore_class`` and ``model_version`` of a
    stored prediction (a SurfQualityPrediction or a row with ``components``,
    ``wind_relation`` and ``algorithm_version``)."""
    components = prediction.components or {}
    detail = components.get("wind_detail")
    strength = detail.get("strength") if isinstance(detail, dict) else None
    return {
        "favourable": [str(r) for r in components.get("favourable") or []],
        "unfavourable": [str(r) for r in components.get("unfavourable") or []],
        "offshore_class": offshore_class(prediction.wind_relation, strength),
        "model_version": prediction.algorithm_version,
    }
