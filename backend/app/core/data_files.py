"""Where the reference data (the repository's ``data/``) are read from.

Every reader of data/ finds its file here: the seed (data/surf_spots/), the research evidence
and profile decisions, the tide catalog and the tide model's constants (data/tide_stations.json,
data/tides/) and the observation station catalog. git holds one copy, the repository's data/;
each place below receives that copy unchanged:

* the repository's data/: development, the tests and the scripts (``backend/../data``);
* the service root's data/ (``backend/data``): the Vercel API function, whose root is
  backend/. The api service's build command (vercel.json ``services.api.buildCommand``) copies
  the repository's data/ there before it migrates and seeds, and the Python builder bundles the
  service root after the build command, so the function has the files at ``/var/task/data``.
  That copy is build output: .gitignore and .vercelignore keep it out of git and uploads. The
  local Vercel-style stack's api image (docker/api-lean/Dockerfile) puts data/ at the same
  place, ``/app/data``;
* ``/data``: the classic images and the sync daemon's (backend/Dockerfile, Containerfile) copy
  data/ there; their service root is /app, so the repository-root candidate is /data as well;
* data/ in the working directory.

Only pathlib: app.services.tides.harmonic_model, which scripts/extract_tide_constants.py imports
without the app's settings, uses it.
"""

from __future__ import annotations

from pathlib import Path

# backend/ in a checkout, /var/task in the Vercel function, /app in the Docker images.
SERVICE_ROOT = Path(__file__).resolve().parents[2]
# The repository root in a checkout, where git keeps data/ (/ in the Docker images).
REPO_ROOT = SERVICE_ROOT.parent
# The directories whose data/ is searched, in this order; the working directory comes last.
DATA_PARENTS: tuple[Path, ...] = (REPO_ROOT, SERVICE_ROOT, Path("/"))


def candidates(relative: str | Path) -> list[Path]:
    """Where ``relative``, a path inside data/ (e.g. "tides/constants_eot20.json"), is looked
    for, in order and without duplicates."""
    found: list[Path] = []
    for parent in (*DATA_PARENTS, Path.cwd()):
        path = parent / "data" / relative
        if path not in found:
            found.append(path)
    return found


def find(relative: str | Path, explicit: str | Path | None = None) -> Path:
    """``explicit`` when it exists, else the first existing path of :func:`candidates`.
    Raises FileNotFoundError naming every path tried."""
    tried = ([Path(explicit)] if explicit else []) + candidates(relative)
    for path in tried:
        if path.exists():
            return path
    raise FileNotFoundError(f"{Path(relative).name} not found in any of: {[str(p) for p in tried]}")
