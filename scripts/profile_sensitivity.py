#!/usr/bin/env python
"""How much a spot-profile change moves the predictions of a stored model run (read-only).

The script re-scores the stored forecast hours of one run with the scoring engine
(``surf_quality.scoring.evaluate``, the same tide stages and inputs as
``surf_quality.predict``) twice, with the baseline profile and with alternative values,
and reports per spot: hours, labels changed, mean / max |delta score|, mean delta score
and the change in the central breaking height. Nothing is written to the database: the
connection is opened with ``default_transaction_read_only=on``.

Baseline = the spot row and physical profile stored in the database, with the
scoring fields of ``--spots`` (default data/surf_spots/spots.json) laid over them, so a
baseline re-score of a run scored with those values reproduces the stored scores (the
``parity`` line counts hours whose stored score differs; it should be 0 when the code and
the profile are unchanged since the run was scored).

Sub-commands (from the repository root, with DATABASE_URL pointing at the database that
holds the run, e.g. the real 2026-10-11 runs in swell_live2):

  rescore   --run-id N --set SLUG:FIELD=VALUE [...] [--physical FILE] [--json OUT]
            FIELD is a spots.json field (offshore_wind_direction, tide_preference,
            wave_height_min_ft, ...) or a physical-profile field prefixed "physical."
            (physical.shore_normal_deg, ...); --physical lays a physical_profiles.json
            over the stored profiles of the spots it lists (the "after" side only).
  decisions --run-id N [--decisions FILE] [--json OUT]
            for every record of profile_decisions.json with a new value (changed) or an
            alternative (kept / disputed, "if adopted"): old value vs that value.
  wind      --runs 2,3 [--slugs a,b] [--decisions FILE] [--json OUT]
            the model wind at each spot's forecast point in those runs (hours above
            5 km/h): speed-weighted vector-mean direction FROM, resultant length,
            modal 16-point sector, and the share of hours within 45 deg of the profile's
            offshore direction and of the decision record's alternative. This is a short
            forecast sample, not a climatology, and the wind 15-20 km offshore.

  PYTHONPATH=backend DATABASE_URL=postgresql+psycopg://swell:swell@localhost:5432/swell_live2 \\
      python scripts/profile_sensitivity.py decisions --run-id 4
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.models import ForecastRun, SurfQualityPrediction, SurfSpot, WaveForecast  # noqa: E402
from app.services.geo import angular_diff  # noqa: E402
from app.services.ml.hook import RunCorrector  # noqa: E402
from app.services.research.decisions import DECISIONS_FILENAME  # noqa: E402
from app.services.surf_quality.models import SeaState, SpotParams  # noqa: E402
from app.services.surf_quality.nearshore import Profile  # noqa: E402
from app.services.surf_quality.predict import _row_values, tide_stages  # noqa: E402
from app.services.surf_quality.scoring import evaluate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "surf_spots"
SCORING_FIELDS = (
    "swell_window_min",
    "swell_window_max",
    "optimal_swell_direction_min",
    "optimal_swell_direction_max",
    "offshore_wind_direction",
    "min_swell_period_s",
    "ideal_swell_period_s",
    "wave_height_min_ft",
    "wave_height_max_ft",
    "height_factor",
    "tide_preference",
)
PROFILE_FIELDS = (
    "shore_normal_deg",
    "forecast_point_depth_m",
    "controlling_depth_m",
    "breaker_index",
    "focusing_ref",
    "focusing_kappa",
    "focusing_t_star_s",
    "shadow_table",
    "spreading_deg",
)
LIGHT_WIND_KMH = 5.0


def read_only_session() -> Session:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("set DATABASE_URL to the database that holds the run")
    engine = create_engine(url, connect_args={"options": "-c default_transaction_read_only=on"})
    return sessionmaker(bind=engine)()


# --------------------------------------------------------------------------- profiles
def _spot_params(spot: SurfSpot, overrides: dict[str, Any]) -> SpotParams:
    base = SpotParams.from_orm(spot)
    fields = {k: v for k, v in overrides.items() if k in SCORING_FIELDS}
    params = replace(base, **fields)
    if fields.get("tide_preference") is not None:
        params = replace(params, tide_preference=str(fields["tide_preference"]))
    if base.profile is not None and any(
        k in fields for k in ("swell_window_min", "swell_window_max")
    ):
        params = replace(
            params,
            profile=replace(
                base.profile,
                swell_window_min=params.swell_window_min,
                swell_window_max=params.swell_window_max,
            ),
        )
    return params


def _with_physical(params: SpotParams, physical: dict[str, Any]) -> SpotParams:
    if not physical:
        return params
    prof: Profile = params.physical_profile()
    changes = {k: physical[k] for k in PROFILE_FIELDS if k in physical}
    if "shadow_table" in changes and changes["shadow_table"] is not None:
        changes["shadow_table"] = tuple(changes["shadow_table"])
    quality = {**prof.data_quality, **(physical.get("data_quality") or {})}
    return replace(params, profile=replace(prof, **changes, data_quality=quality))


def _parse_value(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _sets(items: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = defaultdict(dict)
    for item in items:
        slug, _, assignment = item.partition(":")
        name, _, value = assignment.partition("=")
        if not slug or not name or not value:
            raise SystemExit(f"--set needs SLUG:FIELD=VALUE, got {item!r}")
        out[slug][name] = _parse_value(value)
    return out


# --------------------------------------------------------------------------- rescoring
def _hours(db: Session, run: ForecastRun, spot: SurfSpot) -> list[tuple[Any, ...]]:
    rows = db.execute(
        select(WaveForecast, SurfQualityPrediction)
        .outerjoin(SurfQualityPrediction, SurfQualityPrediction.forecast_id == WaveForecast.id)
        .where(WaveForecast.run_id == run.id, WaveForecast.spot_id == spot.id)
        .order_by(WaveForecast.valid_time)
    ).all()
    stages = tide_stages([f.valid_time for f, _ in rows], [f.sea_level_m for f, _ in rows])
    return [(f, p, s) for (f, p), s in zip(rows, stages, strict=True)]


def compare(
    db: Session,
    run: ForecastRun,
    spot: SurfSpot,
    before: SpotParams,
    after: SpotParams,
    corrector: RunCorrector | None,
) -> dict[str, Any]:
    deltas: list[int] = []
    mids: list[tuple[float, float]] = []
    labels_changed = relations_changed = parity = daylight_labels = 0
    for f, pred, stage in _hours(db, run, spot):
        sea = SeaState.from_orm(f, tide_stage=stage)
        if corrector is not None:
            hit = corrector.factor(spot.slug, _row_values(f), f.valid_time)
            if hit is not None:
                sea = sea.scaled(hit[0])
        a, b = evaluate(sea, before), evaluate(sea, after)
        deltas.append(b.score - a.score)
        mids.append((a.breaking.mid_ft, b.breaking.mid_ft))
        if a.label != b.label:
            labels_changed += 1
            if pred is not None and pred.is_daylight:
                daylight_labels += 1
        relations_changed += a.wind_relation != b.wind_relation
        if pred is not None and pred.score != a.score:
            parity += 1
    n = len(deltas)
    return {
        "slug": spot.slug,
        "hours": n,
        "labels_changed": labels_changed,
        "labels_changed_daylight": daylight_labels,
        "wind_relations_changed": relations_changed,
        "mean_abs_delta_score": round(mean(abs(d) for d in deltas), 2) if n else None,
        "max_abs_delta_score": max((abs(d) for d in deltas), default=None),
        "mean_delta_score": round(mean(deltas), 2) if n else None,
        "mean_breaking_mid_ft": [
            round(mean(m[0] for m in mids), 2) if n else None,
            round(mean(m[1] for m in mids), 2) if n else None,
        ],
        "mean_abs_delta_breaking_mid_ft": round(mean(abs(m[1] - m[0]) for m in mids), 2)
        if n
        else None,
        "parity_mismatches": parity,
    }


def _run(db: Session, run_id: int) -> tuple[ForecastRun, RunCorrector | None]:
    run = db.get(ForecastRun, run_id)
    if run is None:
        raise SystemExit(f"no forecast run {run_id}")
    corrector = None if run.source.kind == "blend" else RunCorrector.for_run(db, run)
    return run, corrector


def _spots(db: Session) -> dict[str, SurfSpot]:
    return {s.slug: s for s in db.scalars(select(SurfSpot)).all()}


def _baseline(spots_file: Path) -> dict[str, dict[str, Any]]:
    return {s["slug"]: s for s in json.loads(spots_file.read_text(encoding="utf-8"))}


def cmd_rescore(args: argparse.Namespace) -> list[dict[str, Any]]:
    db = read_only_session()
    run, corrector = _run(db, args.run_id)
    spots, baseline = _spots(db), _baseline(args.spots)
    sets = _sets(args.set or [])
    physical: dict[str, dict[str, Any]] = (
        json.loads(args.physical.read_text(encoding="utf-8")) if args.physical else {}
    )
    out = []
    for slug in sorted(set(sets) | set(physical)):
        spot = spots[slug]
        before = _spot_params(spot, baseline.get(slug, {}))
        scoring = {k: v for k, v in sets.get(slug, {}).items() if not k.startswith("physical.")}
        phys = {
            k.split(".", 1)[1]: v
            for k, v in sets.get(slug, {}).items()
            if k.startswith("physical.")
        }
        after = _with_physical(
            _spot_params(spot, {**baseline.get(slug, {}), **scoring}),
            {**physical.get(slug, {}), **phys},
        )
        row = compare(db, run, spot, before, after, corrector)
        row["changes"] = {**scoring, **{f"physical.{k}": v for k, v in phys.items()}}
        if slug in physical:
            row["changes"]["physical_profiles"] = sorted(
                k for k in physical[slug] if k in PROFILE_FIELDS
            )
        out.append(row)
    return out


def _field_overrides(field_name: str, value: Any) -> dict[str, Any]:
    if field_name == "optimal_swell_direction":
        return {
            "optimal_swell_direction_min": value["min"],
            "optimal_swell_direction_max": value["max"],
        }
    if field_name == "wave_height_ft":
        return {"wave_height_min_ft": value["min"], "wave_height_max_ft": value["max"]}
    if field_name == "best_months":
        return {}  # best months do not enter the score
    return {field_name: value}


def cmd_decisions(args: argparse.Namespace) -> list[dict[str, Any]]:
    db = read_only_session()
    run, corrector = _run(db, args.run_id)
    spots, baseline = _spots(db), _baseline(args.spots)
    records = json.loads(args.decisions.read_text(encoding="utf-8"))["decisions"]
    out = []
    for rec in records:
        target = rec["new_value"] if rec["decision"] == "changed" else rec.get("alternative")
        if target is None or rec.get("reason") is not None:
            continue
        slug = rec["slug"]
        spot = spots[slug]
        base = {**baseline.get(slug, {}), **_field_overrides(rec["field"], rec["old_value"])}
        before = _spot_params(spot, base)
        after = _spot_params(spot, {**base, **_field_overrides(rec["field"], target)})
        row = compare(db, run, spot, before, after, corrector)
        row.update(
            {
                "field": rec["field"],
                "decision": rec["decision"],
                "old_value": rec["old_value"],
                "value": target,
                "applies": "adopted" if rec["decision"] == "changed" else "if adopted",
                "scored": rec["field"] != "best_months",
            }
        )
        out.append(row)
    return out


def cmd_wind(args: argparse.Namespace) -> list[dict[str, Any]]:
    db = read_only_session()
    run_ids = [int(r) for r in args.runs.split(",")]
    spots = _spots(db)
    alternatives: dict[str, float] = {}
    if args.decisions and args.decisions.exists():
        for rec in json.loads(args.decisions.read_text(encoding="utf-8"))["decisions"]:
            if rec["field"] == "offshore_wind_direction" and rec.get("alternative") is not None:
                alternatives.setdefault(rec["slug"], float(rec["alternative"]))
    slugs = args.slugs.split(",") if args.slugs else sorted(alternatives)
    out = []
    for slug in slugs:
        spot = spots[slug]
        rows = db.execute(
            select(
                WaveForecast.run_id, WaveForecast.wind_speed_kmh, WaveForecast.wind_direction_deg
            ).where(WaveForecast.spot_id == spot.id, WaveForecast.run_id.in_(run_ids))
        ).all()
        winds = [
            (float(s), float(d))
            for _, s, d in rows
            if s is not None and d is not None and s > LIGHT_WIND_KMH
        ]
        alt = alternatives.get(slug)
        entry: dict[str, Any] = {
            "slug": slug,
            "runs": run_ids,
            "hours": len(winds),
            "profile_offshore_deg": spot.offshore_wind_direction,
            "alternative_offshore_deg": alt,
        }
        if winds:
            u = sum(s * math.sin(math.radians(d)) for s, d in winds)
            v = sum(s * math.cos(math.radians(d)) for s, d in winds)
            sectors = [0] * 16
            for _, d in winds:
                sectors[int(((d + 11.25) % 360.0) // 22.5)] += 1
            entry.update(
                {
                    "vector_mean_from_deg": round(math.degrees(math.atan2(u, v)) % 360.0, 1),
                    "resultant": round(math.hypot(u, v) / sum(s for s, _ in winds), 2),
                    "modal_sector_deg": max(range(16), key=lambda i: sectors[i]) * 22.5,
                    "share_within_45_of_profile": round(
                        sum(angular_diff(d, spot.offshore_wind_direction) <= 45 for _, d in winds)
                        / len(winds),
                        2,
                    ),
                    "share_within_45_of_alternative": None
                    if alt is None
                    else round(sum(angular_diff(d, alt) <= 45 for _, d in winds) / len(winds), 2),
                }
            )
        out.append(entry)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("rescore", "decisions", "wind"):
        p = sub.add_parser(name)
        p.add_argument("--json", type=Path, default=None)
        if name in ("rescore", "decisions"):
            p.add_argument("--spots", type=Path, default=DATA_DIR / "spots.json")
            p.add_argument("--run-id", type=int, required=True)
        if name in ("decisions", "wind"):
            p.add_argument("--decisions", type=Path, default=DATA_DIR / DECISIONS_FILENAME)
        if name == "rescore":
            p.add_argument("--set", action="append")
            p.add_argument("--physical", type=Path, default=None)
        if name == "wind":
            p.add_argument("--runs", required=True)
            p.add_argument("--slugs", default=None)
    args = ap.parse_args(argv)
    rows = {"rescore": cmd_rescore, "decisions": cmd_decisions, "wind": cmd_wind}[args.cmd](args)
    text = json.dumps(rows, indent=1, default=str)
    if args.json:
        args.json.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
