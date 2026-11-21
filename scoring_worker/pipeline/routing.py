"""Bước 5 - Triage & Routing (FR-03) theo cấu hình ngưỡng có version."""
from __future__ import annotations

import time

from fraud_common.config import get_settings
from fraud_common.db import Database
from fraud_common.schemas import RiskLevel, ThresholdConfig

# Khớp seed trong db/init.sql: recommended_triage của model trong
# data/models/fraud_xgboost.json (thang xác suất đã calibrate).
DEFAULT_THRESHOLDS = ThresholdConfig(version=0, low_max=0.0072, high_min=0.30)


def route(prob: float, cfg: ThresholdConfig) -> RiskLevel:
    if prob >= cfg.high_min:
        return RiskLevel.HIGH
    if prob >= cfg.low_max:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


class ThresholdProvider:
    """Đọc ngưỡng đang active từ DB, cache ngắn hạn để không tốn thời gian ở Hot Path."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._cfg: ThresholdConfig | None = None
        self._fetched_at = 0.0

    async def get(self) -> ThresholdConfig:
        ttl = get_settings().threshold_cache_ttl_s
        if self._cfg is None or time.monotonic() - self._fetched_at > ttl:
            row = await self.db.rfetchrow("SELECT version, low_max, high_min FROM threshold_configs WHERE active")
            if row is not None:
                self._cfg = ThresholdConfig(version=row["version"], low_max=float(row["low_max"]),
                                            high_min=float(row["high_min"]))
            elif self._cfg is None:
                self._cfg = DEFAULT_THRESHOLDS
            self._fetched_at = time.monotonic()
        return self._cfg
