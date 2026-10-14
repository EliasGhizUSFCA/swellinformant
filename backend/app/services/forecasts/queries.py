"""Read helpers: which model run is authoritative for each spot, and data freshness.

Staleness is per source (docs/decisions.md ADR-015). A run is stale once it is older than

    cycle interval + publication lag + refresh interval + STALENESS_MARGIN_HOURS

of its source, i.e. once the next cycle should have been published *and* fetched by the
next acquisition, with a margin. With the default settings that is 6 + 5 + 3 + 2 = 16 h
for NOAA GFS, 6 + 7.5 + 3 + 2 = 18.5 h for GEFS-Wave and 12 + 8 + 3 + 2 = 25 h for ECMWF
(two long cycles a day). A single FORECAST_STALE_HOURS of 18 h made every ECMWF run stale
for several hours each day (the newest 12-hourly cycle is routinely 20-23 h old before the
next one is fetched), which silently dropped ECMWF from the blend.

The Open-Meteo families follow the same rule with Open-Meteo's own publication lag (the
time between a run's initialisation and its ``last_run_availability_time`` in meta.json,
read on 2026-10-14: GFS 5.3-6.0 h, ECMWF 7.0-7.2 h) plus OPEN_METEO_AVAILABILITY_WAIT_MINUTES:
6 + 6 + 0.17 + 3 + 2 = 17.2 h for ``open_meteo_noaa`` and 12 + 7.5 + 0.17 + 3 + 2 = 24.7 h for
``open_meteo_ecmwf`` (it reads the 00Z and 12Z runs only). Sources without a known cycle
(demo, Windy) keep FORECAST_STALE_HOURS. A blend is stale when any of its members is stale
by its own source's rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import utcnow
from app.models import ForecastRun, ForecastSource
from app.models.enums import ForecastRunStatus
from app.schemas.spots import AttributionLinkOut
from app.services.forecasts.base import EXTRA_GUST_KIND
from app.services.forecasts.registry import (
    ECMWF_IFS_CODE,
    GEFS_WAVE_CODE,
    NOAA_GFS_CODE,
    OPEN_METEO_ECMWF_CODE,
    OPEN_METEO_NOAA_CODE,
    configured_source_codes,
)

USABLE = (ForecastRunStatus.SUCCESS, ForecastRunStatus.PARTIAL)
BLEND_CODE = "blend"  # app.services.forecasts.blend.BLEND_CODE (not imported: cycle)

# Hours between the model cycles each direct ingester fetches (noaa.CYCLE_HOURS,
# ensemble.CYCLE_HOURS; ECMWF open data runs its long 00Z/12Z cycles, ecmwf.CYCLE_HOURS).
# Not imported from those modules because they pull in ecCodes; a test keeps them equal.
SOURCE_CYCLE_HOURS: dict[str, float] = {
    NOAA_GFS_CODE: 6.0,
    GEFS_WAVE_CODE: 6.0,
    ECMWF_IFS_CODE: 12.0,
    OPEN_METEO_NOAA_CODE: 6.0,
    OPEN_METEO_ECMWF_CODE: 12.0,  # open_meteo_ecmwf.OpenMeteoEcmwfProvider.cycle_hours
}
# Open-Meteo's publication lag (hours from initialisation to availability; meta.json of
# 2026-10-14, rounded up to the slowest domain of each family).
OPEN_METEO_LAG_HOURS: dict[str, float] = {OPEN_METEO_NOAA_CODE: 6.0, OPEN_METEO_ECMWF_CODE: 7.5}
# Slack for a slow download or a late upstream publication (heuristic).
STALENESS_MARGIN_HOURS = 2.0

# What a source's 10 m gust is when its records do not say (extra["gust_kind"]): NOAA's
# GUST:surface is an instantaneous diagnostic (it is often below the 10 m mean wind).
DEFAULT_GUST_KINDS: dict[str, str] = {NOAA_GFS_CODE: "gfs_instantaneous"}
GUST_KINDS = frozenset({"gfs_instantaneous", "ecmwf_max_3h", "ecmwf_max_since_pp", "unknown"})


def authoritative_source_codes() -> list[str]:
    """Configured sources in priority order, preceded by the blend when blending is on."""
    codes = configured_source_codes()
    if get_settings().forecast_blend_enabled and BLEND_CODE not in codes:
        return [BLEND_CODE, *codes]
    return codes


def _availability_lag_hours(source_code: str, settings: Settings) -> float:
    if source_code in OPEN_METEO_LAG_HOURS:
        wait = settings.open_meteo_availability_wait_minutes / 60.0
        return OPEN_METEO_LAG_HOURS[source_code] + wait
    return {
        NOAA_GFS_CODE: settings.noaa_availability_lag_hours,
        GEFS_WAVE_CODE: settings.gefs_availability_lag_hours,
        ECMWF_IFS_CODE: settings.ecmwf_availability_lag_hours,
    }[source_code]


def _cycle_hours(source_code: str, settings: Settings) -> float | None:
    """Longest gap between two cycles the source reads (GEFS follows GEFS_CYCLES)."""
    if source_code == GEFS_WAVE_CODE:
        hours = sorted(settings.gefs_cycle_hours)
        if hours:
            gaps = [b - a for a, b in zip(hours, hours[1:], strict=False)]
            return float(max([*gaps, hours[0] + 24 - hours[-1]]))
    return SOURCE_CYCLE_HOURS.get(source_code)


def stale_after_hours(source_code: str, settings: Settings | None = None) -> float:
    """Age (hours since the run's issue time) after which a run of this source is stale.

    Cycle interval + publication lag (the ``*_AVAILABILITY_LAG_HOURS`` settings, or
    Open-Meteo's measured lag plus OPEN_METEO_AVAILABILITY_WAIT_MINUTES) + the acquisition
    interval (``FORECAST_REFRESH_MINUTES``) + STALENESS_MARGIN_HOURS for the direct NOAA /
    GEFS / ECMWF ingesters and the two Open-Meteo families; ``FORECAST_STALE_HOURS`` for
    every other source. A blend has no rule of its own (see :func:`is_stale`).
    """
    settings = settings or get_settings()
    cycle = _cycle_hours(source_code, settings)
    if cycle is None:
        return float(settings.forecast_stale_hours)
    return (
        cycle
        + _availability_lag_hours(source_code, settings)
        + settings.forecast_refresh_minutes / 60.0
        + STALENESS_MARGIN_HOURS
    )


def _member_stale(member: dict[str, Any], now: datetime, factor: float) -> bool | None:
    """Staleness of one blend member from the run's details; None if they are incomplete."""
    code, issued = member.get("code"), member.get("issued_at")
    if not code or not issued:
        return None
    age = now - datetime.fromisoformat(str(issued))
    return age > timedelta(hours=stale_after_hours(str(code)) * factor)


def is_stale(run: ForecastRun, now: datetime | None = None, *, factor: float = 1.0) -> bool:
    """True when the run is older than its source's stale-after age (times ``factor``).

    A blend is stale as soon as one of its members is stale by its own source's rule (its
    issue time is the oldest member's, so the blend's age alone would apply the wrong
    source's limit). ``factor`` > 1 gives a more lenient check (alerts use 2).
    """
    now = now or utcnow()
    if run.source.code == BLEND_CODE:
        states = [_member_stale(m, now, factor) for m in (run.details or {}).get("members", [])]
        if states and all(s is not None for s in states):
            return any(states)
        # Older blend rows without member details: the most lenient member rule known.
        limit = max(stale_after_hours(c) for c in (*SOURCE_CYCLE_HOURS, BLEND_CODE))
        return now - run.issued_at > timedelta(hours=limit * factor)
    return now - run.issued_at > timedelta(hours=stale_after_hours(run.source.code) * factor)


def run_id_is_stale(db: Session, run_id: int | None, now: datetime | None = None) -> bool:
    """:func:`is_stale` for a run referenced by id (an event's ``latest_run_id``); False
    without a run. ``Session.get`` uses the identity map, so a list of events from a few
    runs loads each run once."""
    if run_id is None:
        return False
    run = db.get(ForecastRun, run_id)
    return run is not None and is_stale(run, now)


def gust_kind_for(source_code: str, extra: dict[str, Any] | None) -> str | None:
    """What a record's ``wind_gust_kmh`` is (values as in the ``GustKind`` schema).

    The record's own ``extra["gust_kind"]`` (Open-Meteo, ECMWF direct, Windy, blend) or the
    source's documented definition (NOAA direct); None when neither says.
    """
    kind = (extra or {}).get(EXTRA_GUST_KIND)
    if isinstance(kind, str) and kind in GUST_KINDS:
        return kind
    return DEFAULT_GUST_KINDS.get(source_code)


# Credit and licence links per source. Open-Meteo: "You must include a link next to any
# location Open-Meteo data are displayed" (open-meteo.com/en/licence); ECMWF open data:
# CC BY 4.0 with the notice "Copyright ECMWF"; NOAA NODD asks for attribution and that
# modified data are not presented as original NOAA data.
_OPEN_METEO_LINKS = (
    AttributionLinkOut(label="Weather data by Open-Meteo.com", href="https://open-meteo.com/"),
    AttributionLinkOut(
        label="Open-Meteo licence (CC BY 4.0)", href="https://open-meteo.com/en/licence"
    ),
)
_NOAA_GFS_LINK = AttributionLinkOut(
    label="NOAA NCEP GFS and GFS-Wave (modified)",
    href="https://www.nco.ncep.noaa.gov/pmb/products/gfs/",
)
_ECMWF_LINKS = (
    AttributionLinkOut(
        label="Copyright ECMWF: IFS and ecWAM open data (modified)",
        href="https://www.ecmwf.int/en/forecasts/datasets/open-data",
    ),
    AttributionLinkOut(label="CC BY 4.0", href="https://creativecommons.org/licenses/by/4.0/"),
)
_COPERNICUS_LINK = AttributionLinkOut(
    label="Generated using E.U. Copernicus Marine Service Information",
    href="https://marine.copernicus.eu/",
)
SOURCE_ATTRIBUTION_LINKS: dict[str, tuple[AttributionLinkOut, ...]] = {
    OPEN_METEO_NOAA_CODE: (*_OPEN_METEO_LINKS, _NOAA_GFS_LINK),
    OPEN_METEO_ECMWF_CODE: (*_OPEN_METEO_LINKS, *_ECMWF_LINKS),
    NOAA_GFS_CODE: (
        _NOAA_GFS_LINK,
        AttributionLinkOut(
            label="NOAA Open Data Dissemination (AWS)",
            href="https://registry.opendata.aws/noaa-gfs-bdp-pds/",
        ),
    ),
    ECMWF_IFS_CODE: _ECMWF_LINKS,
    GEFS_WAVE_CODE: (
        AttributionLinkOut(
            label="NOAA NCEP GEFS-Wave ensemble (modified)",
            href="https://registry.opendata.aws/noaa-gefs/",
        ),
    ),
    "windy_gfswave": (AttributionLinkOut(label="Windy.com", href="https://www.windy.com/"),),
}


def attribution_links_for(source: ForecastSource) -> list[AttributionLinkOut]:
    """The links one source asks for; Copernicus Marine is credited whenever its MFWAM
    wave model (Open-Meteo ``meteofrance_wave``) is among the source's models."""
    links = list(SOURCE_ATTRIBUTION_LINKS.get(source.code, ()))
    models = f"{source.wave_model or ''},{source.atmosphere_model or ''}".lower()
    if "meteofrance_wave" in models or "mfwam" in models:
        links.append(_COPERNICUS_LINK)
    return links


def source_attribution_links(db: Session, run: ForecastRun) -> list[AttributionLinkOut]:
    """Credit and licence links the run's data sources ask for (for a blend, every
    member's), shown next to the data as ``ForecastSourceOut.attribution_links``.

    The plain-text attribution (``spot_views.source_attribution``) is unchanged.
    """
    sources: list[ForecastSource] = [run.source]
    if run.source.code == BLEND_CODE:
        codes = [str(m["code"]) for m in (run.details or {}).get("members", []) if m.get("code")]
        found = {
            s.code: s
            for s in db.scalars(select(ForecastSource).where(ForecastSource.code.in_(codes)))
        }
        sources = [found[c] for c in codes if c in found]
    links: dict[tuple[str, str], AttributionLinkOut] = {}
    for source in sources:
        for link in attribution_links_for(source):
            links.setdefault((link.label, link.href), link)
    return list(links.values())


def candidate_runs(db: Session, require_predictions: bool = True) -> list[ForecastRun]:
    codes = authoritative_source_codes()
    stmt = (
        select(ForecastRun)
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(ForecastSource.code.in_(codes), ForecastRun.status.in_(USABLE))
        .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
        .limit(200)
    )
    if require_predictions:
        stmt = stmt.where(ForecastRun.predictions_computed_at.is_not(None))
    return list(db.scalars(stmt).all())


def latest_runs_by_spot(
    db: Session, now: datetime | None = None, require_predictions: bool = True
) -> dict[int, ForecastRun]:
    """Authoritative run per spot.

    Preference order: fresh runs over stale ones, then configured source priority, then
    the newest model run. A partial run only covers the spots listed in its details.
    """
    now = now or utcnow()
    codes = authoritative_source_codes()
    runs = candidate_runs(db, require_predictions)
    # A blend is only current while every member is still its source's newest usable run;
    # otherwise a newer raw run exists and the blend has not been rebuilt yet.
    newest: dict[int, int] = {}
    for r in runs:
        if r.source.code != BLEND_CODE and r.source_id not in newest:
            newest[r.source_id] = r.id
    newest_ids = set(newest.values())
    runs = [
        r
        for r in runs
        if r.source.code != BLEND_CODE
        or all(m.get("run_id") in newest_ids for m in r.details.get("members", []))
    ]
    runs.sort(
        key=lambda r: (
            is_stale(r, now),
            codes.index(r.source.code) if r.source.code in codes else 99,
            -r.issued_at.timestamp(),
            -r.id,
        )
    )
    chosen: dict[int, ForecastRun] = {}
    for run in runs:
        for spot_id in run.details.get("spot_ids", []):
            chosen.setdefault(int(spot_id), run)
    return chosen


def forecast_status(db: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or utcnow()
    codes = configured_source_codes()
    sources: list[dict[str, Any]] = []
    for code in codes:
        src = db.scalar(select(ForecastSource).where(ForecastSource.code == code))
        if src is None:
            sources.append(
                {
                    "code": code,
                    "name": code,
                    "latest_run": None,
                    "stale": True,
                    "is_demo": code == "demo",
                    "stale_after_hours": stale_after_hours(code),
                }
            )
            continue
        latest = db.scalar(
            select(ForecastRun)
            .where(ForecastRun.source_id == src.id)
            .order_by(ForecastRun.started_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        latest_ok = db.scalar(
            select(ForecastRun)
            .where(ForecastRun.source_id == src.id, ForecastRun.status.in_(USABLE))
            .order_by(ForecastRun.issued_at.desc(), ForecastRun.id.desc())
            .limit(1)
        )
        sources.append(
            {
                "code": src.code,
                "name": src.name,
                "is_demo": src.is_demo,
                "attribution": src.attribution,
                "latest_attempt": _run_summary(latest),
                "latest_successful": _run_summary(latest_ok),
                "stale": latest_ok is None or is_stale(latest_ok, now),
                "stale_after_hours": stale_after_hours(src.code),
            }
        )
    # Top-level value: the default rule (sources without a known cycle); each source
    # carries its own ``stale_after_hours``.
    return {"sources": sources, "stale_after_hours": get_settings().forecast_stale_hours}


def _run_summary(run: ForecastRun | None) -> dict[str, object] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "run_key": run.run_key,
        "status": run.status.value,
        "issued_at": run.issued_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "spot_count": run.spot_count,
        "record_count": run.record_count,
        "error": run.error_message,
    }
