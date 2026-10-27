"""Database egress of the default hosted, free-database setup (Upgrade 4 integration, round 2;
docs/map-layers.md, "Free database budget").

Each read below takes only what it uses and gives the same results as the whole-row read it
replaced; the replaced reads are kept here as verbatim reference copies:

* the blend's member forecasts (their columns and two ``extra`` keys);
* the archive's confidence inputs (only the parts its digest keeps);
* matching's own matches and events, flight discovery's matches, offers and airports, the
  alert candidates and their peak hour, and delivery's search check (one comparison of the
  whole chain: detection, matching, flights, alerts and delivery, then the same jobs an
  hour later, once with the reference reads and once with the new ones);
* /api/system/status: one statement for the job list, served from the public snapshot
  cache with the public cache headers.

The public snapshot cache's publication hook is called by a sync (after its publication and
at its end), by run_pipeline and by ``sync publish``.

Forecast values are SYNTHETIC: the varied test provider below, test_retention_bounds'
constant sea states and the DEMO provider with one simulated swell (the dataset of
tests/integration/test_egress_columns.py, whose account and saved searches are made up).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.orm import Session, object_session

from app import cli
from app.core.config import get_settings
from app.core.database import get_engine
from app.core.redis import get_redis, get_redis_bytes
from app.models import (
    Airport,
    BackgroundJobLog,
    FlightOffer,
    FlightSearch,
    ForecastComparison,
    ForecastRun,
    ForecastSource,
    Notification,
    OpportunityMatch,
    PredictionArchive,
    SavedSearch,
    SurfQualityPrediction,
    SurfSpot,
    SwellEvent,
    User,
    WaveForecast,
)
from app.models.enums import (
    JobStatus,
    MatchStatus,
    NotificationKind,
    NotificationStatus,
    SearchStatus,
)
from app.models.enums import SwellEventStatus as EventStatus
from app.services import public_cache
from app.services.flights import registry
from app.services.flights import service as flights
from app.services.flights.demo import AirportInfo, DemoFlightProvider
from app.services.forecasts import archive, blend, queries
from app.services.forecasts.base import (
    FORECAST_FIELDS,
    FetchResult,
    ForecastProvider,
    ForecastRecord,
    RunInfo,
    SourceInfo,
    SpotPoint,
)
from app.services.forecasts.ingestion import acquire_from_provider
from app.services.forecasts.queries import latest_runs_by_spot
from app.services.matching import service as matching
from app.services.notifications import alerts, delivery
from app.services.pipeline import run_pipeline
from app.services.surf_quality.predict import compute_pending_predictions
from app.services.swell_detection import service as detection
from app.services.sync import orchestrator
from app.services.sync.types import SyncResult
from tests.conftest import ApiClient
from tests.integration.test_egress_columns import (  # noqa: F401 (fixtures)
    NOW,
    _natural,
    _reset,
    dataset,
    frozen_clock,
    reference_archive_rows,
)
from tests.integration.test_public_snapshot_cache import reference_body
from tests.integration.test_retention_bounds import (  # noqa: F401 (codes is a fixture)
    SyntheticProvider,
    _ingest,
    codes,
)
from tests.sync_fixtures import NOW as SYNC_NOW
from tests.sync_fixtures import MapWorld, install_synthetic_map

pytestmark = pytest.mark.integration

@pytest.fixture(autouse=True)
def _prediction_archive_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prediction archive is optional and off by default since Swell 2.0
    (PREDICTION_ARCHIVE_ENABLED); these tests check what it archives, so it is on."""
    monkeypatch.setattr(get_settings(), "prediction_archive_enabled", True)


UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def statements() -> Iterator[list[str]]:
    seen: list[str] = []

    def capture(*args: Any) -> None:
        seen.append(str(args[2]))

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    yield seen
    event.remove(engine, "before_cursor_execute", capture)


@pytest.fixture
def result_columns() -> Iterator[list[tuple[str, list[str]]]]:
    """(statement, the names of its result columns) of every statement that returns rows."""
    seen: list[tuple[str, list[str]]] = []

    def capture(_conn: Any, cursor: Any, statement: str, *_args: Any) -> None:
        if cursor.description:
            seen.append((statement, [d[0] for d in cursor.description]))

    engine = get_engine()
    event.listen(engine, "after_cursor_execute", capture)
    yield seen
    event.remove(engine, "after_cursor_execute", capture)


# --------------------------------------------------------------------------- the blend
class VariedProvider(ForecastProvider):
    """SYNTHETIC sea states that change with the spot and the hour (a smooth deterministic
    function, not model output), every ``step`` hours with a gap the blend interpolates
    across, partitions missing at some hours, a gust of the given definition, the
    interpolated mark at some hours and extra keys the blend never reads (grid cell,
    model name), as real providers write them."""

    def __init__(
        self,
        code: str,
        family: str,
        issued: datetime,
        *,
        step: int,
        scale: float,
        period_kind: str,
        gust_kind: str | None = None,
    ) -> None:
        self.code, self.family, self.issued = code, family, issued
        self.step, self.scale, self.period_kind, self.gust_kind = (
            step,
            scale,
            period_kind,
            gust_kind,
        )

    def source_info(self) -> SourceInfo:
        return SourceInfo(
            self.code, self.code, "test", "w", "a", False, f"{self.code} data", "test", self.family
        )

    def current_run(self, now: datetime) -> RunInfo:
        return RunInfo(f"{self.code}:{self.issued:%Y%m%d%H}", self.issued, self.issued)

    def fetch(self, spots: list[SpotPoint], run: RunInfo) -> FetchResult:
        out = FetchResult()
        for p in spots:
            records = []
            for h in range(0, 60, self.step):
                if 25 <= h <= 29:  # a gap of at most 6 h between the steps around it
                    continue
                x = p.spot_id * 0.37 + h * 0.11
                values: dict[str, float | None] = {
                    "sig_wave_height_m": round(self.scale * (1.6 + 0.9 * math.sin(x)), 3),
                    "wave_period_s": round(11 + 3 * math.cos(x), 2),
                    "wave_direction_deg": round((200 + 40 * math.sin(x / 2)) % 360, 1),
                    "wind_speed_kmh": round(6 + 16 * abs(math.sin(1.3 * x)), 2),
                    "wind_direction_deg": round((350 + 7 * h) % 360, 1),
                    "pressure_msl_hpa": round(1012 + 3 * math.sin(x), 1),
                }
                if h % 12 != 6:  # partitions missing at some hours (None, never 0)
                    values |= {
                        "primary_swell_height_m": round(values["sig_wave_height_m"] * 0.9, 3),  # type: ignore[operator]
                        "primary_swell_period_s": values["wave_period_s"],
                        "primary_swell_direction_deg": values["wave_direction_deg"],
                        "wind_wave_height_m": round(self.scale * 0.4, 3),
                        "wind_wave_period_s": 5.5,
                        "wind_wave_direction_deg": values["wind_direction_deg"],
                    }
                    if h % 4 == 0:
                        values |= {
                            "secondary_swell_height_m": 0.7,
                            "secondary_swell_period_s": 8.0,
                            "secondary_swell_direction_deg": 150.0,
                        }
                if h % 15 == 0:
                    values["sea_level_m"] = 0.3
                extra: dict[str, object] = {
                    "wave_cell": {"lat": 1.0 + p.spot_id, "lon": 2.0, "distance_km": 3.5},
                    "wave_model": self.code,
                }
                if self.gust_kind is not None:
                    values["wind_gust_kmh"] = round(1.4 * values["wind_speed_kmh"], 2)  # type: ignore[operator]
                    extra["gust_kind"] = self.gust_kind
                if h % 9 == 0:
                    extra["interpolated"] = True
                records.append(
                    ForecastRecord(
                        run.issued_at + timedelta(hours=h),
                        values,
                        period_kind=self.period_kind,
                        extra=extra,
                    )
                )
            out.records[p.spot_id] = records
        return out


@pytest.fixture
def members(db: Session, few_spots: list[str], monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Two independent families (hourly GFS-like with instantaneous gusts, 3-hourly
    ECMWF-like with 3-hour maximum gusts) and a same-family copy, stored."""
    configured = ["t_gfs", "t_ecmwf", "t_gfs_copy"]
    monkeypatch.setattr(blend, "configured_source_codes", lambda: configured)
    monkeypatch.setattr(queries, "configured_source_codes", lambda: configured)
    providers = [
        VariedProvider(
            "t_gfs",
            "ncep_gfs",
            NOW - timedelta(hours=1),
            step=1,
            scale=1.0,
            period_kind="tp",
            gust_kind="gfs_instantaneous",
        ),
        VariedProvider(
            "t_ecmwf",
            "ecmwf_ifs",
            NOW - timedelta(hours=3),
            step=3,
            scale=1.2,
            period_kind="tp",
            gust_kind="ecmwf_max_3h",
        ),
        VariedProvider(
            "t_gfs_copy", "ncep_gfs", NOW - timedelta(hours=1), step=1, scale=1.3, period_kind="tp"
        ),
    ]
    for priority, provider in enumerate(providers):
        out = acquire_from_provider(db, provider, now=NOW, priority=priority)
        assert out.status == "success", out
    return configured


def reference_load_series(db: Session, member: blend.Member) -> None:
    """blend._load_series before round 2 (verbatim): every member row read whole."""
    by_spot: dict[int, list[Any]] = {}
    for row in db.scalars(
        select(WaveForecast)
        .where(WaveForecast.run_id == member.run.id)
        .order_by(WaveForecast.spot_id, WaveForecast.valid_time)
    ):
        by_spot.setdefault(row.spot_id, []).append(row)
    member.series = by_spot


def _blend_output(db: Session) -> dict[str, Any]:
    db.expire_all()
    run = db.scalar(
        select(ForecastRun)
        .join(ForecastSource, ForecastSource.id == ForecastRun.source_id)
        .where(ForecastSource.code == blend.BLEND_CODE)
    )
    assert run is not None
    forecasts = [
        {
            c.key: getattr(f, c.key)
            for c in WaveForecast.__table__.columns
            if c.key not in ("id", "run_id")
        }
        for f in db.scalars(
            select(WaveForecast)
            .where(WaveForecast.run_id == run.id)
            .order_by(WaveForecast.spot_id, WaveForecast.valid_time)
        )
    ]
    comparisons = [
        {
            c.key: getattr(f, c.key)
            for c in ForecastComparison.__table__.columns
            if c.key not in ("id", "blend_run_id")
        }
        for f in db.scalars(
            select(ForecastComparison)
            .where(ForecastComparison.blend_run_id == run.id)
            .order_by(ForecastComparison.spot_id, ForecastComparison.valid_time)
        )
    ]
    return {
        "run": (run.run_key, run.status, run.record_count, run.spot_count, run.issued_at),
        "details": run.details,
        "forecasts": forecasts,
        "comparisons": comparisons,
    }


def test_the_blend_is_unchanged_by_its_column_selective_member_reads(
    db: Session, members: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        m.setattr(blend, "_load_series", reference_load_series)
        assert blend.build_blend(db, NOW).status == "success"
    reference = _blend_output(db)
    db.execute(delete(ForecastRun).where(ForecastRun.run_key == reference["run"][0]))
    db.commit()
    assert blend.build_blend(db, NOW).status == "success"
    new = _blend_output(db)

    # What is compared covers what the reads feed: interpolated members, both gust
    # definitions, missing partitions, a same-family cross-check.
    extras = [f["extra"] for f in reference["forecasts"]]
    assert any(e.get("interpolated") for e in extras)
    assert {e.get("gust_kind") for e in extras} >= {"gfs_instantaneous"}
    assert any("gust_kinds" in e and len(e["gust_kinds"]) == 2 for e in extras)
    assert any(f["secondary_swell_height_m"] is None for f in reference["forecasts"])
    assert any(len(c["sources"]) == 3 for c in reference["comparisons"])
    assert new == reference


def test_the_blend_reads_only_the_member_columns_it_uses(
    db: Session, members: list[str], statements: list[str]
) -> None:
    statements.clear()
    assert blend.build_blend(db, NOW).status == "success"
    reads = [
        s
        for s in statements
        if s.lstrip().startswith("SELECT") and "FROM wave_forecasts" in s and "run_id =" in s
    ]
    assert len(reads) == 3, reads  # one per member and cross-check, whatever the spots
    for read in reads:
        selected = read.split("FROM wave_forecasts", 1)[0]
        for whole in ("wave_forecasts.extra,", "missing_fields", "issued_at", "wave_power_kw_m"):
            assert whole not in selected, (whole, read)
        assert "wave_forecasts.extra[" in selected  # only the two keys read
    # Every forecast field the blend combines is read.
    assert set(FORECAST_FIELDS) <= set(blend.MemberStep.__dataclass_fields__)


# --------------------------------------------------------------------------- the archive
# Confidence inputs as stored over the versions (a forecast-v1 hour, older or partial ones,
# nested parts missing or null), all SYNTHETIC.
EDGE_INPUTS: list[Any] = [
    {
        "class": "moderate",
        "rules": ["cap:low:hs_strong_disagree", "note:explained", "downgrade:local_research"],
        "reasons": ["Models disagree on the wave height."],
        "local": {"class": "low", "assessment_id": 7, "research_class": "C", "reason": "r"},
        "disagreement": {"hs_log_ratio": 0.21, "dir_diff_deg": 30.5, "tp_diff_s": 1, "x": 9},
        "ensemble": {"rel_spread": 0.12, "members": 31},
        "independent_families": 2,
        "run_change_ratio": 0.05,
        "stale": True,
        "missing": ["wind_gust_kmh"],
        "wind_dispute": {"reason": "Sources disagree about the offshore wind."},
        "notes": ["dropped by the digest"],
    },
    {"notes": ["nothing the digest keeps"]},
    {"local": None, "disagreement": None, "ensemble": None, "rules": None, "class": None},
    {"local": {"research_class": ""}, "stale": False, "missing": [], "wind_dispute": None},
    {},
]


def _set_inputs(db: Session, run_id: int) -> int:
    """Store EDGE_INPUTS, SQL NULL and a JSON null on the run's first archived hours;
    returns how many predictions were changed."""
    ids = list(
        db.scalars(
            select(SurfQualityPrediction.id)
            .where(SurfQualityPrediction.run_id == run_id)
            .order_by(SurfQualityPrediction.spot_id, SurfQualityPrediction.valid_time)
        )
    )
    targets = ids[:: max(1, len(ids) // 40)][: len(EDGE_INPUTS) + 2]
    for pred_id, inputs in zip(targets, EDGE_INPUTS, strict=False):
        db.execute(
            update(SurfQualityPrediction)
            .where(SurfQualityPrediction.id == pred_id)
            .values(confidence_inputs=inputs)
        )
    sql_null, json_null = targets[len(EDGE_INPUTS)], targets[len(EDGE_INPUTS) + 1]
    db.execute(
        text("UPDATE surf_quality_predictions SET confidence_inputs = NULL WHERE id = :i"),
        {"i": sql_null},
    )
    db.execute(
        text("UPDATE surf_quality_predictions SET confidence_inputs = 'null'::jsonb WHERE id = :i"),
        {"i": json_null},
    )
    db.commit()
    return len(targets)


def _archive_anew(db: Session) -> list[tuple[Any, ...]]:
    db.execute(delete(PredictionArchive))
    for run in {r.id: r for r in latest_runs_by_spot(db, NOW).values()}.values():
        run.details = {k: v for k, v in run.details.items() if k != "archived_spot_ids"}
    db.commit()
    assert archive.archive_authoritative(db, NOW)["archived"] > 0
    return _archived(db)


ARCHIVE_COLUMNS = [
    c.key for c in PredictionArchive.__table__.columns if c.key not in ("id", "archived_at")
]


def _archived(db: Session) -> list[tuple[Any, ...]]:
    db.expire_all()
    return sorted(
        (
            tuple(getattr(r, c) for c in ARCHIVE_COLUMNS)
            for r in db.scalars(select(PredictionArchive))
        ),
        key=repr,
    )


def _expected_archive(db: Session) -> list[tuple[Any, ...]]:
    by_run: dict[int, list[int]] = {}
    for spot_id, run in latest_runs_by_spot(db, NOW).items():
        by_run.setdefault(run.id, []).append(spot_id)
    rows = [
        r for run_id, spots in by_run.items() for r in reference_archive_rows(db, run_id, spots)
    ]
    return sorted((tuple(r[c] for c in ARCHIVE_COLUMNS) for r in rows), key=repr)


@pytest.mark.usefixtures("codes")
def test_the_archive_reads_only_the_digested_inputs_and_archives_the_same_rows(
    db: Session, result_columns: list[tuple[str, list[str]]]
) -> None:
    assert get_settings().archive_confidence_digest is True  # the default
    run = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=6)))
    assert compute_pending_predictions(db, NOW)["archived"] > 0
    assert _set_inputs(db, run.id) == len(EDGE_INPUTS) + 2
    result_columns.clear()
    stored = _archive_anew(db)
    reads = [names for sql, names in result_columns if "FROM surf_quality_predictions" in sql]
    assert reads and all("confidence_inputs" not in names for names in reads), reads
    assert any("ci_local__research_class" in names for names in reads)
    assert stored == _expected_archive(db)
    digests = {r[ARCHIVE_COLUMNS.index("confidence_inputs")] is None for r in stored}
    assert digests == {True, False}  # NULL and JSON null inputs stay None; the rest digested


@pytest.mark.usefixtures("codes")
def test_without_the_digest_the_archive_keeps_the_whole_inputs(
    db: Session, monkeypatch: pytest.MonkeyPatch, result_columns: list[tuple[str, list[str]]]
) -> None:
    monkeypatch.setattr(get_settings(), "archive_confidence_digest", False)
    run = _ingest(db, SyntheticProvider("t_gfs", "ncep_gfs", 2.0, NOW - timedelta(hours=6)))
    compute_pending_predictions(db, NOW)
    _set_inputs(db, run.id)
    result_columns.clear()
    stored = _archive_anew(db)
    reads = [names for sql, names in result_columns if "FROM surf_quality_predictions" in sql]
    assert any("confidence_inputs" in names for names in reads)
    assert stored == _expected_archive(db)
    assert any(r[ARCHIVE_COLUMNS.index("confidence_inputs")] == EDGE_INPUTS[0] for r in stored)


# --------------------------------------------------------------- matching to delivery
def reference_alert_candidates(db: Session, now: datetime) -> list[tuple[Any, Any]]:
    """The candidate query of generate_alerts before round 2 (verbatim)."""
    rows = db.execute(
        select(OpportunityMatch, ForecastRun)
        .join(SavedSearch, SavedSearch.id == OpportunityMatch.search_id)
        .join(User, User.id == OpportunityMatch.user_id)
        .join(SwellEvent, SwellEvent.id == OpportunityMatch.swell_event_id)
        .outerjoin(ForecastRun, ForecastRun.id == SwellEvent.latest_run_id)
        .where(
            SavedSearch.status == SearchStatus.ACTIVE,
            User.is_active,
            SwellEvent.status == EventStatus.ACTIVE,
            OpportunityMatch.window_start > now,
            OpportunityMatch.status.in_((MatchStatus.FLIGHT_FOUND, MatchStatus.SURF_ONLY)),
        )
    ).all()
    return [(m, r) for m, r in rows]


def reference_peak_prediction(match: OpportunityMatch) -> SurfQualityPrediction | None:
    """alerts._peak_prediction before round 2 (verbatim): the whole prediction."""
    event_ = match.swell_event
    db = object_session(match)
    if db is None or event_.latest_run_id is None:
        return None
    return db.scalar(
        select(SurfQualityPrediction).where(
            SurfQualityPrediction.run_id == event_.latest_run_id,
            SurfQualityPrediction.spot_id == event_.spot_id,
            SurfQualityPrediction.valid_time == event_.peak_time,
        )
    )


def reference_search_status(db: Session, match_id: Any) -> SearchStatus | None:
    """delivery's match check before round 2 (verbatim): the whole match."""
    match = db.get(OpportunityMatch, match_id) if match_id else None
    return None if match is None else match.search.status


def reference_directory_init(self: registry.AirportDirectory, db: Session) -> None:
    """AirportDirectory before round 2 (verbatim): whole airport rows."""
    self._rows = {
        a.iata: AirportInfo(a.iata, a.latitude, a.longitude, a.timezone)
        for a in db.scalars(select(Airport)).all()
    }


def _reference_reads(m: pytest.MonkeyPatch) -> None:
    m.setattr(matching, "MATCH_ONLY", ())
    m.setattr(matching, "EVENT_ONLY", ())
    m.setattr(flights, "MATCH_FOR_FLIGHTS", ())
    real_process = flights.process_match
    m.setattr(
        flights,
        "process_match",
        lambda db, p, d, match, now, stats, offer_rows=None: real_process(
            db, p, d, match, now, stats
        ),
    )
    m.setattr(registry.AirportDirectory, "__init__", reference_directory_init)
    m.setattr(alerts, "_alert_candidates", reference_alert_candidates)
    m.setattr(alerts, "_peak_prediction", reference_peak_prediction)
    m.setattr(alerts, "_model_runs_once", lambda db, run, memo: alerts._model_runs(db, run))
    m.setattr(delivery, "_search_status", reference_search_status)


@pytest.fixture
def frozen_flights(monkeypatch: pytest.MonkeyPatch) -> None:
    """The demo flight provider's own clock (offer expiry) frozen at the dataset's time."""
    real = DemoFlightProvider.__init__

    def init(
        self: DemoFlightProvider, lookup: Any, now: Callable[[], datetime] | None = None
    ) -> None:
        real(self, lookup, now or (lambda: NOW))

    monkeypatch.setattr(DemoFlightProvider, "__init__", init)


def _normal(value: Any) -> Any:
    """A JSON-ready copy with every UUID replaced (database ids differ between the runs)."""
    return json.loads(UUID.sub("<uuid>", json.dumps(value, default=str, sort_keys=True)))


def _chain_state(db: Session, stats: dict[str, Any]) -> dict[str, Any]:
    db.expire_all()
    events = {e.id: _natural(e) for e in db.scalars(select(SwellEvent))}
    offers = {o.id: (o.provider_offer_id, o.origin_iata) for o in db.scalars(select(FlightOffer))}
    matches = {}
    for m in db.scalars(select(OpportunityMatch)):
        key = (str(m.search_id), events[m.swell_event_id])
        values = {
            c.key: getattr(m, c.key)
            for c in OpportunityMatch.__table__.columns
            if c.key not in ("id", "created_at", "updated_at", "swell_event_id", "best_offer_id")
        }
        values["best_offer"] = offers.get(m.best_offer_id) if m.best_offer_id else None
        values["offers"] = [
            (offers[o.offer_id], o.rank, o.score, o.price_converted) for o in m.offers
        ]
        matches[repr(key)] = values
    match_keys = {
        m.id: (str(m.search_id), events[m.swell_event_id])
        for m in db.scalars(select(OpportunityMatch))
    }
    notifications = sorted(
        (
            {
                **{
                    c.key: getattr(n, c.key)
                    for c in Notification.__table__.columns
                    # dedup_key hashes the event's id; the console provider's message id
                    # is random.
                    if c.key
                    not in (
                        "id",
                        "created_at",
                        "updated_at",
                        "match_id",
                        "dedup_key",
                        "provider_message_id",
                    )
                },
                "match": repr(match_keys.get(n.match_id)),
            }
            for n in db.scalars(select(Notification))
        ),
        key=lambda v: json.dumps(_normal(v), sort_keys=True),
    )
    flight_rows = sorted(
        (
            {
                c.key: getattr(o, c.key)
                for c in FlightOffer.__table__.columns
                if c.key not in ("id", "flight_search_id", "created_at", "updated_at")
            }
            for o in db.scalars(select(FlightOffer))
        ),
        key=lambda v: json.dumps(_normal(v), sort_keys=True),
    )
    return _normal(
        {"stats": stats, "matches": matches, "notifications": notifications, "offers": flight_rows}
    )


def _run_chain(db: Session) -> dict[str, Any]:
    """Detection, matching, flights, alerts and delivery as a sync runs them, then the
    daemon's hourly jobs an hour later."""
    from app.services.flights.service import discover_flights
    from app.services.notifications.alerts import generate_alerts
    from app.services.notifications.delivery import deliver_pending

    later = NOW + timedelta(hours=1)
    stats = {
        "detect": detection.detect_events(db, NOW).as_dict(),
        "match": matching.match_all(db, NOW).as_dict(),
        "flights": discover_flights(db, NOW).as_dict(),
        "alerts": generate_alerts(db, NOW).as_dict(),
        "deliver": deliver_pending(db, NOW).as_dict(),
        "match_later": matching.match_all(db, later).as_dict(),
        "flights_later": discover_flights(db, later).as_dict(),
        "alerts_later": generate_alerts(db, later).as_dict(),
    }
    return _chain_state(db, stats)


def _reset_chain(db: Session) -> None:
    db.execute(delete(Notification))
    _reset(db)
    db.execute(delete(FlightSearch))
    db.commit()
    get_redis().flushdb()  # the flight quota and alert counters


@pytest.mark.usefixtures("dataset", "frozen_clock")
def test_matching_flights_alerts_and_delivery_are_unchanged_by_their_narrow_reads(
    db: Session, frozen_flights: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as m:
        _reference_reads(m)
        reference = _run_chain(db)
    _reset_chain(db)
    new = _run_chain(db)

    # The chain exercises what is compared: matches with flights and without, alerts
    # queued and delivered, offers revalidated and shared between searches.
    statuses = {v["status"] for v in reference["matches"].values()}
    assert {"flight_found"} <= statuses, statuses
    assert reference["stats"]["alerts"]["queued"] > 0
    assert reference["stats"]["deliver"]["sent"] > 0
    assert reference["stats"]["flights"]["cache_hits"] > 0
    assert new == reference


@pytest.mark.usefixtures("dataset", "frozen_clock")
def test_the_alert_candidates_and_peak_hours_match_the_whole_rows(
    db: Session, frozen_flights: None
) -> None:
    from app.services.flights.service import discover_flights

    detection.detect_events(db, NOW)
    matching.match_all(db, NOW)
    discover_flights(db, NOW)
    db.expire_all()
    candidates = alerts._alert_candidates(db, NOW)
    reference = reference_alert_candidates(db, NOW)
    assert candidates and sorted((m.id, r.id if r else None) for m, r in candidates) == sorted(
        (m.id, r.id if r else None) for m, r in reference
    )
    memo: dict[int, dict[str, Any]] = {}
    peaks = 0
    for match, _run in candidates:
        new = alerts.build_snapshot(
            match, NotificationKind.NEW_OPPORTUNITY, NOW, model_runs_memo=memo
        )
        whole = reference_peak_prediction(match)
        peaks += whole is not None
        assert alerts._peak_range(alerts._peak_prediction(match)) == alerts._peak_range(whole)  # type: ignore[arg-type]
        assert alerts._peak_confidence(
            alerts._peak_prediction(match), NOW
        ) == alerts._peak_confidence(whole, NOW)  # type: ignore[arg-type]
        assert (
            new["model_runs"] == alerts._model_runs(db, match.swell_event.latest_run)["model_runs"]
        )
    assert peaks > 0


@pytest.mark.usefixtures("dataset", "frozen_clock")
def test_the_alert_candidates_take_a_fixed_number_of_statements(
    db: Session, frozen_flights: None, statements: list[str]
) -> None:
    from app.services.flights.service import discover_flights

    detection.detect_events(db, NOW)
    matching.match_all(db, NOW)
    discover_flights(db, NOW)
    db.expire_all()
    statements.clear()
    candidates = alerts._alert_candidates(db, NOW)
    assert len(candidates) >= 2
    first = list(statements)
    # The event (with its spot), the best offer and the forecast run: one statement each
    # for every candidate, none joined into each candidate's row.
    assert sum("FROM swell_events" in s for s in first) == 1, first
    assert sum("FROM forecast_runs" in s for s in first) == 1, first
    assert sum("FROM flight_offers" in s for s in first) <= 1, first
    main = next(s for s in first if "FROM opportunity_matches" in s)
    assert "flight_offers" not in main and "swell_events.history" not in main
    statements.clear()
    for match, _ in candidates:
        assert match.spot is match.swell_event.spot  # the event's spot, no statement
        _ = match.best_offer
    assert statements == []


@pytest.mark.usefixtures("dataset", "frozen_clock")
def test_flight_discovery_reads_each_searchs_offers_once(
    db: Session, frozen_flights: None, statements: list[str]
) -> None:
    from app.services.flights.service import discover_flights

    detection.detect_events(db, NOW)
    matching.match_all(db, NOW)
    statements.clear()
    out = discover_flights(db, NOW)
    assert out.matches >= 2 and out.cache_hits > 0
    reads = [s for s in statements if s.lstrip().startswith("SELECT") and "FROM flight_offers" in s]
    searches = db.scalar(select(func.count()).select_from(FlightSearch))
    assert 0 < len(reads) <= int(searches or 0), (len(reads), searches)
    loaded = [
        s for s in statements if "FROM opportunity_matches" in s and "JOIN saved_searches" in s
    ]
    assert loaded and all("swell_events" not in s.split("WHERE")[0] for s in loaded)
    airports = [s for s in statements if "FROM airports" in s]
    assert len(airports) == 1 and "airports.name" not in airports[0]


@pytest.mark.usefixtures("dataset", "frozen_clock")
def test_matching_reads_its_matches_and_events_without_what_it_never_uses(
    db: Session, statements: list[str]
) -> None:
    detection.detect_events(db, NOW)
    matching.match_all(db, NOW)
    statements.clear()
    out = matching.match_all(db, NOW + timedelta(hours=1))
    assert out.updated > 0
    own = [s for s in statements if "FROM opportunity_matches" in s and "search_id IN" in s]
    assert len(own) == 1 and "JOIN" not in own[0], own
    events = [s for s in statements if s.lstrip().startswith("SELECT swell_events.")]
    assert events and all("swell_events.history" not in s for s in events), events


@pytest.mark.usefixtures("dataset", "frozen_clock")
def test_delivery_skips_a_paused_search_or_a_deleted_match(
    db: Session, frozen_flights: None
) -> None:
    from app.services.flights.service import discover_flights
    from app.services.notifications.alerts import generate_alerts
    from app.services.notifications.delivery import deliver_pending

    detection.detect_events(db, NOW)
    matching.match_all(db, NOW)
    discover_flights(db, NOW)
    assert generate_alerts(db, NOW).queued >= 2
    queued = list(
        db.scalars(select(Notification).where(Notification.status == NotificationStatus.QUEUED))
    )
    paused, deleted = queued[0], queued[1]
    match = db.get(OpportunityMatch, paused.match_id)
    assert match is not None
    db.execute(
        update(SavedSearch)
        .where(SavedSearch.id == match.search_id)
        .values(status=SearchStatus.PAUSED)
    )
    db.execute(update(Notification).where(Notification.id == deleted.id).values(match_id=None))
    db.execute(
        update(Notification)
        .where(Notification.id.notin_([paused.id, deleted.id]))
        .values(status=NotificationStatus.SKIPPED)
    )
    db.commit()
    assert delivery._search_status(db, paused.match_id) == SearchStatus.PAUSED
    assert delivery._search_status(db, None) is None
    stats = deliver_pending(db, NOW)
    assert stats.claimed == 2 and stats.skipped == 2
    db.expire_all()
    reasons = {n.id: n.last_error for n in db.scalars(select(Notification))}
    assert reasons[paused.id] == "search paused before delivery"
    assert reasons[deleted.id] == "opportunity or search was deleted"


# --------------------------------------------------------------------- /api/system/status
@pytest.fixture
def cache_clock(db: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[datetime]]:
    clock = [public_cache.public_now() + timedelta(minutes=25)]
    monkeypatch.setattr(public_cache, "utcnow", lambda: clock[0])
    public_cache.clear()
    yield clock
    public_cache.clear()


def reference_jobs(db: Session) -> list[dict[str, Any]]:
    """The job list of /api/system/status before round 2 (verbatim): one query per job."""
    from app.api.system import JOB_NAMES

    jobs = []
    for name in JOB_NAMES:
        row = db.scalar(
            select(BackgroundJobLog)
            .where(BackgroundJobLog.job_name == name)
            .order_by(BackgroundJobLog.started_at.desc())
            .limit(1)
        )
        jobs.append(
            {
                "job": name,
                "status": row.status.value if row else "never_run",
                "started_at": row.started_at.isoformat() if row else None,
                "duration_ms": row.duration_ms if row else None,
            }
        )
    return jobs


def _log(db: Session, name: str, status: JobStatus, started: datetime, ms: int) -> None:
    db.add(
        BackgroundJobLog(
            job_name=name,
            status=status,
            started_at=started,
            duration_ms=ms,
            details={"x": "y" * 500},
        )
    )
    db.commit()


def test_the_system_status_is_one_public_snapshot_per_publication_and_hour(
    app_client: ApiClient, db: Session, cache_clock: list[datetime], statements: list[str]
) -> None:
    from app.api.system import SYSTEM_STATUS, _system_status

    t0 = public_cache.public_now() - timedelta(hours=2)
    _log(db, "matching.match", JobStatus.SUCCESS, t0, 10)
    _log(db, "matching.match", JobStatus.FAILED, t0 + timedelta(minutes=30), 20)
    _log(db, "alerts.generate", JobStatus.SUCCESS, t0, 30)
    public_cache.note_published(5)

    statements.clear()
    first = app_client.get("/api/system/status")
    assert first.status_code == 200
    assert first.headers.get_list("cache-control") == ["public, max-age=60"]
    assert first.headers.get_list("cdn-cache-control") == [
        "public, s-maxage=300, stale-while-revalidate=3600"
    ]
    assert "set-cookie" not in first.headers
    computed = [s for s in statements if "background_job_logs" in s]
    assert len(computed) == 1, computed  # the job list in one statement
    body = first.json()
    assert body["jobs"] == reference_jobs(db)
    jobs = {j["job"]: j for j in body["jobs"]}
    assert (
        jobs["matching.match"]["status"] == "failed" and jobs["matching.match"]["duration_ms"] == 20
    )
    assert jobs["sync.run"]["status"] == "never_run"
    # The bytes FastAPI's own serialization gives for the same data and hour.
    hour = public_cache.public_now()
    assert first.content == reference_body(dict[str, Any], _system_status(db, hour))
    assert first.content == public_cache.render(SYSTEM_STATUS, _system_status(db, hour)).body

    # Within the hour, after the hook: answered without the database, the same bytes.
    statements.clear()
    again = app_client.get("/api/system/status")
    assert again.content == first.content and statements == []
    # A new job log shows after the next publication (a new marker) ...
    _log(db, "alerts.generate", JobStatus.FAILED, t0 + timedelta(hours=1), 40)
    assert app_client.get("/api/system/status").content == first.content
    public_cache.note_published(6)
    fresh = {j["job"]: j for j in app_client.get("/api/system/status").json()["jobs"]}
    assert fresh["alerts.generate"]["status"] == "failed"
    # ... or in the next hour.
    _log(db, "matching.match", JobStatus.SUCCESS, t0 + timedelta(hours=1, minutes=30), 50)
    cache_clock[0] = public_cache.public_now() + timedelta(hours=1)
    later = {j["job"]: j for j in app_client.get("/api/system/status").json()["jobs"]}
    assert later["matching.match"]["status"] == "success"


def test_the_system_status_job_list_takes_the_newest_inserted_of_equal_start_times(
    db: Session,
) -> None:
    from app.api.system import _latest_jobs

    t0 = public_cache.public_now() - timedelta(hours=1)
    _log(db, "sync.run", JobStatus.FAILED, t0, 1)
    _log(db, "sync.run", JobStatus.SUCCESS, t0, 2)
    jobs = {j["job"]: j for j in _latest_jobs(db)}
    assert jobs["sync.run"]["status"] == "success" and jobs["sync.run"]["duration_ms"] == 2


# --------------------------------------------------------------------- the publication hook
@pytest.fixture
def hook_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    calls: list[tuple[str, Any]] = []
    real_note, real_prewarm = public_cache.note_published, public_cache.prewarm

    def note(sync_run_id: int | None = None) -> bool:
        calls.append(("note", sync_run_id))
        return real_note(sync_run_id)

    def prewarm(db: Session) -> bool:
        calls.append(("prewarm", None))
        return real_prewarm(db)

    monkeypatch.setattr(public_cache, "note_published", note)
    monkeypatch.setattr(public_cache, "prewarm", prewarm)
    return calls


def _marker() -> str | None:
    raw = get_redis_bytes().get(public_cache.MARKER_KEY)
    return raw.decode() if isinstance(raw, bytes) else None


@pytest.fixture
def synthetic_map(monkeypatch: pytest.MonkeyPatch, few_spots: list[str]) -> MapWorld:
    return install_synthetic_map(monkeypatch)


def test_a_publishing_sync_calls_the_hook_after_its_publication_and_at_its_end(
    db: Session, synthetic_map: MapWorld, hook_calls: list[tuple[str, Any]]
) -> None:
    assert get_settings().sync_prewarm_public_cache is False  # the default
    result: SyncResult = orchestrator.run_sync(trigger="cli", now=SYNC_NOW)
    assert result.status == "success" and result.sync_run_id is not None
    assert hook_calls == [("note", result.sync_run_id), ("note", result.sync_run_id)]
    assert (_marker() or "").startswith(f"p{result.sync_run_id}.")
    # A sync that publishes nothing leaves the snapshots alone.
    hook_calls.clear()
    assert orchestrator.run_sync(trigger="cli", now=SYNC_NOW + timedelta(minutes=10)).status == (
        "skipped"
    )
    assert hook_calls == []


def test_the_sync_can_prewarm_the_first_page_snapshots(
    db: Session,
    synthetic_map: MapWorld,
    hook_calls: list[tuple[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "sync_prewarm_public_cache", True)
    result = orchestrator.run_sync(trigger="cli", now=SYNC_NOW)
    assert result.status == "success"
    assert hook_calls == [
        ("note", result.sync_run_id),
        ("note", result.sync_run_id),
        ("prewarm", None),
    ]


def test_a_failing_hook_never_fails_the_sync(
    db: Session, synthetic_map: MapWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(sync_run_id: int | None = None) -> bool:
        raise RuntimeError("synthetic failure of the hook")

    monkeypatch.setattr(public_cache, "note_published", broken)
    assert orchestrator.run_sync(trigger="cli", now=SYNC_NOW).status == "success"


def test_run_pipeline_and_sync_publish_call_the_hook(
    db: Session, few_spots: list[str], hook_calls: list[tuple[str, Any]]
) -> None:
    out = run_pipeline(db)
    publication = out["publish"]
    assert hook_calls and hook_calls[-1][0] == "note" and hook_calls[-1][1] is not None
    assert (_marker() or "").startswith(f"p{hook_calls[-1][1]}.")
    assert publication  # the pipeline published what it stored
    hook_calls.clear()
    assert cli.main(["sync", "publish"]) == 0
    assert len(hook_calls) == 1 and hook_calls[0][0] == "note" and hook_calls[0][1] is not None


def test_a_pipeline_change_shows_at_once_on_the_public_pages(
    app_client: ApiClient, db: Session, few_spots: list[str], cache_clock: list[datetime]
) -> None:
    """The developer endpoints and the CLI run run_pipeline: its hook makes the cached
    public routes show the new data now (without it they showed it from the next hour
    while a sync's marker was stored)."""
    run_pipeline(db)
    public_cache.note_published(1)  # as a sync's hook stores it (no database marker read)
    before = app_client.get("/api/events?limit=50&min_score=0").json()
    spot = db.scalar(select(SurfSpot).where(SurfSpot.slug == "jeffreys-bay"))
    assert spot is not None
    from app.services.forecasts.ingestion import simulate_swell

    simulate_swell(db, spot, start=public_cache.public_now() + timedelta(days=6), duration_hours=60)
    run_pipeline(db, acquire=False)
    after = app_client.get("/api/events?limit=50&min_score=0").json()
    assert after != before
