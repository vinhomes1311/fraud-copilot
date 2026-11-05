"""Re-evaluate an existing model artifact without retraining it."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .model import evaluate_model, load_artifact
from .preprocessing import extract_features, read_csv_safely


def evaluate_artifact(
    artifact_path: str | Path,
    test_path: str | Path,
    output_path: str | Path,
) -> dict:
    artifact = load_artifact(artifact_path)
    test = extract_features(read_csv_safely(test_path))
    x_test = test.drop(columns=["is_fraud", "_sort_time"], errors="ignore")
    scores = artifact["calibrator"].transform(
        artifact["pipeline"].predict_proba(x_test)[:, 1]
    )
    metrics = evaluate_model(test["is_fraud"], scores, artifact["threshold"])
    report = {
        "model_version": artifact["model_version"],
        "artifact": str(Path(artifact_path)),
        "test_data": str(Path(test_path)),
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
    parser.add_argument("--output", default="data/models/fraud_xgboost.evaluation.json")
    parser.add_argument("--min-pr-auc", type=float, default=0.90)
    parser.add_argument("--min-recall", type=float, default=0.80)
    parser.add_argument("--min-precision", type=float, default=0.80)
    parser.add_argument("--min-f1", type=float, default=0.85)
    args = parser.parse_args()
    report = evaluate_artifact(args.artifact, args.test_data, args.output)
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
