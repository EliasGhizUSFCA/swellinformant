"""scripts/lib/pg-env.sh: DATABASE_URL parsing for the backup and restore scripts.

The URLs are made-up examples; no connection is made.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
LIB = ROOT / "scripts" / "lib" / "pg-env.sh"
KEYS = ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGSSLMODE")
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]


def parse(url: str, shell: str = "sh") -> dict[str, str]:
    script = '. "$1"; pg_env_from_url "$2"; ' + "; ".join(
        f'printf "{k}=%s\\n" "${{{k}:-}}"' for k in KEYS
    )
    env = {"PATH": "/usr/bin:/bin"}
    out = subprocess.run(  # noqa: S603 - fixed argument list, no user input
        [shell, "-c", script, shell, str(LIB), url],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines())


@pytest.mark.parametrize("shell", SHELLS)
def test_app_style_url(shell: str) -> None:
    env = parse("postgresql+psycopg://swell:swell@localhost:5432/swell", shell)
    assert env == {
        "PGHOST": "localhost",
        "PGPORT": "5432",
        "PGUSER": "swell",
        "PGPASSWORD": "swell",
        "PGDATABASE": "swell",
        "PGSSLMODE": "",
    }


@pytest.mark.parametrize("shell", SHELLS)
def test_percent_encoding_query_and_last_at_sign(shell: str) -> None:
    env = parse(
        "postgresql://us%40er:p%3Ass%2Fw%C3%A9rd%25@db.example.com:6543/my_db"
        "?application_name=x&sslmode=require",
        shell,
    )
    assert env["PGUSER"] == "us@er"
    assert env["PGPASSWORD"] == "p:ss/wérd%"
    assert (env["PGHOST"], env["PGPORT"], env["PGDATABASE"]) == ("db.example.com", "6543", "my_db")
    assert env["PGSSLMODE"] == "require"


def test_ipv6_host_and_defaults() -> None:
    env = parse("postgres://[::1]:5433/x")
    assert (env["PGHOST"], env["PGPORT"], env["PGDATABASE"], env["PGUSER"]) == (
        "::1",
        "5433",
        "x",
        "",
    )
    env = parse("postgresql://dbhost/onlydb")
    assert (env["PGHOST"], env["PGPORT"], env["PGDATABASE"]) == ("dbhost", "", "onlydb")


def test_other_schemes_are_refused() -> None:
    with pytest.raises(subprocess.CalledProcessError) as err:
        parse("mysql://u:p@h/db")
    assert "must start with postgresql://" in err.value.stderr
    assert "p@h" not in err.value.stderr  # the URL (and its password) is not echoed


def test_scripts_are_executable_and_document_themselves() -> None:
    for name in ("backup-db.sh", "restore-db.sh", "restore-drill.sh", "check-health.sh"):
        path = ROOT / "scripts" / name
        assert path.stat().st_mode & 0o111, name
        out = subprocess.run(  # noqa: S603
            [str(path), "--help"], check=True, capture_output=True, text=True
        ).stdout
        assert name in out and "set -eu" not in out, name
