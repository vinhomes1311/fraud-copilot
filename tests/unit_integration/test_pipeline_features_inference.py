"""Tests for scoring_worker/pipeline/features.py and scoring_worker/pipeline/inference.py."""
import math
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from fraud_common.schemas import TransactionValidated
from scoring_worker.pipeline.features import compute_features, haversine_km
from scoring_worker.pipeline.inference import _explanation_value, predict


def test_haversine_distance():
    # Same point should be 0 km
    assert haversine_km(10.0, 20.0, 10.0, 20.0) == 0.0

    # Distance between Hanoi (21.0285, 105.8542) and HCMC (10.8231, 106.6297) is ~1130-1160 km
    dist = haversine_km(21.0285, 105.8542, 10.8231, 106.6297)
    assert 1100 < dist < 1200


@pytest.mark.asyncio
async def test_compute_features_temporal_aggregation():
    mock_db = AsyncMock()
    mock_db.rfetchrow = AsyncMock(return_value={
        "n_1h": 2,
        "n_24h": 5,
        "n_7d": 10,
        "sum_amt_24h": 250.0,
        "avg_amt_7d": 45.0,
        "std_amt_7d": 12.5,
        "max_amt_7d": 120.0,
        "last_txn_time": datetime(2026, 11, 10, 10, 0, 0, tzinfo=timezone.utc),
    })

    tx = TransactionValidated(
        trans_num="tx_test_feat",
        cc_num=1234567890123456,
        amt=150.0,
        trans_date_trans_time=datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc),
        merchant="fraud_test_shop",
        category="shopping_net",
        first="John",
        last="Doe",
        gender="M",
        street="123 Main St",
        city="Springfield",
        state="IL",
        zip="62701",
        lat=39.7817,
        long=-89.6501,
        city_pop=116000,
        job="Engineer",
        dob=datetime(1990, 5, 15, tzinfo=timezone.utc),
        unix_time=1794312000,
        merch_lat=39.8000,
        merch_long=-89.6000,
    )

    feat = await compute_features(mock_db, tx, "hash_acc_123")

    assert feat["amt"] == 150.0
    assert feat["merchant"] == "fraud_test_shop"
    assert feat["category"] == "shopping_net"
    assert feat["trans_count_1h"] == 2.0
    assert feat["trans_count_24h"] == 5.0
    assert feat["trans_count_7d"] == 10.0
    assert feat["sum_amt_24h"] == 250.0
    assert feat["avg_amt_7d"] == 45.0
    assert feat["max_amt_7d"] == 120.0
    assert feat["time_since_last_trans_sec"] == 7200.0  # 12:00 - 10:00 = 2h = 7200s
    assert feat["customer_age"] is not None
    assert feat["customer_merchant_distance_km"] > 0
    assert feat["is_weekend"] == 0.0  # 2026-11-10 is Tuesday


def test_explanation_value_formatting():
    assert _explanation_value(None) == 0.0
    assert _explanation_value("sample_str") == 1.0
    assert _explanation_value(123.456) == 123.456
    assert _explanation_value(42) == 42.0




def test_inference_predict_with_artifact():
    from ml_pipeline.model import load_artifact

    # Check if real artifact exists, test real prediction
    try:
        load_artifact("data/models/fraud_xgboost.joblib")
    except Exception:
        pytest.skip("Model artifact not found; skipping live artifact prediction test")

    sample_features = {
        "amt": 95.0,
        "category": "grocery_pos",
        "city_pop": 50000,
        "customer_age": 35.0,
        "customer_merchant_distance_km": 15.2,
        "time_since_last_trans_sec": 3600.0,
        "trans_count_1h": 1.0,
        "trans_count_24h": 3.0,
        "trans_count_7d": 8.0,
        "sum_amt_24h": 120.0,
        "avg_amt_7d": 40.0,
        "std_amt_7d": 15.0,
        "max_amt_7d": 85.0,
        "amt_to_avg_7d_ratio": 2.3,
        "amt_to_max_7d_ratio": 1.1,
        "transaction_hour": 14.0,
        "transaction_dayofweek": 2.0,
        "transaction_month": 11.0,
        "transaction_day": 10.0,
        "is_weekend": 0.0,
    }

    with patch.dict("os.environ", {"MODEL_ARTIFACT_PATH": "data/models/fraud_xgboost.joblib"}):
        from scoring_worker.pipeline.inference import _artifact
        _artifact.cache_clear()
        out = predict(sample_features)
        assert 0.0 <= out.raw_model_score <= 1.0
        assert 0.0 <= out.calibrated_prob <= 1.0
        assert out.model_version is not None
        assert len(out.explanations) > 0
