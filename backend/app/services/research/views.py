"""API read models for research confidence (used by app.services.spot_views).

The sources listed for a spot come from the snapshot stored in its current assessment,
so they always match the evidence that assessment used (excerpts are never returned).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SpotResearchAssessment
from app.models.research import RESEARCH_VARIABLES, SOURCE_TYPES
from app.schemas.spots import ResearchOut, ResearchSourceOut, ResearchVariableOut
from app.services.research.assess import VARIABLE_LABELS, current_assessment


def research_classes(db: Session) -> dict[int, str]:
    """spot_id -> overall research class of the current assessment (one query)."""
    rows = db.execute(
        select(SpotResearchAssessment.spot_id, SpotResearchAssessment.overall_class).where(
            SpotResearchAssessment.is_current
        )
    )
    return {spot_id: cls for spot_id, cls in rows}


def _sources(per_variable: dict[str, Any]) -> list[ResearchSourceOut]:
    seen: dict[str, dict[str, Any]] = {}
    for entry in per_variable.values():
        for e in entry.get("evidence", []):
            seen.setdefault(e["source_key"], e)
    rank = {t: i for i, t in enumerate(SOURCE_TYPES)}
    ordered = sorted(
        seen.values(),
        key=lambda e: (rank.get(e["source_type"], len(rank)), e["publisher"], e["title"]),
    )
    return [
        ResearchSourceOut(
            title=e["title"],
            url=e["url"],
            publisher=e["publisher"],
            source_type=e["source_type"],
            published_year=e["published_year"],
            accessed_at=e["accessed_at"],
            retrieval=e.get("retrieval"),  # absent in research-v1 assessments
        )
        for e in ordered
    ]


def research_view(row: SpotResearchAssessment) -> ResearchOut:
    per_variable = row.per_variable or {}
    variables = [
        ResearchVariableOut(
            variable=v,
            label=VARIABLE_LABELS[v],
            research_class=entry["class"],
            summary=entry.get("summary", ""),
            gaps=list(entry.get("gaps", [])),
            source_count=int(entry.get("sources", 0)),
        )
        for v in RESEARCH_VARIABLES
        if (entry := per_variable.get(v)) is not None
    ]
    return ResearchOut(
        research_class=row.overall_class,
        summary=row.summary,
        gaps=list(row.gaps or []),
        variables=variables,
        sources=_sources(per_variable),
        methodology_version=row.methodology_version,
        assessed_at=row.assessed_at,
        research_updated_at=row.research_updated_at,
    )


def research_out(db: Session, spot_id: int) -> ResearchOut | None:
    """The spot's current research confidence, or None when it was never assessed."""
    row = current_assessment(db, spot_id)
    return research_view(row) if row is not None else None
