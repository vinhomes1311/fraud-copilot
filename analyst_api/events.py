"""Xử lý sự kiện case_created do Scoring Worker phát ra -> đẩy job tạo brief vào hàng đợi LLM."""
from __future__ import annotations

import logging

from fraud_common.schemas import (
    JOB_GENERATE_BRIEF, CaseCreatedEvent, GenerateBriefPayload, JobEnvelope, RiskLevel)

from .state import state

log = logging.getLogger(__name__)


def llm_priority(level: RiskLevel) -> int:
    return 0 if level == RiskLevel.HIGH else 1     # High trước Medium


async def enqueue_brief(
    *, case_id: int, txn_id: str, level: RiskLevel, trace_id: str,
    regenerate: bool = False, requested_by: str = "system",
) -> JobEnvelope:
    env = JobEnvelope(
        job_type=JOB_GENERATE_BRIEF, priority=llm_priority(level), trace_id=trace_id,
        payload=GenerateBriefPayload(case_id=case_id, txn_id=txn_id, risk_level=level,
                                     regenerate=regenerate, requested_by=requested_by).model_dump(mode="json"))
    await state.db.wexec(
        """INSERT INTO case_briefs (case_id, status, generation_id) VALUES ($1, 'PENDING', $2)
           ON CONFLICT (case_id) DO UPDATE SET status='PENDING', generation_id=$2,
             brief=NULL, citations=NULL, attempts=0, model_name=NULL,
             prompt_version=NULL, tokens_used=NULL, last_error=NULL, updated_at=now()""",
        case_id, env.job_id)
    await state.llm_queue.publish(env)
    return env


async def handle_case_created(env: JobEnvelope) -> None:
    ev = CaseCreatedEvent.model_validate(env.payload)
    await enqueue_brief(case_id=ev.case_id, txn_id=ev.txn_id, level=ev.risk_level, trace_id=env.trace_id)
    log.info("đã đẩy job brief", extra={"ctx": {"case_id": ev.case_id, "trace_id": env.trace_id}})
