"""Luồng 1 (Hot Path), phía Scoring: validation -> PII -> features -> inference -> routing -> ghi DB."""
from __future__ import annotations

import asyncio
import logging
import time

from fraud_common.config import get_settings
from fraud_common.db import Database
from fraud_common.metrics import INVALID_TXN, ROUTED, STAGE_SECONDS
from fraud_common.queues import ReliableQueue
from fraud_common.schemas import (
    JOB_CASE_CREATED, CaseCreatedEvent, JobEnvelope, RiskLevel, ScoreTransactionPayload,
    ScoringStatus)

from .pipeline.features import compute_features
from .pipeline.inference import predict
from .pipeline.pii import mask_pii
from .pipeline.routing import ThresholdProvider, route
from .pipeline.validation import validate

log = logging.getLogger(__name__)


class _Stage:
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()

    def __exit__(self, *exc):
        STAGE_SECONDS.labels(self.name).observe(time.perf_counter() - self.t0)


class ScoringProcessor:
    def __init__(self, db: Database, events_queue: ReliableQueue) -> None:
        self.db = db
        self.events_queue = events_queue
        self.thresholds = ThresholdProvider(db)
        self.s = get_settings()

    # Được worker gọi cho mỗi job. Lỗi/timeout -> ném exception -> queue tự retry.
    async def handle(self, env: JobEnvelope) -> None:
        payload = ScoreTransactionPayload.model_validate(env.payload)
        await asyncio.wait_for(self._score(payload, env), timeout=self.s.ml_job_timeout_s)

    async def _score(self, payload: ScoreTransactionPayload, env: JobEnvelope) -> None:
        t0 = time.perf_counter()
        safe = mask_pii(payload.raw)

        with _Stage("validation"):
            result = validate(payload.raw)
        if not result.ok:
            INVALID_TXN.inc()
            await self._persist_unscored(payload.txn_id, safe, result.errors, env.trace_id)
            return

        tx = result.tx
        account_hash = safe["account_hash_id"]
        with _Stage("features"):
            features = await compute_features(self.db, tx, account_hash)
        with _Stage("inference"):
            pred = predict(features)
        cfg = await self.thresholds.get()
        level = route(pred.calibrated_prob, cfg)
        latency_ms = (time.perf_counter() - t0) * 1000

        with _Stage("persist"):
            case_id = await self._persist_scored(
                payload.txn_id, safe, tx.trans_date_trans_time, features, pred, level, cfg.version,
                latency_ms, env.trace_id)

        ROUTED.labels(level.value).inc()
        if case_id is not None and level != RiskLevel.LOW:
            # Phát sự kiện SAU khi commit. Analyst API sẽ biến nó thành job tạo brief.
            await self.events_queue.publish(JobEnvelope(
                job_type=JOB_CASE_CREATED, priority=0 if level == RiskLevel.HIGH else 1, trace_id=env.trace_id,
                payload=CaseCreatedEvent(case_id=case_id, txn_id=payload.txn_id, risk_level=level,
                                         calibrated_prob=pred.calibrated_prob).model_dump(mode="json")))
        total_ms = (time.perf_counter() - t0) * 1000
        if total_ms > 500:
            log.warning("vượt ngân sách 500ms", extra={"ctx": {"txn_id": payload.txn_id, "ms": round(total_ms)}})

    # ------------------------------------------------------------------ DB
    async def _persist_scored(self, txn_id, safe, txn_time, features, pred, level, cfg_version,
                              latency_ms, trace_id) -> int | None:
        if txn_time.tzinfo is None:
            from datetime import timezone
            txn_time = txn_time.replace(tzinfo=timezone.utc)
        async with self.db.write_tx() as conn:
            await conn.execute(
                """INSERT INTO transactions (txn_id, account_hash_id, payload_masked, txn_time, trace_id)
                   VALUES ($1,$2,$3,$4,$5) ON CONFLICT (txn_id) DO NOTHING""",
                txn_id, safe.get("account_hash_id"), safe, txn_time, trace_id)
            inserted = await conn.fetchrow(
                """INSERT INTO scoring_results
                   (txn_id, status, raw_model_score, calibrated_prob, risk_level, model_version,
                    threshold_config_version, features, explanations, latency_ms, trace_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                   ON CONFLICT (txn_id) DO NOTHING RETURNING txn_id""",
                txn_id, ScoringStatus.SCORED.value, pred.raw_model_score, pred.calibrated_prob, level.value,
                pred.model_version, cfg_version, features,
                [e.model_dump() for e in pred.explanations], latency_ms, trace_id)
            if inserted is None:       # job bị giao lại -> đã xử lý rồi
                return None
            case_id = None
            if level != RiskLevel.LOW:
                case_id = await conn.fetchval(
                    """INSERT INTO cases (txn_id, risk_level) VALUES ($1,$2)
                       ON CONFLICT (txn_id) DO UPDATE SET risk_level=EXCLUDED.risk_level,
                       updated_at=now() RETURNING case_id""", txn_id, level.value)
            await conn.execute(
                "INSERT INTO audit_log (actor, event, entity, entity_id, detail, trace_id) VALUES ($1,$2,$3,$4,$5,$6)",
                "scoring-worker", "transaction.scored", "transaction", txn_id,
                {"risk_level": level.value, "prob": pred.calibrated_prob, "model_version": pred.model_version,
                 "threshold_config_version": cfg_version, "case_id": case_id}, trace_id)
        return case_id

    async def _persist_unscored(self, txn_id, safe, errors, trace_id) -> None:
        async with self.db.write_tx() as conn:
            await conn.execute(
                """INSERT INTO transactions (txn_id, account_hash_id, payload_masked, trace_id)
                   VALUES ($1,$2,$3,$4) ON CONFLICT (txn_id) DO NOTHING""",
                txn_id, safe.get("account_hash_id"), safe, trace_id)
            await conn.execute(
                """INSERT INTO scoring_results (txn_id, status, validation_errors, trace_id)
                   VALUES ($1,$2,$3,$4) ON CONFLICT (txn_id) DO NOTHING""",
                txn_id, ScoringStatus.MANUAL_FALLBACK_UNSCORED.value, errors, trace_id)
            await conn.execute(
                "INSERT INTO unscored_queue (txn_id, reasons) VALUES ($1,$2) ON CONFLICT (txn_id) DO NOTHING",
                txn_id, errors)
            await conn.execute(
                "INSERT INTO audit_log (actor, event, entity, entity_id, detail, trace_id) VALUES ($1,$2,$3,$4,$5,$6)",
                "scoring-worker", "transaction.unscored", "transaction", txn_id, {"errors": errors}, trace_id)
        log.warning("giao dịch không hợp lệ -> MANUAL_FALLBACK_UNSCORED",
                    extra={"ctx": {"txn_id": txn_id, "trace_id": trace_id, "errors": errors}})

    async def handle_dead(self, env: JobEnvelope, error: str) -> None:
        """Job lỗi quá số lần thử: vẫn phải để lại dấu vết, KHÔNG được coi là Low Risk."""
        payload = ScoreTransactionPayload.model_validate(env.payload)
        await self._persist_unscored(
            payload.txn_id, mask_pii(payload.raw),
            [{"field": "_system", "msg": f"scoring failed after retries: {error}"}], env.trace_id)
