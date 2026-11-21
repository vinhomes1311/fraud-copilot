from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    average_precision_score,
    brier_score_loss,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler
from xgboost import XGBClassifier

from .calibration import describe_calibrator, fit_calibrator

INTERNAL_COLUMNS = ("_sort_time", "_split_tag")
DEFAULT_EVALUATION = {
    "threshold_min": 0.05,
    "threshold_max": 0.95,
    "threshold_step": 0.05,
    "top_fraction": 0.01,
    "calibration_method": "platt",
}
DEFAULT_XGBOOST_PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "aucpr",
    "n_estimators": 1000,
    "max_depth": 6,
    "learning_rate": 0.1,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 1,
    "reg_lambda": 1.0,
    "tree_method": "hist",
    "n_jobs": -1,
    "random_state": 42,
    "early_stopping_rounds": 100,
    "device": "cpu",
}


def build_preprocessor(
    frame: pd.DataFrame,
    scale_numeric: bool = False,
    categorical_encoding: str = "onehot",
) -> ColumnTransformer:
    """Median-impute numeric columns and encode categorical ones.

    The defaults (no scaling, sparse one-hot) are what the served XGBoost
    artifact uses. Linear models need ``scale_numeric=True``; sklearn forests
    work better with ``categorical_encoding="ordinal"`` than with a wide
    sparse one-hot matrix.
    """
    numeric = frame.select_dtypes(include=["number", "bool"]).columns.tolist()
    categorical = [column for column in frame.columns if column not in numeric]
    numeric_steps: list[tuple[str, Any]] = [("imputer", SimpleImputer(strategy="median"))]
    if scale_numeric:
        numeric_steps.append(("scaler", StandardScaler()))
    if categorical_encoding == "onehot":
        try:
            encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=True)
        except TypeError:
            encoder = OneHotEncoder(handle_unknown="ignore", sparse=True)
    elif categorical_encoding == "ordinal":
        encoder = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
    else:
        raise ValueError(f"Unsupported categorical_encoding: {categorical_encoding}")
    return ColumnTransformer(
        [
            ("num", Pipeline(numeric_steps), numeric),
            (
                "cat",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        ("encoder" if categorical_encoding == "ordinal" else "onehot", encoder),
                    ]
                ),
                categorical,
            ),
        ],
        remainder="drop",
    )


def split_features_target(frame: pd.DataFrame, target: str = "is_fraud") -> tuple[pd.DataFrame, pd.Series]:
    features = frame.drop(columns=[target, *INTERNAL_COLUMNS], errors="ignore")
    return features, frame[target].astype(int)


def evaluation_settings(evaluation: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**DEFAULT_EVALUATION, **(evaluation or {})}


def scale_pos_weight_for(y_train: pd.Series) -> float:
    negatives = int((y_train == 0).sum())
    positives = int((y_train == 1).sum())
    return negatives / max(positives, 1)


def fit_xgboost(
    x_train: pd.DataFrame,
    y_train: pd.Series,
    x_validation: pd.DataFrame,
    y_validation: pd.Series,
    **params: Any,
) -> Pipeline:
    """Fit the production XGBoost pipeline with early stopping on validation."""
    preprocessor = build_preprocessor(x_train)
    x_train_transformed = preprocessor.fit_transform(x_train)
    x_validation_transformed = preprocessor.transform(x_validation)
    model_params = {
        **DEFAULT_XGBOOST_PARAMS,
        "scale_pos_weight": scale_pos_weight_for(y_train),
        **params,
    }
    estimator = XGBClassifier(**model_params)
    estimator.fit(
        x_train_transformed,
        y_train,
        eval_set=[(x_validation_transformed, y_validation)],
        verbose=False,
    )
    return Pipeline([("preprocessor", preprocessor), ("model", estimator)])


def train_model(
    train_frame: pd.DataFrame,
    validation_frame: pd.DataFrame,
    test_frame: pd.DataFrame | None = None,
    target: str = "is_fraud",
    evaluation: dict[str, Any] | None = None,
    **params: Any,
) -> dict[str, Any]:
    settings = evaluation_settings(evaluation)
    x_train, y_train = split_features_target(train_frame, target)
    x_validation, y_validation = split_features_target(validation_frame, target)
    pipeline = fit_xgboost(x_train, y_train, x_validation, y_validation, **params)
    validation_scores = pipeline.predict_proba(x_validation)[:, 1]
    calibrator = fit_calibrator(validation_scores, y_validation, settings["calibration_method"])
    calibrated_scores = calibrator.transform(validation_scores)
    threshold = select_threshold(y_validation, calibrated_scores, **_threshold_grid(settings))
    test_metrics = None
    if test_frame is not None:
        x_test, y_test = split_features_target(test_frame, target)
        raw_test_scores = pipeline.predict_proba(x_test)[:, 1]
        test_metrics = evaluate_model(
            y_test,
            calibrator.transform(raw_test_scores),
            threshold,
            top_fraction=settings["top_fraction"],
            raw_scores=raw_test_scores,
        )
    estimator = pipeline.named_steps["model"]
    return {
        "pipeline": pipeline,
        "calibrator": calibrator,
        "calibration": describe_calibrator(calibrator),
        "threshold": threshold,
        "recommended_triage": recommend_triage_thresholds(
            calibrated_scores, threshold, settings["top_fraction"]
        ),
        "best_iteration": _best_iteration(estimator),
        "feature_columns": list(x_train.columns),
        "model_version": datetime.now(timezone.utc).strftime("xgb-%Y%m%d%H%M%S"),
        "metrics": evaluate_model(
            y_validation,
            calibrated_scores,
            threshold,
            top_fraction=settings["top_fraction"],
            raw_scores=validation_scores,
        ),
        "test_metrics": test_metrics,
    }


def select_threshold(
    y_true: pd.Series,
    scores: np.ndarray,
    threshold_min: float = 0.05,
    threshold_max: float = 0.95,
    threshold_step: float = 0.05,
) -> float:
    """Return the F1-maximising threshold on the grid; ties go to the lower one."""
    candidates = np.round(
        np.arange(threshold_min, threshold_max + threshold_step / 2, threshold_step), 6
    )
    values = [(f1_score(y_true, scores >= threshold, zero_division=0), threshold) for threshold in candidates]
    return float(max(values, key=lambda item: (item[0], -item[1]))[1])


def top_k_metrics(y_true: pd.Series | np.ndarray, scores: np.ndarray, fraction: float) -> dict[str, float]:
    """Precision/recall when analysts review only the top ``fraction`` of transactions."""
    labels = np.asarray(y_true)
    k = max(1, int(np.ceil(len(scores) * fraction)))
    top_index = np.argsort(-np.asarray(scores), kind="stable")[:k]
    found = int(labels[top_index].sum())
    return {
        "top_k": k,
        "top_k_fraud_found": found,
        "precision_at_top_k": found / k,
        "recall_at_top_k": found / max(int(labels.sum()), 1),
    }


def evaluate_model(
    y_true: pd.Series,
    scores: np.ndarray,
    threshold: float,
    top_fraction: float | None = None,
    raw_scores: np.ndarray | None = None,
) -> dict[str, float]:
    """Metrics for calibrated ``scores`` at ``threshold``.

    ``raw_scores`` (the uncalibrated model output) is used for ranking metrics
    because isotonic calibration collapses many scores into ties.
    """
    labels = np.asarray(y_true).astype(int)
    predictions = np.asarray(scores) >= threshold
    tn, fp, fn, tp = confusion_matrix(labels, predictions, labels=[0, 1]).ravel()
    ranking = np.asarray(raw_scores) if raw_scores is not None else np.asarray(scores)
    metrics: dict[str, float] = {
        "pr_auc": float(average_precision_score(labels, scores)),
        "precision": float(precision_score(labels, predictions, zero_division=0)),
        "recall": float(recall_score(labels, predictions, zero_division=0)),
        "f1": float(f1_score(labels, predictions, zero_division=0)),
        "roc_auc": float(roc_auc_score(labels, ranking)) if len(np.unique(labels)) > 1 else float("nan"),
        "brier": float(brier_score_loss(labels, np.clip(scores, 0, 1))),
        "tp": int(tp),
        "fp": int(fp),
        "fn": int(fn),
        "tn": int(tn),
    }
    if raw_scores is not None:
        metrics["pr_auc_raw"] = float(average_precision_score(labels, ranking))
    if top_fraction:
        metrics.update(top_k_metrics(labels, ranking, top_fraction))
    return metrics


def recommend_triage_thresholds(
    calibrated_validation_scores: np.ndarray, high_threshold: float, review_fraction: float
) -> dict[str, float]:
    """Suggest ``low_max``/``high_min`` for ``threshold_configs`` (calibrated scale).

    HIGH starts at the F1 threshold; MEDIUM starts where the review queue
    (MEDIUM + HIGH) holds about ``review_fraction`` of validation traffic,
    as in the notebook's 1% analyst-capacity rule.
    """
    scores = np.asarray(calibrated_validation_scores)
    low = float(np.quantile(scores, 1 - review_fraction))
    if low >= high_threshold:
        low = float(np.quantile(scores, 1 - 1.5 * review_fraction))
    low = min(low, high_threshold)
    return {
        "low_max": round(low, 4),
        "high_min": round(float(high_threshold), 4),
        "review_fraction": float(review_fraction),
        "validation_review_rate": float((scores >= low).mean()),
    }


def save_artifact(artifact: dict[str, Any], path: str | Path, source_files: list[Path] | None = None) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lineage = {
        "model_version": artifact["model_version"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metrics": artifact["metrics"],
        "test_metrics": artifact.get("test_metrics"),
        "threshold": artifact["threshold"],
        "calibration": artifact.get("calibration") or describe_calibrator(artifact["calibrator"]),
        "recommended_triage": artifact.get("recommended_triage"),
        "best_iteration": artifact.get("best_iteration"),
        "feature_columns": artifact["feature_columns"],
        "source_sha256": _hash_files(source_files or []),
    }
    artifact = {**artifact, "lineage": lineage}
    joblib.dump(artifact, destination)
    destination.with_suffix(".json").write_text(json.dumps(lineage, indent=2), encoding="utf-8")


def load_artifact(path: str | Path) -> dict[str, Any]:
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise FileNotFoundError(f"Trained model artifact does not exist: {artifact_path.resolve()}")
    artifact = joblib.load(artifact_path)
    for key in ("pipeline", "calibrator", "model_version", "feature_columns"):
        if key not in artifact:
            raise ValueError(f"Model artifact is invalid; missing key: {key}")
    return artifact


def _threshold_grid(settings: dict[str, Any]) -> dict[str, float]:
    return {key: settings[key] for key in ("threshold_min", "threshold_max", "threshold_step")}


def _best_iteration(estimator: Any) -> int | None:
    value = getattr(estimator, "best_iteration", None)
    return int(value) if value is not None else None


def _hash_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.read_bytes())
    return digest.hexdigest()
