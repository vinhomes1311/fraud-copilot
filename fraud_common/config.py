"""Cấu hình tập trung, đọc từ biến môi trường (không hard-code logic nghiệp vụ)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _s(name: str, default: str) -> str:
    return os.getenv(name, default)


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    service_name: str = _s("SERVICE_NAME", "service")
    metrics_port: int = _i("METRICS_PORT", 9100)

    # Hạ tầng
    redis_url: str = _s("REDIS_URL", "redis://localhost:6379/0")
    db_write_url: str = _s("DB_WRITE_URL", "postgresql://fraud:fraud@localhost:6432/fraud")
    db_read_url: str = _s("DB_READ_URL", "postgresql://fraud:fraud@localhost:6433/fraud")

    # Bảo mật
    jwt_secret: str = _s("JWT_SECRET", "dev-only-change-me")
    jwt_ttl_s: int = _i("JWT_TTL_S", 8 * 3600)
    simulator_api_key: str = _s("SIMULATOR_API_KEY", "dev-sim-key")
    pii_salt: str = _s("PII_SALT", "dev-salt")

    # Hàng đợi ML (Hot Path)
    max_ml_queue_depth: int = _i("MAX_ML_QUEUE_DEPTH", 5000)   # vượt -> API trả 429
    ml_concurrency: int = _i("ML_CONCURRENCY", 8)
    ml_job_timeout_s: float = _f("ML_JOB_TIMEOUT_S", 2.0)
    ml_visibility_timeout_s: float = _f("ML_VISIBILITY_TIMEOUT_S", 10.0)
    ml_max_attempts: int = _i("ML_MAX_ATTEMPTS", 3)
    threshold_cache_ttl_s: float = _f("THRESHOLD_CACHE_TTL_S", 30.0)

    # Hàng đợi LLM (Cold Path)
    llm_concurrency: int = _i("LLM_CONCURRENCY", 2)
    llm_job_timeout_s: float = _f("LLM_JOB_TIMEOUT_S", 30.0)
    llm_visibility_timeout_s: float = _f("LLM_VISIBILITY_TIMEOUT_S", 120.0)
    llm_max_attempts: int = _i("LLM_MAX_ATTEMPTS", 3)
    brief_token_budget: int = _i("BRIEF_TOKEN_BUDGET", 5000)
    regen_limit_per_hour: int = _i("REGEN_LIMIT_PER_HOUR", 3)
    breaker_failure_threshold: int = _i("BREAKER_FAILURE_THRESHOLD", 5)
    breaker_reset_s: float = _f("BREAKER_RESET_S", 30.0)

    # Stub LLM (chỉ dùng khi chưa có LLM thật)
    llm_provider: str = _s("LLM_PROVIDER", "stub")
    llm_stub_latency_s: float = _f("LLM_STUB_LATENCY_S", 1.0)
    llm_stub_fail_rate: float = _f("LLM_STUB_FAIL_RATE", 0.0)

    def validate_llm_timeouts(self) -> None:
        # Retrieval and generation each have their own deadline; leave time for DB I/O.
        if self.llm_job_timeout_s <= 0 or self.llm_visibility_timeout_s <= 2 * self.llm_job_timeout_s + 10:
            raise ValueError("LLM_VISIBILITY_TIMEOUT_S must exceed 2 * LLM_JOB_TIMEOUT_S + 10")


@lru_cache
def get_settings() -> Settings:
    return Settings()
