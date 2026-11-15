"""Re-evaluate an existing model artifact without retraining it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .model import DEFAULT_EVALUATION, evaluate_model, load_artifact, split_features_target
from .preprocessing import extract_features, read_csv_safely


def build_test_features(
    test_path: str | Path,
    history_path: str | Path | None = None,
    target: str = "is_fraud",
) -> pd.DataFrame:
    """Compute test features, optionally warm-starting card history.

    Training computes rolling features on fraudTrain + fraudTest together, so
    the first test transactions of each card already see train-period history
    (as the online scoring worker does). Passing ``history_path`` reproduces
    that; without it the first test transactions start with empty history.
    """
    test_raw = read_csv_safely(test_path)
    if history_path is None or not Path(history_path).exists():
        return extract_features(test_raw, target)
    history_raw = read_csv_safely(history_path)
    history_raw["_split_tag"] = "history"
    test_raw["_split_tag"] = "test"
    prepared = extract_features(pd.concat([history_raw, test_raw], ignore_index=True), target)
    return (
        prepared[prepared["_split_tag"] == "test"]
        .drop(columns="_split_tag")
        .reset_index(drop=True)
    )


def evaluate_artifact(
    artifact_path: str | Path,
    test_path: str | Path,
    output_path: str | Path,
    history_path: str | Path | None = None,
    top_fraction: float = DEFAULT_EVALUATION["top_fraction"],
    target: str = "is_fraud",
) -> dict:
    artifact = load_artifact(artifact_path)
    test = build_test_features(test_path, history_path, target)
    x_test, y_test = split_features_target(test, target)
    x_test = x_test.reindex(columns=artifact["feature_columns"])
    raw_scores = artifact["pipeline"].predict_proba(x_test)[:, 1]
    scores = artifact["calibrator"].transform(raw_scores)
    metrics = evaluate_model(
        y_test, scores, artifact["threshold"], top_fraction=top_fraction, raw_scores=raw_scores
    )
    report = {
        "model_version": artifact["model_version"],
        "artifact": str(Path(artifact_path)),
        "test_data": str(Path(test_path)),
        "history_data": str(Path(history_path)) if history_path and Path(history_path).exists() else None,
        "rows": len(test),
        "threshold": artifact["threshold"],
        "metrics": metrics,
        "stored_test_metrics": artifact.get("lineage", {}).get("test_metrics"),
    }
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an existing fraud model artifact")
    parser.add_argument("--artifact", default="data/models/fraud_xgboost.joblib")
    parser.add_argument("--test-data", default="data/fraudTest.csv")
    parser.add_argument(
        "--history-data",
        default="data/fraudTrain.csv",
        help="Earlier transactions used only to warm up per-card history features; pass '' to disable",
    )
    parser.add_argument("--output", default="data/models/fraud_xgboost.evaluation.json")
    parser.add_argument("--top-fraction", type=float, default=DEFAULT_EVALUATION["top_fraction"])
    parser.add_argument("--min-pr-auc", type=float, default=0.90)
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--min-precision", type=float, default=0.80)
    parser.add_argument("--min-f1", type=float, default=0.85)
    args = parser.parse_args()
    report = evaluate_artifact(
        args.artifact,
        args.test_data,
        args.output,
        history_path=args.history_data or None,
        top_fraction=args.top_fraction,
    )
    print(json.dumps(report, indent=2))
    metrics = report["metrics"]
    gates = {
        "pr_auc": metrics["pr_auc"] >= args.min_pr_auc,
        "recall": metrics["recall"] >= args.min_recall,
        "precision": metrics["precision"] >= args.min_precision,
        "f1": metrics["f1"] >= args.min_f1,
    }
    if not all(gates.values()):
        failed = ", ".join(name for name, passed in gates.items() if not passed)
        raise SystemExit(f"ML quality gate failed: {failed}")
    print("ML quality gate passed")


if __name__ == "__main__":
    main()
