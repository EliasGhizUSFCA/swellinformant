#!/usr/bin/env python3
"""Size check for the API's Vercel function: the lean dependency set, installed from uv.lock.

Usage (needs uv on PATH, or --uv, and access to PyPI):
  python scripts/check_api_bundle.py [--max-mb 200]

1. `uv export --locked --no-dev --no-emit-project` in backend/: the locked
   [project].dependencies without any extra, which is what the Vercel builder installs for the
   api service. --locked fails when uv.lock no longer matches pyproject.toml.
2. `uv pip install` of that list into a temporary --target directory for the function's
   platform: CPython 3.13 (backend/.python-version) on x86_64-unknown-linux-gnu, the builder's
   target outside Vercel's build image. Wheels only, hashes checked, bytecode compiled (the
   builder adds bytecode while the bundle has room).
3. Prints each distribution's installed size, the project files (backend/ without tests and
   local state, as vercel.json excludeFiles and .vercelignore leave it), the reference data
   (the repository's data/, which the api build command copies into backend/data) and the
   total, then deletes the directory.

Exit status: 0 within the limit, 1 above it, 2 when the size could not be measured.
MB is 1024 x 1024 bytes, the unit of the builder's 225 MB in-zip threshold
(LAMBDA_SIZE_THRESHOLD_BYTES in @vercel/python) and of Vercel's 500 MB function limit.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
MB = 1024 * 1024
DEFAULT_MAX_MB = 200.0
PYTHON_VERSION = "3.13"
PLATFORM = "x86_64-unknown-linux-gnu"
# Under backend/, what never reaches the function bundle: vercel.json's excludeFiles
# (tests/**, **/*.rdb, *.egg-info/**, build/**), .vercelignore, and local tool state.
EXCLUDED_DIRS = frozenset(
    {
        "tests",
        "build",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "htmlcov",
        "node_modules",
        ".vercel",
    }
)
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo", ".pyd", ".rdb", ".log")
EXCLUDED_FILE_NAMES = frozenset({".env", ".coverage", ".DS_Store"})


@dataclass(frozen=True)
class Distribution:
    name: str
    version: str
    size: int  # bytes: the files its RECORD lists plus their compiled bytecode
    files: int


@dataclass(frozen=True)
class TargetSizes:
    distributions: list[Distribution]  # largest first
    unattributed: int  # bytes of files no RECORD names (bytecode of unlisted files, caches)
    total: int  # bytes of every file in the target directory


def _file_sizes(root: Path) -> dict[Path, int]:
    sizes: dict[Path, int] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath, name)
            sizes[path.relative_to(root)] = path.lstat().st_size
    return sizes


def _source_of_bytecode(path: Path) -> Path | None:
    """``pkg/__pycache__/mod.cpython-313.pyc`` -> ``pkg/mod.py``."""
    if path.parent.name != "__pycache__" or path.suffix != ".pyc":
        return None
    return path.parent.parent / (path.name.split(".", 1)[0] + ".py")


def measure_target(target: Path) -> TargetSizes:
    """Installed size per distribution of a ``pip install --target`` directory."""
    sizes = _file_sizes(target)
    owner: dict[Path, int] = {}
    names: list[tuple[str, str]] = []
    for record in sorted(target.glob("*.dist-info/RECORD")):
        name, _, version = record.parent.name.removesuffix(".dist-info").rpartition("-")
        index = len(names)
        names.append((name, version))
        with record.open(newline="", encoding="utf-8") as handle:
            for row in csv.reader(handle):
                if row:
                    owner[Path(os.path.normpath(row[0]))] = index
    for path in sizes:
        if path not in owner and (source := _source_of_bytecode(path)) in owner:
            owner[path] = owner[source]
    totals = [0] * len(names)
    counts = [0] * len(names)
    unattributed = 0
    for path, size in sizes.items():
        found = owner.get(path)
        if found is None:
            unattributed += size
        else:
            totals[found] += size
            counts[found] += 1
    distributions = sorted(
        (
            Distribution(name, version, totals[i], counts[i])
            for i, (name, version) in enumerate(names)
        ),
        key=lambda d: (-d.size, d.name),
    )
    return TargetSizes(distributions, unattributed, sum(sizes.values()))


def project_files_size(backend: Path) -> int:
    """Bytes of the files under backend/ that the function bundle holds."""
    total = 0
    for dirpath, dirnames, filenames in os.walk(backend):
        dirnames[:] = [
            d for d in dirnames if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
        ]
        for name in filenames:
            if (
                name in EXCLUDED_FILE_NAMES
                or name.startswith(".env.")
                or name.endswith(EXCLUDED_FILE_SUFFIXES)
            ):
                continue
            total += Path(dirpath, name).lstat().st_size
    return total


def _mb(size: int) -> str:
    return f"{size / MB:8.2f} MB"


def report(sizes: TargetSizes, project: int, max_mb: float, data: int = 0) -> tuple[str, bool]:
    """The printed table and whether the total is within ``max_mb``."""
    total = sizes.total + project + data
    width = max([len(f"{d.name} {d.version}") for d in sizes.distributions] + [40])
    lines = [
        "API function dependencies: backend/uv.lock, no extras, "
        f"CPython {PYTHON_VERSION} on {PLATFORM}",
    ]
    lines += [f"  {f'{d.name} {d.version}':<{width}} {_mb(d.size)}" for d in sizes.distributions]
    if sizes.unattributed:
        lines.append(f"  {'(files no RECORD names)':<{width}} {_mb(sizes.unattributed)}")
    within = total <= max_mb * MB
    lines += [
        f"  {f'{len(sizes.distributions)} distributions':<{width}} {_mb(sizes.total)}",
        f"  {'project files (backend/ without tests)':<{width}} {_mb(project)}",
        f"  {'reference data (data/, copied by the build)':<{width}} {_mb(data)}",
        f"  {'total':<{width}} {_mb(total)}   limit {max_mb:g} MB",
        ("OK: within the limit" if within else "FAIL: above the limit")
        + f" ({total / MB:.1f} of {max_mb:g} MB)",
    ]
    return "\n".join(lines), within


def _run(command: Sequence[str], *, cwd: Path, quiet: bool = False) -> None:
    print("+ " + " ".join(command), file=sys.stderr, flush=True)
    subprocess.run(  # noqa: S603 - fixed argument lists, no shell
        command, cwd=cwd, check=True, stdout=subprocess.DEVNULL if quiet else None
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Size check for the API's Vercel function: uv.lock's lean set, no extras."
    )
    parser.add_argument(
        "--max-mb",
        type=float,
        default=DEFAULT_MAX_MB,
        help=f"fail above this many MB (1024 x 1024 bytes); default {DEFAULT_MAX_MB:g}",
    )
    parser.add_argument("--uv", default=os.environ.get("UV") or shutil.which("uv"))
    args = parser.parse_args(argv)
    if not args.uv:
        print("uv not found: install it (pip install uv) or pass --uv PATH", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="api-bundle-"))
    try:
        requirements = work / "requirements.txt"
        target = work / "site-packages"
        _run(
            [
                args.uv,
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            cwd=BACKEND,
            quiet=True,
        )
        _run(
            [
                args.uv,
                "pip",
                "install",
                "--target",
                str(target),
                "--python",
                sys.executable,
                "--python-version",
                PYTHON_VERSION,
                "--python-platform",
                PLATFORM,
                "--only-binary",
                ":all:",
                "--no-deps",
                "--require-hashes",
                "--compile-bytecode",
                "--requirement",
                str(requirements),
            ],
            cwd=work,
        )
        sizes = measure_target(target)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"could not measure the API bundle: {exc}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(work, ignore_errors=True)
    project = project_files_size(BACKEND)
    if (BACKEND / "data").is_dir():  # a local copy left by running the build command
        project -= project_files_size(BACKEND / "data")
    text, within = report(sizes, project, args.max_mb, project_files_size(ROOT / "data"))
    print(text)
    return 0 if within else 1


if __name__ == "__main__":
    sys.exit(main())
