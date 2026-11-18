"""Candidate models benchmarked against the production XGBoost model.

Every candidate is fit on the same train split, may use validation only for
early stopping, and returns raw scores; calibration, threshold selection and
test evaluation are applied identically to all of them in ``compare_models``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

from .model import build_preprocessor, fit_xgboost, scale_pos_weight_for

PRIMARY_MODEL = "xgboost"


class CandidateUnavailable(RuntimeError):
    """Raised when an optional dependency for a candidate is not installed."""


@dataclass
class FitContext:
    random_state: int = 42
    xgboost_params: dict[str, Any] = field(default_factory=dict)
    fast: bool = False


@dataclass
class FittedCandidate:
    predict_raw: Callable[[pd.DataFrame], np.ndarray]
    details: dict[str, Any] = field(default_factory=dict)
    estimator: Any = None


def rule_based_scores(frame: pd.DataFrame) -> np.ndarray:
    """Heuristic business rules from the XGBoost notebook (score in [0, 1])."""
    score = np.zeros(len(frame), dtype=float)
    if "amt" in frame:
        amount = frame["amt"].to_numpy(dtype=float)
        score += np.where(amount > 500, 0.40, np.where(amount > 250, 0.20, 0.0))
        score += np.clip(amount / 100_000.0, 0, 0.04)
    if "transaction_hour" in frame:
        score += np.where(frame["transaction_hour"].isin([0, 1, 2, 3, 4]), 0.25, 0.0)
    if "amt_to_avg_7d_ratio" in frame:
        score += np.where(frame["amt_to_avg_7d_ratio"] > 5.0, 0.20, 0.0)
    if "trans_count_1h" in frame:
        score += np.where(frame["trans_count_1h"] >= 3, 0.15, 0.0)
    return np.clip(score, 0.0, 1.0)


def fit_rule_based(x_train, y_train, x_validation, y_validation, context: FitContext) -> FittedCandidate:
    return FittedCandidate(rule_based_scores, {"rules": 4})


def fit_logistic_regression(x_train, y_train, x_validation, y_validation, context: FitContext) -> FittedCandidate:
    pipeline = Pipeline(
        [
            ("preprocessor", build_preprocessor(x_train, scale_numeric=True)),
            (
                "model",
                LogisticRegression(
                    class_weight="balanced",
                    solver="lbfgs",
                    max_iter=200 if context.fast else 1000,
                    random_state=context.random_state,
                ),
            ),
        ]
    )
    pipeline.fit(x_train, y_train)
    return FittedCandidate(
        lambda frame: pipeline.predict_proba(frame)[:, 1],
        {"iterations": int(np.max(pipeline.named_steps["model"].n_iter_))},
        pipeline,
    )


def fit_random_forest(x_train, y_train, x_validation, y_validation, context: FitContext) -> FittedCandidate:
    pipeline = Pipeline(
        [
            ("preprocessor", build_preprocessor(x_train, categorical_encoding="ordinal")),
            (
                "model",
                RandomForestClassifier(
                    n_estimators=50 if context.fast else 300,
                    min_samples_leaf=5,
                    max_features="sqrt",
                    max_samples=0.5,
                    class_weight="balanced_subsample",
                    n_jobs=-1,
                    random_state=context.random_state,
                ),
            ),
        ]
    )
    pipeline.fit(x_train, y_train)
    return FittedCandidate(
        lambda frame: pipeline.predict_proba(frame)[:, 1],
        {"n_estimators": pipeline.named_steps["model"].n_estimators},
        pipeline,
    )


def fit_lightgbm(x_train, y_train, x_validation, y_validation, context: FitContext) -> FittedCandidate:
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise CandidateUnavailable("lightgbm is not installed (pip install -r requirements-ml.txt)") from exc
    preprocessor = build_preprocessor(x_train)
    x_train_transformed = preprocessor.fit_transform(x_train).astype(np.float32)
    x_validation_transformed = preprocessor.transform(x_validation).astype(np.float32)
    estimator = lgb.LGBMClassifier(
        objective="binary",
        n_estimators=200 if context.fast else 3000,
        learning_rate=0.05,
        num_leaves=63,
        max_depth=8,
        subsample=0.8,
        subsample_freq=1,
        colsample_bytree=0.8,
        min_child_samples=50,
        reg_lambda=1.0,
        scale_pos_weight=scale_pos_weight_for(y_train),
        n_jobs=-1,
        random_state=context.random_state,
        verbose=-1,
    )
    estimator.fit(
        x_train_transformed,
        y_train,
        eval_set=[(x_validation_transformed, y_validation)],
        eval_metric="average_precision",
        callbacks=[lgb.early_stopping(stopping_rounds=100, verbose=False)],
    )
    pipeline = Pipeline([("preprocessor", preprocessor), ("model", estimator)])

    def predict(frame: pd.DataFrame) -> np.ndarray:
        return estimator.predict_proba(preprocessor.transform(frame).astype(np.float32))[:, 1]

    return FittedCandidate(predict, {"best_iteration": int(estimator.best_iteration_ or 0)}, pipeline)


def fit_xgboost_candidate(x_train, y_train, x_validation, y_validation, context: FitContext) -> FittedCandidate:
    params = {"random_state": context.random_state, **context.xgboost_params}
    if context.fast:
        params["n_estimators"] = min(int(params.get("n_estimators", 1000)), 100)
    pipeline = fit_xgboost(x_train, y_train, x_validation, y_validation, **params)
    best_iteration = getattr(pipeline.named_steps["model"], "best_iteration", None)
    return FittedCandidate(
        lambda frame: pipeline.predict_proba(frame)[:, 1],
        {"best_iteration": int(best_iteration) if best_iteration is not None else None},
        pipeline,
    )


CANDIDATES: dict[str, Callable[..., FittedCandidate]] = {
    "rule_based": fit_rule_based,
    "logistic_regression": fit_logistic_regression,
    "random_forest": fit_random_forest,
    "lightgbm": fit_lightgbm,
    PRIMARY_MODEL: fit_xgboost_candidate,
}

# Ensembles average the raw scores of already-fitted members.
ENSEMBLES: dict[str, tuple[str, ...]] = {
    "ensemble_xgboost_lightgbm": (PRIMARY_MODEL, "lightgbm"),
}

DISPLAY_NAMES = {
    "rule_based": "Rule-based heuristic",
    "logistic_regression": "Logistic Regression",
    "random_forest": "Random Forest",
    "lightgbm": "LightGBM",
    PRIMARY_MODEL: "XGBoost (production)",
    "ensemble_xgboost_lightgbm": "Ensemble XGBoost + LightGBM",
}
