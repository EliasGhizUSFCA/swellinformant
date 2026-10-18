"""The sync daemon (``python -m app.cli sync daemon``). Owned by the sync-core package.

Created by the foundation with the stub's behaviour only: sync-core replaces it with the
``--once`` scenarios of its design (init-db, kicks consumed, heartbeat written, no database
query on an idle check).
"""

from __future__ import annotations

import pytest

from app.services.sync import daemon

pytestmark = pytest.mark.integration


def test_daemon_stub_says_it_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="not implemented yet"):
        daemon.run_daemon(once=True)
