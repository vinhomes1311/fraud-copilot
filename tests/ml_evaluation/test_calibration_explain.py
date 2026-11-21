import json

import numpy as np
import pandas as pd
import pytest

from fraud_common.schemas import Explanation
from ml_pipeline.calibration import LogitPlattCalibrator, fit_calibrator
from ml_pipeline.config import load_config
from ml_pipeline.explain import describe_feature, display_value, explain_prediction
from ml_pipeline.model import split_features_target, train_model
from ml_pipeline.preprocessing import chronological_split, extract_features


def _scores(seed: int = 0, rows: int = 5000) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    labels = (rng.random(rows) < 0.05).astype(int)
    margin = rng.normal(-4, 1.5, rows) + labels * rng.normal(7, 2, rows)
    return 1 / (1 + np.exp(-margin)), labels


def test_platt_is_smooth_monotonic_and_never_exactly_zero_or_one():
    raw, labels = _scores()
    calibrated = LogitPlattCalibrator().fit(raw, labels).transform(raw)
    order = np.argsort(raw)
    assert np.all(np.diff(calibrated[order]) >= 0)
    assert calibrated.min() > 0 and calibrated.max() < 1
    assert len(np.unique(calibrated)) > 0.9 * len(np.unique(raw))
    # Isotonic is a step function: far fewer distinct probabilities.
    isotonic = fit_calibrator(raw, labels, "isotonic").transform(raw)
    assert len(np.unique(isotonic)) < len(np.unique(calibrated)) / 10


def test_platt_corrects_prior_shift_on_average():
    raw, labels = _scores(seed=1)
    inflated = 1 / (1 + np.exp(-(np.log(raw / (1 - raw)) + np.log(20))))  # like scale_pos_weight
    calibrated = LogitPlattCalibrator().fit(inflated, labels).transform(inflated)
    assert inflated.mean() > 2 * labels.mean()
    assert calibrated.mean() == pytest.approx(labels.mean(), rel=0.1)


def test_calibrator_rejects_single_class_and_unknown_method():
    with pytest.raises(ValueError, match="both"):
        LogitPlattCalibrator().fit([0.1, 0.2], [0, 0])
    with pytest.raises(ValueError, match="Unsupported"):
        fit_calibrator([0.1, 0.9], [0, 1], "beta")
    with pytest.raises(RuntimeError, match="not fitted"):
        LogitPlattCalibrator().transform([0.5])


def test_config_rejects_unknown_calibration_method(tmp_path):
    raw = json.loads(open("ml_pipeline/config.json", encoding="utf-8").read())
    raw["evaluation"]["calibration_method"] = "beta"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="calibration_method"):
        load_config(path)


def test_reasons_compare_with_card_history_and_never_invent_currency():
    features = {"amt": 780.52, "avg_amt_7d": 125.4, "trans_count_7d": 6.0, "max_amt_7d": 300.0,
                "trans_count_1h": 0.0, "trans_count_24h": 2.0, "sum_amt_24h": 210.0,
                "time_since_last_trans_sec": 5400.0, "transaction_hour": 23.0, "category": "shopping_net",
                "merchant": "fraud_Kub-Smith", "customer_merchant_distance_km": 78.6}
    assert describe_feature("amt", features) == (
        "780.52 is 6.2× this card's average of 125.40 over 6 transactions in the previous 7 days")
    assert "2 earlier transactions" in describe_feature("sum_amt_24h", features)
    assert describe_feature("time_since_last_trans_sec", features) == (
        "Previous transaction on this card was 1.5 hours earlier")
    assert describe_feature("transaction_hour", features) == "Made at 23:00 (late night)"
    assert describe_feature("trans_count_1h", features) == "0 earlier transactions on this card in the previous hour"
    assert display_value("category", "shopping_net") == "Shopping (online)"
    assert display_value("merchant", "fraud_Kub-Smith") == "fraud_Kub-Smith"
    assert "78.6 km" in describe_feature("customer_merchant_distance_km", features)
    for name in features:
        assert "$" not in describe_feature(name, features) + display_value(name, features[name])


def test_reasons_state_missing_history_instead_of_comparing_with_zero():
    first = {"amt": 50.0, "amt_to_avg_7d_ratio": 49.0, "avg_amt_7d": 0.0, "trans_count_7d": 0.0, "trans_count_24h": 0.0,
             "sum_amt_24h": 0.0, "time_since_last_trans_sec": -1.0, "std_amt_7d": 0.0}
    assert "no earlier transactions" in describe_feature("amt", first)
    assert describe_feature("amt_to_avg_7d_ratio", first).startswith("No 7-day spending history")
    assert describe_feature("sum_amt_24h", first) == "No spending on this card in the previous 24 hours"
    assert describe_feature("time_since_last_trans_sec", first) == "First transaction seen for this card"
    assert describe_feature("std_amt_7d", first).startswith("Not enough")
    assert describe_feature("customer_age", {"customer_age": None}) == "Value not available for this transaction"


@pytest.fixture(scope="module")
def small_artifact():
    frame = extract_features(_synthetic_raw(2000))
    train, validation, test = chronological_split(frame, 0.6, 0.2)
    artifact = train_model(train, validation, test, n_estimators=60, early_stopping_rounds=20)
    return artifact, test


def test_train_model_uses_platt_by_default_and_records_it(small_artifact):
    artifact, _ = small_artifact
    assert isinstance(artifact["calibrator"], LogitPlattCalibrator)
    assert artifact["calibration"]["method"] == "platt"
    assert artifact["calibration"]["slope"] > 0


def test_explanations_rank_by_impact_and_quantify_it(small_artifact):
    artifact, test = small_artifact
    x_test, _ = split_features_target(test)
    row = x_test.iloc[[int(np.argmax(artifact["pipeline"].predict_proba(x_test)[:, 1]))]]
    features = row.iloc[0].to_dict()
    calibrated = float(artifact["calibrator"].transform(artifact["pipeline"].predict_proba(row)[:, 1])[0])
    factors = explain_prediction(artifact, row, features)
    assert 1 <= len(factors) <= 5
    contributions = [abs(factor["contribution"]) for factor in factors]
    assert contributions == sorted(contributions, reverse=True)
    assert 0 < sum(factor["share_pct"] for factor in factors) <= 100.5
    for factor in factors:
        Explanation(**factor)  # schema accepts every field
        assert factor["label"] and factor["reason"] and factor["display_value"]
        if factor["contribution"] > 0:
            assert factor["probability_without"] < calibrated
        elif factor["contribution"] < 0:
            assert factor["probability_without"] > calibrated
    merchant = next((factor for factor in factors if factor["feature"] == "merchant"), None)
    if merchant:
        assert merchant["display_value"] == features["merchant"]


def test_legacy_explanation_records_still_validate():
    legacy = Explanation(feature="amt", value=10.0, contribution=0.4)
    assert legacy.reason is None and legacy.share_pct is None


def _synthetic_raw(rows: int) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    amount = rng.gamma(2.0, 40.0, rows)
    timestamp = pd.Timestamp("2024-01-01") + pd.to_timedelta(np.sort(rng.uniform(0, 24 * 60, rows)), unit="h")
    fraud = ((amount > 150) & (timestamp.hour < 6)) | (rng.random(rows) < 0.01)
    return pd.DataFrame({
        "trans_date_trans_time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "cc_num": rng.integers(1, 25, rows).astype(str),
        "merchant": rng.choice(["m1", "m2", "m3"], rows),
        "category": rng.choice(["grocery_pos", "shopping_net", "travel"], rows),
        "amt": amount.round(2), "city_pop": rng.integers(100, 100_000, rows),
        "lat": 10.0, "long": 20.0,
        "merch_lat": 10.0 + rng.normal(0, 0.5, rows), "merch_long": 20.0 + rng.normal(0, 0.5, rows),
        "dob": "1980-01-01", "is_fraud": fraud.astype(int),
    })
