"""Build the drift-monitoring baseline stored with each model artifact.

The reference window is the validation split: it is the most recent labelled
period before the hold-out test, and its scores are out-of-sample (train-split
scores would be over-confident). Live traffic is compared against it by
``fraud_common.drift.evaluate_drift``.

Usage for an artifact trained before baselines existed (the model is not
retrained, only its metadata is extended)::

    python -m ml_pipeline.drift --artifact data/models/fraud_xgboost.joblib \
        --validation data/processed/validation.csv
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from fraud_common.drift import MISSING, OTHER

from .explain import FEATURE_LABELS

# Calendar fields (month, day, day of week, weekend) are left out on purpose: a
# monitoring window spans a few days of transactions, so they would always look
# shifted. ``merchant`` has ~700 values and is covered by ``category``.
DRIFT_FEATURES = (
    "category",
    "amt",
    "transaction_hour",
    "customer_age",
    "city_pop",
    "customer_merchant_distance_km",
    "time_since_last_trans_sec",
    "trans_count_1h",
    "trans_count_24h",
    "trans_count_7d",
    "sum_amt_24h",
    "avg_amt_7d",
    "std_amt_7d",
    "max_amt_7d",
    "amt_to_avg_7d_ratio",
    "amt_to_max_7d_ratio",
)
DEFAULT_BINS = 10
MAX_CATEGORIES = 30


def numeric_spec(values: Any, bins: int = DEFAULT_BINS, extra_edges: tuple[float, ...] = ()) -> dict[str, Any]:
    """Quantile bins of the reference values (duplicate cut points merged)."""
    array = pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(dtype=float)
    finite = array[np.isfinite(array)]
    edges: list[float] = []
    if finite.size:
        cut_points = np.quantile(finite, np.linspace(0, 1, bins + 1)[1:-1])
        edges = sorted({float(edge) for edge in (*cut_points, *extra_edges)})
    # side="left" == bisect_left: right-closed bins, as in fraud_common.drift.assign_bin.
    counts = np.bincount(np.searchsorted(edges, finite, side="left"), minlength=len(edges) + 1)
    total = max(array.size, 1)
    expected = {str(index): float(count / total) for index, count in enumerate(counts) if count}
    missing = array.size - finite.size
    if missing:
        expected[MISSING] = float(missing / total)
    return {"kind": "numeric", "edges": edges, "expected": expected}


def categorical_spec(values: Any, max_categories: int = MAX_CATEGORIES) -> dict[str, Any]:
    series = pd.Series(values)
    total = max(len(series), 1)
    counts = series.dropna().astype(str).value_counts()
    expected = {str(key): float(count / total) for key, count in counts.head(max_categories).items()}
    rest = int(counts.iloc[max_categories:].sum()) if len(counts) > max_categories else 0
    if rest:
        expected[OTHER] = float(rest / total)
    missing = int(series.isna().sum())
    if missing:
        expected[MISSING] = float(missing / total)
    return {"kind": "categorical", "expected": expected}


def build_drift_baseline(
    features: pd.DataFrame,
    calibrated_scores: Any,
    recommended_triage: dict[str, Any] | None = None,
    reference: str = "validation",
) -> dict[str, Any]:
    triage = recommended_triage or {}
    threshold_edges = tuple(float(triage[key]) for key in ("low_max", "high_min") if key in triage)
    specs: dict[str, Any] = {}
    for name in DRIFT_FEATURES:
        if name not in features.columns:
            continue
        column = features[name]
        is_numeric = pd.api.types.is_numeric_dtype(column) and not pd.api.types.is_bool_dtype(column)
        spec = numeric_spec(column) if is_numeric else categorical_spec(column)
        spec["label"] = FEATURE_LABELS.get(name, name)
        specs[name] = spec
    score_spec = numeric_spec(np.asarray(calibrated_scores, dtype=float), extra_edges=threshold_edges)
    score_spec["label"] = "Calibrated fraud probability"
    return {
        "reference": reference,
        "rows": int(len(features)),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "features": specs,
        "score": score_spec,
        "expected_review_rate": triage.get("validation_review_rate"),
    }


def attach_baseline(artifact_path: str | Path, validation_path: str | Path, target: str = "is_fraud") -> dict[str, Any]:
    """Add a baseline to an existing artifact without retraining the model."""
    from .model import save_artifact, split_features_target

    path = Path(artifact_path)
    artifact = joblib.load(path)
    frame = pd.read_csv(validation_path)
    x_validation, _ = split_features_target(frame, target)
    x_validation = x_validation.reindex(columns=artifact["feature_columns"])
    raw = artifact["pipeline"].predict_proba(x_validation)[:, 1]
    calibrated = artifact["calibrator"].transform(raw)
    artifact["drift_baseline"] = build_drift_baseline(
        x_validation, calibrated, artifact.get("recommended_triage"))
    lineage = artifact.get("lineage", {})
    save_artifact(artifact, path)
    # save_artifact recomputes lineage; keep the original training-data hash and time.
    written = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    for key in ("created_at", "source_sha256"):
        if key in lineage:
            written[key] = lineage[key]
    path.with_suffix(".json").write_text(json.dumps(written, indent=2), encoding="utf-8")
    artifact["lineage"] = written
    joblib.dump(artifact, path)
    return artifact["drift_baseline"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach a drift baseline to an existing model artifact")
    parser.add_argument("--artifact", default="data/models/fraud_xgboost.joblib")
    parser.add_argument("--validation", default="data/processed/validation.csv")
    args = parser.parse_args()
    baseline = attach_baseline(args.artifact, args.validation)
    print(f"Baseline: {len(baseline['features'])} features + score, {baseline['rows']} reference rows")


if __name__ == "__main__":
    main()
