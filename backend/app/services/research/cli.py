"""``python -m app.cli research ...``: research evidence and research confidence.

  research load [--path P]            validate and load research_evidence.json, then assess
                                      every spot (only changed assessments get a new row)
  research assess [--slug S] [--force]
                                      (re)assess from the stored evidence; --force inserts a
                                      new assessment even if nothing changed
  research show SLUG                  per-variable classes, sources and gaps
  research lint [--decisions P] [--max-gap DEG]
                                      list spots whose offshore wind is more than DEG
                                      (default 60) from straight offshore (shore normal +
                                      180) without a recorded reason, contradicted profile
                                      fields without a decision record, records the rule no
                                      longer gives, and problems in profile_decisions.json

Exit codes: 0 ok, 1 unknown spot or missing file (lint: issues listed), 2 evidence or
decision-file validation errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import select

GROUP = "research"


def register(subparsers: Any) -> None:
    parser = subparsers.add_parser(GROUP, help="research evidence and research confidence")
    sub = parser.add_subparsers(dest="research_cmd", required=True)
    load = sub.add_parser(
        "load", help="validate and load the research evidence file, then assess every spot"
    )
    load.add_argument(
        "--path",
        default=None,
        help="evidence file (default: research_evidence.json next to spots.json)",
    )
    assess = sub.add_parser("assess", help="assess research confidence from stored evidence")
    assess.add_argument("--slug", default=None, help="only this spot")
    assess.add_argument(
        "--force",
        action="store_true",
        help="insert a new assessment even if the evidence and methodology are unchanged",
    )
    show = sub.add_parser("show", help="print a spot's research confidence")
    show.add_argument("slug")
    lint = sub.add_parser(
        "lint", help="check spot profiles against the evidence and the decision records"
    )
    lint.add_argument(
        "--decisions",
        default=None,
        help="decision file (default: profile_decisions.json next to spots.json)",
    )
    lint.add_argument("--max-gap", type=float, default=None, help="degrees (default 60)")


def run(args: argparse.Namespace) -> int:
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        if args.research_cmd == "load":
            return _load(db, args.path)
        if args.research_cmd == "assess":
            return _assess(db, args.slug, args.force)
        if args.research_cmd == "show":
            return _show(db, args.slug)
        if args.research_cmd == "lint":
            return _lint(db, args.decisions, args.max_gap)
    return 2


def _lint(db: Any, path: str | None, max_gap: float | None) -> int:
    from pathlib import Path

    from app.models import SpotPhysicalProfile, SpotResearchSource, SurfSpot
    from app.services.geo import window_center
    from app.services.research import current_assessments
    from app.services.research.decisions import (
        GEOMETRY_LINT_DEG,
        default_decisions_path,
        lint,
        validate_decisions,
    )
    from app.services.seed import SPOT_FIELDS

    file = Path(path) if path else default_decisions_path()
    if path and not file.exists():
        print(f"research lint: no such file: {file}", file=sys.stderr)
        return 1
    raw = json.loads(file.read_text(encoding="utf-8")) if file.exists() else None
    rows = db.scalars(select(SurfSpot).where(SurfSpot.is_active).order_by(SurfSpot.slug)).all()
    spots = {s.slug: _spot_mapping(s, SPOT_FIELDS) for s in rows}
    normals: dict[str, float] = {}
    profiles = {p.spot_id: p for p in db.scalars(select(SpotPhysicalProfile)).all()}
    for s in rows:
        prof = profiles.get(s.id)
        normals[s.slug] = (
            float(prof.shore_normal_deg)
            if prof is not None
            else window_center(s.optimal_swell_direction_min, s.optimal_swell_direction_max)
        )
    keys: dict[str, set[str]] = {}
    slug_of = {s.id: s.slug for s in rows}
    for spot_id, key in db.execute(
        select(SpotResearchSource.spot_id, SpotResearchSource.source_key)
    ):
        if spot_id in slug_of:
            keys.setdefault(slug_of[spot_id], set()).add(key)
    if raw is not None:
        problems = validate_decisions(raw, spots, keys)
        if problems:
            print(f"research lint: {len(problems)} problem(s) in {file.name}:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 2
    decisions: dict[str, list[dict[str, Any]]] = {}
    for rec in (raw or {}).get("decisions", []):
        decisions.setdefault(rec["slug"], []).append(rec)
    assessments = {
        slug_of[spot_id]: a.per_variable or {}
        for spot_id, a in current_assessments(db).items()
        if spot_id in slug_of
    }
    issues = lint(
        spots,
        normals,
        assessments,
        decisions,
        GEOMETRY_LINT_DEG if max_gap is None else max_gap,
    )
    for issue in issues:
        print(f"{issue.slug}: [{issue.kind}] {issue.message}")
    print(
        f"research lint: {len(issues)} issue(s); {len(spots)} spots, "
        f"{sum(len(v) for v in decisions.values())} decision records"
    )
    return 1 if issues else 0


def _spot_mapping(spot: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    """A spots.json-like mapping of a SurfSpot row (enums as their string values)."""
    out: dict[str, Any] = {}
    for f in fields:
        v = getattr(spot, f)
        out[f] = v.value if hasattr(v, "value") else v
    return out


def _load(db: Any, path: str | None) -> int:
    from pathlib import Path

    from app.services.research.assess import assess_all
    from app.services.research.evidence import (
        EvidenceValidationError,
        default_evidence_path,
        load_evidence,
        sync_evidence,
    )

    file = Path(path) if path else default_evidence_path()
    if path and not file.exists():
        print(f"research load: no such file: {file}", file=sys.stderr)
        return 1
    try:
        evidence = load_evidence(db, file)
    except EvidenceValidationError as exc:
        print(f"research load: {len(exc.problems)} problem(s) in {file.name}:", file=sys.stderr)
        for p in exc.problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    synced = sync_evidence(db, evidence)
    assessed = assess_all(db)
    db.commit()
    print(
        json.dumps(
            {"evidence_file": str(file) if evidence else None, **synced, **assessed}, indent=2
        )
    )
    return 0


def _assess(db: Any, slug: str | None, force: bool) -> int:
    from app.models import SurfSpot
    from app.services.research.assess import assess_all

    if slug is not None and db.scalar(select(SurfSpot.id).where(SurfSpot.slug == slug)) is None:
        print(f"research assess: unknown spot {slug}", file=sys.stderr)
        return 1
    counts = assess_all(db, slug=slug, force=force)
    db.commit()
    print(json.dumps(counts, indent=2))
    return 0


def _show(db: Any, slug: str) -> int:
    from app.models import SurfSpot
    from app.services.research.assess import current_assessment

    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == slug))
    if spot is None:
        print(f"research show: unknown spot {slug}", file=sys.stderr)
        return 1
    row = current_assessment(db, spot.id)
    if row is None:
        print(f"{spot.name} ({slug}): not assessed yet; run `research assess --slug {slug}`")
        return 0
    print(format_assessment(spot.name, slug, row))
    return 0


def format_assessment(name: str, slug: str, row: Any) -> str:
    """Plain-text report of one assessment (``research show``)."""
    from app.models.research import RESEARCH_VARIABLES
    from app.services.research.views import research_view

    view = research_view(row)
    updated = row.research_updated_at.date().isoformat() if row.research_updated_at else "never"
    lines = [
        f"{name} ({slug})",
        f"Research confidence: {row.overall_class.upper()}  "
        f"[{row.methodology_version}; assessed {row.assessed_at:%Y-%m-%d %H:%M} UTC; "
        f"research updated {updated}]",
        "(How well this break's ideal conditions are documented; not forecast confidence.)",
        row.summary,
    ]
    if row.gaps:
        lines.append("Main gaps:")
        lines += [f"  - {g}" for g in row.gaps]
    lines.append("")
    lines.append(f"{'Variable':<22}{'Class':<10}{'Sources':>7}  Summary")
    for v in RESEARCH_VARIABLES:
        entry = row.per_variable.get(v)
        if entry is None:
            continue
        lines.append(
            f"{entry['label']:<22}{entry['class']:<10}{entry['sources']:>7}  {entry['summary']}"
        )
        lines += [f"{'':<41}gap: {g}" for g in entry.get("gaps", [])]
        if entry.get("flag_differences"):
            lines.append(
                f"{'':<41}note: {entry['flag_differences']} researcher flag(s) differ from the "
                "computed agreement"
            )
    lines.append("")
    lines.append(f"Sources ({len(view.sources)}):")
    if not view.sources:
        lines.append("  none recorded")
    for s in view.sources:
        year = s.published_year if s.published_year is not None else "n.d."
        accessed = s.accessed_at.date().isoformat() if s.accessed_at else "?"
        read = "; search summary only" if s.retrieval == "web_search" else ""
        lines.append(
            f"  [{s.source_type}] {s.publisher}: {s.title} ({year}; accessed {accessed}{read})"
            + (f"\n      {s.url}" if s.url else "")
        )
    return "\n".join(lines)
