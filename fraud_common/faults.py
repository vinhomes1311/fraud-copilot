"""Service fault injection for the ML scoring stage (requirement 2.2 / FR-07).

Faults are applied per scoring attempt inside the scoring worker:

* ``latency_ms`` delays inference, so the p95 budget and the job timeout
  (``ML_JOB_TIMEOUT_S``) can be exercised;
* ``failure_rate`` raises :class:`InjectedFault`; the job is retried and, after
  ``ML_MAX_ATTEMPTS``, recorded as ``MANUAL_FALLBACK_UNSCORED`` (never Low).

Static defaults come from the environment; the simulator or an admin can
override them at runtime through a Redis hash, which every worker reads with a
short cache so all replicas switch together.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import asdict, dataclass
from typing import Any

from .config import get_settings
from .metrics import INJECTED_FAULTS

log = logging.getLogger(__name__)

FAULT_KEY = "fault_injection:ml"
MAX_LATENCY_MS = 10_000.0


class InjectedFault(RuntimeError):
    """A deliberately injected ML service failure (not a real outage)."""


@dataclass(frozen=True)
class MLFaultConfig:
    failure_rate: float = 0.0
    latency_ms: float = 0.0
    source: str = "env"

    @property
    def active(self) -> bool:
        return self.failure_rate > 0 or self.latency_ms > 0

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "active": self.active}


def _clamp(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return min(max(number, low), high)


def env_defaults() -> MLFaultConfig:
    s = get_settings()
    if not s.fault_injection_enabled:
        return MLFaultConfig(source="disabled")
    return MLFaultConfig(_clamp(s.ml_fault_rate, 0, 1), _clamp(s.ml_fault_latency_ms, 0, MAX_LATENCY_MS))


def parse_config(stored: dict[str, str] | None) -> MLFaultConfig:
    """Runtime override from Redis; falls back to the environment defaults."""
    if not get_settings().fault_injection_enabled:
        return MLFaultConfig(source="disabled")
    if not isinstance(stored, dict) or not stored:
        return env_defaults()
    return MLFaultConfig(
        failure_rate=_clamp(stored.get("failure_rate"), 0, 1),
        latency_ms=_clamp(stored.get("latency_ms"), 0, MAX_LATENCY_MS),
        source=stored.get("set_by") or "runtime",
    )


async def read_config(redis) -> MLFaultConfig:
    return parse_config(await redis.hgetall(FAULT_KEY))


async def write_config(redis, failure_rate: float, latency_ms: float, set_by: str) -> MLFaultConfig:
    await redis.hset(FAULT_KEY, mapping={
        "failure_rate": str(_clamp(failure_rate, 0, 1)),
        "latency_ms": str(_clamp(latency_ms, 0, MAX_LATENCY_MS)),
        "set_by": set_by,
    })
    return await read_config(redis)


async def clear_config(redis) -> MLFaultConfig:
    await redis.delete(FAULT_KEY)
    return env_defaults()


class FaultProvider:
    """Cached view of the current fault configuration for one worker."""

    def __init__(self, redis, ttl_s: float | None = None, rng: random.Random | None = None) -> None:
        self.redis = redis
        self.ttl_s = get_settings().fault_cache_ttl_s if ttl_s is None else ttl_s
        self.rng = rng or random.Random()
        self._cached: MLFaultConfig | None = None
        self._expires = 0.0

    async def get(self) -> MLFaultConfig:
        now = time.monotonic()
        if self._cached is None or now >= self._expires:
            try:
                self._cached = await read_config(self.redis)
            except Exception:
                # Redis trouble must not change scoring; keep the last or env config.
                self._cached = self._cached or env_defaults()
            self._expires = now + self.ttl_s
        return self._cached

    async def inject(self) -> MLFaultConfig:
        config = await self.get()
        if config.latency_ms > 0:
            INJECTED_FAULTS.labels("latency").inc()
            await asyncio.sleep(config.latency_ms / 1000)
        if config.failure_rate > 0 and self.rng.random() < config.failure_rate:
            INJECTED_FAULTS.labels("failure").inc()
            raise InjectedFault(f"injected ML service failure (rate={config.failure_rate:g})")
        return config
