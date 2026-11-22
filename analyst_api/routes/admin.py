"""Quản trị: cấu hình ngưỡng phân luồng (có version) và theo dõi hàng đợi."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from fraud_common.config import get_settings
from fraud_common.faults import MAX_LATENCY_MS, clear_config, read_config, write_config
from fraud_common.schemas import Role

from ..audit import write_audit
from ..auth import User, require_roles, simulator_or_admin
from ..state import state

router = APIRouter(prefix="/admin", tags=["admin"])
admin_only = require_roles(Role.ADMIN)


class ThresholdUpdate(BaseModel):
    low_max: float
    high_min: float

    @model_validator(mode="after")
    def _check(self):
        if not (0 < self.low_max <= self.high_min < 1):
            raise ValueError("Yêu cầu 0 < low_max <= high_min < 1")
        return self


@router.get("/threshold-config")
async def get_threshold(_: User = Depends(admin_only)):
    row = await state.db.rfetchrow(
        "SELECT version, low_max, high_min, created_by, created_at FROM threshold_configs WHERE active")
    return dict(row) if row else {}


@router.put("/threshold-config")
async def put_threshold(body: ThresholdUpdate, user: User = Depends(admin_only)):
    """Tạo version mới và kích hoạt. Scoring Worker nhận version mới sau tối đa THRESHOLD_CACHE_TTL_S giây."""
    async with state.db.write_tx() as conn:
        await conn.execute("UPDATE threshold_configs SET active=false WHERE active")
        row = await conn.fetchrow(
            """INSERT INTO threshold_configs (low_max, high_min, active, created_by)
               VALUES ($1,$2,true,$3) RETURNING version""", body.low_max, body.high_min, user.username)
        await write_audit(conn, actor=user.username, event="threshold.update", entity="threshold_config",
                          entity_id=row["version"], detail=body.model_dump())
    return {"version": row["version"], **body.model_dump()}


@router.get("/queues")
async def queue_stats(_: User = Depends(admin_only)):
    if state.ml_queue is None:
        raise HTTPException(503, "Chưa sẵn sàng")
    return {
        "ml_jobs": await state.ml_queue.depth(),
        "llm_jobs": await state.llm_queue.depth(),
        "case_events": await state.events_queue.depth(),
    }


class FaultInjectionUpdate(BaseModel):
    ml_failure_rate: float = Field(0.0, ge=0, le=1)
    ml_latency_ms: float = Field(0.0, ge=0, le=MAX_LATENCY_MS)


def _fault_response(config) -> dict:
    s = get_settings()
    return {**config.as_dict(), "enabled": s.fault_injection_enabled,
            "ml_job_timeout_ms": s.ml_job_timeout_s * 1000, "ml_max_attempts": s.ml_max_attempts}


@router.get("/fault-injection")
async def get_fault_injection(_: str = Depends(simulator_or_admin)):
    return _fault_response(await read_config(state.redis))


@router.put("/fault-injection")
async def put_fault_injection(body: FaultInjectionUpdate, actor: str = Depends(simulator_or_admin)):
    """Make the scoring worker slow or fail on purpose (requirement 2.2, FR-07 tests).

    Failed attempts are retried; after ML_MAX_ATTEMPTS the transaction becomes
    MANUAL_FALLBACK_UNSCORED. Latency above ML_JOB_TIMEOUT_S causes timeouts.
    """
    if not get_settings().fault_injection_enabled:
        raise HTTPException(403, "Fault injection đã bị tắt (FAULT_INJECTION_ENABLED=false)")
    config = await write_config(state.redis, body.ml_failure_rate, body.ml_latency_ms, actor)
    async with state.db.write_tx() as conn:
        await write_audit(conn, actor=actor, event="fault_injection.update", entity="fault_injection",
                          entity_id="ml", detail=body.model_dump())
    return _fault_response(config)


@router.delete("/fault-injection")
async def delete_fault_injection(actor: str = Depends(simulator_or_admin)):
    config = await clear_config(state.redis)
    async with state.db.write_tx() as conn:
        await write_audit(conn, actor=actor, event="fault_injection.clear", entity="fault_injection",
                          entity_id="ml", detail={})
    return _fault_response(config)
