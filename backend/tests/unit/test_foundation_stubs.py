"""Cross-package interfaces and contracts declared by the foundation (F7, F8).

The signatures (F7) and the options of ``python -m app.cli sync`` and ``migrate`` (F8) are
frozen: other packages call them, so a change here is a change of contract. The behaviour
behind them is tested in the owning packages' files:

* sync: tests/unit/test_sync_detect.py, tests/unit/test_cli_migrate_on_build.py,
  tests/integration/test_sync_orchestrator.py, tests/integration/test_sync_daemon.py,
  tests/integration/test_sync_spot_scope.py;
* dispatch and the internal cron: tests/unit/test_dispatch_runner.py,
  tests/integration/test_internal_cron.py;
* pruning: tests/integration/test_prune_pass.py.
"""

from __future__ import annotations

import argparse
import inspect
from collections.abc import Callable
from typing import Any

import pytest

from app import cli
from app.services.sync import cli as sync_cli
from app.services.sync import daemon, detect, orchestrator, publish
from app.services.sync.types import SyncBusy
from app.workers import dispatch

EMPTY = inspect.Parameter.empty
KW = inspect.Parameter.KEYWORD_ONLY
POS = inspect.Parameter.POSITIONAL_OR_KEYWORD


def params(fn: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


# --------------------------------------------------------------------------- F7 signatures
def test_sync_core_signatures() -> None:
    assert params(detect.load_known) == [("db", POS, EMPTY)]
    assert params(detect.plan_sync) == [
        ("db", POS, EMPTY),
        ("settings", KW, None),
        ("now", KW, None),
        ("known", KW, None),
        ("force", KW, False),
    ]
    assert params(orchestrator.run_sync) == [
        ("trigger", KW, EMPTY),
        ("force", KW, False),
        ("only", KW, None),
        ("now", KW, None),
        ("stop", KW, None),
        ("plan", KW, None),
    ]
    assert params(orchestrator.tick) == [("trigger", KW, EMPTY)]
    assert orchestrator.SyncBusy is SyncBusy
    assert params(publish.publish) == [
        ("db", POS, EMPTY),
        ("sync_run", KW, None),
        ("trigger", KW, "pipeline"),
        ("now", KW, None),
    ]
    assert params(daemon.run_daemon) == [
        ("stop", KW, None),
        ("once", KW, False),
        ("init_db", KW, None),
    ]


def test_other_package_signatures() -> None:
    from app.services.forecasts.ingestion import acquire_all
    from app.services.health import data_health
    from app.services.maintenance import prune_pass
    from app.services.notifications.ops_alerts import run_data_health_check
    from app.services.notifications.transactional import (
        deliver_account_email,
        deliver_email,
        deliver_sms,
    )

    assert params(acquire_all) == [
        ("db", POS, EMPTY),
        ("now", KW, None),
        ("force", KW, False),
        ("only", KW, None),
    ]
    assert params(prune_pass) == [("db", POS, EMPTY), ("now", POS, None)]
    assert params(run_data_health_check) == [("db", POS, EMPTY), ("now", POS, None)]
    assert callable(data_health)
    assert params(dispatch.kick) == [("job", POS, EMPTY), ("match_ids", KW, ())]
    # The inline senders the JOB_RUNNER=daemon dispatch uses (no Celery import).
    assert [n for n, _, _ in params(deliver_account_email)] == [
        "message_id",
        "text",
        "html",
        "final_attempt",
        "now",
    ]
    assert [n for n, _, _ in params(deliver_email)] == ["to", "subject", "text", "html", "tag"]
    assert [n for n, _, _ in params(deliver_sms)] == ["to", "body"]


# --------------------------------------------------------------------------- F8 CLI
@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    """The namespaces ``python -m app.cli sync ...`` hands to app.services.sync.cli.run."""
    seen: list[argparse.Namespace] = []

    def run(args: argparse.Namespace) -> int:
        seen.append(args)
        return sync_cli.EXIT_OK

    monkeypatch.setattr(sync_cli, "run", run)
    return seen


def test_sync_group_is_registered() -> None:
    assert cli.PLUGIN_GROUPS["sync"] == "app.services.sync.cli"
    assert (sync_cli.EXIT_OK, sync_cli.EXIT_FAILED, sync_cli.EXIT_BUSY) == (0, 1, 3)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["sync", "check"], {"sync_cmd": "check", "json": False}),
        (["sync", "check", "--json"], {"sync_cmd": "check", "json": True}),
        (
            ["sync", "run"],
            {"sync_cmd": "run", "force": False, "only": None, "if_due": False, "trigger": "cli"},
        ),
        (
            ["sync", "run", "--force", "--only", "map,spots", "--if-due", "--trigger", "github"],
            {
                "sync_cmd": "run",
                "force": True,
                "only": ["spots", "map"],
                "if_due": True,
                "trigger": "github",
            },
        ),
        (["sync", "run", "--only", "ensemble"], {"only": ["ensemble"]}),
        (["sync", "daemon"], {"sync_cmd": "daemon", "once": False, "no_init_db": False}),
        (
            ["sync", "daemon", "--once", "--no-init-db"],
            {"sync_cmd": "daemon", "once": True, "no_init_db": True},
        ),
        (["sync", "publish"], {"sync_cmd": "publish"}),
        (["sync", "status"], {"sync_cmd": "status", "limit": 10}),
        (["sync", "status", "--limit", "5"], {"sync_cmd": "status", "limit": 5}),
    ],
)
def test_sync_command_options(
    argv: list[str], expected: dict[str, object], parsed: list[argparse.Namespace]
) -> None:
    assert cli.main(argv) == sync_cli.EXIT_OK
    (args,) = parsed
    assert args.cmd == "sync"
    assert {key: getattr(args, key) for key in expected} == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["sync"],
        ["sync", "run", "--only", "spots,gefs"],
        ["sync", "run", "--only", ""],
        ["sync", "run", "--trigger", "daemon"],
        ["sync", "status", "--limit", "many"],
        ["sync", "nope"],
    ],
)
def test_sync_options_are_validated(argv: list[str], parsed: list[argparse.Namespace]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2 and parsed == []


def test_plain_migrate_is_unchanged_and_on_build_is_off_by_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.core.config import get_settings

    migrated: list[bool] = []
    monkeypatch.setattr(cli, "migrate", lambda: migrated.append(True))
    assert cli.main(["migrate"]) == 0
    assert migrated == [True] and capsys.readouterr().out == "database migrated\n"
    # MIGRATE_ON_BUILD=off (the default): a build step never migrates.
    assert get_settings().migrate_on_build == "off"
    cli.main(["migrate", "--on-build"])
    assert migrated == [True]
