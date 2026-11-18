import numpy as np
import pandas as pd
import pytest

from ml_pipeline.candidates import FitContext
from ml_pipeline.compare_models import compare_models, write_reports
from ml_pipeline.config import PipelineConfig
from ml_pipeline.model import evaluate_model, select_threshold
from ml_pipeline.preprocessing import (
    chronological_split,
    chronological_train_validation_split,
    extract_features,
    validate_schema,
)
from ml_pipeline.workflow import PreparedData, prepare_datasets


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trans_date_trans_time": pd.date_range("2024-01-01", periods=10, freq="h"),
            "cc_num": ["1"] * 10,
            "amt": range(1, 11),
            "lat": [10.0] * 10,
            "long": [20.0] * 10,
            "merch_lat": [10.1] * 10,
            "merch_long": [20.1] * 10,
            "is_fraud": [0, 1] * 5,
        }
    )


def test_extract_features_is_temporal_and_removes_pii():
    """#sym:test_extract_features_is_temporal_and_removes_pii
    Kiem tra: feature lich su va loai PII.
    @param Khong co tham so.
    Dung khi: cc_num bi loai, count giao dich dau la 0 va giao dich sau la 1.
    Sai khi: PII con lo hoac count lich su sai.
    """
    result = extract_features(_frame())
    assert "cc_num" not in result
    assert result.loc[0, "trans_count_24h"] == 0
    assert result.loc[1, "trans_count_24h"] == 1
    assert result["is_fraud"].dtype == "int8"


def test_chronological_split_preserves_time_order():
    """#sym:test_chronological_split_preserves_time_order
    Kiem tra: chia tap theo thoi gian.
    @param Khong co tham so.
    Dung khi: cuoi train < dau validation < dau test.
    Sai khi: du lieu bi xao tron gay leakage.
    """
    train, validation, test = chronological_split(extract_features(_frame()), 0.6, 0.2)
    assert train["_sort_time"].max() < validation["_sort_time"].min()
    assert validation["_sort_time"].max() < test["_sort_time"].min()


def test_train_validation_split_keeps_every_row():
    """Pre-split mode must not drop the most recent fraudTrain rows."""
    frame = extract_features(_frame())
    train, validation = chronological_train_validation_split(frame, 0.2)
    assert len(train) + len(validation) == len(frame)
    assert len(validation) == 2
    assert train["_sort_time"].max() < validation["_sort_time"].min()


def test_prepare_datasets_uses_all_presplit_training_rows(tmp_path):
    train_raw = _synthetic_raw(400, "2024-01-01", seed=1)
    test_raw = _synthetic_raw(100, "2024-02-21", seed=2)
    train_raw.to_csv(tmp_path / "train.csv", index=False)
    test_raw.to_csv(tmp_path / "test.csv", index=False)
    config = PipelineConfig(
        data={
            "train_file": str(tmp_path / "train.csv"),
            "test_file": str(tmp_path / "test.csv"),
            "single_file": str(tmp_path / "missing.csv"),
            "processed_dir": str(tmp_path / "processed"),
            "target": "is_fraud",
        },
        split={"train_ratio": 0.7, "validation_ratio": 0.15},
        model={},
        evaluation={},
    )
    prepared = prepare_datasets(config)
    assert len(prepared.train) + len(prepared.validation) == 400
    assert len(prepared.validation) == 60
    assert len(prepared.test) == 100
    assert prepared.validation["_sort_time"].max() < prepared.test["_sort_time"].min()
    # Test rows inherit card history from the train period.
    assert (prepared.test["time_since_last_trans_sec"] != -1).all()
    assert prepared.test["trans_count_7d"].iloc[:5].gt(0).any()


def test_select_threshold_uses_configured_grid():
    y = pd.Series([0, 0, 1, 1])
    scores = np.array([0.1, 0.2, 0.6, 0.9])
    threshold = select_threshold(y, scores, threshold_min=0.3, threshold_max=0.5, threshold_step=0.1)
    assert threshold == pytest.approx(0.3)


def test_evaluate_model_reports_top_k_and_confusion_counts():
    y = pd.Series([0] * 98 + [1, 1])
    scores = np.linspace(0, 1, 100)
    metrics = evaluate_model(y, scores, 0.985, top_fraction=0.01, raw_scores=scores)
    assert (metrics["tp"], metrics["fp"], metrics["fn"]) == (2, 0, 0)
    assert metrics["top_k"] == 1
    assert metrics["recall_at_top_k"] == pytest.approx(0.5)
    assert metrics["precision_at_top_k"] == pytest.approx(1.0)


def test_compare_models_ranks_candidates_against_xgboost(tmp_path):
    frame = extract_features(_synthetic_raw(1500, "2024-01-01", seed=3))
    train, validation, test = chronological_split(frame, 0.6, 0.2)
    prepared = PreparedData(train, validation, test, "synthetic")
    report = compare_models(
        prepared,
        models=["rule_based", "logistic_regression", "random_forest", "xgboost", "ensemble_xgboost_lightgbm"],
        context=FitContext(fast=True),
    )
    by_model = {row["model"]: row for row in report["results"]}
    assert by_model["xgboost"]["status"] == "ok"
    assert by_model["xgboost"]["delta_vs_primary"]["pr_auc"] == 0
    for name in ("rule_based", "logistic_regression", "random_forest"):
        assert by_model[name]["status"] == "ok"
        assert {"pr_auc", "f1", "recall_at_top_k", "brier"} <= set(by_model[name]["test"])
    ranks = [row["rank_by_test_pr_auc"] for row in report["results"] if row["status"] == "ok"]
    assert ranks == sorted(ranks)
    paths = write_reports(report, tmp_path)
    assert all(path.exists() for path in paths.values())
    assert "XGBoost (production)" in paths["md"].read_text(encoding="utf-8")


def test_compare_models_rejects_unknown_model():
    frame = extract_features(_frame())
    train, validation, test = chronological_split(frame, 0.6, 0.2)
    with pytest.raises(ValueError, match="Unknown models"):
        compare_models(PreparedData(train, validation, test, "synthetic"), models=["catboost"])


def _synthetic_raw(rows: int, start: str, seed: int) -> pd.DataFrame:
    """Small Sparkov-like frame where large night-time amounts are fraud."""
    rng = np.random.default_rng(seed)
    amount = rng.gamma(2.0, 40.0, rows)
    hour_offsets = np.sort(rng.uniform(0, 24 * 50, rows))
    timestamp = pd.Timestamp(start) + pd.to_timedelta(hour_offsets, unit="h")
    fraud = (amount > 150) & (timestamp.hour < 6)
    fraud |= rng.random(rows) < 0.01
    return pd.DataFrame(
        {
            "trans_date_trans_time": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            "cc_num": rng.integers(1, 20, rows).astype(str),
            "merchant": rng.choice(["m1", "m2", "m3", "m4"], rows),
            "category": rng.choice(["grocery_pos", "shopping_net", "travel"], rows),
            "amt": amount.round(2),
            "city_pop": rng.integers(100, 100_000, rows),
            "lat": 10.0,
            "long": 20.0,
            "merch_lat": 10.0 + rng.normal(0, 0.5, rows),
            "merch_long": 20.0 + rng.normal(0, 0.5, rows),
            "dob": "1980-01-01",
            "is_fraud": fraud.astype(int),
        }
    )


def test_schema_rejects_missing_timestamp():
    """#sym:test_schema_rejects_missing_timestamp
    Kiem tra: schema bat buoc timestamp.
    @param Khong co tham so.
    Dung khi: thieu timestamp phat ValueError.
    Sai khi: dataframe thieu timestamp van qua schema.
    """
    frame = _frame().drop(columns=["trans_date_trans_time"])
    with pytest.raises(ValueError, match="timestamp"):
        validate_schema(frame)
