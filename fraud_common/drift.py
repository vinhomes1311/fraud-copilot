"""Data/score drift monitoring with the Population Stability Index (PSI).

The reference distribution ("baseline") is built at training time by
``ml_pipeline.drift.build_drift_baseline`` and stored in the model lineage
JSON. This module only needs the standard library, so the Analyst API can
compare live scoring traffic against it without importing numpy/xgboost.

Numeric bins are right-closed: ``(-inf, e0], (e0, e1], ..., (e_last, inf)``,
which ``bisect_left`` reproduces exactly (``numpy.searchsorted(side="left")``
on the training side). Missing values get their own bin so a broken upstream
field shows up as drift instead of disappearing.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from typing import Any

MISSING = "missing"
OTHER = "other"
EPSILON = 1e-4
# Common industry cut-offs for PSI.
STABLE_MAX = 0.10
MODERATE_MAX = 0.25
# Below this many live rows, PSI is too noisy to report a status.
MIN_ROWS = 200


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def assign_bin(spec: Mapping[str, Any], value: Any) -> str:
    """Return the bin key of ``value`` under one feature's baseline spec."""
    if spec["kind"] == "categorical":
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return MISSING
        key = str(value)
        return key if key in spec["expected"] else OTHER
    number = _number(value)
    if number is None:
        return MISSING
    return str(bisect_left(spec["edges"], number))


def proportions(spec: Mapping[str, Any], values: Iterable[Any]) -> tuple[dict[str, float], int]:
    counts: dict[str, int] = {}
    total = 0
    for value in values:
        key = assign_bin(spec, value)
        counts[key] = counts.get(key, 0) + 1
        total += 1
    if not total:
        return {}, 0
    return {key: count / total for key, count in counts.items()}, total


def psi(expected: Mapping[str, float], actual: Mapping[str, float]) -> float:
    """PSI = sum((a - e) * ln(a / e)); empty bins are floored at EPSILON."""
    total = 0.0
    for key in set(expected) | set(actual):
        e = max(float(expected.get(key, 0.0)), EPSILON)
        a = max(float(actual.get(key, 0.0)), EPSILON)
        total += (a - e) * math.log(a / e)
    return total


def drift_status(value: float | None, rows: int) -> str:
    if value is None or rows < MIN_ROWS:
        return "insufficient_data"
    if value < STABLE_MAX:
        return "stable"
    if value < MODERATE_MAX:
        return "moderate"
    return "significant"


def _top_shift(spec: Mapping[str, Any], actual: Mapping[str, float]) -> dict[str, Any] | None:
    """The bin whose share moved most, described with readable bounds."""
    expected = spec["expected"]
    keys = set(expected) | set(actual)
    if not keys:
        return None
    key = max(keys, key=lambda k: abs(actual.get(k, 0.0) - expected.get(k, 0.0)))
    return {"bin": describe_bin(spec, key), "expected": round(expected.get(key, 0.0), 4),
            "actual": round(actual.get(key, 0.0), 4)}


def describe_bin(spec: Mapping[str, Any], key: str) -> str:
    if spec["kind"] == "categorical" or key in (MISSING, OTHER):
        return key
    edges = spec["edges"]
    index = int(key)
    low = edges[index - 1] if index > 0 else None
    high = edges[index] if index < len(edges) else None
    fmt = lambda v: f"{v:.4g}"  # noqa: E731
    if low is None:
        return f"<= {fmt(high)}"
    if high is None:
        return f"> {fmt(low)}"
    return f"({fmt(low)}, {fmt(high)}]"


def evaluate_drift(
    baseline: Mapping[str, Any],
    feature_rows: list[Mapping[str, Any]],
    scores: list[Any],
    risk_levels: list[str] | None = None,
) -> dict[str, Any]:
    """Compare live rows against the training-time baseline.

    ``feature_rows`` are the stored online feature dicts, ``scores`` the
    calibrated probabilities and ``risk_levels`` the routing decisions of the
    same transactions.
    """
    rows = len(feature_rows)
    features = []
    for name, spec in baseline.get("features", {}).items():
        actual, count = proportions(spec, (row.get(name) for row in feature_rows))
        value = psi(spec["expected"], actual) if count else None
        features.append({
            "feature": name,
            "label": spec.get("label", name),
            "psi": round(value, 4) if value is not None else None,
            "status": drift_status(value, count),
            "rows": count,
            "missing_rate": round(actual.get(MISSING, 0.0), 4),
            "top_shift": _top_shift(spec, actual) if count else None,
        })
    features.sort(key=lambda item: -(item["psi"] or 0.0))

    score_report = None
    if baseline.get("score"):
        spec = baseline["score"]
        actual, count = proportions(spec, scores)
        value = psi(spec["expected"], actual) if count else None
        score_report = {"psi": round(value, 4) if value is not None else None,
                        "status": drift_status(value, count), "rows": count,
                        "top_shift": _top_shift(spec, actual) if count else None}

    review = None
    if risk_levels is not None and baseline.get("expected_review_rate") is not None:
        flagged = sum(level in ("MEDIUM", "HIGH") for level in risk_levels)
        actual_rate = flagged / len(risk_levels) if risk_levels else None
        review = {"expected_rate": round(float(baseline["expected_review_rate"]), 5),
                  "actual_rate": round(actual_rate, 5) if actual_rate is not None else None,
                  "rows": len(risk_levels)}

    ranked = [f["status"] for f in features] + ([score_report["status"]] if score_report else [])
    overall = "insufficient_data"
    for level in ("significant", "moderate", "stable"):
        if level in ranked:
            overall = level
            break
    return {
        "rows": rows,
        "overall_status": overall,
        "score": score_report,
        "review_rate": review,
        "features": features,
        "thresholds": {"stable_max": STABLE_MAX, "moderate_max": MODERATE_MAX, "min_rows": MIN_ROWS},
    }
