#!/usr/bin/env python
"""Generate the per-spot research documents in docs/surf_spot_research/ from the data files.

Writes ``docs/surf_spot_research/<slug>.md`` for every spot in data/surf_spots/spots.json and
``docs/surf_spot_research/README.md`` (an index), from:

* ``data/surf_spots/spots.json``: names, regions and the app's model settings;
* ``data/surf_spots/research_evidence.json``: the sources and their verbatim excerpts;
* ``data/surf_spots/profile_decisions.json``: changed / kept / disputed profile values;
* ``data/surf_spots/research_conditions.json``: the spec-17 ideal-condition fields, each
  citing the evidence claims it rests on (or "not_documented" / "conflicting");
* ``data/surf_spots/physical_profiles.json``: researched wind shelter and tolerance, measured
  shore normals (shown as model settings);
* ``data/tide_stations.json`` and ``data/tides/constants_eot20.json``: the app's tide source
  per spot (the ``tide.local_tide_data`` field must equal what they give).

Research confidence is the class the app computes (``app.services.research.assess``,
methodology research-v1.2), never a new score; forecast confidence is per forecast hour and
is not part of these files. The output is deterministic (no generation timestamps).

Before writing anything the mechanical citation check runs (:func:`check_conditions`): every
citation's source key exists for the spot, its claim index exists and its excerpt equals
that claim's excerpt exactly; documented and conflicting fields cite at least one claim and
not-documented fields none; each conflicting value names cited sources. Any problem stops the
script.

From the repository root:

  backend/.venv/bin/python scripts/generate_spot_research_docs.py           # (re)write docs
  backend/.venv/bin/python scripts/generate_spot_research_docs.py --check   # exit 1 if stale
  backend/.venv/bin/python scripts/generate_spot_research_docs.py --out DIR # write elsewhere
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.models import SurfSpot
from app.services.geo import window_center
from app.services.research.assess import (
    METHODOLOGY_VERSION,
    VARIABLE_LABELS,
    ComputedAssessment,
    compute_assessment,
    publisher_groups,
)
from app.services.research.decisions import record_for
from app.services.research.evidence import EvidenceSource, parse_evidence
from app.services.research.values import profile_values
from app.services.seed import SPOT_FIELDS
from app.services.surf_quality.models import SpotParams
from app.services.surf_quality.scoring import tide_excluded_reason
from app.services.surf_quality.wind import (
    CROSS_OFFSHORE_MAX_DEG,
    CROSS_ONSHORE_MAX_DEG,
    OFFSHORE_MAX_DEG,
    WindParams,
)

REPO = Path(__file__).resolve().parents[1]
# slug -> profile decision records (profile_decisions.json), as decisions.record_for reads them.
Decisions = dict[str, list[dict[str, Any]]]
DATA_DIR = REPO / "data"
SPOTS_DIR = DATA_DIR / "surf_spots"
DOCS_DIR = REPO / "docs" / "surf_spot_research"
CONDITIONS_FILENAME = "research_conditions.json"
SCRIPT = "scripts/generate_spot_research_docs.py"

STATUSES = ("documented", "conflicting", "not_documented")
STATUS_LABELS = {
    "documented": "documented",
    "conflicting": "conflicting",
    "not_documented": "not documented",
}
# The spec-17 fields in the order of the owner's spec.
FIELDS: dict[str, tuple[str, ...]] = {
    "swell_height": ("min_suitable", "ideal_range", "max_suitable"),
    "swell_period": ("ideal", "acceptable", "short_period_behaviour", "long_period_behaviour"),
    "swell_direction": ("ideal_bearing", "acceptable_range", "blocked_directions", "exposure"),
    "wind": (
        "offshore",
        "cross_offshore",
        "onshore",
        "ideal_speed",
        "strong_wind_limit",
        "sheltering",
    ),
    "tide": ("preferred_stage", "rising_or_falling", "size_dependent", "local_tide_data"),
    "seasonality": ("prime_months", "secondary_season", "swell_sources", "wind_patterns"),
    "geography": ("break_type", "orientation", "exposure", "bathymetry"),
}
GROUP_LABELS = {
    "swell_height": "Swell height",
    "swell_period": "Swell period",
    "swell_direction": "Swell direction",
    "wind": "Wind",
    "tide": "Tide",
    "seasonality": "Seasonality",
    "geography": "Geography",
}
FIELD_LABELS = {
    ("swell_height", "min_suitable"): "Minimum suitable",
    ("swell_height", "ideal_range"): "Ideal range",
    ("swell_height", "max_suitable"): "Maximum suitable",
    ("swell_period", "ideal"): "Ideal",
    ("swell_period", "acceptable"): "Acceptable",
    ("swell_period", "short_period_behaviour"): "Short-period behaviour",
    ("swell_period", "long_period_behaviour"): "Long-period behaviour",
    ("swell_direction", "ideal_bearing"): "Ideal bearing",
    ("swell_direction", "acceptable_range"): "Acceptable range",
    ("swell_direction", "blocked_directions"): "Blocked directions",
    ("swell_direction", "exposure"): "Exposure",
    ("wind", "offshore"): "Offshore",
    ("wind", "cross_offshore"): "Cross-offshore",
    ("wind", "onshore"): "Onshore",
    ("wind", "ideal_speed"): "Ideal speed",
    ("wind", "strong_wind_limit"): "Strong-wind limit",
    ("wind", "sheltering"): "Sheltering",
    ("tide", "preferred_stage"): "Preferred stage",
    ("tide", "rising_or_falling"): "Rising or falling",
    ("tide", "size_dependent"): "Size-dependent preference",
    ("tide", "local_tide_data"): "Local tide data",
    ("seasonality", "prime_months"): "Prime months",
    ("seasonality", "secondary_season"): "Secondary season",
    ("seasonality", "swell_sources"): "Swell sources",
    ("seasonality", "wind_patterns"): "Wind patterns",
    ("geography", "break_type"): "Break type",
    ("geography", "orientation"): "Orientation",
    ("geography", "exposure"): "Exposure",
    ("geography", "bathymetry"): "Bathymetry",
}
# App data, not research evidence: checked against the tide catalog instead of citations.
APP_DATA_FIELD = ("tide", "local_tide_data")
RESEARCH_FIELD_COUNT = sum(len(v) for v in FIELDS.values()) - 1
FIELD_KEYS = frozenset({"value", "unit", "status", "citations", "note"})
CITATION_KEYS = frozenset({"source_key", "claim_index", "excerpt"})
CONFLICT_ENTRY_KEYS = frozenset({"value", "sources", "label", "scale"})
SPOT_KEYS = frozenset({"extractor", "fields", "judgement_check"})
FILE_KEYS = frozenset({"version", "generated_from", "method", "extracted_on", "spots"})

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
COMPASS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)
RETRIEVAL_LABELS = {"fetched": "read directly", "web_search": "search summary only"}
TIDE_PROVIDER_LABELS = {"noaa_coops": "NOAA CO-OPS station", "harmonic_model": "EOT20 model cell"}
# Profile fields compared by the research-confidence assessment, with the decision-record
# field name of profile_decisions.json.
DECISION_FIELDS = {
    "optimal_swell_direction": "swell_direction",
    "wave_height_ft": "swell_size",
    "offshore_wind_direction": "wind",
    "tide_preference": "tide",
    "best_months": "seasonality",
}
FORECAST_CONFIDENCE_SENTENCE = (
    "Forecast confidence is a separate class computed for every forecast hour (lead time, "
    "ensemble spread, model agreement and data freshness; see "
    "[forecast-confidence.md](../forecast-confidence.md)); it is not part of this file and is "
    "never combined with research confidence."
)


# --------------------------------------------------------------------------- inputs
def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_inputs(data_dir: Path = DATA_DIR) -> dict[str, Any]:
    spots_dir = data_dir / "surf_spots"
    return {
        "spots": _load_json(spots_dir / "spots.json"),
        "evidence": _load_json(spots_dir / "research_evidence.json"),
        "decisions": _load_json(spots_dir / "profile_decisions.json"),
        "conditions": _load_json(spots_dir / CONDITIONS_FILENAME),
        "physical": _load_json(spots_dir / "physical_profiles.json"),
        "tide_catalog": _load_json(data_dir / "tide_stations.json"),
        "tide_constants": _load_json(data_dir / "tides" / "constants_eot20.json"),
    }


def expected_local_tide_data(
    slug: str, tide_catalog: Mapping[str, Any], tide_constants: Mapping[str, Any]
) -> dict[str, Any]:
    """The ``tide.local_tide_data`` value the tide catalog gives for one spot."""
    stations = {(s["provider"], s["code"]): s for s in tide_catalog["stations"]}
    links: list[dict[str, Any]] = []
    for link in tide_catalog["spots"].get(slug, {}).get("stations", []):
        item: dict[str, Any] = {
            "provider": link["provider"],
            "code": link["code"],
            "relation": link["relation"],
            "distance_km": link["distance_km"],
        }
        station = stations.get((link["provider"], link["code"]))
        if station is not None:
            item["name"] = station["name"]
            item["datum"] = station["datum"]
        elif link["provider"] == "harmonic_model":
            point = tide_constants["points"].get(slug, {})
            if not point.get("available"):
                continue
            item["name"] = f"{tide_constants['model']} model cell"
            item["datum"] = tide_constants["datum"]
        if link.get("caveat"):
            item["caveat"] = link["caveat"]
        links.append(item)
    return {"available": bool(links), "links": links}


# --------------------------------------------------------------------------- check
def check_conditions(inputs: Mapping[str, Any]) -> list[str]:
    """Every problem of research_conditions.json (an empty list when it passes).

    Citations: the source key exists for the spot, the claim index exists, and the excerpt
    equals that claim's excerpt exactly. Documented and conflicting fields cite at least one
    claim, not-documented fields none and have a null value; every entry of a conflicting
    value names sources the field cites. ``tide.local_tide_data`` is app data: no
    citations, and its value must be what data/tide_stations.json gives.
    """
    problems: list[str] = []
    conditions = inputs["conditions"]
    evidence = inputs["evidence"]["spots"]
    slugs = {s["slug"] for s in inputs["spots"]}
    if not isinstance(conditions, dict) or set(conditions) != FILE_KEYS:
        return [f"research conditions: top-level keys must be {sorted(FILE_KEYS)}"]
    if conditions["version"] != 1:
        problems.append("research conditions: version must be 1")
    if conditions["generated_from"] != "research_evidence.json":
        problems.append("research conditions: generated_from must be research_evidence.json")
    spots = conditions["spots"]
    if set(spots) != slugs:
        missing = sorted(slugs - set(spots))
        extra = sorted(set(spots) - slugs)
        problems.append(f"research conditions: spots differ from spots.json ({missing=} {extra=})")
    for slug in sorted(set(spots) & slugs):
        entry = spots[slug]
        if set(entry) != SPOT_KEYS:
            problems.append(f"{slug}: keys must be {sorted(SPOT_KEYS)}")
            continue
        sources = {s["key"]: s for s in evidence.get(slug, {}).get("sources", [])}
        fields = entry["fields"]
        if set(fields) != set(FIELDS):
            problems.append(f"{slug}: groups must be {list(FIELDS)}")
            continue
        for group, names in FIELDS.items():
            if set(fields[group]) != set(names):
                problems.append(f"{slug}: {group} fields must be {list(names)}")
                continue
            for name in names:
                where = f"{slug} {group}.{name}"
                problems.extend(
                    _check_field(where, fields[group][name], sources, (group, name), slug, inputs)
                )
    return problems


def _check_field(
    where: str,
    field: Any,
    sources: Mapping[str, Any],
    key: tuple[str, str],
    slug: str,
    inputs: Mapping[str, Any],
) -> list[str]:
    if not isinstance(field, dict) or set(field) != FIELD_KEYS:
        return [f"{where}: keys must be {sorted(FIELD_KEYS)}"]
    problems: list[str] = []
    status, value, citations = field["status"], field["value"], field["citations"]
    if status not in STATUSES:
        problems.append(f"{where}: status {status!r} is not one of {list(STATUSES)}")
    note = field["note"]
    if not isinstance(note, str) or not note.strip():
        problems.append(f"{where}: note must be a non-empty sentence")
    if not isinstance(citations, list):
        return [*problems, f"{where}: citations must be a list"]
    if key == APP_DATA_FIELD:
        expected = expected_local_tide_data(slug, inputs["tide_catalog"], inputs["tide_constants"])
        if value != expected:
            problems.append(f"{where}: value differs from data/tide_stations.json ({expected})")
        if citations:
            problems.append(f"{where}: app tide data carries no research citations")
        want = "documented" if expected["available"] else "not_documented"
        if status != want:
            problems.append(f"{where}: status must be {want!r}")
        return problems
    if status in ("documented", "conflicting") and not citations:
        problems.append(f"{where}: {status} without a citation")
    if status == "not_documented":
        if citations:
            problems.append(f"{where}: not_documented but cites {len(citations)} claim(s)")
        if value is not None:
            problems.append(f"{where}: not_documented must have a null value")
    elif value is None:
        problems.append(f"{where}: {status} needs a value")
    cited: set[str] = set()
    for i, c in enumerate(citations):
        cw = f"{where} citation {i + 1}"
        if not isinstance(c, dict) or set(c) != CITATION_KEYS:
            problems.append(f"{cw}: keys must be {sorted(CITATION_KEYS)}")
            continue
        source = sources.get(c["source_key"])
        if source is None:
            problems.append(f"{cw}: source {c['source_key']!r} is not a source of this spot")
            continue
        cited.add(c["source_key"])
        index = c["claim_index"]
        claims = source["claims"]
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(claims):
            problems.append(f"{cw}: {c['source_key']} has no claim {index!r}")
            continue
        if c["excerpt"] != claims[index]["excerpt"]:
            problems.append(
                f"{cw}: excerpt differs from {c['source_key']} claim {index} "
                f"({claims[index]['excerpt']!r})"
            )
    if status == "conflicting":
        if not isinstance(value, list) or len(value) < 2:
            problems.append(f"{where}: a conflicting value lists at least two stated values")
        else:
            for j, item in enumerate(value):
                if (
                    not isinstance(item, dict)
                    or not {"value", "sources"} <= set(item) <= CONFLICT_ENTRY_KEYS
                    or not isinstance(item["sources"], list)
                    or not item["sources"]
                ):
                    problems.append(f"{where} value {j + 1}: needs value and sources")
                    continue
                uncited = sorted(set(item["sources"]) - cited)
                if uncited:
                    problems.append(f"{where} value {j + 1}: sources not cited: {uncited}")
    return problems


# --------------------------------------------------------------------------- formatting
def _esc(text: str) -> str:
    """Plain text for Markdown: no raw HTML tags."""
    return text.replace("<", "&lt;")


def _cell(text: str) -> str:
    return _esc(text).replace("|", "\\|").replace("\n", " ")


def _link_text(text: str) -> str:
    return _cell(text).replace("[", "\\[").replace("]", "\\]")


def _num(x: float) -> str:
    return f"{x:g}"


def compass(deg: float) -> str:
    return COMPASS[round((deg % 360) / 22.5) % 16]


def _unit_kind(unit: str | None) -> tuple[str | None, str]:
    """(kind of number, qualifier shown once after the value)."""
    if unit is None:
        return None, ""
    if unit.startswith("deg"):
        return "deg", ""
    if unit in ("month", "s", "kt"):
        return unit, ""
    if unit == "ft" or unit.startswith("ft "):
        return "ft", unit[2:].strip()
    if unit == "m" or unit.startswith("m "):
        return "m", unit[1:].strip()
    return None, unit


def _scalar(x: float, kind: str | None) -> str:
    if kind == "deg":
        return f"{_num(x)}° ({compass(x)})"
    if kind == "month" and float(x).is_integer() and 1 <= x <= 12:
        return MONTHS[int(x) - 1]
    if kind in ("s", "kt", "ft", "m"):
        return f"{_num(x)} {kind}"
    return _num(x)


def _is_num(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool)


def _label(key: str) -> str:
    return key.replace("_", " ")


def fmt_value(value: Any, kind: str | None) -> str:
    """A field value as short plain text (numbers with their unit, months by name)."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if _is_num(value):
        return _scalar(value, kind)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if kind == "month" and value and all(_is_num(m) for m in value):
            return ", ".join(_scalar(m, "month") for m in value)
        return "; ".join(fmt_value(v, kind) for v in value)
    if isinstance(value, dict):
        return _fmt_dict(value, kind)
    return str(value)


def _fmt_range(lo: Any, hi: Any, kind: str | None) -> str:
    if _is_num(lo) and _is_num(hi):
        if kind == "deg":
            return f"{_num(lo)}–{_num(hi)}° ({compass(lo)} to {compass(hi)}, clockwise)"
        suffix = f" {kind}" if kind in ("s", "kt", "ft", "m") else ""
        if kind == "month":
            return f"{_scalar(lo, kind)}–{_scalar(hi, kind)}"
        return f"{_num(lo)}–{_num(hi)}{suffix}"
    if _is_num(lo):
        return f"{_scalar(lo, kind)} or more"
    if _is_num(hi):
        return f"up to {_scalar(hi, kind)}"
    return "—"


def _sub_key(key: str, kind: str | None) -> tuple[str, str | None]:
    """(label, number kind) of an extra key such as ``face_ft`` or ``ft_converted``."""
    if key.endswith("_ft"):
        return _label(key[:-3]), "ft"
    if key.startswith("ft_"):
        return _label(key[3:]), "ft"
    if key.endswith("_s"):
        return _label(key[:-2]), "s"
    return _label(key), kind


def _fmt_dict(value: Mapping[str, Any], kind: str | None) -> str:
    d = dict(value)
    open_ended = d.pop("open_ended", None) is True
    more = " or more" if open_ended else ""
    parts: list[str] = []
    for lo_key, hi_key in (("min", "max"), ("from", "to"), ("min_s", "max_s")):
        if lo_key in d or hi_key in d:
            unit = "s" if lo_key == "min_s" else kind
            parts.append(_fmt_range(d.pop(lo_key, None), d.pop(hi_key, None), unit))
    if "ft" in d:
        ft = d.pop("ft")
        text = fmt_value(ft, "ft") if not isinstance(ft, str) else f"{ft} ft"
        if "m" in d:
            m = d.pop("m")
            text += f" ({fmt_value(m, 'm') if not isinstance(m, str) else m + ' m'})"
        parts.append(text + more)
        more = ""
    elif "m" in d:
        m = d.pop("m")
        parts.append((fmt_value(m, "m") if not isinstance(m, str) else f"{m} m") + more)
        more = ""
    if "s" in d:
        parts.append(fmt_value(d.pop("s"), "s"))
    if "value" in d:
        parts.append(fmt_value(d.pop("value"), kind))
    for k in sorted(d):
        label, sub_kind = _sub_key(k, kind)
        parts.append(f"{label}: {fmt_value(d[k], sub_kind)}")
    text = "; ".join(p for p in parts if p)
    return text + more


def _with_qualifier(text: str, qualifier: str) -> str:
    if not qualifier:
        return text
    if qualifier.startswith("("):
        return f"{text} {qualifier}"
    if "(" in qualifier:
        return f"{text} · {qualifier}"
    return f"{text} ({qualifier})"


def fmt_field_value(field: Mapping[str, Any], key: tuple[str, str], refs: Mapping[str, str]) -> str:
    """The research value of one field for the overview table."""
    if field["status"] == "not_documented" and key != APP_DATA_FIELD:
        return "—"
    kind, qualifier = _unit_kind(field["unit"])
    value = field["value"]
    if key == APP_DATA_FIELD:
        return fmt_tide_links(value)
    if field["status"] != "conflicting":
        return _with_qualifier(fmt_value(value, kind), qualifier)
    # One entry per distinct stated value, with every source that states it.
    merged: dict[str, list[str]] = {}
    for item in value:
        label = f"{item['label']}: " if item.get("label") else ""
        scale = f" ({item['scale']})" if item.get("scale") else ""
        text = f"{label}{fmt_value(item['value'], kind)}{scale}"
        merged.setdefault(text, [])
        merged[text] += [s for s in item["sources"] if s not in merged[text]]
    items = [f"{text} [{', '.join(refs[s] for s in srcs)}]" for text, srcs in merged.items()]
    return _with_qualifier(" · ".join(items), qualifier)


def fmt_tide_links(value: Mapping[str, Any]) -> str:
    if not value.get("available"):
        return "no tide source"
    out = []
    for link in value["links"]:
        provider = TIDE_PROVIDER_LABELS.get(link["provider"], link["provider"])
        datum = "model mean sea level" if link.get("datum") == "model_msl" else link.get("datum")
        name = f" {link['name']}" if link["provider"] == "noaa_coops" else ""
        out.append(
            f"{provider} {link['code']}{name} ({datum}), {_num(link['distance_km'])} km, "
            f"{link['relation']}"
        )
    return "; ".join(out)


# --------------------------------------------------------------------------- model settings
def _deg(x: float) -> str:
    return f"{_num(x)}° ({compass(x)})"


def _arc(lo: float, hi: float) -> str:
    return f"{_num(lo)}–{_num(hi)}° ({compass(lo)} to {compass(hi)}, clockwise)"


def _months(months: Sequence[int]) -> str:
    return ", ".join(MONTHS[m - 1] for m in months) if months else "none"


def _arc_sum(deg: float, offset: float) -> float:
    return (deg + offset) % 360


def model_settings(
    spot: Mapping[str, Any],
    physical: Mapping[str, Any],
    tide_reason: str | None,
) -> dict[tuple[str, str], str]:
    """The app's current model setting next to each spec-17 field (plain text)."""
    off = float(spot["offshore_wind_direction"])
    left_far, left_near = _arc_sum(off, -CROSS_OFFSHORE_MAX_DEG), _arc_sum(off, -OFFSHORE_MAX_DEG)
    right_near, right_far = _arc_sum(off, OFFSHORE_MAX_DEG), _arc_sum(off, CROSS_OFFSHORE_MAX_DEG)
    wind_profile = physical.get("wind_profile") or {}
    shelter = wind_profile.get("shelter") or []
    tolerance = wind_profile.get("offshore_tolerance_kmh")
    default_tolerance = WindParams().offshore_tolerance_kmh
    lo_ft, hi_ft = spot["wave_height_min_ft"], spot["wave_height_max_ft"]
    if shelter:
        shelter_text = "; ".join(
            f"shelter from {_arc(s['from_min'], s['from_max'])}, factor {_num(s['factor'])}"
            for s in shelter
        )
        shelter_text += " (physical_profiles.json wind_profile)"
    else:
        shelter_text = "none"
    if tolerance is not None:
        prov = wind_profile.get("tolerance_provenance", "research")
        strong = (
            f"offshore tolerance {_num(tolerance)} km/h ({prov}; default {_num(default_tolerance)})"
        )
    else:
        strong = f"offshore tolerance {_num(default_tolerance)} km/h (heuristic default, all spots)"
    tide = spot.get("tide_preference")
    tide_text = f"`tide_preference` {tide}" if tide else "`tide_preference` not set"
    tide_text += ": used in the score" if tide_reason is None else f": not scored ({tide_reason})"
    normal = physical.get("shore_normal_deg")
    quality = (physical.get("data_quality") or {}).get("shore_normal_deg")
    if normal is not None:
        orientation = f"shore normal {_deg(normal)} ({quality}, physical_profiles.json)"
    else:
        centre = window_center(
            spot["optimal_swell_direction_min"], spot["optimal_swell_direction_max"]
        )
        orientation = (
            "nearshore-model shore normal estimated as the centre of the optimal swell "
            f"direction ({_deg(round(centre, 1))})"
        )
    return {
        ("swell_height", "min_suitable"): f"`wave_height_min_ft` {_num(lo_ft)} ft (breaking face)",
        ("swell_height", "ideal_range"): (
            f"no separate setting; working range {_num(lo_ft)}–{_num(hi_ft)} ft"
        ),
        ("swell_height", "max_suitable"): f"`wave_height_max_ft` {_num(hi_ft)} ft (breaking face)",
        ("swell_period", "ideal"): f"`ideal_swell_period_s` {_num(spot['ideal_swell_period_s'])} s",
        ("swell_period", "acceptable"): (
            f"`min_swell_period_s` {_num(spot['min_swell_period_s'])} s (minimum)"
        ),
        ("swell_period", "short_period_behaviour"): (
            "no spot-specific setting (periods below `min_swell_period_s` score lower)"
        ),
        ("swell_period", "long_period_behaviour"): "no spot-specific setting",
        ("swell_direction", "ideal_bearing"): (
            "`optimal_swell_direction` "
            + _arc(spot["optimal_swell_direction_min"], spot["optimal_swell_direction_max"])
        ),
        ("swell_direction", "acceptable_range"): (
            "`swell_window` " + _arc(spot["swell_window_min"], spot["swell_window_max"])
        ),
        ("swell_direction", "blocked_directions"): "no separate setting (outside `swell_window`)",
        ("swell_direction", "exposure"): "no setting",
        ("wind", "offshore"): f"`offshore_wind_direction` {_deg(off)}",
        ("wind", "cross_offshore"): (
            f"derived: {_num(OFFSHORE_MAX_DEG)}–{_num(CROSS_OFFSHORE_MAX_DEG)}° either side of "
            f"offshore ({_arc(left_far, left_near)} and {_arc(right_near, right_far)})"
        ),
        ("wind", "onshore"): (
            f"derived: more than {_num(CROSS_ONSHORE_MAX_DEG)}° from offshore (centred on "
            f"{_deg(_arc_sum(off, 180))})"
        ),
        ("wind", "ideal_speed"): "no spot-specific setting (one wind fit for every spot)",
        ("wind", "strong_wind_limit"): strong,
        ("wind", "sheltering"): shelter_text,
        ("tide", "preferred_stage"): tide_text,
        ("tide", "rising_or_falling"): "no setting",
        ("tide", "size_dependent"): "no setting",
        ("tide", "local_tide_data"): "the app's tide source (data/tide_stations.json)",
        ("seasonality", "prime_months"): f"`best_months` {_months(spot['best_months'])}",
        ("seasonality", "secondary_season"): "no setting",
        ("seasonality", "swell_sources"): "no setting",
        ("seasonality", "wind_patterns"): "no setting",
        ("geography", "break_type"): (
            f"`break_type` {spot['break_type']}, `wave_direction` {spot['wave_direction']}"
        ),
        ("geography", "orientation"): orientation,
        ("geography", "exposure"): "no setting",
        ("geography", "bathymetry"): (
            "measured BlueTopo values in physical_profiles.json"
            if physical.get("provenance")
            else "no measured bathymetry (profile derived from the swell windows)"
        ),
    }


# --------------------------------------------------------------------------- assessment
def _spot_row(spot: Mapping[str, Any]) -> SurfSpot:
    row = SurfSpot(slug=spot["slug"])
    for f in SPOT_FIELDS:
        setattr(row, f, spot[f])
    return row


def assessments(inputs: Mapping[str, Any], now: datetime) -> dict[str, ComputedAssessment]:
    """The app's research-confidence assessment of every spot (assess.compute_assessment)."""
    spots = inputs["spots"]
    parsed = parse_evidence(inputs["evidence"], {s["slug"] for s in spots}, now=now)
    out: dict[str, ComputedAssessment] = {}
    for spot in spots:
        sources: Sequence[EvidenceSource] = (
            parsed.spots[spot["slug"]].sources if spot["slug"] in parsed.spots else ()
        )
        out[spot["slug"]] = compute_assessment(sources, profile_values(_spot_row(spot)))
    return out


def _tide_reason(
    spot: Mapping[str, Any], assessment: ComputedAssessment, decisions: Decisions
) -> str | None:
    record = record_for(decisions, spot["slug"], "tide_preference")
    params = SpotParams(
        slug=spot["slug"],
        latitude=spot["latitude"],
        longitude=spot["longitude"],
        swell_window_min=spot["swell_window_min"],
        swell_window_max=spot["swell_window_max"],
        optimal_swell_direction_min=spot["optimal_swell_direction_min"],
        optimal_swell_direction_max=spot["optimal_swell_direction_max"],
        offshore_wind_direction=spot["offshore_wind_direction"],
        min_swell_period_s=spot["min_swell_period_s"],
        ideal_swell_period_s=spot["ideal_swell_period_s"],
        wave_height_min_ft=spot["wave_height_min_ft"],
        wave_height_max_ft=spot["wave_height_max_ft"],
        tide_preference=spot.get("tide_preference"),
        tide_research_class=assessment.per_variable["tide"]["class"],
        tide_decision=record["decision"] if record else None,
    )
    return tide_excluded_reason(params)


# --------------------------------------------------------------------------- rendering
def _date(value: str) -> str:
    return value[:10]


def _changes(n: int) -> str:
    return "no change" if n == 0 else "1 change" if n == 1 else f"{n} changes"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _counts(fields: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(STATUSES, 0)
    for group, names in FIELDS.items():
        for name in names:
            if (group, name) != APP_DATA_FIELD:
                counts[fields[group][name]["status"]] += 1
    return counts


def _last_review(sources: Sequence[Mapping[str, Any]]) -> str:
    return max((_date(s["accessed_at"]) for s in sources), default="none")


# (model setting label, app value, research fields, assessment variable, decision field)
def _setting_rows(
    spot: Mapping[str, Any],
) -> list[tuple[str, str, tuple[tuple[str, str], ...], str | None, str | None]]:
    return [
        (
            "Swell window",
            _arc(spot["swell_window_min"], spot["swell_window_max"]),
            (("swell_direction", "acceptable_range"),),
            None,
            None,
        ),
        (
            "Optimal swell direction",
            _arc(spot["optimal_swell_direction_min"], spot["optimal_swell_direction_max"]),
            (("swell_direction", "ideal_bearing"),),
            "swell_direction",
            "optimal_swell_direction",
        ),
        (
            "Working size range",
            f"{_num(spot['wave_height_min_ft'])}–{_num(spot['wave_height_max_ft'])} ft "
            "(breaking face)",
            (("swell_height", "min_suitable"), ("swell_height", "max_suitable")),
            "swell_size",
            "wave_height_ft",
        ),
        (
            "Minimum swell period",
            f"{_num(spot['min_swell_period_s'])} s",
            (("swell_period", "acceptable"),),
            "swell_period",
            None,
        ),
        (
            "Ideal swell period",
            f"{_num(spot['ideal_swell_period_s'])} s",
            (("swell_period", "ideal"),),
            "swell_period",
            None,
        ),
        (
            "Offshore wind direction",
            _deg(spot["offshore_wind_direction"]),
            (("wind", "offshore"),),
            "wind",
            "offshore_wind_direction",
        ),
        (
            "Tide preference",
            spot.get("tide_preference") or "not set",
            (("tide", "preferred_stage"),),
            "tide",
            "tide_preference",
        ),
        (
            "Best months",
            _months(spot["best_months"]),
            (("seasonality", "prime_months"),),
            "seasonality",
            "best_months",
        ),
        (
            "Break type",
            f"{spot['break_type']}, {spot['wave_direction']}",
            (("geography", "break_type"),),
            None,
            None,
        ),
    ]


_CONFLICT_RULES = ("conflict:same_tier", "conflict:stronger", "conflict:unsupported")
_NUMERIC_SETTINGS = frozenset(
    {
        "Swell window",
        "Optimal swell direction",
        "Working size range",
        "Minimum swell period",
        "Ideal swell period",
        "Offshore wind direction",
    }
)


def fmt_decision_value(field_name: str, value: Any) -> str:
    """A profile-decision value (old, new or alternative) in the units of its field."""
    if field_name == "offshore_wind_direction" and _is_num(value):
        return _deg(value)
    if field_name == "optimal_swell_direction" and isinstance(value, dict):
        if value.get("min") == value.get("max"):
            return _deg(value["min"])
        return _arc(value["min"], value["max"])
    if field_name == "wave_height_ft" and isinstance(value, dict):
        return _fmt_range(value.get("min"), value.get("max"), "ft")
    if field_name == "best_months" and isinstance(value, list):
        return _months(value)
    return fmt_value(value, None)


def unsupported_settings(
    spot: Mapping[str, Any],
    fields: Mapping[str, Mapping[str, Any]],
    assessment: ComputedAssessment,
    decisions: Decisions,
) -> list[tuple[str, str, list[str]]]:
    """(setting, app value, reasons) for every model setting the evidence does not support.

    A setting is listed when a research field it corresponds to is not documented or
    conflicting, is stated only in words for a numeric setting, or when the app's
    research-confidence assessment of its variable finds no supporting source or an
    unresolved conflict; the profile decision record is added when there is one.
    """
    out: list[tuple[str, str, list[str]]] = []
    for label, value, keys, variable, decision_field in _setting_rows(spot):
        reasons: list[str] = []
        for key in keys:
            f = fields[key[0]][key[1]]
            name = f"{GROUP_LABELS[key[0]].lower()} · {FIELD_LABELS[key].lower()}"
            if f["status"] == "not_documented":
                reasons.append(f"no excerpt states the {name}")
            elif f["status"] == "conflicting":
                reasons.append(f"sources disagree on the {name}")
            elif (
                label in _NUMERIC_SETTINGS
                and isinstance(f["value"], str)
                and not any(ch.isdigit() for ch in f["value"])
            ):
                reasons.append(f"the {name} is described only in words ({f['value']})")
        if variable is not None:
            entry = assessment.per_variable[variable]
            rules = set(entry["rules"])
            if entry["independent_supporting"] == 0 or rules & set(_CONFLICT_RULES):
                gap = entry["gaps"][0] if entry["gaps"] else entry["summary"]
                reasons.append(
                    f"research confidence {entry['class'].capitalize()} for "
                    f"{VARIABLE_LABELS[variable].lower()}: {gap[:1].lower()}{gap[1:].rstrip('.')}"
                )
        if decision_field is not None:
            rec = record_for(decisions, spot["slug"], decision_field)
            if rec is not None and reasons:
                alt = rec.get("alternative")
                alt_text = (
                    f", alternative {fmt_decision_value(decision_field, alt)}"
                    if alt is not None
                    else ""
                )
                reasons.append(f"profile decision {rec['decided_at']}: {rec['decision']}{alt_text}")
        if reasons:
            out.append((label, value, reasons))
    return out


def render_spot(
    spot: Mapping[str, Any],
    evidence: Mapping[str, Any],
    entry: Mapping[str, Any],
    assessment: ComputedAssessment,
    decisions: Decisions,
    physical: Mapping[str, Any],
    extracted_on: str,
) -> str:
    slug = spot["slug"]
    sources = evidence.get("sources", [])
    refs = {s["key"]: f"S{i + 1}" for i, s in enumerate(sources)}
    by_key = {s["key"]: s for s in sources}
    fields = entry["fields"]
    counts = _counts(fields)
    groups = publisher_groups([(s["key"], s["publisher"], s.get("url")) for s in sources])
    direct = sum(1 for s in sources if s.get("retrieval", "fetched") == "fetched")
    settings = model_settings(spot, physical, _tide_reason(spot, assessment, decisions))
    tide_value = fields["tide"]["local_tide_data"]["value"]
    check = entry["judgement_check"]
    lines: list[str] = [
        f"# {_esc(spot['name'])}",
        "",
        f"<!-- Generated by {SCRIPT} from data/surf_spots/spots.json, research_evidence.json, "
        "profile_decisions.json and research_conditions.json. Do not edit by hand: change the "
        "data and run the script. -->",
        "",
        f"{_esc(spot['region'])}, {_esc(spot['country'])} ({_esc(spot['region_group'])}) · "
        f"{spot['break_type']} break, {spot['wave_direction']} · slug `{slug}` · "
        "[all spots](README.md)",
        "",
        "| | |",
        "|---|---|",
        f"| Research confidence | **{assessment.overall_class.capitalize()}** "
        f"(methodology {assessment.methodology_version}) |",
        f"| Forecast confidence | {FORECAST_CONFIDENCE_SENTENCE} |",
        f"| Researched fields ({RESEARCH_FIELD_COUNT}) | {counts['documented']} documented · "
        f"{counts['conflicting']} conflicting · {counts['not_documented']} not documented |",
        f"| Local tide data | {_cell(fmt_tide_links(tide_value))} |",
        f"| Sources | {len(sources)} from {len(set(groups.values()))} independent publishers "
        f"({direct} read directly, {len(sources) - direct} search summary only) |",
        f"| Last review | {_last_review(sources)} (newest source access) |",
        f"| Extraction | {extracted_on} (extractor {entry['extractor']}"
        + (
            f"; judgement check {check['checked_on']}, {_changes(len(check['changes']))})"
            if check
            else ")"
        )
        + " |",
        "",
        "## Summary",
        "",
        f"Research confidence is **{assessment.overall_class.capitalize()}**. "
        f"{_esc(assessment.summary)} It is the class the app computes from the evidence "
        "(how well the break's ideal conditions are documented), not a forecast and not a "
        "probability.",
        "",
    ]
    if assessment.gaps:
        lines += ["Main gaps:", ""]
        lines += [f"- {_esc(g)}" for g in assessment.gaps]
        lines.append("")
    lines += [
        "| Variable | Research confidence | Evidence |",
        "|---|---|---|",
    ]
    for variable, e in assessment.per_variable.items():
        lines.append(
            f"| {VARIABLE_LABELS[variable]} | {e['class'].capitalize()} | {_cell(e['summary'])} |"
        )
    lines.append("")
    if evidence.get("notes"):
        lines += [
            "Research notes (research_evidence.json):",
            "",
            f"> {_esc(evidence['notes'])}",
            "",
        ]
    lines += [
        "## Ideal conditions",
        "",
        "Directions are compass degrees the swell or wind comes FROM, ranges clockwise. "
        "Heights keep the scale each source states (breaking face, Hawaiian, swell or "
        "relative to the surfer); the app's sizes are breaking face heights in feet. "
        "Sources are numbered as in [Sources](#sources).",
        "",
    ]
    for group, names in FIELDS.items():
        lines += [
            f"### {GROUP_LABELS[group]}",
            "",
            "| Field | Research value | Status | Sources | App model setting |",
            "|---|---|---|---|---|",
        ]
        for name in names:
            f = fields[group][name]
            key = (group, name)
            cited = sorted(
                {refs[c["source_key"]] for c in f["citations"]}, key=lambda r: int(r[1:])
            )
            lines.append(
                f"| {FIELD_LABELS[key]} | {_cell(fmt_field_value(f, key, refs))} | "
                f"{STATUS_LABELS[f['status']]} | {', '.join(cited) or '—'} | "
                f"{_cell(settings[key])} |"
            )
        lines += ["", "Notes:", ""]
        for name in names:
            f = fields[group][name]
            lines.append(f"- **{FIELD_LABELS[(group, name)]}**: {_esc(f['note'])}")
            for c in f["citations"]:
                src = by_key[c["source_key"]]
                lines.append(
                    f"  - {refs[c['source_key']]} ({_esc(src['publisher'])}): "
                    f"“{_esc(c['excerpt'])}”"
                )
        lines.append("")
    conflicting = [
        (g, n) for g, ns in FIELDS.items() for n in ns if fields[g][n]["status"] == "conflicting"
    ]
    lines += ["## Conflicting information", ""]
    if conflicting:
        for g, n in conflicting:
            lines.append(
                f"- **{GROUP_LABELS[g]} · {FIELD_LABELS[(g, n)].lower()}**: "
                f"{_esc(fields[g][n]['note'])}"
            )
    else:
        lines.append("No field has disagreeing sources.")
    lines.append("")
    missing = [
        (g, n)
        for g, ns in FIELDS.items()
        for n in ns
        if fields[g][n]["status"] == "not_documented" and (g, n) != APP_DATA_FIELD
    ]
    lines += ["## Missing information", ""]
    if missing:
        lines.append("No excerpt in the evidence supports a value for these fields:")
        lines.append("")
        for g, n in missing:
            lines.append(
                f"- **{GROUP_LABELS[g]} · {FIELD_LABELS[(g, n)].lower()}**: "
                f"{_esc(fields[g][n]['note'])}"
            )
    else:
        lines.append("Every researched field has at least one supporting excerpt.")
    lines.append("")
    unsupported = unsupported_settings(spot, fields, assessment, decisions)
    lines += [
        "## Model settings the evidence does not support",
        "",
        "A setting is listed when its research field is not documented, conflicting or (for a "
        "number) described only in words, or when the app's research-confidence check finds "
        "no supporting source or an unresolved conflict. Decisions are in "
        "data/surf_spots/profile_decisions.json.",
        "",
    ]
    if unsupported:
        lines += ["| Model setting | App value | Why it is not supported |", "|---|---|---|"]
        for label, value, reasons in unsupported:
            lines.append(f"| {label} | {_cell(value)} | {_cell(_cap('; '.join(reasons)))}. |")
    else:
        lines.append("The evidence supports every model setting this check covers.")
    lines.append("")
    if check and check["changes"]:
        intro = (
            f"Not part of the full re-read: {_esc(check['scope'])} on {check['checked_on']}."
            if check.get("scope")
            else f"Every field's excerpts were re-read on {check['checked_on']}."
        )
        lines += ["## Judgement check", "", f"{intro} Changes:", ""]
        lines += [f"- {_esc(c)}" for c in check["changes"]]
        lines.append("")
    elif check:
        lines += [
            "## Judgement check",
            "",
            f"Excerpts re-read on {check['checked_on']}; every value follows from its excerpts "
            "(no change).",
            "",
        ]
    lines += [
        "## Sources",
        "",
        "| # | Publisher | Title | Year | Accessed | Retrieval | Type |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in sources:
        title = _link_text(s["title"])
        if s.get("url"):
            title = f"[{title}]({s['url']})"
        lines.append(
            f"| {refs[s['key']]} | {_cell(s['publisher'])} | {title} | "
            f"{s['published_year'] if s['published_year'] is not None else 'n/a'} | "
            f"{_date(s['accessed_at'])} | "
            f"{RETRIEVAL_LABELS.get(s.get('retrieval', 'fetched'), s.get('retrieval'))} | "
            f"{s['source_type'].replace('_', ' ')} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_index(
    spots: Sequence[Mapping[str, Any]],
    rows: Sequence[tuple[Mapping[str, Any], str, dict[str, int], str]],
    totals: Mapping[str, int],
    conditions: Mapping[str, Any],
) -> str:
    classes = {
        cls: sum(1 for _s, c, _n, _d in rows if c == cls) for cls in ("high", "moderate", "low")
    }
    lines = [
        "# Surf spot research",
        "",
        f"<!-- Generated by {SCRIPT}. Do not edit by hand: change the data and run the script. -->",
        "",
        f"One file per spot ({len(spots)} spots) with the ideal conditions that the published "
        "sources in [research_evidence.json](../../data/surf_spots/research_evidence.json) "
        "support, the conflicting and missing information, the app's model settings that the "
        "evidence does not support, research confidence and the sources. The field values come "
        "from [research_conditions.json](../../data/surf_spots/research_conditions.json): every "
        "value cites the verbatim excerpts it rests on; a field without a supporting excerpt is "
        "*not documented* and nothing is filled in from general knowledge.",
        "",
        f"How the fields were extracted: {_esc(conditions['method'])}",
        "",
        "**Research confidence** is the class the app computes from the evidence "
        f"(methodology {METHODOLOGY_VERSION}, [research-confidence.md](../research-confidence.md)):"
        " how well each break's ideal conditions are documented. **Forecast confidence** is "
        "computed for every forecast hour and is not part of these files; the two are never "
        "combined.",
        "",
        f"Totals over {len(spots)} spots × {RESEARCH_FIELD_COUNT} researched fields: "
        f"{totals['documented']} documented, {totals['conflicting']} conflicting, "
        f"{totals['not_documented']} not documented. Research confidence: "
        f"{classes['high']} High, {classes['moderate']} Moderate, {classes['low']} Low. "
        f"Extraction date: {conditions['extracted_on']}.",
        "",
        f"Regenerate with `backend/.venv/bin/python {SCRIPT}`; "
        "`backend/tests/unit/test_spot_research_docs.py` fails when a file is out of date.",
        "",
        "| Spot | Region | Research confidence | Documented | Conflicts | Not documented | "
        "Last review |",
        "|---|---|---|---|---|---|---|",
    ]
    for spot, cls, counts, last in rows:
        lines.append(
            f"| [{_link_text(spot['name'])}]({spot['slug']}.md) | "
            f"{_cell(spot['region'])}, {_cell(spot['country'])} | {cls.capitalize()} | "
            f"{counts['documented']} | {counts['conflicting']} | {counts['not_documented']} | "
            f"{last} |"
        )
    lines.append("")
    return "\n".join(lines)


def generate(inputs: Mapping[str, Any]) -> dict[str, str]:
    """File name -> content of every document (after the mechanical check passed)."""
    problems = check_conditions(inputs)
    if problems:
        raise ValueError(
            "research_conditions.json fails the citation check:\n" + "\n".join(problems)
        )
    conditions = inputs["conditions"]
    extracted_on = conditions["extracted_on"]
    now = datetime.combine(date.fromisoformat(extracted_on), time(23, 59), tzinfo=UTC)
    assessed = assessments(inputs, now)
    decisions: Decisions = {}
    for rec in inputs["decisions"]["decisions"]:
        decisions.setdefault(rec["slug"], []).append(rec)
    evidence = inputs["evidence"]["spots"]
    spots = sorted(inputs["spots"], key=lambda s: s["slug"])
    out: dict[str, str] = {}
    rows = []
    totals = dict.fromkeys(STATUSES, 0)
    for spot in spots:
        slug = spot["slug"]
        entry = conditions["spots"][slug]
        spot_evidence = evidence.get(slug, {"notes": "", "sources": []})
        out[f"{slug}.md"] = render_spot(
            spot,
            spot_evidence,
            entry,
            assessed[slug],
            decisions,
            inputs["physical"].get(slug, {}),
            extracted_on,
        )
        counts = _counts(entry["fields"])
        for k, v in counts.items():
            totals[k] += v
        rows.append(
            (spot, assessed[slug].overall_class, counts, _last_review(spot_evidence["sources"]))
        )
    out["README.md"] = render_index(spots, rows, totals, conditions)
    return out


def stale_files(generated: Mapping[str, str], directory: Path) -> list[str]:
    """Names whose committed copy differs from the generated one (or is missing/extra)."""
    on_disk = {p.name for p in directory.glob("*.md")} if directory.is_dir() else set()
    stale = [
        name
        for name, text in generated.items()
        if not (directory / name).is_file() or (directory / name).read_text("utf-8") != text
    ]
    return sorted(stale + sorted(on_disk - set(generated)))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", type=Path, default=DATA_DIR, help="data directory")
    parser.add_argument("--out", type=Path, default=DOCS_DIR, help="output directory")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 when the files in --out are out of date"
    )
    args = parser.parse_args(argv)
    inputs = load_inputs(args.data)
    try:
        generated = generate(inputs)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.check:
        stale = stale_files(generated, args.out)
        for name in stale:
            print(f"out of date: {args.out / name}", file=sys.stderr)
        return 1 if stale else 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in generated.items():
        (args.out / name).write_text(text, encoding="utf-8")
    # Remove docs of spots no longer in spots.json (only files this script generated).
    for extra in sorted({p.name for p in args.out.glob("*.md")} - set(generated)):
        if f"Generated by {SCRIPT}" in (args.out / extra).read_text("utf-8"):
            (args.out / extra).unlink()
    print(f"wrote {len(generated)} files to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
