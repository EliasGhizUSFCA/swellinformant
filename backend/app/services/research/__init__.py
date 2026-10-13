"""Spot research evidence and research confidence (see app.models.research).

``current_assessments`` is the read side shared with the prediction job (local
confidence) and the spot views. The other modules (docs/research-confidence.md):

* ``values``: structured claim values and their comparison with a spot profile;
* ``evidence``: loading, validating and syncing ``research_evidence.json``;
* ``assess``: the research-v1.2 methodology and the assessment history;
* ``views``: API read models; ``cli``: ``python -m app.cli research ...``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SpotResearchAssessment


def current_assessments(db: Session) -> dict[int, SpotResearchAssessment]:
    """The current research assessment of every assessed spot (spot_id -> row)."""
    return {
        a.spot_id: a
        for a in db.scalars(
            select(SpotResearchAssessment).where(SpotResearchAssessment.is_current)
        ).all()
    }
