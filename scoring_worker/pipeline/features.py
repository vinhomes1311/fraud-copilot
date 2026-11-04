"""Bước 3 - Temporal feature engineering. Chỉ dùng lịch sử với t_history < t_current (chống leakage)."""
from __future__ import annotations

import math
from typing import Any

from fraud_common.db import Database
from fraud_common.schemas import TransactionValidated


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


async def compute_features(db: Database, tx: TransactionValidated, account_hash_id: str) -> dict[str, Any]:
    # Đọc từ Replica qua PgBouncer-read. Điều kiện txn_time < thời điểm hiện tại là bắt buộc.
    hist = await db.rfetchrow(
        """SELECT
               count(*) FILTER (WHERE txn_time >= $2::timestamptz - interval '1 hour') AS n_1h,
               count(*) FILTER (WHERE txn_time >= $2::timestamptz - interval '24 hours') AS n_24h,
               count(*) AS n_7d,
               COALESCE(sum((payload_masked->>'amt')::float)
                        FILTER (WHERE txn_time >= $2::timestamptz - interval '24 hours'), 0) AS sum_amt_24h,
               COALESCE(avg((payload_masked->>'amt')::float), 0) AS avg_amt_7d,
               COALESCE(stddev_samp((payload_masked->>'amt')::float), 0) AS std_amt_7d,
               COALESCE(max((payload_masked->>'amt')::float), 0) AS max_amt_7d,
               max(txn_time) AS last_txn_time
           FROM transactions
           WHERE account_hash_id = $1 AND txn_time < $2::timestamptz
             AND txn_time >= $2::timestamptz - interval '7 days'""",
        account_hash_id, tx.trans_date_trans_time)
    n_1h = float(hist["n_1h"] or 0)
    n_24h = float(hist["n_24h"] or 0)
    n_7d = float(hist["n_7d"] or 0)
    sum_amt_24h = float(hist["sum_amt_24h"] or 0)
    avg_amt_7d = float(hist["avg_amt_7d"] or 0)
    std_amt_7d = float(hist["std_amt_7d"] or 0)
    max_amt_7d = float(hist["max_amt_7d"] or 0)
    timestamp = tx.trans_date_trans_time
    distance = haversine_km(tx.lat, tx.long, tx.merch_lat, tx.merch_long)
    customer_age = (
        (timestamp - tx.dob).total_seconds() / (365.25 * 24 * 3600)
        if tx.dob is not None else None
    )
    if customer_age is not None:
        customer_age = round(customer_age, 1)
    time_since_last = (
        (timestamp - hist["last_txn_time"]).total_seconds()
        if hist["last_txn_time"] is not None else -1.0
    )
    return {
        "merchant": tx.merchant,
        "category": tx.category,
        "amt": tx.amt,
        "city_pop": tx.city_pop,
        "customer_age": customer_age,
        "customer_merchant_distance_km": distance,
        "time_since_last_trans_sec": time_since_last,
        "trans_count_1h": n_1h,
        "trans_count_24h": n_24h,
        "trans_count_7d": n_7d,
        "sum_amt_24h": sum_amt_24h,
        "avg_amt_7d": avg_amt_7d,
        "std_amt_7d": std_amt_7d,
        "max_amt_7d": max_amt_7d,
        "amt_to_avg_7d_ratio": tx.amt / (avg_amt_7d + 1.0),
        "amt_to_max_7d_ratio": tx.amt / (max_amt_7d + 1.0),
        "transaction_hour": float(timestamp.hour),
        "transaction_dayofweek": float(timestamp.weekday()),
        "transaction_month": float(timestamp.month),
        "transaction_day": float(timestamp.day),
        "is_weekend": float(timestamp.weekday() in (5, 6)),
    }
