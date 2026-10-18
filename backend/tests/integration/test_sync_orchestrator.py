"""The sync orchestrator, publish stage and ``sync`` commands. Owned by the sync-core
package.

Created by the foundation with the stubs' behaviour only: sync-core replaces these tests
with the orchestrator scenarios of its design (demo provider, synthetic map fields, the
lease, stage logs with cpu_s, stop between stages).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable

import pytest

from app import cli
from app.services.sync import cli as sync_cli
from app.services.sync import orchestrator, publish

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "call",
    [
        lambda: orchestrator.run_sync(trigger="cli", stop=threading.Event()),
        lambda: orchestrator.tick(trigger="celery"),
        lambda: publish.publish(None),  # type: ignore[arg-type]
    ],
)
def test_orchestrator_stubs_say_they_are_not_implemented(call: Callable[[], object]) -> None:
    with pytest.raises(NotImplementedError, match="not implemented yet"):
        call()


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["sync", "check", "--json"], "sync check"),
        (
            ["sync", "run", "--force", "--only", "map,spots", "--if-due", "--trigger", "github"],
            "sync run",
        ),
        (["sync", "daemon", "--once", "--no-init-db"], "sync daemon"),
        (["sync", "publish"], "sync publish"),
        (["sync", "status", "--limit", "5"], "sync status"),
    ],
)
def test_sync_commands_are_registered_stubs(
    argv: list[str], command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == sync_cli.EXIT_NOT_IMPLEMENTED == 4
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "not_implemented" and out["command"] == command
    assert "not implemented yet" in out["detail"]
