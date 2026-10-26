"""``python -m app.cli ml ...``: datasets, training, the model registry.

  ml build-dataset [--source S] [--station CODE] [--start ISO --end ISO] [--hs-kind K]
                   [--out PATH.parquet]
  ml train [--name N] [--candidates linear,rf,gbt] [--folds K] [--auto-activate]
           [--source S | --dataset PATH.parquet] [--hs-kind K] [--loso] [--no-obs-features]
  ml evaluate VERSION_ID
  ml list [--name N]
  ml activate VERSION_ID
  ml deactivate NAME
  ml export-manifest VERSION_ID DIR [--compress]
  ml import-manifest PATH
  ml restrict VERSION_ID --exclude-spot SLUG [--exclude-spot SLUG ...] --reason TEXT

``--hs-kind`` selects the observed height definition used as the target (default
spectral_hm0, like with like). Versions trained on any other kind (e.g. h_third) are
for evaluation only and can never be activated.

Exit codes: 0 success, 1 not found / not enough data, 3 activation refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

HS_KIND_CHOICES = ("spectral_hm0", "h_third", "unknown")


def _iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def register(subparsers: Any) -> None:
    from app.services.ml.registry import DEFAULT_MODEL_NAME

    parser = subparsers.add_parser("ml", help="ML experiments and the model registry")
    sub = parser.add_subparsers(dest="ml_cmd", required=True)
    build = sub.add_parser("build-dataset", help="assemble the training dataset")
    build.add_argument("--source", action="append", default=None)
    build.add_argument("--station", action="append", default=None)
    build.add_argument("--start", type=_iso, default=None)
    build.add_argument("--end", type=_iso, default=None)
    build.add_argument("--hs-kind", default="spectral_hm0", choices=HS_KIND_CHOICES)
    build.add_argument("--out", default=None, help="write the dataset to PATH.parquet")
    train = sub.add_parser("train", help="train, evaluate and register candidates")
    train.add_argument("--name", default=DEFAULT_MODEL_NAME)
    train.add_argument("--candidates", default="linear,rf,gbt")
    train.add_argument("--folds", type=int, default=3, help="rolling-origin folds (0 = none)")
    train.add_argument("--auto-activate", action="store_true")
    train.add_argument("--source", default=None, help="point-forecast source code")
    train.add_argument("--dataset", default=None, help="train from a saved .parquet dataset")
    train.add_argument("--station", action="append", default=None)
    train.add_argument("--start", type=_iso, default=None)
    train.add_argument("--end", type=_iso, default=None)
    train.add_argument("--hs-kind", default="spectral_hm0", choices=HS_KIND_CHOICES)
    train.add_argument("--loso", action="store_true", help="add leave-one-station-out runs")
    train.add_argument("--no-obs-features", action="store_true")
    train.add_argument("--boot", type=int, default=1000, help="bootstrap replicates")
    evaluate = sub.add_parser("evaluate", help="show a version's decision and fresh scores")
    evaluate.add_argument("version_id", type=int)
    lst = sub.add_parser("list", help="list registered versions")
    lst.add_argument("--name", default=None)
    act = sub.add_parser("activate", help="activate a version that passed the criteria")
    act.add_argument("version_id", type=int)
    deact = sub.add_parser("deactivate", help="retire the active version of a model")
    deact.add_argument("name")
    exp = sub.add_parser("export-manifest", help="export a version (artifact + record)")
    exp.add_argument("version_id", type=int)
    exp.add_argument("directory")
    exp.add_argument(
        "--compress",
        action="store_true",
        help="re-serialise the same model with joblib compression (smaller file to ship)",
    )
    imp = sub.add_parser("import-manifest", help="import an exported version")
    imp.add_argument("path")
    res = sub.add_parser("restrict", help="remove spots from a version's applicability")
    res.add_argument("version_id", type=int)
    res.add_argument("--exclude-spot", action="append", required=True, help="spot slug")
    res.add_argument("--reason", required=True, help="why (stored with the version)")


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def run(args: argparse.Namespace) -> int:
    from app.core.config import get_settings
    from app.core.database import SessionLocal
    from app.services.ml import registry
    from app.services.ml.dataset import InsufficientDataError, LeakageError

    with SessionLocal() as db:
        try:
            if args.ml_cmd == "build-dataset":
                from app.services.ml.dataset import build_dataset, save_dataset

                ds = build_dataset(
                    db,
                    sources=args.source,
                    station_codes=args.station,
                    start=args.start,
                    end=args.end,
                    hs_kinds=(args.hs_kind,),
                )
                out: dict[str, Any] = {"summary": ds.summary}
                if args.out:
                    out["written"] = str(save_dataset(ds, args.out))
                _print(out)
                return 0
            if args.ml_cmd == "train":
                from app.services.ml.dataset import build_dataset, load_dataset
                from app.services.ml.models import train_candidates

                registry.validate_model_name(args.name)  # before the (slow) training
                if args.dataset:
                    ds = load_dataset(args.dataset)
                else:
                    sources = get_settings().point_forecast_source_list
                    source = args.source or (sources[0] if sources else None)
                    if not source:
                        print("ml train: give --source or POINT_FORECAST_SOURCES", file=sys.stderr)
                        return 1
                    ds = build_dataset(
                        db,
                        sources=[source],
                        station_codes=args.station,
                        start=args.start,
                        end=args.end,
                        hs_kinds=(args.hs_kind,),
                    )
                report = train_candidates(
                    ds,
                    candidates=[c.strip() for c in args.candidates.split(",") if c.strip()],
                    folds=args.folds,
                    loso=args.loso,
                    use_observation_features=not args.no_obs_features,
                )
                record = registry.record_training_run(
                    db, report, name=args.name, auto_activate=args.auto_activate, n_boot=args.boot
                )
                _print(
                    {
                        "experiment_id": record.experiment.id,
                        "conclusion": record.experiment.conclusion,
                        "versions": [registry.version_summary(v) for v in record.versions],
                        "activated": record.activated.label if record.activated else None,
                    }
                )
                return 0
            if args.ml_cmd == "evaluate":
                _print(registry.evaluate_version(db, args.version_id))
                return 0
            if args.ml_cmd == "list":
                _print([registry.version_summary(v) for v in registry.list_versions(db, args.name)])
                return 0
            if args.ml_cmd == "activate":
                v = registry.activate(db, args.version_id)
                _print(registry.version_summary(v))
                return 0
            if args.ml_cmd == "deactivate":
                retired = registry.deactivate(db, args.name)
                _print({"retired": retired.label if retired else None})
                return 0
            if args.ml_cmd == "export-manifest":
                path = registry.export_manifest(
                    db, args.version_id, args.directory, compress=args.compress
                )
                _print({"manifest": str(path)})
                return 0
            if args.ml_cmd == "import-manifest":
                _print(registry.version_summary(registry.import_manifest(db, args.path)))
                return 0
            if args.ml_cmd == "restrict":
                v = registry.restrict_applicability(
                    db, args.version_id, dict.fromkeys(args.exclude_spot, args.reason)
                )
                _print({**registry.version_summary(v), "applicability": v.applicability})
                return 0
        except registry.ActivationError as exc:
            print(f"ml {args.ml_cmd}: {exc}", file=sys.stderr)
            return 3
        except (LookupError, InsufficientDataError, LeakageError, registry.RegistryError) as exc:
            print(f"ml {args.ml_cmd}: {exc}", file=sys.stderr)
            return 1
    return 2
