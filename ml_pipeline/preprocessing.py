from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


DATETIME_CANDIDATES = ("trans_date_trans_time", "transaction_time", "timestamp", "datetime")
PII_COLUMNS = (
    "cc_num", "card_number", "first", "last", "name", "gender", "street",
    "city", "state", "zip", "dob", "job", "trans_num", "transaction_id",
    "id", "unix_time", "trans_date_trans_time", "transaction_time", "timestamp",
    "datetime", "lat", "long", "merch_lat", "merch_long",
)


def read_csv_safely(path: str | Path) -> pd.DataFrame:
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"Input data file does not exist: {source.resolve()}")
    frame = pd.read_csv(source, low_memory=False)
    frame = frame.loc[:, ~frame.columns.astype(str).str.match(r"^Unnamed:")]
    if frame.empty:
        raise ValueError(f"Input data file is empty: {source.resolve()}")
    return frame


def validate_schema(frame: pd.DataFrame, target: str = "is_fraud") -> None:
    missing = {target, "amt", "cc_num"} - set(frame.columns)
    if missing:
        raise ValueError(f"Input data is missing required columns: {sorted(missing)}")
    datetime_columns = set(frame.columns) & set(DATETIME_CANDIDATES)
    if "unix_time" not in frame.columns and not datetime_columns:
        raise ValueError(
            "Input data must contain a supported transaction timestamp or unix_time"
        )


def compute_historical_features(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"cc_num", "_sort_time", "amt"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Historical features require columns: {sorted(missing)}")
    result = frame.sort_values(["cc_num", "_sort_time"]).reset_index(drop=True).copy()
    grouped = result.set_index("_sort_time").groupby("cc_num")["amt"]
    result["time_since_last_trans_sec"] = (
        result.groupby("cc_num")["_sort_time"].diff().dt.total_seconds().fillna(-1.0)
    )
    rolling = {
        "trans_count_1h": ("1h", "count"),
        "trans_count_24h": ("24h", "count"),
        "trans_count_7d": ("7D", "count"),
        "sum_amt_24h": ("24h", "sum"),
        "avg_amt_7d": ("7D", "mean"),
        "std_amt_7d": ("7D", "std"),
        "max_amt_7d": ("7D", "max"),
    }
    for column, (window, operation) in rolling.items():
        values = getattr(grouped.rolling(window, closed="left"), operation)()
        result[column] = values.to_numpy()
    history_columns = list(rolling)
    result[history_columns] = result[history_columns].fillna(0.0)
    result["amt_to_avg_7d_ratio"] = result["amt"] / (result["avg_amt_7d"] + 1.0)
    result["amt_to_max_7d_ratio"] = result["amt"] / (result["max_amt_7d"] + 1.0)
    return result.sort_values("_sort_time").reset_index(drop=True)


def extract_features(frame: pd.DataFrame, target: str = "is_fraud") -> pd.DataFrame:
    validate_schema(frame, target)
    result = frame.copy()
    result.columns = [str(column).strip() for column in result.columns]
    datetime_column = next(
        (column for column in DATETIME_CANDIDATES if column in result.columns), None
    )
    if datetime_column:
        timestamp = pd.to_datetime(result[datetime_column], errors="coerce")
    else:
        timestamp = pd.to_datetime(result["unix_time"], unit="s", errors="coerce")
    if timestamp.isna().any():
        raise ValueError("Input data contains invalid transaction timestamps")
    result["transaction_hour"] = timestamp.dt.hour
    result["transaction_dayofweek"] = timestamp.dt.dayofweek
    result["transaction_month"] = timestamp.dt.month
    result["transaction_day"] = timestamp.dt.day
    result["is_weekend"] = timestamp.dt.dayofweek.isin([5, 6]).astype("int8")
    result["_sort_time"] = timestamp
    if "dob" in result.columns:
        dob = pd.to_datetime(result["dob"], errors="coerce")
        result["customer_age"] = ((timestamp - dob).dt.days / 365.25).round(1)
    coordinates = {"lat", "long", "merch_lat", "merch_long"}
    if coordinates.issubset(result.columns):
        lat1 = np.radians(pd.to_numeric(result["lat"], errors="coerce"))
        lon1 = np.radians(pd.to_numeric(result["long"], errors="coerce"))
        lat2 = np.radians(pd.to_numeric(result["merch_lat"], errors="coerce"))
        lon2 = np.radians(pd.to_numeric(result["merch_long"], errors="coerce"))
        a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(
            (lon2 - lon1) / 2
        ) ** 2
        result["customer_merchant_distance_km"] = 6371 * 2 * np.arcsin(
            np.sqrt(np.clip(a, 0, 1))
        )
    result = compute_historical_features(result)
    result = result.drop(columns=[column for column in PII_COLUMNS if column in result], errors="ignore")
    result[target] = pd.to_numeric(result[target], errors="coerce")
    result = result[result[target].isin([0, 1])].copy()
    result[target] = result[target].astype("int8")
    return result


def chronological_split(
    frame: pd.DataFrame, train_ratio: float = 0.7, validation_ratio: float = 0.15
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if train_ratio <= 0 or validation_ratio <= 0 or train_ratio + validation_ratio >= 1:
        raise ValueError("Split ratios must be positive and leave a non-empty test split")
    ordered = frame.sort_values("_sort_time", na_position="last").reset_index(drop=True)
    train_end = int(len(ordered) * train_ratio)
    validation_end = int(len(ordered) * (train_ratio + validation_ratio))
    if min(train_end, validation_end - train_end, len(ordered) - validation_end) < 1:
        raise ValueError("Input data is too small for the requested split ratios")
    return (
        ordered.iloc[:train_end].copy(),
        ordered.iloc[train_end:validation_end].copy(),
        ordered.iloc[validation_end:].copy(),
    )


def drop_internal_columns(frame: pd.DataFrame, target: str = "is_fraud") -> pd.DataFrame:
    return frame.drop(columns=[column for column in ("_sort_time", target) if column in frame], errors="ignore")
