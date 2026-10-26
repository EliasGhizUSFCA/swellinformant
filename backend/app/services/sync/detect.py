"""Model-run detection: is a sync due? (docs/sync.md).

* :func:`load_known` reads, per source, the newest run key that is stored *and published*
  (a run stored after the newest publication counts as not stored yet, so a sync that
  stopped before publishing is finished by the next one) and the cycle of the published
  map run.
* :func:`plan_sync` asks each component's provider for its newest servable run, with
  metadata only (Open-Meteo ``meta.json`` with its availability wait, NOAA NODD ``.idx``
  HEAD requests), and applies the rules of docs/sync.md: triggers (SYNC_TRIGGER_SOURCES),
  coalescing (SYNC_COALESCE_MINUTES), retries (SYNC_RETRY_MINUTES, SYNC_MAX_ATTEMPTS) and
  ``force``. With ``db=None`` it works from ``known`` alone: the daemon's idle check needs
  no database. Provider errors never raise; the component is ``unknown``.
* :func:`apply_result` records a sync's outcome per component in ``known`` (failed
  attempts, first sightings), :func:`refresh_known` reloads the stored keys after a sync and
  :func:`promote_pending` turns held-back components into due ones (``sync run``).

Components (:class:`Probe`): one ``spots`` component per FORECAST_PROVIDERS token (the
point forecasts of the active spots), ``map`` (the world map layers, source
``noaa_gfs_fields``, in the standalone map-fields mode) and ``ensemble`` (GEFS-Wave, when
GEFS_ENABLED). The provider modules are imported when the probes are built, never at
import time.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.models import ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.models.sync import SyncRun
from app.services.forecasts.base import RunInfo
from app.services.sync.types import (
    ComponentKind,
    ComponentState,
    ComponentStatus,
    KnownRuns,
    SyncPlan,
    SyncResult,
    known_key,
)

logger = logging.getLogger(__name__)

# Source codes (not imported from their modules: app.services.forecasts.noaa and
# ensemble load ecCodes and numpy).
FIELDS_SOURCE_CODE = "noaa_gfs_fields"  # app.services.forecasts.noaa.FIELDS_SOURCE_CODE
GEFS_SOURCE_CODE = "noaa_gefs_wave"  # app.services.forecasts.registry.GEFS_WAVE_CODE
BLEND_CODE = "blend"  # app.services.forecasts.blend.BLEND_CODE
# ComponentStatus.provider of the map and ensemble components (spots: the
# FORECAST_PROVIDERS token).
MAP_PROVIDER = "map_fields"
ENSEMBLE_PROVIDER = "gefs"
# The model family whose spot run is coalesced with the map layers of the same cycle.
ANCHOR_FAMILY = "ncep_gfs"
USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)
DETAIL_MAX_CHARS = 300
# HTTP timeout of the metadata requests of a check (seconds; FORECAST_HTTP_TIMEOUT_SECONDS
# when lower): a hanging host must not hold the daemon's loop, and its kick polls, for
# minutes. A timed-out component is "unknown" and is checked again at the next check.
PROBE_TIMEOUT_SECONDS = 10.0
# A model cycle inside a run key: every run key format has ":YYYYMMDDHH" stamps
# (open_meteo "gfswave016:2026101912|gfs013:...", "noaa-gfs-fields:2026101912", ...).
_CYCLE_STAMP = re.compile(r"(?<!\d)(\d{10})(?!\d)")


@dataclass
class Probe:
    """How to read one component's newest servable run.

    ``current_run(now)`` is the provider's own run discovery (metadata only). A component
    that is not configured has ``disabled`` (the reason) and no ``current_run``; one whose
    provider could not be built has ``error``. ``family`` is the spot provider's model
    family (coalescing); ``close`` releases its HTTP client.
    """

    kind: ComponentKind
    source_code: str
    provider: str
    current_run: Callable[[datetime], RunInfo] | None = None
    family: str | None = None
    disabled: str | None = None
    error: str | None = None
    close: Callable[[], None] | None = None


def _error_detail(exc: BaseException) -> str:
    from app.services.http import ProviderError

    text = str(exc) if isinstance(exc, ProviderError) else f"{type(exc).__name__}: {exc}"
    return redact(text)[:DETAIL_MAX_CHARS]


def servable_run(provider: Any) -> Callable[[datetime], RunInfo]:
    """``current_run`` of an Open-Meteo provider followed by its ``ensure_servable``, both on
    one read of the metadata (each ``meta.json`` is fetched once per check).

    Open-Meteo can label a run that it no longer serves unmixed (e.g. the ECMWF family
    reads only 00Z/12Z, while Open-Meteo serves the 06Z run between them): such a run raises
    RunNotServedError here instead of making a sync due that can only fail.
    """

    def current_run(now: datetime) -> RunInfo:
        metas = provider.read_metas()
        provider.read_metas = lambda: metas  # shadows the method for this check only
        try:
            run: RunInfo = provider.current_run(now)
            provider.ensure_servable(run, now)
            return run
        finally:
            del provider.read_metas

    return current_run


def _closer(provider: object) -> Callable[[], None] | None:
    client = getattr(provider, "client", None)
    close = getattr(client, "close", None)
    return close if callable(close) else None


def build_probes(settings: Settings) -> list[Probe]:
    """The components of a plan for these settings, in display order (spots in
    FORECAST_PROVIDERS order, then the ensemble, then the map)."""
    from app.services.forecasts.registry import SOURCE_CODES, build_provider
    from app.services.map_fields import map_fields_mode

    timeout = min(settings.forecast_http_timeout_seconds, PROBE_TIMEOUT_SECONDS)
    settings = settings.model_copy(update={"forecast_http_timeout_seconds": timeout})
    probes: list[Probe] = []
    for token in settings.forecast_provider_list:
        code = SOURCE_CODES.get(token, token)
        try:
            provider = build_provider(token, settings)
        except Exception as exc:  # e.g. a missing optional dependency or a refused setting
            probes.append(Probe("spots", code, token, error=_error_detail(exc)))
            continue
        info = provider.source_info()
        servable = callable(getattr(provider, "ensure_servable", None))
        probes.append(
            Probe(
                "spots",
                info.code,
                token,
                current_run=servable_run(provider) if servable else provider.current_run,
                family=info.family,
                close=_closer(provider),
            )
        )

    if settings.gefs_enabled:
        try:
            from app.services.forecasts.ensemble import GefsWaveEnsembleProvider

            gefs = GefsWaveEnsembleProvider(settings)
            probes.append(
                Probe(
                    "ensemble",
                    GEFS_SOURCE_CODE,
                    ENSEMBLE_PROVIDER,
                    current_run=gefs.current_run,
                    close=_closer(gefs),
                )
            )
        except Exception as exc:
            probes.append(
                Probe("ensemble", GEFS_SOURCE_CODE, ENSEMBLE_PROVIDER, error=_error_detail(exc))
            )
    else:
        probes.append(
            Probe("ensemble", GEFS_SOURCE_CODE, ENSEMBLE_PROVIDER, disabled="GEFS_ENABLED=false")
        )

    mode = map_fields_mode(settings)
    if mode == "standalone":
        try:
            from app.services.forecasts.noaa import NoaaGfsFieldsProvider

            fields = NoaaGfsFieldsProvider(settings)
            probes.append(
                Probe(
                    "map",
                    FIELDS_SOURCE_CODE,
                    MAP_PROVIDER,
                    current_run=fields.current_run,
                    close=_closer(fields),
                )
            )
        except Exception as exc:
            probes.append(Probe("map", FIELDS_SOURCE_CODE, MAP_PROVIDER, error=_error_detail(exc)))
    else:
        reason = (
            "the map layers are stored by the noaa_gfs spot acquisition"
            if mode == "with_noaa"
            else "map fields are off (MAP_FIELDS_ENABLED)"
        )
        probes.append(Probe("map", FIELDS_SOURCE_CODE, MAP_PROVIDER, disabled=reason))
    return probes


# --------------------------------------------------------------------------- known runs
def _latest_publication_at(db: Session) -> datetime | None:
    return db.scalar(select(func.max(SyncRun.published_at)))


def _not_simulated() -> Any:
    # simulate-swell's demo runs (details.simulated) are never a provider's current run.
    return ForecastRun.details["simulated"].astext.is_distinct_from("true")


def load_known(db: Session) -> KnownRuns:
    """Stored run keys and the published map cycle, read from the database.

    ``stored``: per source (blend excluded), the newest usable (success or partial),
    non-simulated run that completed before the newest publication. Before the first
    publication nothing counts as stored, so the first sync publishes everything.
    ``map_cycle``: the cycle of the newest publication's map run.
    """
    published_at = _latest_publication_at(db)
    stored: dict[str, str] = {}
    map_cycle: datetime | None = None
    if published_at is not None:
        rows = db.execute(
            select(ForecastSource.code, ForecastRun.run_key)
            .join(ForecastSource, ForecastRun.source_id == ForecastSource.id)
            .where(
                ForecastRun.status.in_(USABLE),
                ForecastSource.code != BLEND_CODE,
                ForecastRun.completed_at <= published_at,
                _not_simulated(),
            )
            .ext(distinct_on(ForecastRun.source_id))
            .order_by(ForecastRun.source_id, ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        ).all()
        stored = {str(code): str(key) for code, key in rows}
        map_run = db.execute(
            select(ForecastRun.wave_model_run_at, ForecastRun.issued_at)
            .join(SyncRun, SyncRun.map_run_id == ForecastRun.id)
            .where(SyncRun.published_at.is_not(None))
            .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
            .limit(1)
        ).first()
        if map_run is not None:
            map_cycle = map_run[0] or map_run[1]
    return KnownRuns(
        stored=stored, map_cycle=map_cycle, first_seen={}, attempts={}, loaded_at=utcnow()
    )


def empty_known(now: datetime | None = None) -> KnownRuns:
    """Nothing known: every available run counts as new until the stored keys are loaded."""
    return KnownRuns(
        stored={}, map_cycle=None, first_seen={}, attempts={}, loaded_at=now or utcnow()
    )


def refresh_known(db: Session, known: KnownRuns | None) -> KnownRuns:
    """``known`` with ``stored``, ``map_cycle`` and ``loaded_at`` reloaded from the database
    (first sightings and attempts are kept); a fresh :func:`load_known` when None."""
    fresh = load_known(db)
    if known is None:
        return fresh
    known.stored, known.map_cycle, known.loaded_at = fresh.stored, fresh.map_cycle, fresh.loaded_at
    return known


def newest_cycle(run_key: str | None) -> datetime | None:
    """The newest model cycle named in a run key (its ``YYYYMMDDHH`` stamps), or None."""
    if not run_key:
        return None
    stamps = _CYCLE_STAMP.findall(run_key)
    if not stamps:
        return None
    try:
        return max(datetime.strptime(s, "%Y%m%d%H").replace(tzinfo=UTC) for s in stamps)
    except ValueError:
        return None


def domain_cycles(run_key: str | None) -> dict[str, datetime] | None:
    """The cycle of each model domain of a multi-domain run key (Open-Meteo
    ``"gfswave016:2026102118|gfs025:2026102200"``), or None for any other format."""
    if not run_key:
        return None
    out: dict[str, datetime] = {}
    for part in run_key.split("|"):
        name, sep, stamp = part.rpartition(":")
        if not sep or not name or not _CYCLE_STAMP.fullmatch(stamp):
            return None
        try:
            out[name] = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            return None
    return out


def domains_behind(available: str | None, stored: str | None) -> bool:
    """True when the available run has the stored run's model domains, none on a newer
    cycle and at least one on an older one: a lagging metadata server (Open-Meteo's servers
    can disagree for minutes) offers part of the previous run, which is never new."""
    have, kept = domain_cycles(available), domain_cycles(stored)
    if not have or not kept or have.keys() != kept.keys():
        return False
    return all(have[k] <= kept[k] for k in have) and any(have[k] < kept[k] for k in have)


# --------------------------------------------------------------------------- planning
@dataclass
class _Seen:
    """One probed component before the rules are applied."""

    probe: Probe
    run: RunInfo | None = None
    error: str | None = None
    stored: str | None = None
    cycle: datetime | None = None  # newest model init of the available run
    complete: bool = True  # every model domain of the run is on ``cycle``
    stored_cycle: datetime | None = None
    pending: bool = False  # a newer run than the stored one is available
    current_detail: str | None = None
    # The provider labels a newer run but does not serve it unmixed yet (RunNotServedError):
    # the reason, and when to look again (None: at the next check).
    not_served: str | None = None
    not_served_retry_s: float | None = None

    @property
    def key(self) -> str | None:
        return self.run.run_key if self.run is not None else None


def _inits(run: RunInfo) -> list[datetime]:
    if run.model_runs:
        return [r.init_time for r in run.model_runs]
    return [run.issued_at]


def _observe(probe: Probe, known: KnownRuns, now: datetime) -> _Seen:
    seen = _Seen(probe, stored=known.stored.get(probe.source_code))
    seen.stored_cycle = newest_cycle(seen.stored)
    if probe.kind == "map" and known.map_cycle is not None:
        seen.stored_cycle = max(filter(None, (seen.stored_cycle, known.map_cycle)))
    if probe.disabled is not None:
        return seen
    if probe.error is not None or probe.current_run is None:
        seen.error = probe.error or "no run discovery"
        return seen
    from app.services.http import RunNotServedError

    try:
        run = probe.current_run(now)
    except RunNotServedError as exc:
        # Something newer than the stored run exists, but it cannot be read yet (Open-Meteo
        # publishing a run, or serving a run this source does not read).
        seen.not_served = _error_detail(exc)
        seen.not_served_retry_s = exc.retry_after_s
        return seen
    except Exception as exc:  # metadata outage, no complete cycle yet, a bug: unknown
        seen.error = _error_detail(exc)
        logger.info("Sync check: %s run unknown: %s", probe.source_code, seen.error)
        return seen
    seen.run = run
    inits = _inits(run)
    seen.cycle, seen.complete = max(inits), min(inits) == max(inits)
    if run.run_key == seen.stored:
        return seen
    if (seen.stored_cycle is not None and seen.cycle < seen.stored_cycle) or domains_behind(
        run.run_key, seen.stored
    ):
        seen.current_detail = (
            f"the provider serves {run.run_key}, older than the stored run; nothing to fetch"
        )
        return seen
    if probe.kind == "map" and known.map_cycle is not None and seen.cycle <= known.map_cycle:
        seen.current_detail = f"the map layers of {seen.cycle:%Y-%m-%d %H}Z are published"
        return seen
    seen.pending = True
    return seen


def _track_first_seen(seen: _Seen, known: KnownRuns, now: datetime) -> datetime | None:
    """Keep one first sighting per source: when anything newer than the stored run was
    first seen (carried over when the available run key changes again before a sync)."""
    if seen.error is not None or seen.probe.disabled is not None or seen.not_served:
        return None  # nothing servable observed: keep what is known
    prefix = f"{seen.probe.source_code}|"
    entries = {k: t for k, t in known.first_seen.items() if k.startswith(prefix)}
    for k in entries:
        del known.first_seen[k]
    stale = [k for k in known.attempts if k.startswith(prefix) and k != _key(seen)]
    for k in stale:
        del known.attempts[k]
    if not seen.pending or seen.key is None:
        return None
    first = min(entries.values(), default=now)
    known.first_seen[known_key(seen.probe.source_code, seen.key)] = first
    return first


def _key(seen: _Seen) -> str:
    return known_key(seen.probe.source_code, seen.key or "")


def _hhmm(when: datetime) -> str:
    return f"{when:%Y-%m-%d %H:%M} UTC"


def plan_sync(
    db: Session | None,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    known: KnownRuns | None = None,
    force: bool = False,
) -> SyncPlan:
    """The sync plan at ``now`` (rules: docs/sync.md).

    With ``db``, the stored keys are reloaded from the database first (into ``known`` when
    given) and a running sync with a valid lease turns new components ``in_progress``.
    With ``db=None`` only ``known`` is used (None: nothing is known as stored). ``known``
    is updated in place: first sightings of new runs, attempts of runs no longer offered.
    """
    settings = settings or get_settings()
    now = now or utcnow()
    running: SyncRun | None = None
    if db is not None:
        known = refresh_known(db, known)
        running = valid_lease(db)
    elif known is None:
        known = empty_known(now)

    probes = build_probes(settings)
    try:
        seen = [_observe(p, known, now) for p in probes]
    finally:
        for p in probes:
            if p.close is not None:
                try:
                    p.close()
                except Exception:  # closing a client never fails a check
                    logger.debug("closing %s failed", p.source_code, exc_info=True)
    first_seen = {id(s): _track_first_seen(s, known, now) for s in seen}
    return _decide(seen, first_seen, known, settings, now, force, running)


def trigger_kinds(seen: Iterable[_Seen], settings: Settings) -> tuple[frozenset[str], str | None]:
    """The component kinds whose new run makes a sync due: SYNC_TRIGGER_SOURCES, and a note.

    When this setup has none of them (the free-database profile's ``map`` while the map
    layers are off, or come with the noaa_gfs spot run), spot runs trigger instead, so the
    spot forecasts never stop syncing; the note says so in the plan's reasons.
    """
    configured = settings.sync_trigger_list
    enabled = {s.probe.kind for s in seen if s.probe.disabled is None}
    if configured & enabled:
        return configured, None
    kinds = ",".join(sorted(configured))
    return configured | {"spots"}, (
        f"SYNC_TRIGGER_SOURCES={kinds} names no component this setup has: new spot runs "
        "trigger the syncs"
    )


def valid_lease(db: Session) -> SyncRun | None:
    row = db.scalar(select(SyncRun).where(SyncRun.status == "running").limit(1))
    if row is None or row.lease_expires_at is None or row.lease_expires_at <= utcnow():
        return None
    return row


def _decide(
    seen: list[_Seen],
    first_seen: dict[int, datetime | None],
    known: KnownRuns,
    settings: Settings,
    now: datetime,
    force: bool,
    running: SyncRun | None,
) -> SyncPlan:
    window = timedelta(minutes=settings.sync_coalesce_minutes)
    retry = timedelta(minutes=settings.sync_retry_minutes)
    triggers, fallback = trigger_kinds(seen, settings)
    map_seen = next((s for s in seen if s.probe.kind == "map" and not s.probe.disabled), None)
    anchor_spots = [
        s
        for s in seen
        if s.probe.kind == "spots" and s.probe.family == ANCHOR_FAMILY and not s.probe.disabled
    ]
    anchored = map_seen is not None and bool(anchor_spots)

    def in_anchor(s: _Seen) -> bool:
        if not anchored or s.cycle is None:
            return False
        if s.probe.kind == "map":
            return True
        return s in anchor_spots and _is_map_cycle(settings, s.cycle)

    def partners(s: _Seen) -> list[_Seen]:
        assert map_seen is not None
        return anchor_spots if s.probe.kind == "map" else [map_seen]

    def matched(p: _Seen, cycle: datetime) -> bool:
        if p.stored_cycle is not None and p.stored_cycle >= cycle:
            return True
        return p.pending and p.complete and p.cycle is not None and p.cycle >= cycle

    statuses: list[ComponentStatus] = []
    reasons: list[str] = []
    waits: list[datetime] = []
    for s in seen:
        p = s.probe
        base: dict[str, Any] = {
            "kind": p.kind,
            "source_code": p.source_code,
            "provider": p.provider,
            "stored_run_key": s.stored,
            "available_run_key": s.key,
            "available_cycle": s.cycle,
        }
        if p.disabled is not None:
            statuses.append(ComponentStatus(state="disabled", detail=p.disabled, **base))
            continue
        if s.error is not None:
            statuses.append(ComponentStatus(state="unknown", detail=s.error, **base))
            continue
        if s.not_served is not None:
            # Held back until the provider serves a run: never due, no attempt counted.
            hold_s = s.not_served_retry_s
            if hold_s is not None and p.kind in triggers:
                waits.append(now + timedelta(seconds=hold_s))
            statuses.append(
                ComponentStatus(
                    state="waiting",
                    detail=f"not served yet: {s.not_served}",
                    retry_after_s=hold_s,
                    **base,
                )
            )
            continue
        if not s.pending:
            statuses.append(ComponentStatus(state="current", detail=s.current_detail, **base))
            continue
        first = first_seen.get(id(s)) or now
        attempts, last = known.attempts.get(_key(s), (0, None))
        state: ComponentState
        detail: str | None
        retry_after: float | None = None
        if attempts >= settings.sync_max_attempts:
            state = "failed"
            detail = (
                f"{attempts} failed attempts for {s.key}; waiting for the next run "
                "(SYNC_MAX_ATTEMPTS)"
            )
        elif last is not None and now < last + retry:
            state = "waiting"
            retry_after = (last + retry - now).total_seconds()
            detail = f"attempt {attempts} failed; next attempt after {_hhmm(last + retry)}"
            if p.kind in triggers:
                waits.append(last + retry)
        else:
            start = first
            missing: list[str] = []
            if not s.complete:
                missing.append("model domains still on an older cycle")
            if in_anchor(s):
                assert s.cycle is not None
                for q in partners(s):
                    if q.pending and q.cycle == s.cycle and first_seen.get(id(q)):
                        start = min(start, first_seen[id(q)] or start)
                    if not matched(q, s.cycle):
                        missing.append(f"{q.probe.source_code} {s.cycle:%Y-%m-%d %H}Z")
            if not missing:
                state, detail = "new", None
            elif now >= start + window:
                state = "new"
                detail = f"waited {settings.sync_coalesce_minutes} min for {', '.join(missing)}"
            else:
                state = "waiting"
                detail = f"waiting for {', '.join(missing)} until {_hhmm(start + window)}"
                if p.kind in triggers:
                    waits.append(start + window)
        statuses.append(
            ComponentStatus(
                state=state,
                detail=detail,
                first_seen_at=first,
                retry_after_s=retry_after,
                attempts=attempts,
                **base,
            )
        )

    triggered = [c for c in statuses if c.state == "new" and c.kind in triggers]
    due = force or bool(triggered)
    if running is not None and not force:
        statuses = [
            replace(c, state="in_progress", detail=f"sync {running.id} is running")
            if c.state == "new"
            else c
            for c in statuses
        ]
        reasons.append(f"sync {running.id} is running ({running.holder or 'unknown holder'})")
        due = False
    if force:
        reasons.append("forced")
    if fallback:
        reasons.append(fallback)
    for c in statuses:
        if c.state == "new":
            trigger = "" if c.kind in triggers else " (not a trigger)"
            reasons.append(f"{c.source_code}: new run {c.available_run_key}{trigger}")
        elif c.state in ("waiting", "failed", "unknown") and c.detail:
            reasons.append(f"{c.source_code}: {c.detail}")
    if not reasons:
        reasons.append("every component is current")
    anchor_codes = {s.probe.source_code for s in anchor_spots} if anchored else set()
    if anchored and map_seen is not None:
        anchor_codes.add(map_seen.probe.source_code)
    anchor = _anchor_cycle(statuses, anchor_codes, settings)
    return SyncPlan(
        checked_at=now,
        due=due,
        components=tuple(statuses),
        anchor_cycle=anchor,
        reasons=tuple(reasons),
        waiting_until=min(waits) if waits and not due else None,
        forced=force,
    )


def _is_map_cycle(settings: Settings, cycle: datetime) -> bool:
    from app.services.map_fields import is_map_cycle

    return is_map_cycle(settings, cycle)


def _anchor_cycle(
    statuses: Iterable[ComponentStatus], anchor_codes: set[str], settings: Settings
) -> datetime | None:
    """The GFS cycle a sync of this plan acquires the map layers (or else the GFS spot run
    of a map cycle) for; None when no anchor component (map, GFS-family spots) is new."""
    new = [
        c
        for c in statuses
        if c.state == "new" and c.available_cycle is not None and c.source_code in anchor_codes
    ]
    maps = [c.available_cycle for c in new if c.kind == "map"]
    if maps:
        return max(c for c in maps if c is not None)
    spots = [
        c.available_cycle
        for c in new
        if c.kind == "spots"
        and c.available_cycle is not None
        and _is_map_cycle(settings, c.available_cycle)
    ]
    return max(c for c in spots if c is not None) if spots else None


def promote_pending(plan: SyncPlan) -> SyncPlan:
    """``plan`` with every component that has a newer run than stored (waiting for a
    partner or a retry, or given up after SYNC_MAX_ATTEMPTS) made ``new`` and due: what
    ``sync run`` (without --if-due) acquires."""
    held = ("waiting", "failed")
    promoted = tuple(
        replace(c, state="new", detail=f"run now ({c.detail})" if c.detail else "run now")
        if c.state in held and c.available_run_key is not None
        else c
        for c in plan.components
    )
    due = plan.forced or any(c.state == "new" for c in promoted)
    reasons = [
        f"{c.source_code}: new run {c.available_run_key} (run now)"
        for c, old in zip(promoted, plan.components, strict=True)
        if c.state == "new" and old.state != "new"
    ]
    return replace(
        plan,
        components=promoted,
        due=due,
        reasons=(*plan.reasons, *reasons),
        waiting_until=None if due else plan.waiting_until,
    )


# --------------------------------------------------------------------------- outcomes
def settle_plan(plan: SyncPlan, result: SyncResult) -> SyncPlan:
    """The plan as a sync on it left the components (the daemon's heartbeat after a sync,
    without asking the providers again): a component whose newest available run the sync
    stored, or found stored, is current; one whose acquisition failed is failed (the next
    check says when it is retried); one the provider stopped serving unmixed waits. Any
    other component keeps its state, and ``due`` and the reasons stay the check's."""
    outcomes: dict[str, dict[str, Any]] = {}
    for stage in result.stages:
        for comp in stage.detail.get("components", []) or []:
            if isinstance(comp, dict) and comp.get("source_code"):
                outcomes[str(comp["source_code"])] = comp
    settled: list[ComponentStatus] = []
    for c in plan.components:
        comp = outcomes.get(c.source_code)
        outcome = comp.get("outcome") if comp else None
        sync = f"sync {result.sync_run_id}"
        if comp is not None and outcome in ("stored", "current"):
            c = replace(
                c,
                state="current",
                stored_run_key=comp.get("run_key") or c.available_run_key,
                detail=f"stored by {sync}" if outcome == "stored" else None,
                retry_after_s=None,
                attempts=0,
            )
        elif outcome == "failed":
            message = redact(str((comp or {}).get("message") or ""))[:200]
            c = replace(c, state="failed", detail=f"{sync} failed: {message}".rstrip(": "))
        elif outcome == "deferred":
            c = replace(c, state="waiting", detail=f"not served unmixed at {sync}")
        settled.append(c)
    return replace(plan, components=tuple(settled))


def apply_result(known: KnownRuns, result: SyncResult, now: datetime | None = None) -> None:
    """Record a sync's acquisition outcomes in ``known``.

    Each acquisition stage lists ``detail["components"]``: ``{source_code, run_key,
    outcome: stored | current | deferred | failed, retry_after_s}``. A stored or current
    run clears the source's first sighting and attempts; a failure adds an attempt; a
    deferred run (not served unmixed at acquisition time) changes nothing. The retry waits
    SYNC_RETRY_MINUTES after the attempt, or longer when the provider asked for it (the
    attempt time is then moved forward by the extra wait).
    """
    now = now or utcnow()
    retry_s = get_settings().sync_retry_minutes * 60
    for stage in result.stages:
        for comp in stage.detail.get("components", []) or []:
            source, run_key = comp.get("source_code"), comp.get("run_key")
            if not source or not run_key:
                continue
            key = known_key(str(source), str(run_key))
            outcome = comp.get("outcome")
            if outcome in ("stored", "current"):
                known.attempts.pop(key, None)
                for k in [k for k in known.first_seen if k.startswith(f"{source}|")]:
                    del known.first_seen[k]
            elif outcome == "failed":
                count = known.attempts.get(key, (0, now))[0]
                extra = max(0.0, float(comp.get("retry_after_s") or 0.0) - retry_s)
                known.attempts[key] = (count + 1, now + timedelta(seconds=extra))
