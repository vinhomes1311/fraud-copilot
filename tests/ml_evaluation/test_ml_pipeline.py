import pandas as pd
import pytest

from ml_pipeline.preprocessing import chronological_split, extract_features, validate_schema


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
