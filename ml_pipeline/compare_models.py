"""Benchmark alternative models against the production XGBoost model.

Usage::

    python -m ml_pipeline.compare_models --config ml_pipeline/config.json
    python -m ml_pipeline.compare_models --models xgboost,lightgbm --use-processed

Every model goes through the exact procedure used for the served artifact:

1. fit on the train split (validation only for early stopping),
2. fit the configured calibrator (Platt by default) on validation scores,
3. pick the F1-optimal threshold on calibrated validation scores,
4. report hold-out test metrics once, at that threshold.

Results are written to ``<output-dir>/model-comparison.{json,csv,md}``. The
production artifact in ``data/models`` is never modified.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .candidates import (
    CANDIDATES,
    DISPLAY_NAMES,
    ENSEMBLES,
    PRIMARY_MODEL,
    CandidateUnavailable,
    FitContext,
)
from .calibration import fit_calibrator
from .config import PipelineConfig, load_config
from .model import (
    evaluate_model,
    evaluation_settings,
    select_threshold,
    split_features_target,
)
from .workflow import PreparedData, load_processed_datasets, prepare_datasets

log = logging.getLogger(__name__)
ALL_MODELS = (*CANDIDATES, *ENSEMBLES)


def compare_models(
    prepared: PreparedData,
    models: Iterable[str] = ALL_MODELS,
    target: str = "is_fraud",
    evaluation: dict[str, Any] | None = None,
    context: FitContext | None = None,
) -> dict[str, Any]:
    models = list(models)
    unknown = sorted(set(models) - set(ALL_MODELS))
    if unknown:
        raise ValueError(f"Unknown models: {unknown}. Available: {list(ALL_MODELS)}")
    settings = evaluation_settings(evaluation)
    context = context or FitContext()
    x_train, y_train = split_features_target(prepared.train, target)
    x_validation, y_validation = split_features_target(prepared.validation, target)
    x_test, y_test = split_features_target(prepared.test, target)
    x_validation = x_validation.reindex(columns=x_train.columns)
    x_test = x_test.reindex(columns=x_train.columns)

    requested_members = {member for name in models if name in ENSEMBLES for member in ENSEMBLES[name]}
    base_models = [name for name in CANDIDATES if name in models or name in requested_members]
    raw_scores: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    results: list[dict[str, Any]] = []
    for name in base_models:
        log.info("fitting_candidate", extra={"model": name})
        started = time.perf_counter()
        try:
            fitted = CANDIDATES[name](x_train, y_train, x_validation, y_validation, context)
        except CandidateUnavailable as exc:
            log.warning("candidate_skipped model=%s reason=%s", name, exc)
            if name in models:
                results.append(_status_row(name, "skipped", str(exc)))
            continue
        except Exception as exc:  # keep benchmarking the remaining models
            log.exception("candidate_failed model=%s", name)
            if name in models:
                results.append(_status_row(name, "failed", f"{type(exc).__name__}: {exc}"))
            continue
        fit_seconds = time.perf_counter() - started
        validation_scores = fitted.predict_raw(x_validation)
        predict_started = time.perf_counter()
        test_scores = fitted.predict_raw(x_test)
        predict_seconds = time.perf_counter() - predict_started
        raw_scores[name] = (validation_scores, test_scores)
        if name in models:
            row = _score(name, validation_scores, test_scores, y_validation, y_test, settings)
            row.update(
                fit_seconds=round(fit_seconds, 2),
                predict_ms_per_1k_rows=round(predict_seconds / max(len(x_test), 1) * 1e6, 3),
                details=fitted.details,
            )
            results.append(row)
            log.info(
                "candidate_scored model=%s test_pr_auc=%.5f test_f1=%.5f fit_s=%.1f",
                name, row["test"]["pr_auc"], row["test"]["f1"], fit_seconds,
            )

    for name, members in ENSEMBLES.items():
        if name not in models:
            continue
        missing = [member for member in members if member not in raw_scores]
        if missing:
            results.append(_status_row(name, "skipped", f"missing members: {missing}"))
            continue
        validation_scores = np.mean([raw_scores[member][0] for member in members], axis=0)
        test_scores = np.mean([raw_scores[member][1] for member in members], axis=0)
        row = _score(name, validation_scores, test_scores, y_validation, y_test, settings)
        row["details"] = {"members": list(members), "weights": "equal"}
        results.append(row)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary_model": PRIMARY_MODEL,
        "procedure": (
            f"fit on train; {settings['calibration_method']} calibration and F1 threshold "
            "on validation; metrics on hold-out test"
        ),
        "evaluation": settings,
        "data": {
            "source_mode": prepared.source_mode,
            **{
                split: _split_summary(frame, target)
                for split, frame in (
                    ("train", prepared.train),
                    ("validation", prepared.validation),
                    ("test", prepared.test),
                )
            },
        },
        "results": _rank(results),
    }


def _score(
    name: str,
    validation_scores: np.ndarray,
    test_scores: np.ndarray,
    y_validation: pd.Series,
    y_test: pd.Series,
    settings: dict[str, Any],
) -> dict[str, Any]:
    calibrator = fit_calibrator(validation_scores, y_validation, settings["calibration_method"])
    calibrated_validation = calibrator.transform(validation_scores)
    calibrated_test = calibrator.transform(test_scores)
    threshold = select_threshold(
        y_validation,
        calibrated_validation,
        settings["threshold_min"],
        settings["threshold_max"],
        settings["threshold_step"],
    )
    top_fraction = settings["top_fraction"]
    return {
        "model": name,
        "display_name": DISPLAY_NAMES.get(name, name),
        "status": "ok",
        "threshold": threshold,
        "validation": evaluate_model(
            y_validation, calibrated_validation, threshold, top_fraction, validation_scores
        ),
        "test": evaluate_model(y_test, calibrated_test, threshold, top_fraction, test_scores),
    }


def _status_row(name: str, status: str, reason: str) -> dict[str, Any]:
    return {"model": name, "display_name": DISPLAY_NAMES.get(name, name), "status": status, "reason": reason}


def _split_summary(frame: pd.DataFrame, target: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "rows": int(len(frame)),
        "fraud": int(frame[target].sum()),
        "fraud_rate": float(frame[target].mean()),
    }
    if "_sort_time" in frame and len(frame):
        summary["start"] = str(frame["_sort_time"].min())
        summary["end"] = str(frame["_sort_time"].max())
    return summary


def _rank(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed = sorted(
        (row for row in results if row["status"] == "ok"),
        key=lambda row: row["test"]["pr_auc"],
        reverse=True,
    )
    primary = next((row for row in completed if row["model"] == PRIMARY_MODEL), None)
    for rank, row in enumerate(completed, start=1):
        row["rank_by_test_pr_auc"] = rank
        if primary is not None:
            row["delta_vs_primary"] = {
                metric: row["test"][metric] - primary["test"][metric]
                for metric in ("pr_auc", "f1", "precision", "recall", "recall_at_top_k")
            }
    return completed + [row for row in results if row["status"] != "ok"]


def comparison_table(report: dict[str, Any]) -> pd.DataFrame:
    """One row per model with the same columns as the notebook comparison table."""
    top_percent = f"{report['evaluation']['top_fraction'] * 100:g}%"
    rows = []
    for row in report["results"]:
        if row["status"] != "ok":
            rows.append({"Model": row["display_name"], "Status": f"{row['status']}: {row['reason']}"})
            continue
        test = row["test"]
        rows.append(
            {
                "Rank": row["rank_by_test_pr_auc"],
                "Model": row["display_name"],
                "Threshold": round(row["threshold"], 2),
                "PR-AUC": round(test["pr_auc"], 5),
                "ROC-AUC": round(test["roc_auc"], 5),
                "Precision": round(test["precision"], 5),
                "Recall": round(test["recall"], 5),
                "F1": round(test["f1"], 5),
                f"Recall@Top{top_percent}": round(test["recall_at_top_k"], 5),
                f"Precision@Top{top_percent}": round(test["precision_at_top_k"], 5),
                f"Fraud@Top{top_percent}": f"{test['top_k_fraud_found']}/{test['tp'] + test['fn']}",
                "TP / FP / FN": f"{test['tp']} / {test['fp']} / {test['fn']}",
                "Brier": round(test["brier"], 6),
                "Val PR-AUC": round(row["validation"]["pr_auc"], 5),
                "Fit (s)": row.get("fit_seconds"),
                "Predict (ms/1k rows)": row.get("predict_ms_per_1k_rows"),
                "Status": "ok",
            }
        )
    return pd.DataFrame(rows)


def write_reports(report: dict[str, Any], output_dir: str | Path) -> dict[str, Path]:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    table = comparison_table(report)
    paths = {
        "json": destination / "model-comparison.json",
        "csv": destination / "model-comparison.csv",
        "md": destination / "model-comparison.md",
    }
    paths["json"].write_text(json.dumps(_json_safe(report), indent=2), encoding="utf-8")
    table.to_csv(paths["csv"], index=False)
    paths["md"].write_text(_markdown(report, table), encoding="utf-8")
    return paths


def _markdown(report: dict[str, Any], table: pd.DataFrame) -> str:
    data = report["data"]
    lines = [
        "# Model comparison",
        "",
        f"Generated: {report['generated_at']}  ",
        f"Primary model: `{report['primary_model']}`  ",
        f"Procedure: {report['procedure']}",
        "",
        "| Split | Rows | Fraud | Fraud rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    for split in ("train", "validation", "test"):
        info = data[split]
        lines.append(f"| {split} | {info['rows']:,} | {info['fraud']:,} | {info['fraud_rate']:.5f} |")
    lines += ["", "## Test results (ranked by PR-AUC)", "", _markdown_table(table), ""]
    return "\n".join(lines)


def _markdown_table(table: pd.DataFrame) -> str:
    columns = list(table.columns)
    rendered = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for _, row in table.iterrows():
        cells = ["" if pd.isna(value) else str(value) for value in row]
        rendered.append("| " + " | ".join(cells) + " |")
    return "\n".join(rendered)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.floating, float)):
        return None if math.isnan(float(value)) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    return value


def run_comparison(
    config_path: str | Path | None = None,
    models: Iterable[str] = ALL_MODELS,
    output_dir: str | Path = "reports",
    use_processed: bool = False,
    fast: bool = False,
) -> dict[str, Any]:
    config: PipelineConfig = load_config(config_path)
    prepared = load_processed_datasets(config) if use_processed else prepare_datasets(config)
    xgboost_params = {
        key: value for key, value in config.model.items() if key not in {"artifact_path", "random_state"}
    }
    context = FitContext(
        random_state=int(config.model.get("random_state", 42)),
        xgboost_params=xgboost_params,
        fast=fast,
    )
    report = compare_models(
        prepared,
        models,
        target=config.data["target"],
        evaluation=config.evaluation,
        context=context,
    )
    paths = write_reports(report, output_dir)
    report["outputs"] = {kind: str(path) for kind, path in paths.items()}
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare candidate fraud models against XGBoost")
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--models",
        default="all",
        help=f"Comma-separated subset of: {', '.join(ALL_MODELS)} (default: all)",
    )
    parser.add_argument("--output-dir", default="reports")
    parser.add_argument(
        "--use-processed",
        action="store_true",
        help="Reuse data/processed/*.csv from the last training run instead of rebuilding features",
    )
    parser.add_argument("--fast", action="store_true", help="Fewer trees/iterations for a quick smoke run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    models = ALL_MODELS if args.models == "all" else [name.strip() for name in args.models.split(",") if name.strip()]
    report = run_comparison(args.config, models, args.output_dir, args.use_processed, args.fast)
    print(_markdown_table(comparison_table(report)))
    print("\nReports:", ", ".join(report["outputs"].values()))


if __name__ == "__main__":
    main()
