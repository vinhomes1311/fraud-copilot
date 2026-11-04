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
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from xgboost import XGBClassifier


def build_preprocessor(frame: pd.DataFrame) -> ColumnTransformer:
    numeric = frame.select_dtypes(include=["number", "bool"]).columns.tolist()
    categorical = [column for column in frame.columns if column not in numeric]
    try:
        encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=True)
    except TypeError:
        encoder = OneHotEncoder(handle_unknown="ignore", sparse=True)
    return ColumnTransformer(
        [
            ("num", Pipeline([("imputer", SimpleImputer(strategy="median"))]), numeric),
            (
                "cat",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="most_frequent")),
                        ("onehot", encoder),
                    ]
                ),
                categorical,
            ),
        ],
        remainder="drop",
    )


def train_model(
    train_frame: pd.DataFrame,
    validation_frame: pd.DataFrame,
    test_frame: pd.DataFrame | None = None,
    target: str = "is_fraud",
    **params: Any,
) -> dict[str, Any]:
    x_train = train_frame.drop(columns=[target, "_sort_time"], errors="ignore")
    y_train = train_frame[target].astype(int)
    x_validation = validation_frame.drop(columns=[target, "_sort_time"], errors="ignore")
    y_validation = validation_frame[target].astype(int)
    preprocessor = build_preprocessor(x_train)
    x_train_transformed = preprocessor.fit_transform(x_train)
    x_validation_transformed = preprocessor.transform(x_validation)
    negatives = int((y_train == 0).sum())
    positives = int((y_train == 1).sum())
    model_params = {
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "n_estimators": 1000,
        "max_depth": 6,
        "learning_rate": 0.1,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 1,
        "reg_lambda": 1.0,
        "scale_pos_weight": negatives / max(positives, 1),
        "tree_method": "hist",
        "n_jobs": -1,
        "random_state": 42,
        "early_stopping_rounds": 100,
        "device": "cpu",
    }
    model_params.update(params)
    estimator = XGBClassifier(**model_params)
    estimator.fit(
        x_train_transformed,
        y_train,
        eval_set=[(x_validation_transformed, y_validation)],
        verbose=False,
    )
    pipeline = Pipeline([("preprocessor", preprocessor), ("model", estimator)])
    validation_scores = pipeline.predict_proba(x_validation)[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip").fit(validation_scores, y_validation)
    calibrated_scores = calibrator.transform(validation_scores)
    threshold = select_threshold(y_validation, calibrated_scores)
    test_metrics = None
    if test_frame is not None:
        x_test = test_frame.drop(columns=[target, "_sort_time"], errors="ignore")
        y_test = test_frame[target].astype(int)
        test_scores = calibrator.transform(pipeline.predict_proba(x_test)[:, 1])
        test_metrics = evaluate_model(y_test, test_scores, threshold)
    return {
        "pipeline": pipeline,
        "calibrator": calibrator,
        "threshold": threshold,
        "feature_columns": list(x_train.columns),
        "model_version": datetime.now(timezone.utc).strftime("xgb-%Y%m%d%H%M%S"),
        "metrics": evaluate_model(y_validation, calibrated_scores, threshold),
        "test_metrics": test_metrics,
    }


def select_threshold(y_true: pd.Series, scores: np.ndarray) -> float:
    candidates = np.arange(0.05, 0.96, 0.05)
    values = [(f1_score(y_true, scores >= threshold, zero_division=0), threshold) for threshold in candidates]
    return float(max(values, key=lambda item: (item[0], -item[1]))[1])


def evaluate_model(y_true: pd.Series, scores: np.ndarray, threshold: float) -> dict[str, float]:
    predictions = scores >= threshold
    return {
        "pr_auc": float(average_precision_score(y_true, scores)),
        "precision": float(precision_score(y_true, predictions, zero_division=0)),
        "recall": float(recall_score(y_true, predictions, zero_division=0)),
        "f1": float(f1_score(y_true, predictions, zero_division=0)),
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


def _hash_files(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.read_bytes())
    return digest.hexdigest()
