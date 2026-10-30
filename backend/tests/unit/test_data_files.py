"""app.core.data_files: every reader of data/ finds it in a checkout, in the Vercel function
(the api build command copies data/ into backend/data, which the builder bundles) and in the
Docker images (/data)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.core import data_files
from app.services import seed
from app.services.observations import stations
from app.services.tides import catalog, harmonic_model

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
# What the readers need: the seed, the two station catalogs and the tide model's constants.
READ_FILES = (
    "surf_spots/spots.json",
    "tide_stations.json",
    "observation_stations.json",
    "tides/constants_eot20.json",
)


def test_a_checkout_reads_the_repositorys_data() -> None:
    assert data_files.REPO_ROOT == ROOT and data_files.SERVICE_ROOT == ROOT / "backend"
    assert seed.resolve_data_dir() == DATA / "surf_spots" == seed.DEFAULT_DATA_DIR
    assert catalog.resolve_catalog_path() == DATA / "tide_stations.json"
    assert stations.resolve_catalog_path() == DATA / "observation_stations.json"
    assert harmonic_model.resolve_constants_path() == DATA / "tides" / "constants_eot20.json"


def test_the_order_is_repository_service_root_slash_then_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert data_files.candidates("tide_stations.json") == [
        ROOT / "data" / "tide_stations.json",
        ROOT / "backend" / "data" / "tide_stations.json",
        Path("/data/tide_stations.json"),
        tmp_path / "data" / "tide_stations.json",
    ]


def test_an_explicit_path_comes_first_and_a_missing_one_falls_back(tmp_path: Path) -> None:
    own = tmp_path / "tide_stations.json"
    own.write_text("{}")
    assert data_files.find("tide_stations.json", own) == own
    assert data_files.find("tide_stations.json", tmp_path / "absent.json") == (
        DATA / "tide_stations.json"
    )


def test_nothing_found_names_every_place_tried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(data_files, "DATA_PARENTS", (tmp_path / "repo", tmp_path / "service"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError) as exc:
        data_files.find("surf_spots/spots.json")
    message = str(exc.value)
    assert message.startswith("spots.json not found in any of:")
    for place in ("repo", "service"):
        assert str(tmp_path / place / "data" / "surf_spots" / "spots.json") in message


@pytest.mark.parametrize("package_dir", ["", "_vendor"])
def test_the_function_bundle_layout_reads_the_copied_data(tmp_path: Path, package_dir: str) -> None:
    """The Vercel function's files as the offline build lays them out: app/ and data/ side by
    side at the function root (/var/task), no repository around them, and a working directory
    without data/. The build also installs the project into _vendor/ (its _vendor/app), which
    the runtime puts after the function root on sys.path; whichever copy of app is imported,
    every reader resolves into the bundle's data/."""
    function = tmp_path / "task"
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ROOT / "backend" / "app", function / "app", ignore=ignore)
    if package_dir:
        shutil.copytree(ROOT / "backend" / "app", function / package_dir / "app", ignore=ignore)
    for relative in READ_FILES:
        (function / "data" / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(DATA / relative, function / "data" / relative)
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    probe = (
        "import json\n"
        "from app.core import data_files\n"
        "from app.services import seed\n"
        "from app.services.observations import stations\n"
        "from app.services.tides import catalog, harmonic_model\n"
        "print(json.dumps({\n"
        "  'service_root': str(data_files.SERVICE_ROOT),\n"
        "  'spots': str(seed.resolve_data_dir()),\n"
        "  'tides': str(catalog.resolve_catalog_path()),\n"
        "  'obs': str(stations.resolve_catalog_path()),\n"
        "  'constants': str(harmonic_model.resolve_constants_path()),\n"
        "  'points': len(harmonic_model.load_constants()),\n"
        "}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "TIDE_CONSTANTS_PATH"}
    # Import app from the function root, or from _vendor/app (the vendored copy first).
    env["PYTHONPATH"] = str(function / package_dir)
    out = subprocess.run(  # noqa: S603 - fixed argument list
        [sys.executable, "-c", probe],
        cwd=elsewhere,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found = json.loads(out.strip().splitlines()[-1])
    bundled = function / "data"
    assert found["service_root"] == str(function / package_dir)
    assert found["spots"] == str(bundled / "surf_spots")
    assert found["tides"] == str(bundled / "tide_stations.json")
    assert found["obs"] == str(bundled / "observation_stations.json")
    assert found["constants"] == str(bundled / "tides" / "constants_eot20.json")
    assert found["points"] > 0
