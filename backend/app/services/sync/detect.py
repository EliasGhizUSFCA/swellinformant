"""Model-run detection: is a sync due? (docs/sync.md).

* :func:`load_known` reads, per source and per component, the run key that is stored *and
  published*: the published forecast version's component runs for its source (design D7),
  else the newest run that completed before the newest publication (a run stored after it
  counts as not stored yet, so a sync that stopped before publishing is finished by the next
  one); the cycle of the published map run; and the planned retry of the spots that failed
  in the published version (:class:`~app.services.sync.types.SpotRetry`, design D9).
* :func:`plan_sync` asks each component's provider for its newest servable run, with
  metadata only (Open-Meteo ``meta.json`` with its availability wait, NOAA NODD ``.idx``
  HEAD requests, Windy's sentinel fingerprint), and applies the rules of docs/sync.md:
  triggers (SYNC_TRIGGER_SOURCES), coalescing within a component (SYNC_COALESCE_MINUTES),
  retries (SYNC_RETRY_MINUTES, SYNC_MAX_ATTEMPTS; the failed spots 5/15/45 minutes apart)
  and ``force``. With ``db=None`` it works from ``known`` alone: an idle check needs no
  database. Provider errors never raise; the component is ``unknown``.
* :func:`apply_result` records a sync's outcome per component in ``known`` (failed
  attempts, first sightings), :func:`refresh_known` reloads the stored keys after a sync,
  :func:`promote_pending` turns held-back components into due ones (``sync run``) and
  :func:`limit_plan` restricts a plan to the component kinds one runner syncs (the refresh
  endpoint: spots; the daemon: SYNC_DAEMON_KINDS).

Components (:class:`Probe`): the spot forecasts of each FORECAST_PROVIDERS token (the point
forecasts of the active spots), split into ``wave`` and ``atmosphere`` when the provider
names a run key per part (``RunInfo.component_keys``) and one provider is configured (with
several, the blend path, a source is one component: design D5); ``map`` (the world map
layers, source ``noaa_gfs_fields``, in the standalone map-fields mode) and ``ensemble``
(GEFS-Wave, when GEFS_ENABLED). The provider modules are imported when the probes are built,
never at import time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.core.logging import redact
from app.models import ForecastRun, ForecastSource, SurfSpot
from app.models.enums import ForecastRunStatus
from app.models.sync import SyncRun
from app.services.forecasts.base import ATMOSPHERE, WAVE, RunInfo
from app.services.forecasts.carry import (
    domain_cycles,
    domains_behind,
    newest_cycle,
    older_than_stored,
    run_component_keys,
)
from app.services.sync.types import (
    ComponentKind,
    ComponentState,
    ComponentStatus,
    KnownRuns,
    SpotRetry,
    SyncPlan,
    SyncResult,
    known_key,
    stored_key,
)

__all__ = [
    "PartlyServedRun",
    "Probe",
    "apply_result",
    "build_probes",
    "component_split",
    "domain_cycles",
    "domains_behind",
    "empty_known",
    "limit_plan",
    "load_known",
    "newest_cycle",
    "plan_sync",
    "promote_pending",
    "refresh_known",
    "servable_run",
    "settle_plan",
    "valid_lease",
]

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
# The order the two parts of a spot source are listed in.
SPOT_COMPONENTS = (WAVE, ATMOSPHERE)
RETRY_SUFFIX = "#retry"  # app.services.forecasts.ingestion.RETRY_SUFFIX


def component_split(settings: Settings) -> bool:
    """Whether spot sources are planned per component (design D6): with one provider. With
    several (FORECAST_PROVIDERS, the blend path) a source is one component (design D5)."""
    return len(settings.forecast_provider_list) == 1


@dataclass(frozen=True)
class PartlyServedRun(RunInfo):
    """A provider's run of which some components are not served unmixed yet (e.g. Open-Meteo
    publishing a new wave run while the atmosphere is current): ``not_served`` holds why, by
    component; the other components are planned as usual."""

    not_served: Mapping[str, Exception] = field(default_factory=lambda: MappingProxyType({}))


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
        from app.services.http import RunNotServedError

        metas = provider.read_metas()
        provider.read_metas = lambda: metas  # shadows the method for this check only
        try:
            run: RunInfo = provider.current_run(now)
            try:
                provider.ensure_servable(run, now)
                return run
            except RunNotServedError:
                # Per component (D6): a wave run being published does not hold the
                # atmosphere back. Providers without components refuse the whole run.
                if not run.component_keys or not _takes_components(provider.ensure_servable):
                    raise
                refused: dict[str, Exception] = {}
                for component in sorted(run.component_keys):
                    try:
                        provider.ensure_servable(run, now, components=frozenset({component}))
                    except RunNotServedError as exc:
                        refused[component] = exc
                if len(refused) == len(run.component_keys):
                    raise
                values = {f.name: getattr(run, f.name) for f in fields(run)}
                return PartlyServedRun(**values, not_served=MappingProxyType(refused))
        finally:
            del provider.read_metas

    return current_run


def _takes_components(fn: Callable[..., Any]) -> bool:
    import inspect

    try:
        return "components" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


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
        with_simulated_runs(provider)
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


def with_simulated_runs[P](provider: P) -> P:
    """The demo provider with the simulated model runs of every process (developer tool
    ``POST /api/dev/simulate-model-run``, the advances in Redis); any other provider as it
    is."""
    from app.services.forecasts.demo import DemoForecastProvider, load_advances

    if isinstance(provider, DemoForecastProvider) and not provider.run_key_override:
        provider.advances = load_advances()
    return provider


# --------------------------------------------------------------------------- known runs
def _latest_publication_at(db: Session) -> datetime | None:
    return db.scalar(select(func.max(SyncRun.published_at)))


def _not_simulated() -> Any:
    # simulate-swell's demo runs (details.simulated) are never a provider's current run.
    return ForecastRun.details["simulated"].astext.is_distinct_from("true")


def load_known(db: Session) -> KnownRuns:
    """Stored run keys, the published map cycle and the spot retries, from the database.

    ``stored``: per source (blend excluded), the newest usable (success or partial),
    non-simulated run that completed before the newest publication (its run key, and its
    component keys under ``'<source>:<component>'``); for the published forecast version's
    source, the version's own run and component keys instead (design D7: what is served).
    Before the first publication nothing counts as stored, so the first sync publishes
    everything. ``map_cycle``: the cycle of the newest publication's map run. ``retries``:
    the planned retry of the spots that failed in the published version.
    """
    from app.services.forecasts.versions import component_runs_of, current_version

    published_at = _latest_publication_at(db)
    stored: dict[str, str] = {}
    map_cycle: datetime | None = None
    if published_at is not None:
        rows = db.execute(
            select(ForecastSource.code, ForecastRun.run_key, ForecastRun.details["component_keys"])
            .join(ForecastSource, ForecastRun.source_id == ForecastSource.id)
            .where(
                ForecastRun.status.in_(USABLE),
                ForecastSource.code != BLEND_CODE,
                ForecastRun.completed_at <= published_at,
                _not_simulated(),
                ForecastRun.run_key.not_like(f"%{RETRY_SUFFIX}%"),
            )
            .ext(distinct_on(ForecastRun.source_id))
            .order_by(ForecastRun.source_id, ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        ).all()
        for code, key, keys in rows:
            stored[str(code)] = str(key)
            for component, value in run_component_keys({"component_keys": keys}).items():
                stored[stored_key(str(code), component)] = value
        map_run = db.execute(
            select(ForecastRun.wave_model_run_at, ForecastRun.issued_at)
            .join(SyncRun, SyncRun.map_run_id == ForecastRun.id)
            .where(SyncRun.published_at.is_not(None))
            .order_by(SyncRun.published_at.desc(), SyncRun.id.desc())
            .limit(1)
        ).first()
        if map_run is not None:
            map_cycle = map_run[0] or map_run[1]
    version = current_version(db)
    retries: dict[str, SpotRetry] = {}
    if version is not None and version.source_code:
        code = version.source_code
        run_key, simulated = db.execute(
            select(ForecastRun.run_key, ForecastRun.details["simulated"].astext).where(
                ForecastRun.id == version.forecast_run_id
            )
        ).first() or (None, None)
        components = component_runs_of(version)
        # A simulated demo run (simulate-swell) is never the stored run: its key is never a
        # provider's (the legacy rule above skips it too).
        if run_key is not None and simulated != "true":
            for key in [k for k in stored if k == code or k.startswith(f"{code}:")]:
                del stored[key]
            stored[code] = str(run_key).split(RETRY_SUFFIX)[0]
            for component, comp in components.items():
                if comp.run_key:
                    stored[stored_key(code, component)] = comp.run_key
        retry = _spot_retry(db, version, stored.get(code))
        if retry is not None:
            retries[code] = retry
    return KnownRuns(
        stored=stored,
        map_cycle=map_cycle,
        first_seen={},
        attempts={},
        loaded_at=utcnow(),
        retries=retries,
    )


def _spot_retry(db: Session, version: Any, base_key: str | None) -> SpotRetry | None:
    """The retry of the active spots that failed in ``version`` (stale or missing with an
    error), counted by the retry runs stored for its run key (``<key>#retry<n>``)."""
    from app.services.forecasts.versions import MISSING, STALE, component_runs_of, spot_runs_of

    if not base_key or not version.source_code:
        return None
    failed = {
        spot_id
        for spot_id, entry in spot_runs_of(version).items()
        if entry.status in (STALE, MISSING) and entry.error
    }
    if not failed:
        return None
    active = set(
        db.scalars(select(SurfSpot.id).where(SurfSpot.is_active, SurfSpot.id.in_(failed))).all()
    )
    if not active:
        return None
    count, last = db.execute(
        select(func.count(ForecastRun.id), func.max(ForecastRun.started_at))
        .join(ForecastSource, ForecastRun.source_id == ForecastSource.id)
        .where(
            ForecastSource.code == version.source_code,
            ForecastRun.run_key.like(f"{base_key}{RETRY_SUFFIX}%"),
        )
    ).one()
    staged = version.staged_at or version.created_at or utcnow()
    changed = tuple(sorted(c for c, comp in component_runs_of(version).items() if comp.changed))
    return SpotRetry(
        source_code=str(version.source_code),
        run_key=base_key,
        version_id=int(version.id),
        spot_ids=tuple(sorted(active)),
        retries=int(count or 0),
        last_attempt_at=max(t for t in (staged, last) if t is not None),
        components=changed,
    )


def empty_known(now: datetime | None = None) -> KnownRuns:
    """Nothing known: every available run counts as new until the stored keys are loaded."""
    return KnownRuns(
        stored={}, map_cycle=None, first_seen={}, attempts={}, loaded_at=now or utcnow()
    )


def refresh_known(db: Session, known: KnownRuns | None) -> KnownRuns:
    """``known`` with ``stored``, ``map_cycle``, ``retries`` and ``loaded_at`` reloaded from
    the database (first sightings and attempts are kept); a fresh :func:`load_known` when
    None."""
    fresh = load_known(db)
    if known is None:
        return fresh
    known.stored, known.map_cycle, known.loaded_at = fresh.stored, fresh.map_cycle, fresh.loaded_at
    known.retries = fresh.retries
    return known


# --------------------------------------------------------------------------- planning
@dataclass
class _Seen:
    """One probed component before the rules are applied (``component`` None: a whole
    source, the map or the ensemble)."""

    probe: Probe
    component: str | None = None
    run: RunInfo | None = None
    available: str | None = None  # the component's available run key
    error: str | None = None
    stored: str | None = None
    cycle: datetime | None = None  # newest model init of the available run
    complete: bool = True  # every model domain of the component is on ``cycle``
    stored_cycle: datetime | None = None
    pending: bool = False  # a newer run than the stored one is available
    current_detail: str | None = None
    # The provider labels a newer run but does not serve it unmixed yet (RunNotServedError):
    # the reason, and when to look again (None: at the next check).
    not_served: str | None = None
    not_served_retry_s: float | None = None

    @property
    def key(self) -> str | None:
        return self.available

    @property
    def skey(self) -> str:
        return stored_key(self.probe.source_code, self.component)


def _inits(run: RunInfo, component: str | None = None) -> list[datetime]:
    refs = [r for r in run.model_runs if component is None or r.component in (None, component)]
    if refs:
        return [r.init_time for r in refs]
    if component == WAVE and run.wave_model_run_at is not None:
        return [run.wave_model_run_at]
    if component == ATMOSPHERE and run.atmosphere_model_run_at is not None:
        return [run.atmosphere_model_run_at]
    return [run.issued_at]


def _judge(seen: _Seen, known: KnownRuns) -> _Seen:
    """New, or current (the stored run, or an older one: monotonic detection)."""
    if seen.available == seen.stored:
        return seen
    assert seen.cycle is not None
    if (seen.stored_cycle is not None and seen.cycle < seen.stored_cycle) or older_than_stored(
        seen.available, seen.stored
    ):
        seen.current_detail = (
            f"the provider serves {seen.available}, older than the stored run; nothing to fetch"
        )
        return seen
    if seen.probe.kind == "map" and known.map_cycle is not None and seen.cycle <= known.map_cycle:
        seen.current_detail = f"the map layers of {seen.cycle:%Y-%m-%d %H}Z are published"
        return seen
    seen.pending = True
    return seen


def _observe(probe: Probe, known: KnownRuns, now: datetime, split: bool = False) -> list[_Seen]:
    """The probe's component(s): the wave and atmosphere parts of a spot source whose
    provider names a key per part (with ``split``), else the source as one component."""
    seen = _Seen(probe, stored=known.stored.get(probe.source_code))
    seen.stored_cycle = newest_cycle(seen.stored)
    if probe.kind == "map" and known.map_cycle is not None:
        seen.stored_cycle = max(filter(None, (seen.stored_cycle, known.map_cycle)))
    if probe.disabled is not None:
        return [seen]
    if probe.error is not None or probe.current_run is None:
        seen.error = probe.error or "no run discovery"
        return [seen]
    from app.services.http import RunNotServedError

    try:
        run = probe.current_run(now)
    except RunNotServedError as exc:
        # Something newer than the stored run exists, but it cannot be read yet (Open-Meteo
        # publishing a run, or serving a run this source does not read).
        seen.not_served = _error_detail(exc)
        seen.not_served_retry_s = exc.retry_after_s
        return [seen]
    except Exception as exc:  # metadata outage, no complete cycle yet, a bug: unknown
        seen.error = _error_detail(exc)
        logger.info("Sync check: %s run unknown: %s", probe.source_code, seen.error)
        return [seen]
    if not (split and probe.kind == "spots" and run.component_keys):
        seen.run, seen.available = run, run.run_key
        inits = _inits(run)
        seen.cycle, seen.complete = max(inits), min(inits) == max(inits)
        return [_judge(seen, known)]
    refused: Mapping[str, Exception] = getattr(run, "not_served", None) or {}
    whole = known.stored.get(probe.source_code)
    out: list[_Seen] = []
    for component in SPOT_COMPONENTS:
        part = _Seen(
            probe,
            component=component,
            run=run,
            available=str(run.component_keys.get(component) or run.run_key),
            stored=known.stored.get(stored_key(probe.source_code, component)),
        )
        if part.stored is None and whole is not None and whole == run.run_key:
            part.stored = part.available  # stored before component keys were recorded
        part.stored_cycle = newest_cycle(part.stored)
        inits = _inits(run, component)
        part.cycle, part.complete = max(inits), min(inits) == max(inits)
        why = refused.get(component)
        if why is not None:
            part.not_served = _error_detail(why)
            part.not_served_retry_s = getattr(why, "retry_after_s", None)
            out.append(part)
            continue
        out.append(_judge(part, known))
    return out


def _track_first_seen(seen: _Seen, known: KnownRuns, now: datetime) -> datetime | None:
    """Keep one first sighting per component: when anything newer than the stored run was
    first seen (carried over when the available run key changes again before a sync)."""
    if seen.error is not None or seen.probe.disabled is not None or seen.not_served:
        return None  # nothing servable observed: keep what is known
    prefix = f"{seen.skey}|"
    entries = {k: t for k, t in known.first_seen.items() if k.startswith(prefix)}
    for k in entries:
        del known.first_seen[k]
    stale = [k for k in known.attempts if k.startswith(prefix) and k != _key(seen)]
    for k in stale:
        del known.attempts[k]
    if not seen.pending or seen.key is None:
        return None
    first = min(entries.values(), default=now)
    known.first_seen[known_key(seen.skey, seen.key)] = first
    return first


def _key(seen: _Seen) -> str:
    return known_key(seen.skey, seen.key or "")


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
    split = component_split(settings)
    try:
        seen = [part for p in probes for part in _observe(p, known, now, split)]
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

    statuses: list[ComponentStatus] = []
    reasons: list[str] = []
    waits: list[datetime] = []
    for s in seen:
        p = s.probe
        base: dict[str, Any] = {
            "kind": p.kind,
            "source_code": p.source_code,
            "provider": p.provider,
            "component": s.component,
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
            spot_retry = _retry_for(s, known)
            if spot_retry is None:
                statuses.append(ComponentStatus(state="current", detail=s.current_detail, **base))
                continue
            status, wait = _retry_status(spot_retry, settings, now, base)
            if wait is not None and p.kind in triggers:
                waits.append(wait)
            statuses.append(status)
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
        elif s.complete:
            state, detail = "new", None
        elif now >= first + window:
            # Coalescing within the component only (design D6): its model domains wait for
            # each other up to SYNC_COALESCE_MINUTES (0 by default: no wait).
            state = "new"
            detail = (
                f"waited {settings.sync_coalesce_minutes} min for model domains still on an "
                "older cycle"
                if settings.sync_coalesce_minutes
                else None
            )
        else:
            state = "waiting"
            detail = (
                f"waiting for model domains still on an older cycle until {_hhmm(first + window)}"
            )
            if p.kind in triggers:
                waits.append(first + window)
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
        name = c.source_code if c.component is None else f"{c.source_code} {c.component}"
        if c.state == "new":
            trigger = "" if c.kind in triggers else " (not a trigger)"
            what = (
                f"retry of {len(c.retry_spot_ids)} failed spots"
                if c.retry_spot_ids
                else f"new run {c.available_run_key}"
            )
            reasons.append(f"{name}: {what}{trigger}")
        elif c.state in ("waiting", "failed", "unknown") and c.detail:
            reasons.append(f"{name}: {c.detail}")
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
        triggers=frozenset(triggers),
    )


def _retry_for(seen: _Seen, known: KnownRuns) -> SpotRetry | None:
    """The planned retry of failed spots that belongs to this current component: the
    source's retry while its stored run is still the one that failed them, on the
    components that refresh fetched (both for a whole source)."""
    if seen.probe.kind != "spots":
        return None
    spot_retry = known.retries.get(seen.probe.source_code)
    if spot_retry is None or not spot_retry.spot_ids:
        return None
    if known.stored.get(seen.probe.source_code) not in (None, spot_retry.run_key):
        return None
    if seen.component is not None:
        owners = spot_retry.components or SPOT_COMPONENTS
        # One entry per source carries the retry: the first component it fetched.
        if seen.component != next(c for c in SPOT_COMPONENTS if c in owners):
            return None
    return spot_retry


def _retry_status(
    spot_retry: SpotRetry, settings: Settings, now: datetime, base: dict[str, Any]
) -> tuple[ComponentStatus, datetime | None]:
    """The state of a retry of failed spots (design D9): due after 5, 15, then 45 minutes,
    at most SYNC_MAX_ATTEMPTS retries per run."""
    n = len(spot_retry.spot_ids)
    ids = spot_retry.spot_ids
    done = spot_retry.retries
    common = {"retry_spot_ids": ids, "attempts": done, **base}
    if done >= settings.sync_max_attempts:
        detail = (
            f"{n} spots failed in version {spot_retry.version_id}; {done} retries made, "
            "waiting for the next run (SYNC_MAX_ATTEMPTS)"
        )
        return ComponentStatus(state="failed", detail=detail, **common), None
    due_at = spot_retry.due_at()
    if now < due_at:
        detail = f"retry {done + 1} of {n} failed spots after {_hhmm(due_at)}"
        wait = (due_at - now).total_seconds()
        status = ComponentStatus(state="waiting", detail=detail, retry_after_s=wait, **common)
        return status, due_at
    detail = f"retry {done + 1} of {n} failed spots"
    return ComponentStatus(state="new", detail=detail, **common), None


def limit_plan(
    plan: SyncPlan, kinds: Collection[str], settings: Settings | None = None
) -> SyncPlan:
    """``plan`` as one runner sees it that syncs only these component kinds (the refresh
    endpoint: ``spots``; the daemon: SYNC_DAEMON_KINDS): due when one of its kinds has a new
    component that triggers (SYNC_TRIGGER_SOURCES; when none of its kinds is a trigger,
    its own kinds trigger), or when forced. The components stay as planned."""
    settings = settings or get_settings()
    wanted = frozenset(kinds)
    triggers = (plan.triggers or settings.sync_trigger_list) & wanted or wanted
    running = any(c.state == "in_progress" for c in plan.components)
    due = not running and (
        plan.forced or any(c.state == "new" and c.kind in triggers for c in plan.components)
    )
    others = sorted({c.kind for c in plan.components if c.state == "new"} - wanted)
    reasons = list(plan.reasons)
    if others:
        reasons.append(
            f"{', '.join(others)}: synced by another runner (this one syncs "
            f"{','.join(sorted(wanted))})"
        )
    return replace(
        plan,
        due=due,
        reasons=tuple(reasons),
        waiting_until=plan.waiting_until if not due else None,
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
    outcomes: dict[tuple[str, str | None], dict[str, Any]] = {}
    for stage in result.stages:
        for comp in stage.detail.get("components", []) or []:
            if isinstance(comp, dict) and comp.get("source_code"):
                outcomes[(str(comp["source_code"]), comp.get("component"))] = comp
    settled: list[ComponentStatus] = []
    for c in plan.components:
        comp = outcomes.get((c.source_code, c.component)) or outcomes.get((c.source_code, None))
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
                retry_spot_ids=(),
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

    Each acquisition stage lists ``detail["components"]``: ``{source_code, component,
    run_key, outcome: stored | current | deferred | failed, retry_after_s}``. A stored or
    current run clears the component's first sighting and attempts; a failure adds an
    attempt; a deferred run (not served unmixed at acquisition time) changes nothing. The
    retry waits SYNC_RETRY_MINUTES after the attempt, or longer when the provider asked for
    it (the attempt time is then moved forward by the extra wait). A retry of failed spots
    (``retry``) is counted in the database instead (its retry run), never here.
    """
    now = now or utcnow()
    retry_s = get_settings().sync_retry_minutes * 60
    for stage in result.stages:
        for comp in stage.detail.get("components", []) or []:
            source, run_key = comp.get("source_code"), comp.get("run_key")
            if not source or not run_key or comp.get("retry"):
                continue
            skey = stored_key(str(source), comp.get("component"))
            key = known_key(skey, str(run_key))
            outcome = comp.get("outcome")
            if outcome in ("stored", "current"):
                known.attempts.pop(key, None)
                for k in [k for k in known.first_seen if k.startswith(f"{skey}|")]:
                    del known.first_seen[k]
            elif outcome == "failed":
                count = known.attempts.get(key, (0, now))[0]
                extra = max(0.0, float(comp.get("retry_after_s") or 0.0) - retry_s)
                known.attempts[key] = (count + 1, now + timedelta(seconds=extra))
