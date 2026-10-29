"""Luồng 2 (Cold Path): lấy job theo ưu tiên -> RAG -> LLM -> grounding -> ghi brief vào DB."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fraud_common.config import get_settings
from fraud_common.db import Database
from fraud_common.metrics import BREAKER_STATE, BRIEFS, STAGE_SECONDS
from fraud_common.resilience import CircuitBreaker
from fraud_common.schemas import BriefStatus, GenerateBriefPayload, JobEnvelope
from fraud_common.worker import NonRetryableError

from .fallback import rule_based_brief
from .grounding import check_grounding
from .llm_client import PROMPT_VERSION, get_llm_client
from .rag import get_retriever

log = logging.getLogger(__name__)


class CaseNotReady(Exception):
    """Case chưa thấy trên Replica (trễ đồng bộ) -> thử lại sau."""


class BriefProcessor:
    def __init__(self, db: Database) -> None:
        s = get_settings()
        self.s = s
        self.db = db
        self.retriever = get_retriever()
        self.llm = get_llm_client()
        self.breaker = CircuitBreaker(s.breaker_failure_threshold, s.breaker_reset_s)

    async def handle(self, env: JobEnvelope) -> None:
        p = GenerateBriefPayload.model_validate(env.payload)
        if not await self._set_status(p.case_id, BriefStatus.RUNNING,
                                      attempts=env.attempts + 1, generation_id=env.job_id):
            return  # Superseded generation or completed duplicate delivery.

        ctx = await self._load_context(p.case_id)
        signals = [e["feature"] for e in (ctx.get("explanations") or [])]

        with STAGE_SECONDS.labels("rag").time():
            chunks = await asyncio.wait_for(
                self.retriever.retrieve(signals, ctx["risk_level"]), self.s.llm_job_timeout_s)

        # Timeout + circuit breaker bao quanh lời gọi LLM
        async def _call_llm():
            return await asyncio.wait_for(self.llm.generate(ctx, chunks), self.s.llm_job_timeout_s)

        with STAGE_SECONDS.labels("llm").time():
            try:
                result = await self.breaker.call(_call_llm)
            finally:
                BREAKER_STATE.set(1 if self.breaker.is_open else 0)

        if result.tokens_used > self.s.brief_token_budget:
            await self.handle_dead(env.model_copy(update={"attempts": env.attempts + 1}),
                                   f"Token budget exceeded: {result.tokens_used}")
            raise NonRetryableError(f"vượt ngân sách token: {result.tokens_used}")
        check_grounding(result.brief, chunks)       # GroundingError -> job lỗi -> retry

        written = await self.db.wexec(
            """UPDATE case_briefs SET status='READY', brief=$2, citations=$3,
                 attempts=$4, model_name=$5, prompt_version=$6, tokens_used=$7,
                 last_error=NULL, updated_at=now()
               WHERE case_id=$1 AND generation_id=$8 AND status='RUNNING' AND attempts=$4""",
            p.case_id, result.brief.model_dump(), [c.model_dump() for c in result.brief.citations],
            env.attempts + 1, result.model_name, PROMPT_VERSION, result.tokens_used, env.job_id)
        if written != "UPDATE 1":
            return
        BRIEFS.labels("READY").inc()
        log.info("brief READY", extra={"ctx": {"case_id": p.case_id, "trace_id": env.trace_id,
                                               "tokens": result.tokens_used}})

    async def handle_dead(self, env: JobEnvelope, error: str) -> None:
        """Hết số lần thử: đánh dấu FAILED_TIMEOUT, ghi brief rule-based. Case + kết quả ML giữ nguyên (FR-07)."""
        p = GenerateBriefPayload.model_validate(env.payload)
        status = BriefStatus.FAILED_TIMEOUT if ("timeout" in error.lower() or "circuitopen" in error.lower()) else BriefStatus.FAILED
        try:
            ctx = await self._load_context(p.case_id)
            fb = rule_based_brief(ctx).model_dump()
        except Exception:
            fb = None
        written = await self.db.wexec(
            """UPDATE case_briefs SET status=$2, brief=$3, citations=$4,
                 attempts=$5, last_error=$6, model_name=NULL, prompt_version=NULL,
                 tokens_used=NULL, updated_at=now()
               WHERE case_id=$1 AND generation_id=$7
                 AND status IN ('PENDING','RUNNING') AND attempts <= $5""",
            p.case_id, status.value, fb, fb["citations"] if fb else [],
            env.attempts, error[:500], env.job_id)
        if written == "UPDATE 1":
            BRIEFS.labels(status.value).inc()

    async def _set_status(self, case_id: int, status: BriefStatus, attempts: int,
                          generation_id: str) -> bool:
        written = await self.db.wexec(
            """UPDATE case_briefs SET status=$2, attempts=$3, generation_id=$4,
                 updated_at=now()
               WHERE case_id=$1 AND (generation_id=$4 OR generation_id IS NULL)
                 AND status IN ('PENDING','RUNNING') AND attempts <= $3""",
            case_id, status.value, attempts, generation_id)
        return written == "UPDATE 1"

    async def _load_context(self, case_id: int) -> dict[str, Any]:
        """Chỉ lấy dữ liệu ĐÃ che PII (payload_masked) để đưa vào prompt (FR-09)."""
        row = await self.db.rfetchrow(
            """SELECT c.case_id, c.txn_id, c.risk_level, s.calibrated_prob, s.raw_model_score,
                      s.explanations, s.features, t.payload_masked AS transaction
               FROM cases c
               JOIN scoring_results s ON s.txn_id = c.txn_id
               JOIN transactions t ON t.txn_id = c.txn_id
               WHERE c.case_id = $1""", case_id)
        if row is None:
            raise CaseNotReady(f"case {case_id} chưa có trên replica")
        return dict(row)
