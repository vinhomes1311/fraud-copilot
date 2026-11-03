"""Strict, allowlisted contract: no labels, arbitrary features, or PII."""
import math


class ContractError(ValueError):
    pass


def fields(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ContractError("Missing or unexpected fields")


def text(value, maximum=120):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ContractError("Invalid text")
    return value


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContractError("Expected finite number")
    return value


def validate_case(case):
    fields(case, ("case_id", "dataset", "data_version", "transaction", "ml_result", "routing"))
    text(case["case_id"])
    text(case["data_version"])
    if case["dataset"] not in ("ulb", "ieee_cis"):
        raise ContractError("Unsupported dataset")
    tx = case["transaction"]
    fields(tx, ("amount", "relative_time_seconds"))
    if number(tx["amount"]) < 0 or number(tx["relative_time_seconds"]) < 0:
        raise ContractError("Negative amount or relative time")
    ml = case["ml_result"]
    fields(ml, ("risk_score", "score_type", "model_version"))
    number(ml["risk_score"])
    text(ml["model_version"])
    if ml["score_type"] not in ("raw_score", "calibrated_probability"):
        raise ContractError("Unknown score type")
    if ml["score_type"] == "calibrated_probability" and not 0 <= ml["risk_score"] <= 1:
        raise ContractError("Probability out of range")
    fields(case["routing"], ("priority", "config_version"))
    if case["routing"]["priority"] not in ("low", "medium", "high"):
        raise ContractError("Invalid routing priority")
    text(case["routing"]["config_version"])
    return case


def adapt_row(row, *, dataset, case_id, data_version, ml_result, routing):
    """Explicit boundary: rejects labels; projects only documented safe fields.

    Caller must remove ground truth before using this adapter. Other columns
    (including anonymous features and identifiers) are never sent to the LLM.
    """
    if not isinstance(row, dict):
        raise ContractError("Expected row object")
    if any(str(k).lower() in {"class", "isfraud", "is_fraud", "label", "target"} for k in row):
        raise ContractError("Ground truth must be separated before inference")
    mapping = {"ulb": ("Amount", "Time"), "ieee_cis": ("TransactionAmt", "TransactionDT")}
    if dataset not in mapping:
        raise ContractError("Unsupported dataset")
    amount, timestamp = mapping[dataset]
    try:
        tx = {"amount": float(row[amount]), "relative_time_seconds": float(row[timestamp])}
    except (KeyError, TypeError, ValueError):
        raise ContractError("Missing or invalid source values") from None
    if isinstance(row[amount], bool) or isinstance(row[timestamp], bool):
        raise ContractError("Boolean numeric input")
    return validate_case(dict(case_id=case_id, dataset=dataset, data_version=data_version,
                              transaction=tx, ml_result=ml_result, routing=routing))
