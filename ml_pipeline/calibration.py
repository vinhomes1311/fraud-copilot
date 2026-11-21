"""Probability calibration for the fraud model.

XGBoost is trained with ``scale_pos_weight`` so its raw scores overstate the
fraud rate. Isotonic regression fixes that but is a step function: on the test
set it produced only 137 distinct probabilities, 69% of frauds shared the exact
same value (100%) and a third of all transactions got exactly 0%.

``LogitPlattCalibrator`` fits ``p = sigmoid(slope * logit(raw) + intercept)`` on
validation scores instead. It is smooth and strictly increasing (ranking is
unchanged), never returns exactly 0 or 1, and gave lower Brier score and log
loss than isotonic on the hold-out test set.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

CALIBRATION_METHODS = ("platt", "isotonic")
_EPS = 1e-7


def logit(probability: Any) -> np.ndarray:
    clipped = np.clip(np.asarray(probability, dtype=float), _EPS, 1 - _EPS)
    return np.log(clipped / (1 - clipped))


def sigmoid(value: Any) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.asarray(value, dtype=float)))


class LogitPlattCalibrator:
    """Platt scaling on the log-odds of the raw model score.

    Exposes ``transform`` like ``IsotonicRegression`` so serving code can use
    either calibrator. Only two floats are stored, so the artifact stays small
    and the mapping is easy to audit.
    """

    method = "platt"

    def __init__(self) -> None:
        self.slope_: float | None = None
        self.intercept_: float | None = None

    def fit(self, raw_scores: Any, y_true: Any) -> "LogitPlattCalibrator":
        labels = np.asarray(y_true).astype(int)
        if len(np.unique(labels)) < 2:
            raise ValueError("Calibration needs both fraud and non-fraud examples")
        model = LogisticRegression(C=1e6, max_iter=1000)
        model.fit(logit(raw_scores).reshape(-1, 1), labels)
        self.slope_ = float(model.coef_[0, 0])
        self.intercept_ = float(model.intercept_[0])
        if self.slope_ <= 0:
            raise ValueError("Calibration slope must be positive to preserve the model ranking")
        return self

    def transform(self, raw_scores: Any) -> np.ndarray:
        if self.slope_ is None or self.intercept_ is None:
            raise RuntimeError("LogitPlattCalibrator is not fitted")
        return sigmoid(self.slope_ * logit(raw_scores) + self.intercept_)

    predict = transform

    def describe(self) -> dict[str, Any]:
        return {"method": self.method, "slope": self.slope_, "intercept": self.intercept_}


def fit_calibrator(raw_scores: Any, y_true: Any, method: str = "platt") -> Any:
    if method == "platt":
        return LogitPlattCalibrator().fit(raw_scores, y_true)
    if method == "isotonic":
        return IsotonicRegression(out_of_bounds="clip").fit(raw_scores, y_true)
    raise ValueError(f"Unsupported calibration method: {method}. Use one of {CALIBRATION_METHODS}")


def describe_calibrator(calibrator: Any) -> dict[str, Any]:
    if hasattr(calibrator, "describe"):
        return calibrator.describe()
    if isinstance(calibrator, IsotonicRegression):
        return {"method": "isotonic", "steps": int(len(getattr(calibrator, "X_thresholds_", [])))}
    return {"method": type(calibrator).__name__}
