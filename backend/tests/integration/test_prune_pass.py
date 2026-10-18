"""maintenance.prune_pass, the prune after every sync (F7, SYNC_PRUNE_EACH_PASS). Owned by
the egress-map-serving package.

Created by the foundation with the behaviour of the first implementation: superseded member
predictions (when PRUNE_SUPERSEDED_MEMBER_PREDICTIONS) plus the map fields of runs that are
no longer among the best servable ones, committed. egress-map-serving extends it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services import maintenance
from app.services.maintenance import prune_pass

pytestmark = pytest.mark.integration


def test_prune_pass_on_an_empty_database(db: Session) -> None:
    assert prune_pass(db) == {"superseded_predictions": 0, "map_fields": 0}


@pytest.mark.parametrize("prune_predictions", [True, False])
def test_prune_pass_runs_both_prunes_and_commits(
    db: Session, monkeypatch: pytest.MonkeyPatch, prune_predictions: bool
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(get_settings(), "prune_superseded_member_predictions", prune_predictions)
    monkeypatch.setattr(
        maintenance,
        "prune_superseded_predictions",
        lambda _db, now=None: calls.append("predictions") or 4,
    )
    monkeypatch.setattr(maintenance, "prune_map_fields", lambda _db: calls.append("fields") or 7)
    committed: list[bool] = []
    monkeypatch.setattr(db, "commit", lambda: committed.append(True))
    out = prune_pass(db)
    assert out == {"superseded_predictions": 4 if prune_predictions else 0, "map_fields": 7}
    assert calls == (["fields", "predictions"] if prune_predictions else ["fields"])
    assert committed == [True]
