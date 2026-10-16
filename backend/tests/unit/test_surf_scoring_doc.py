"""docs/SURF_SCORING.md states the scoring constants the code uses (algorithm 2.1.0)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.seed import DEFAULT_DATA_DIR
from app.services.surf_quality import scoring, wind

DOC = Path(__file__).resolve().parents[3] / "docs" / "SURF_SCORING.md"


def test_documented_defaults_equal_the_code() -> None:
    """docs/SURF_SCORING.md section 3b lists every WindParams default with its provenance."""
    text = DOC.read_text(encoding="utf-8")
    rows = {
        m.group(1): (float(m.group(2)), m.group(3))
        for m in re.finditer(
            r"^\| `(\w+)` \| ([\d.]+) \| (research|beaufort|heuristic) \|", text, re.M
        )
    }
    for name, value, provenance in wind.documented_defaults():
        assert rows[name] == (value, provenance), name
    assert rows["glassy_max_kmh"] == (wind.GLASSY_MAX_KMH, "beaufort")
    assert "| strength bounds | 5.0 / 12.0 / 29.0 | beaufort |" in text
    assert "| favourability bounds | 0.75 / 0.4 | heuristic |" in text
    assert "| relation sector bounds | 45 / 75 / 105 / 135° | heuristic |" in text
    assert (wind.OFFSHORE_MAX_DEG, wind.CROSS_OFFSHORE_MAX_DEG) == (45.0, 75.0)
    assert (wind.CROSS_SHORE_MAX_DEG, wind.CROSS_ONSHORE_MAX_DEG) == (105.0, 135.0)
    assert (wind.LIGHT_BELOW_KMH, wind.MODERATE_BELOW_KMH) == (12.0, 29.0)
    assert (wind.FAVOURABLE_MIN_Q, wind.WORKABLE_MIN_Q) == (0.75, 0.4)
    assert wind.SHELTER_TAPER_DEG == 15.0
    assert "| wind gate start | 0.75 | heuristic |" in text
    assert scoring.WIND_GATE_FULL_Q == 0.75
    assert 'ALGORITHM_VERSION = "2.1.0"' in text


def test_documented_shelter_strength_is_the_committed_one() -> None:
    text = DOC.read_text(encoding="utf-8")
    m = re.search(r"^\| shelter factor \| ([\d.]+) \| heuristic \|", text, re.M)
    assert m is not None
    profiles = json.loads((DEFAULT_DATA_DIR / "physical_profiles.json").read_text("utf-8"))
    factors = {
        s["factor"]
        for entry in profiles.values()
        for s in (entry.get("wind_profile") or {}).get("shelter", [])
    }
    assert factors == {float(m.group(1))}
