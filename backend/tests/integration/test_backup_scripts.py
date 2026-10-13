"""scripts/backup-db.sh, restore-db.sh and restore-drill.sh against the test database.

Needs the PostgreSQL client tools (pg_dump, pg_restore, psql, createdb, dropdb) of the
server's major version or newer, and a role that may create databases; skipped otherwise.
The fake "old backups" used for the retention test are empty files, labelled synthetic.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.database import get_engine

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
TOOLS = ("pg_dump", "pg_restore", "psql", "createdb", "dropdb")


@pytest.fixture(scope="module")
def tools(migrated_db: None) -> None:
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        pytest.skip(f"PostgreSQL client tools missing: {missing}")
    version = subprocess.run(
        ["pg_dump", "--version"],  # noqa: S607 - resolved from PATH on purpose
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    match = re.search(r"(\d+)\.", version)
    with get_engine().connect() as conn:
        server = int(conn.execute(text("SHOW server_version_num")).scalar_one()) // 10000
    if match is None or int(match.group(1)) < server:
        pytest.skip(f"pg_dump {version.strip()} is older than the server ({server})")


def run(
    script: str, *args: str, env: dict[str, str] | None = None, stdin: str | None = ""
) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DATABASE_URL": get_settings().database_url,
        "HOME": os.environ.get("HOME", "/tmp"),  # noqa: S108
    }
    return subprocess.run(  # noqa: S603 - our own scripts with fixed arguments
        [str(SCRIPTS / script), *args],
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
        input=stdin,
        timeout=300,
        check=False,
    )


def db_name() -> str:
    return make_url(get_settings().database_url).database or ""


def test_backup_restore_drill_round_trip(tools: None, tmp_path: Path) -> None:
    result = run("restore-drill.sh", env={"BACKUP_DIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert "DRILL PASSED" in result.stderr
    assert re.search(r"surf_spots\s+50\s+50", result.stdout)
    dumps = list(tmp_path.glob(f"drill-{db_name()}-*.dump"))
    assert len(dumps) == 1 and Path(f"{dumps[0]}.sha256").exists()
    assert "swell:swell@" not in result.stderr + result.stdout  # no credentials in output
    with get_engine().connect() as conn:  # the scratch database was dropped
        left = conn.execute(
            text("SELECT count(*) FROM pg_database WHERE datname LIKE :p"),
            {"p": f"{db_name()}_drill_%"},
        ).scalar_one()
    assert left == 0


def test_retention_by_count_and_age(tools: None, tmp_path: Path) -> None:
    name = db_name()
    now = time.time()
    # Synthetic empty "backups": three recent, two older than the age limit, one of
    # another database and one of another prefix (both must be left alone).
    files = {
        f"swell-{name}-20261001T000000Z.dump": 40,
        f"swell-{name}-20261002T000000Z.dump": 35,
        f"swell-{name}-20261010T000000Z.dump": 2,
        f"swell-{name}-20261011T000000Z.dump": 1,
        f"swell-{name}-20261011T120000Z.dump": 0.5,
        "swell-otherdb-20260101T000000Z.dump": 300,
        f"drill-{name}-20260101T000000Z.dump": 300,
    }
    for fname, days in files.items():
        path = tmp_path / fname
        path.write_bytes(b"")
        os.utime(path, (now - days * 86400, now - days * 86400))
    result = run("backup-db.sh", "--dir", str(tmp_path), "--keep", "3", "--max-age-days", "30")
    assert result.returncode == 0, result.stderr
    new = Path(result.stdout.strip())
    assert new.exists() and new.stat().st_size > 0 and Path(f"{new}.sha256").exists()
    kept = sorted(p.name for p in tmp_path.glob("*.dump"))
    assert kept == sorted(
        [
            new.name,
            f"swell-{name}-20261011T000000Z.dump",
            f"swell-{name}-20261011T120000Z.dump",
            "swell-otherdb-20260101T000000Z.dump",
            f"drill-{name}-20260101T000000Z.dump",
        ]
    )
    assert "retention: 3 removed, 3 kept" in result.stderr


def test_backups_are_never_picked_up_by_git(tools: None, tmp_path: Path) -> None:
    """The default BACKUP_DIR is ./backups, inside the checkout when run from the
    repository root (as documented), and backups hold every account: `git add -A` staged
    the dump and its checksum before the backup directory ignored itself."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    git = ["git", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603 - fixed arguments
    result = run("backup-db.sh", "--dir", str(tmp_path / "backups"))
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).exists()
    status = subprocess.run(  # noqa: S603 - fixed arguments
        [*git, "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""


def test_failed_backup_leaves_no_file(tools: None, tmp_path: Path) -> None:
    url = make_url(get_settings().database_url).set(database="swell_no_such_database_ops")
    result = run(
        "backup-db.sh",
        "--dir",
        str(tmp_path),
        env={"DATABASE_URL": url.render_as_string(hide_password=False)},
    )
    assert result.returncode != 0
    assert list(tmp_path.iterdir()) == []


def test_restore_refuses_without_confirmation_and_on_a_bad_checksum(
    tools: None, tmp_path: Path
) -> None:
    backup = run("backup-db.sh", "--dir", str(tmp_path))
    assert backup.returncode == 0, backup.stderr
    archive = backup.stdout.strip()
    no_tty = run("restore-db.sh", "--create", "--target-db", "swell_ops_never_created", archive)
    assert no_tty.returncode != 0 and "refusing to restore without a terminal" in no_tty.stderr
    Path(f"{archive}.sha256").write_text("0" * 64 + "  x.dump\n")
    bad = run(
        "restore-db.sh", "--yes", "--create", "--target-db", "swell_ops_never_created", archive
    )
    assert bad.returncode != 0 and "checksum mismatch" in bad.stderr
    with get_engine().connect() as conn:
        exists = conn.execute(
            text("SELECT count(*) FROM pg_database WHERE datname = 'swell_ops_never_created'")
        ).scalar_one()
    assert exists == 0


def test_in_place_restore_refuses_another_schema_revision(tools: None, tmp_path: Path) -> None:
    """An older backup over a migrated database would leave the newer tables behind (they
    block the drop of the tables they reference, or make the next migration fail): the
    script compares Alembic revisions first. The revision written here is synthetic."""
    backup = run("backup-db.sh", "--dir", str(tmp_path))
    assert backup.returncode == 0, backup.stderr
    archive = backup.stdout.strip()
    scratch = "swell_ops_revision_check"
    url = make_url(get_settings().database_url)
    scratch_url = url.set(database=scratch).render_as_string(hide_password=False)
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))
    try:
        created = run("restore-db.sh", "--yes", "--create", "--target-db", scratch, archive)
        assert created.returncode == 0, created.stderr
        marker = (
            "UPDATE alembic_version SET version_num = 'synthetic_later_revision'; "
            "CREATE TABLE synthetic_later_table (spot_id integer REFERENCES surf_spots (id))"
        )
        psql = ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", scratch, "-c"]
        env = {"DATABASE_URL": scratch_url}
        assert run_cmd([*psql, marker], env).returncode == 0

        refused = run("restore-db.sh", "--yes", archive, env=env)
        assert refused.returncode != 0
        assert "schema revision synthetic_later_revision" in refused.stderr
        assert "Nothing was changed" in refused.stderr and "--create" in refused.stderr
        left = run_cmd(
            [*psql, "SELECT version_num FROM alembic_version, synthetic_later_table LIMIT 1"],
            env,
        )
        assert left.returncode == 0  # both still there: nothing was touched

        same = "DROP TABLE synthetic_later_table; UPDATE alembic_version SET version_num = '{}'"
        head = run_cmd(["pg_restore", "--data-only", "--table=alembic_version", "-f-", archive])
        revision = head.stdout.split("FROM stdin;\n", 1)[1].split("\n", 1)[0]
        assert run_cmd([*psql, same.format(revision)], env).returncode == 0
        in_place = run("restore-db.sh", "--yes", archive, env=env)
        assert in_place.returncode == 0, in_place.stderr
        assert f"schema revision: backup {revision}, target {revision}" in in_place.stderr
    finally:
        with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS {scratch}"))


def run_cmd(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """A PostgreSQL client tool with the connection settings of ``env``'s DATABASE_URL."""
    url = make_url((env or {}).get("DATABASE_URL", get_settings().database_url))
    pg = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
    }
    return subprocess.run(  # noqa: S603 - fixed client tools, test-built arguments
        args, capture_output=True, text=True, env=pg, timeout=120, check=False
    )
