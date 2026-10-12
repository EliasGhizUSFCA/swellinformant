"""Load ``data/surf_spots/research_evidence.json`` and sync it into the evidence tables.

The file (format in docs/research-confidence.md) is the single source of truth for
research evidence, like ``spots.json`` is for spots:

* a missing file means "no evidence file": nothing is loaded and the stored evidence is
  left as it is (to remove evidence, ship a file without it);
* every problem in the file is collected and raised together as an
  :class:`EvidenceValidationError` before anything is written;
* the sync is idempotent: sources are upserted by (spot, key), a source's claims are
  replaced only when they changed, and sources (of any spot) that are no longer in the
  file are deleted. Running it twice changes nothing.

Each source records how it was read (``retrieval``: "fetched" by default, or "web_search"
when only a search tool's summary of the page was available). Claims are stored as the
researchers wrote them, including their own ``agrees`` judgement; the assessment
recomputes agreement against the current profile (:mod:`app.services.research.assess`),
so a later profile edit never needs a re-sync.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.database import utcnow
from app.models import SpotResearchClaim, SpotResearchSource, SurfSpot
from app.models.research import RESEARCH_VARIABLES, RETRIEVAL_METHODS, SOURCE_TYPES
from app.services.research.values import validate_value
from app.services.seed import SeedValidationError, resolve_data_dir

logger = logging.getLogger(__name__)

EVIDENCE_FILENAME = "research_evidence.json"
FORMAT_VERSION = 1
MAX_EXCERPT_CHARS = 300
MAX_KEY_CHARS = 120
MAX_TITLE_CHARS = 400
MAX_URL_CHARS = 1000
MAX_PUBLISHER_CHARS = 200
MIN_PUBLISHED_YEAR = 1800

FILE_KEYS = frozenset({"version", "generated_at", "method", "spots"})
SPOT_KEYS = frozenset({"notes", "sources"})
SOURCE_REQUIRED = frozenset(
    {"key", "title", "url", "publisher", "source_type", "published_year", "accessed_at"}
)
SOURCE_KEYS = SOURCE_REQUIRED | {"claims", "notes", "retrieval"}
DEFAULT_RETRIEVAL = "fetched"  # the page itself was read (RETRIEVAL_METHODS)
CLAIM_KEYS = frozenset({"variable", "claim", "value", "excerpt", "agrees"})
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class EvidenceValidationError(SeedValidationError):
    """The evidence file has problems; ``problems`` lists every one of them."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class EvidenceClaim:
    variable: str
    claim: str
    value: dict[str, Any] | None
    excerpt: str
    agrees: bool | None


@dataclass(frozen=True)
class EvidenceSource:
    key: str
    title: str
    url: str | None
    publisher: str
    source_type: str
    published_year: int | None
    accessed_at: datetime | None  # always set when parsed from the file
    notes: str
    claims: tuple[EvidenceClaim, ...]
    # "fetched" (the page was read) or "web_search" (only a search tool's summary of it).
    retrieval: str = DEFAULT_RETRIEVAL


@dataclass(frozen=True)
class SpotEvidence:
    slug: str
    notes: str
    sources: tuple[EvidenceSource, ...]


@dataclass(frozen=True)
class EvidenceFile:
    version: int
    generated_at: datetime
    method: str
    spots: dict[str, SpotEvidence]


def default_evidence_path(data_dir: str | Path | None = None) -> Path:
    return resolve_data_dir(data_dir) / EVIDENCE_FILENAME


def parse_timestamp(value: Any) -> datetime | None:
    """A date ("2026-10-11", midnight UTC) or ISO date-time; None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            d = date.fromisoformat(text)
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _text(
    problems: list[str], where: str, raw: dict[str, Any], key: str, limit: int | None = None
) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{where}: {key} must be a non-empty string")
        return ""
    if limit is not None and len(v) > limit:
        problems.append(f"{where}: {key} is longer than {limit} characters ({len(v)})")
    return v.strip()


def _unknown_keys(
    problems: list[str], where: str, raw: dict[str, Any], allowed: frozenset[str]
) -> None:
    extra = sorted(set(raw) - allowed)
    if extra:
        problems.append(f"{where}: unknown fields {extra}")


def _valid_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # e.g. an unbalanced IPv6 bracket: a problem to list, not a crash
        return False
    return parts.scheme in ("http", "https") and bool(host) and " " not in url


def _parse_claim(problems: list[str], where: str, raw: Any) -> EvidenceClaim | None:
    if not isinstance(raw, dict):
        problems.append(f"{where}: claim must be an object")
        return None
    _unknown_keys(problems, where, raw, CLAIM_KEYS)
    missing = sorted(CLAIM_KEYS - set(raw))
    if missing:
        problems.append(f"{where}: missing fields {missing}")
        return None
    n = len(problems)
    variable = raw["variable"]
    if variable not in RESEARCH_VARIABLES:
        problems.append(f"{where}: variable {variable!r} is not one of {list(RESEARCH_VARIABLES)}")
    claim = _text(problems, where, raw, "claim")
    excerpt = _text(problems, where, raw, "excerpt", MAX_EXCERPT_CHARS)
    agrees = raw["agrees"]
    if agrees is not None and not isinstance(agrees, bool):
        problems.append(f"{where}: agrees must be true, false or null")
    value = raw["value"]
    if variable in RESEARCH_VARIABLES:
        problems.extend(f"{where}: {p}" for p in validate_value(variable, value))
    if len(problems) > n:
        return None
    return EvidenceClaim(
        variable=variable, claim=claim, value=value, excerpt=excerpt, agrees=agrees
    )


def _parse_source(
    problems: list[str], where: str, raw: Any, now: datetime
) -> EvidenceSource | None:
    if not isinstance(raw, dict):
        problems.append(f"{where}: source must be an object")
        return None
    _unknown_keys(problems, where, raw, SOURCE_KEYS)
    missing = sorted((SOURCE_REQUIRED | {"claims"}) - set(raw))
    if missing:
        problems.append(f"{where}: missing fields {missing}")
        return None
    n = len(problems)
    key = raw["key"]
    if not isinstance(key, str) or len(key) > MAX_KEY_CHARS or not _KEY_RE.match(key):
        problems.append(
            f"{where}: key must be lower-case letters, digits, '.', '_' or '-' "
            f"(max {MAX_KEY_CHARS} characters)"
        )
    title = _text(problems, where, raw, "title", MAX_TITLE_CHARS)
    publisher = _text(problems, where, raw, "publisher", MAX_PUBLISHER_CHARS)
    url = raw["url"]
    if url is not None and (
        not isinstance(url, str) or len(url) > MAX_URL_CHARS or not _valid_url(url)
    ):
        problems.append(f"{where}: url must be an http(s) URL or null")
    if raw["source_type"] not in SOURCE_TYPES:
        problems.append(
            f"{where}: source_type {raw['source_type']!r} is not one of {list(SOURCE_TYPES)}"
        )
    year = raw["published_year"]
    if year is not None and (
        not isinstance(year, int)
        or isinstance(year, bool)
        or not MIN_PUBLISHED_YEAR <= year <= now.year + 1
    ):
        problems.append(f"{where}: published_year must be a year or null")
    accessed = parse_timestamp(raw["accessed_at"])
    if accessed is None:
        problems.append(f"{where}: accessed_at must be a date such as 2026-10-11")
    elif accessed > now + timedelta(days=1):
        problems.append(f"{where}: accessed_at is in the future")
    notes = raw.get("notes", "")
    if not isinstance(notes, str):
        problems.append(f"{where}: notes must be a string")
    retrieval = raw.get("retrieval", DEFAULT_RETRIEVAL)
    if retrieval not in RETRIEVAL_METHODS:
        problems.append(f"{where}: retrieval {retrieval!r} is not one of {list(RETRIEVAL_METHODS)}")
    claims_raw = raw["claims"]
    claims: list[EvidenceClaim] = []
    if not isinstance(claims_raw, list) or not claims_raw:
        problems.append(f"{where}: claims must be a non-empty list")
    else:
        for i, c in enumerate(claims_raw):
            parsed = _parse_claim(problems, f"{where} claim {i + 1}", c)
            if parsed is not None:
                claims.append(parsed)
    if len(problems) > n or accessed is None:
        return None
    return EvidenceSource(
        key=key,
        title=title,
        url=url,
        publisher=publisher,
        source_type=raw["source_type"],
        published_year=year,
        accessed_at=accessed,
        notes=notes.strip(),
        claims=tuple(claims),
        retrieval=retrieval,
    )


def parse_evidence(raw: Any, known_slugs: set[str], now: datetime | None = None) -> EvidenceFile:
    """Validate the decoded file; raises :class:`EvidenceValidationError` with every problem."""
    now = now or utcnow()
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise EvidenceValidationError(["research evidence: top level must be an object"])
    _unknown_keys(problems, "research evidence", raw, FILE_KEYS)
    if raw.get("version") != FORMAT_VERSION:
        problems.append(f"research evidence: version must be {FORMAT_VERSION}")
    generated_at = parse_timestamp(raw.get("generated_at"))
    if generated_at is None:
        problems.append("research evidence: generated_at must be an ISO date-time")
    method = raw.get("method")
    if not isinstance(method, str) or not method.strip():
        problems.append("research evidence: method must describe how evidence was gathered")
    spots_raw = raw.get("spots")
    spots: dict[str, SpotEvidence] = {}
    if not isinstance(spots_raw, dict):
        problems.append("research evidence: spots must be an object keyed by spot slug")
        spots_raw = {}
    for slug, entry in spots_raw.items():
        where = f"{slug}"
        if slug not in known_slugs:
            problems.append(f"{where}: unknown spot slug")
            continue
        if not isinstance(entry, dict):
            problems.append(f"{where}: entry must be an object")
            continue
        _unknown_keys(problems, where, entry, SPOT_KEYS)
        notes = entry.get("notes", "")
        if not isinstance(notes, str):
            problems.append(f"{where}: notes must be a string")
            notes = ""
        sources_raw = entry.get("sources")
        if not isinstance(sources_raw, list):
            problems.append(f"{where}: sources must be a list")
            continue
        sources: list[EvidenceSource] = []
        seen: set[str] = set()
        for i, s in enumerate(sources_raw):
            key = s.get("key") if isinstance(s, dict) else None
            label = f"{where} source {key!r}" if isinstance(key, str) else f"{where} source {i + 1}"
            if isinstance(key, str):
                if key in seen:
                    problems.append(f"{where}: duplicate source key {key!r}")
                seen.add(key)
            parsed = _parse_source(problems, label, s, now)
            if parsed is not None:
                sources.append(parsed)
        spots[slug] = SpotEvidence(slug=slug, notes=notes.strip(), sources=tuple(sources))
    if problems:
        raise EvidenceValidationError(problems)
    assert generated_at is not None and isinstance(method, str)
    return EvidenceFile(
        version=FORMAT_VERSION, generated_at=generated_at, method=method.strip(), spots=spots
    )


def load_evidence(
    db: Session, path: str | Path | None = None, data_dir: str | Path | None = None
) -> EvidenceFile | None:
    """Read and validate the evidence file; None when it does not exist.

    Slugs are checked against the spots in the database (seed spots first).
    """
    file = Path(path) if path is not None else default_evidence_path(data_dir)
    if not file.exists():
        return None
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError([f"{file.name}: not valid UTF-8 JSON ({exc})"]) from exc
    known = set(db.scalars(select(SurfSpot.slug)).all())
    return parse_evidence(raw, known)


def _claim_signature(c: EvidenceClaim | SpotResearchClaim) -> str:
    return json.dumps(
        [c.variable, c.claim, c.value, c.excerpt, c.agrees], sort_keys=True, ensure_ascii=False
    )


def _source_fields(s: EvidenceSource) -> dict[str, Any]:
    return {
        "title": s.title,
        "url": s.url,
        "publisher": s.publisher,
        "source_type": s.source_type,
        "published_year": s.published_year,
        "accessed_at": s.accessed_at,
        "notes": s.notes,
        "retrieval": s.retrieval,
    }


def sync_evidence(db: Session, evidence: EvidenceFile | None) -> dict[str, int]:
    """Make the evidence tables match the file (flushes, does not commit).

    ``evidence=None`` (no file) changes nothing. Returns counts of what changed.
    """
    counts = {
        "evidence_spots": 0,
        "sources_created": 0,
        "sources_updated": 0,
        "sources_unchanged": 0,
        "sources_removed": 0,
        "claim_sets_replaced": 0,
    }
    if evidence is None:
        return counts
    spot_ids = {slug: sid for slug, sid in db.execute(select(SurfSpot.slug, SurfSpot.id))}
    existing: dict[tuple[int, str], SpotResearchSource] = {
        (s.spot_id, s.source_key): s for s in db.scalars(select(SpotResearchSource)).all()
    }
    claims_by_source: dict[int, list[SpotResearchClaim]] = {}
    for c in db.scalars(select(SpotResearchClaim).order_by(SpotResearchClaim.id)).all():
        claims_by_source.setdefault(c.source_id, []).append(c)

    wanted: set[tuple[int, str]] = set()
    for slug, spot_evidence in evidence.spots.items():
        spot_id = spot_ids[slug]
        if spot_evidence.sources:
            counts["evidence_spots"] += 1
        for src in spot_evidence.sources:
            wanted.add((spot_id, src.key))
            fields = _source_fields(src)
            row = existing.get((spot_id, src.key))
            is_new = row is None
            changed = False
            old_claims: list[SpotResearchClaim] = []
            if row is None:
                row = SpotResearchSource(spot_id=spot_id, source_key=src.key, **fields)
                db.add(row)
                db.flush()  # assigns row.id for the claims
                counts["sources_created"] += 1
            else:
                diff = {k: v for k, v in fields.items() if getattr(row, k) != v}
                for k, v in diff.items():
                    setattr(row, k, v)
                changed = bool(diff)
                old_claims = claims_by_source.get(row.id, [])
            new_signature = sorted(_claim_signature(c) for c in src.claims)
            if sorted(_claim_signature(c) for c in old_claims) != new_signature:
                if old_claims:
                    db.execute(
                        delete(SpotResearchClaim).where(SpotResearchClaim.source_id == row.id)
                    )
                    counts["claim_sets_replaced"] += 1
                db.add_all(
                    SpotResearchClaim(
                        spot_id=spot_id,
                        source_id=row.id,
                        variable=c.variable,
                        claim=c.claim,
                        value=c.value,
                        excerpt=c.excerpt,
                        agrees=c.agrees,
                    )
                    for c in src.claims
                )
                changed = True
            if not is_new:
                counts["sources_updated" if changed else "sources_unchanged"] += 1
    for key, row in existing.items():
        if key not in wanted:
            db.delete(row)  # claims go with it (ON DELETE CASCADE)
            counts["sources_removed"] += 1
    db.flush()
    logger.info("Research evidence synced: %s", counts)
    return counts
