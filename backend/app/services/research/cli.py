"""``python -m app.cli research ...``: research evidence and research confidence.

  research load [--path P]            validate and load research_evidence.json, then assess
                                      every spot (only changed assessments get a new row)
  research assess [--slug S] [--force]
                                      (re)assess from the stored evidence; --force inserts a
                                      new assessment even if nothing changed
  research show SLUG                  per-variable classes, sources and gaps

Exit codes: 0 ok, 1 unknown spot or missing file, 2 evidence validation errors.
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


def run(args: argparse.Namespace) -> int:
    from app.core.database import SessionLocal

    with SessionLocal() as db:
        if args.research_cmd == "load":
            return _load(db, args.path)
        if args.research_cmd == "assess":
            return _assess(db, args.slug, args.force)
        if args.research_cmd == "show":
            return _show(db, args.slug)
    return 2


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
