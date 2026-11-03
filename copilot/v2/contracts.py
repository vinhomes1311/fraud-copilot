"""Provisional backend contract v2; all unknown fields are rejected."""
import re
from datetime import datetime
from uuid import UUID
from ..contracts import ContractError, fields, text, number

ROUTES = ("LOW_RISK", "MEDIUM_RISK", "HIGH_RISK", "UNSCORED_FALLBACK")


def timestamp(value):
    text(value, 50)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ContractError("Invalid ISO timestamp") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ContractError("Timezone required")
    return dt


def validate(case):
    fields(case, ("schema_version", "case_id", "transaction_id", "dataset", "input_data_version",
                  "transaction", "scoring", "risk_factors_json", "history"))
    if case["schema_version"] != "2.0":
        raise ContractError("Unsupported schema_version")
    try:
        UUID(text(case["case_id"]))
    except ValueError:
        raise ContractError("case_id must be UUID") from None
    for name in ("transaction_id", "input_data_version"):
        text(case[name])
    if case["dataset"] not in ("ulb", "ieee_cis", "sparkov"):
        raise ContractError("Unknown dataset")
    tx = case["transaction"]
    fields(tx, ("amount", "currency", "tx_timestamp", "relative_time_seconds", "data_quality_status"))
    if tx["data_quality_status"] not in ("VALID", "MISSING_REQUIRED", "SCHEMA_ERROR", "OUT_OF_RANGE"):
        raise ContractError("Invalid data_quality_status")
    for key in ("amount", "relative_time_seconds"):
        if tx[key] is not None and number(tx[key]) < 0:
            raise ContractError("Negative amount/time; sanitize invalid raw fields to null")
    if tx["currency"] is not None and (not isinstance(tx["currency"], str) or not re.fullmatch(r"[A-Z]{3}", tx["currency"])):
        raise ContractError("Invalid currency code")
    if tx["tx_timestamp"] is not None:
        timestamp(tx["tx_timestamp"])
    scoring = case["scoring"]
    fields(scoring, ("raw_model_score", "calibrated_prob", "model_version", "threshold_config_version", "routing_decision"))
    if scoring["routing_decision"] not in ROUTES:
        raise ContractError("Unknown routing_decision")
    unscored = scoring["routing_decision"] == "UNSCORED_FALLBACK"
    if tx["data_quality_status"] != "VALID" and not unscored:
        raise ContractError("Invalid data cannot be scored/routed as normal")
    if unscored:
        if any(scoring[k] is not None for k in ("raw_model_score", "calibrated_prob")):
            raise ContractError("Unscored case must not contain scores")
    else:
        if tx["amount"] is None or (tx["tx_timestamp"] is None and tx["relative_time_seconds"] is None):
            raise ContractError("Scored case requires amount and time reference")
        number(scoring["raw_model_score"])
        text(scoring["model_version"])
        text(scoring["threshold_config_version"])
    if scoring["calibrated_prob"] is not None and not 0 <= number(scoring["calibrated_prob"]) <= 1:
        raise ContractError("Invalid calibrated probability")
    for name in ("model_version", "threshold_config_version"):
        if scoring[name] is not None:
            text(scoring[name])
    factors = case["risk_factors_json"]
    if not isinstance(factors, list) or len(factors) > 10 or (unscored and factors):
        raise ContractError("Invalid risk factors")
    names = set()
    for factor in factors:
        fields(factor, ("feature", "value", "contribution", "method", "output_scale", "business_meaning"))
        feature = text(factor["feature"], 80)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", feature) or feature.lower() in {"class", "isfraud", "is_fraud", "target", "label", "transaction_id", "transactionid", "card_number", "cc_num", "name", "address", "email"}:
            raise ContractError("Forbidden or invalid feature")
        if feature in names:
            raise ContractError("Duplicate feature")
        names.add(feature)
        number(factor["value"])
        number(factor["contribution"])
        if factor["method"] not in ("shap", "local_contribution"):
            raise ContractError("Only local explanations are supported")
        if factor["output_scale"] not in ("raw_score", "log_odds", "probability"):
            raise ContractError("Explanation scale required")
        meaning = factor["business_meaning"]
        if meaning is not None:
            text(meaning, 200)
            if re.fullmatch(r"V\d+", feature, re.I):
                raise ContractError("No business meaning may be assigned to anonymous V-features")
    history = case["history"]
    fields(history, ("available", "window_end", "transaction_count", "median_amount"))
    if type(history["available"]) is not bool:
        raise ContractError("history.available must be boolean")
    if not history["available"]:
        if any(history[key] is not None for key in ("window_end", "transaction_count", "median_amount")):
            raise ContractError("Unavailable history must be null")
    else:
        if tx["tx_timestamp"] is None or timestamp(history["window_end"]) >= timestamp(tx["tx_timestamp"]):
            raise ContractError("History must end strictly before transaction")
        if type(history["transaction_count"]) is not int or history["transaction_count"] < 0:
            raise ContractError("Invalid history count")
        if history["median_amount"] is not None and number(history["median_amount"]) < 0:
            raise ContractError("Invalid historical amount")
        if history["transaction_count"] == 0 and history["median_amount"] is not None:
            raise ContractError("Empty history cannot have median")
        if case["dataset"] == "ulb":
            raise ContractError("ULB does not supply account history in this contract")
    return case
