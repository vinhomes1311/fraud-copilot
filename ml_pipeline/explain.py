"""Turn XGBoost TreeSHAP contributions into analyst-readable risk factors.

Each factor keeps the raw ``feature``/``value``/``contribution`` (log-odds) for
auditing and adds:

- ``label``: readable feature name,
- ``display_value``: the original value formatted for people,
- ``reason``: one factual sentence, compared with the card's own history where
  that history exists,
- ``share_pct``: share of the model's total evidence (sum of |contribution|),
- ``probability_without``: calibrated probability if this factor's
  contribution were removed (approximate; ignores feature interactions).

Text is English to match the dashboard and never adds a currency symbol,
because the dataset does not state one.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd
import xgboost as xgb

from .calibration import sigmoid

DEFAULT_TOP_K = 5

FEATURE_LABELS = {
    "amt": "Transaction amount",
    "amt_to_avg_7d_ratio": "Amount vs. 7-day average",
    "amt_to_max_7d_ratio": "Amount vs. 7-day maximum",
    "sum_amt_24h": "Total spent (previous 24 hours)",
    "avg_amt_7d": "Average amount (previous 7 days)",
    "std_amt_7d": "Amount variability (previous 7 days)",
    "max_amt_7d": "Largest amount (previous 7 days)",
    "trans_count_1h": "Transactions in the previous hour",
    "trans_count_24h": "Transactions in the previous 24 hours",
    "trans_count_7d": "Transactions in the previous 7 days",
    "time_since_last_trans_sec": "Time since previous transaction",
    "transaction_hour": "Transaction hour",
    "transaction_dayofweek": "Day of week",
    "is_weekend": "Weekend transaction",
    "transaction_month": "Transaction month",
    "transaction_day": "Day of month",
    "customer_merchant_distance_km": "Home-to-merchant distance",
    "customer_age": "Customer age",
    "city_pop": "City population",
    "merchant": "Merchant",
    "category": "Category",
}

CATEGORY_LABELS = {
    "entertainment": "Entertainment",
    "food_dining": "Food & dining",
    "gas_transport": "Gas & transport",
    "grocery_net": "Grocery (online)",
    "grocery_pos": "Grocery (in person)",
    "health_fitness": "Health & fitness",
    "home": "Home",
    "kids_pets": "Kids & pets",
    "misc_net": "Miscellaneous (online)",
    "misc_pos": "Miscellaneous (in person)",
    "personal_care": "Personal care",
    "shopping_net": "Shopping (online)",
    "shopping_pos": "Shopping (in person)",
    "travel": "Travel",
}

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def explain_prediction(
    artifact: dict[str, Any],
    frame: pd.DataFrame,
    features: dict[str, Any],
    top_k: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """Return the ``top_k`` factors ranked by absolute contribution."""
    grouped, bias = feature_contributions(artifact, frame)
    if not grouped:
        return []
    margin = bias + sum(grouped.values())
    total = sum(abs(value) for value in grouped.values()) or 1.0
    calibrator = artifact["calibrator"]
    ranked = sorted(grouped.items(), key=lambda item: abs(item[1]), reverse=True)[:top_k]
    factors = []
    for name, contribution in ranked:
        probability_without = float(calibrator.transform([float(sigmoid(margin - contribution))])[0])
        factors.append(
            {
                "feature": name,
                "value": _numeric_value(features.get(name)),
                "contribution": float(contribution),
                "label": FEATURE_LABELS.get(name, name.replace("_", " ").capitalize()),
                "display_value": display_value(name, features.get(name)),
                "reason": describe_feature(name, features),
                "share_pct": round(abs(contribution) / total * 100, 1),
                "probability_without": probability_without,
            }
        )
    return factors


def feature_contributions(artifact: dict[str, Any], frame: pd.DataFrame) -> tuple[dict[str, float], float]:
    """TreeSHAP log-odds per original column (one-hot columns summed back)."""
    estimator = artifact["pipeline"].named_steps["model"]
    preprocessor = artifact["pipeline"].named_steps["preprocessor"]
    names = preprocessor.get_feature_names_out()
    # predict_proba stops at the early-stopping best iteration; SHAP must use
    # the same trees or contributions will not add up to the served score.
    best_iteration = getattr(estimator, "best_iteration", None)
    iteration_range = (0, int(best_iteration) + 1) if best_iteration is not None else (0, 0)
    contributions = estimator.get_booster().predict(
        xgb.DMatrix(preprocessor.transform(frame)), pred_contribs=True, iteration_range=iteration_range
    )[0]
    grouped: dict[str, float] = {}
    for name, contribution in zip(names, contributions[:-1]):
        source = str(name).split("__", 1)[-1]
        if source not in frame.columns:
            source = next((column for column in frame.columns if source.startswith(f"{column}_")), source)
        grouped[source] = grouped.get(source, 0.0) + float(contribution)
    return grouped, float(contributions[-1])


def display_value(name: str, value: Any) -> str:
    if _missing(value):
        return "Not available"
    if name == "category":
        return CATEGORY_LABELS.get(str(value), str(value))
    if isinstance(value, str):
        return value
    number = float(value)
    if name in {"amt", "sum_amt_24h", "avg_amt_7d", "std_amt_7d", "max_amt_7d"}:
        return _amount(number)
    if name in {"amt_to_avg_7d_ratio", "amt_to_max_7d_ratio"}:
        return f"{number:.1f}×"
    if name.startswith("trans_count_"):
        return f"{int(number):,}"
    if name == "time_since_last_trans_sec":
        return "No previous transaction" if number < 0 else _duration(number)
    if name == "transaction_hour":
        return f"{int(number):02d}:00"
    if name == "transaction_dayofweek":
        return _DAYS[int(number)] if 0 <= number <= 6 else "Not available"
    if name == "transaction_month":
        return _MONTHS[int(number) - 1] if 1 <= number <= 12 else "Not available"
    if name == "is_weekend":
        return "Yes" if number >= 1 else "No"
    if name == "customer_merchant_distance_km":
        return f"{number:,.1f} km"
    if name == "customer_age":
        return f"{number:.0f} years"
    if name in {"city_pop", "transaction_day"}:
        return f"{int(number):,}"
    return f"{number:,.4g}"


def describe_feature(name: str, features: dict[str, Any]) -> str:
    value = features.get(name)
    if _missing(value):
        return "Value not available for this transaction"
    count_1h = _number(features.get("trans_count_1h"))
    count_24h = _number(features.get("trans_count_24h"))
    count_7d = _number(features.get("trans_count_7d"))
    average = _number(features.get("avg_amt_7d"))
    maximum = _number(features.get("max_amt_7d"))
    if name == "category":
        label = CATEGORY_LABELS.get(str(value), str(value))
        return f"{label} purchases carry this risk effect in the model's training history"
    if name == "merchant":
        return f"Risk effect learned from past transactions at {value}"
    number = float(value)
    if name == "amt":
        if count_7d and average:
            ratio = number / average
            return (f"{_amount(number)} is {ratio:.1f}× this card's average of {_amount(average)} "
                    f"over {_plural(count_7d, 'transaction')} in the previous 7 days")
        return f"{_amount(number)}; no earlier transactions on this card in the previous 7 days to compare with"
    if name == "amt_to_avg_7d_ratio":
        if not count_7d:
            return "No 7-day spending history on this card to compare the amount with"
        return f"Amount is about {number:.1f}× this card's 7-day average of {_amount(average)}"
    if name == "amt_to_max_7d_ratio":
        if not count_7d:
            return "No earlier purchase on this card in the previous 7 days"
        return f"Amount is about {number:.1f}× the largest purchase in the previous 7 days ({_amount(maximum)})"
    if name == "sum_amt_24h":
        if not count_24h:
            return "No spending on this card in the previous 24 hours"
        return f"{_amount(number)} spent across {_plural(count_24h, 'earlier transaction')} in the previous 24 hours"
    if name == "avg_amt_7d":
        if not count_7d:
            return "No spending history on this card in the previous 7 days"
        return f"Average of {_amount(number)} across {_plural(count_7d, 'transaction')} in the previous 7 days"
    if name == "std_amt_7d":
        if count_7d < 2:
            return "Not enough 7-day history to measure how much spending varies"
        return f"Spending on this card varied by about ±{_amount(number)} over the previous 7 days"
    if name == "max_amt_7d":
        if not count_7d:
            return "No earlier purchase on this card in the previous 7 days"
        return f"Largest purchase in the previous 7 days was {_amount(number)}"
    if name in {"trans_count_1h", "trans_count_24h", "trans_count_7d"}:
        window = {"trans_count_1h": "hour", "trans_count_24h": "24 hours", "trans_count_7d": "7 days"}[name]
        return f"{_plural(number, 'earlier transaction')} on this card in the previous {window}"
    if name == "time_since_last_trans_sec":
        if number < 0:
            return "First transaction seen for this card"
        return f"Previous transaction on this card was {_duration(number)} earlier"
    if name == "transaction_hour":
        hour = int(number)
        period = " (late night)" if hour >= 22 or hour < 4 else " (early morning)" if hour < 6 else ""
        return f"Made at {hour:02d}:00{period}"
    if name == "transaction_dayofweek":
        return f"Made on a {display_value(name, number)}"
    if name == "is_weekend":
        return "Made on a weekend" if number >= 1 else "Made on a weekday"
    if name == "transaction_month":
        return f"Made in {display_value(name, number)}"
    if name == "transaction_day":
        return f"Made on day {int(number)} of the month"
    if name == "customer_merchant_distance_km":
        return f"Merchant is {number:,.1f} km from the cardholder's address"
    if name == "customer_age":
        return f"Cardholder is {number:.0f} years old"
    if name == "city_pop":
        return f"Cardholder's city has a population of {int(number):,}"
    return f"{FEATURE_LABELS.get(name, name)} is {display_value(name, number)}"


def _numeric_value(value: Any) -> float:
    if _missing(value):
        return 0.0
    if isinstance(value, str):
        return 1.0
    return float(value)


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def _number(value: Any) -> float:
    return 0.0 if _missing(value) or isinstance(value, str) else float(value)


def _amount(value: float) -> str:
    return f"{value:,.2f}"


def _plural(count: float, noun: str) -> str:
    whole = int(count)
    return f"{whole:,} {noun}{'' if whole == 1 else 's'}"


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} seconds"
    if seconds < 3600:
        return f"{seconds / 60:.0f} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


__all__ = ["explain_prediction", "feature_contributions", "describe_feature", "display_value"]
