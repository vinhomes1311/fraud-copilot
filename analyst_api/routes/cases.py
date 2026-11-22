"""Luồng 3: Analyst / Risk Lead đọc hàng đợi, xem hồ sơ, ra quyết định."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from fraud_common.config import get_settings
from fraud_common.queues import QUEUE_CASE_EVENTS, QUEUE_LLM_JOBS, QUEUE_ML_JOBS
from fraud_common.schemas import AnalystAction, CaseOutcome, CaseStatus, RiskLeadDecision, RiskLevel, Role

from ..audit import write_audit
from ..auth import User, require_roles
from ..events import enqueue_brief
from ..state import state

ScoringStream = Literal["SCORED", "UNSCORED"]
ScoreBand = Literal["0-25", "25-50", "50-75", "75-100"]
SortOrder = Literal["newest", "oldest", "highest_score", "lowest_score"]
EvidenceStatus = Literal["NONE", "PENDING", "RUNNING", "READY", "FAILED", "FAILED_TIMEOUT"]
SCORE_BAND_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "0-25": (0.0, 0.25),
    "25-50": (0.25, 0.5),
    "50-75": (0.5, 0.75),
    "75-100": (0.75, None),
}

router = APIRouter(prefix="/cases", tags=["cases"])
any_reviewer = require_roles(Role.ANALYST, Role.RISK_LEAD)
log = logging.getLogger(__name__)

ACTION_TO_STATUS = {
    AnalystAction.NOTE: CaseStatus.IN_REVIEW,
    AnalystAction.NEEDS_MORE_INFO: CaseStatus.NEEDS_MORE_INFO,
    AnalystAction.CONFIRM_SIMULATED: CaseStatus.CONFIRMED_SIMULATED,
    AnalystAction.ESCALATE: CaseStatus.ESCALATED,
}
OUTCOME_TO_STATUS = {
    CaseOutcome.CONFIRMED_FRAUD: CaseStatus.CONFIRMED_FRAUD,
    CaseOutcome.DISMISSED: CaseStatus.DISMISSED,
    CaseOutcome.INSUFFICIENT_EVIDENCE: CaseStatus.RETURNED,
}
OUTCOME_ACTIONS = tuple(f"OUTCOME_{outcome.value}" for outcome in CaseOutcome)


async def _case_summary() -> dict:
    row = await state.db.rfetchrow(
        """SELECT count(*) FILTER (
                     WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                 ) AS open_count,
                 count(*) FILTER (
                     WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                       AND c.risk_level = 'HIGH'
                 ) AS high_count,
                 count(*) FILTER (
                     WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                       AND c.risk_level = 'MEDIUM'
                 ) AS medium_count,
                 coalesce(avg(s.calibrated_prob) FILTER (WHERE s.status = 'SCORED'), 0) AS average_probability,
                 coalesce(avg(s.latency_ms) FILTER (WHERE s.status = 'SCORED'), 0) AS scoring_latency_ms
          FROM cases c
          JOIN scoring_results s ON s.txn_id = c.txn_id"""
    )
    return dict(row)


async def _decision_summary() -> dict:
    row = await state.db.rfetchrow(
        """SELECT count(*) FILTER (WHERE latest.action = 'OUTCOME_CONFIRMED_FRAUD') AS confirmed_fraud,
                  count(*) FILTER (WHERE latest.action = 'OUTCOME_DISMISSED') AS dismissed,
                  count(*) FILTER (WHERE latest.action = 'OUTCOME_INSUFFICIENT_EVIDENCE') AS insufficient_evidence,
                  count(*) FILTER (WHERE latest.action = 'ESCALATE') AS escalated,
                  count(*) FILTER (WHERE latest.action = 'NEEDS_MORE_INFO') AS needs_more_info
           FROM (
               SELECT DISTINCT ON (ca.case_id) ca.case_id, ca.action
               FROM case_actions ca
               WHERE ca.action IN (
                   'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                   'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE', 'NEEDS_MORE_INFO'
               )
               ORDER BY ca.case_id, ca.created_at DESC, ca.id DESC
           ) latest"""
    )
    return dict(row)


def can_update_outcome(status: CaseStatus | str, has_previous_outcome: bool) -> bool:
    status_value = status.value if isinstance(status, CaseStatus) else status
    if status_value in {CaseStatus.CLOSED.value, CaseStatus.CONFIRMED_SIMULATED.value}:
        return False
    if status_value in {CaseStatus.CONFIRMED_FRAUD.value, CaseStatus.DISMISSED.value}:
        return has_previous_outcome
    return True


class ActionRequest(BaseModel):
    action: AnalystAction
    note: str | None = Field(default=None, max_length=2000)


class ResolveRequest(BaseModel):
    decision: RiskLeadDecision
    note: str | None = Field(default=None, max_length=2000)


class ManualCaseRequest(BaseModel):
    txn_id: str = Field(min_length=1)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    reason: str = Field(min_length=1, max_length=2000)


class AssignmentRequest(BaseModel):
    assigned_to: str | None = Field(default=None, max_length=200)


class OutcomeRequest(BaseModel):
    outcome: CaseOutcome
    note: str | None = Field(default=None, max_length=2000)


@router.get("")
async def list_cases(
    user: User = Depends(any_reviewer),
    risk_level: RiskLevel | None = None,
    status: CaseStatus | None = None,
    stream: ScoringStream | None = None,
    merchant_type: str | None = None,
    score_band: ScoreBand | None = None,
    received_from: datetime | None = None,
    received_before: datetime | None = None,
    evidence_status: EvidenceStatus | None = None,
    sort: SortOrder = "newest",
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    """List review cases with server-side filters and selectable ordering."""
    score_min, score_max = SCORE_BAND_BOUNDS[score_band] if score_band else (None, None)
    rows = await state.db.rfetch(
        """WITH numbered_cases AS (
               SELECT c.*,
                      row_number() OVER (ORDER BY c.created_at, c.case_id) AS run_number
               FROM cases c
           )
           SELECT c.case_id, c.run_number, c.txn_id, c.risk_level, c.status, c.assigned_to, c.created_at,
                  s.calibrated_prob, b.status AS brief_status
           FROM numbered_cases c
           JOIN scoring_results s ON s.txn_id = c.txn_id
           JOIN transactions t ON t.txn_id = c.txn_id
           LEFT JOIN case_briefs b ON b.case_id = c.case_id
           WHERE ($1::text IS NULL OR c.risk_level = $1)
             AND ($2::text IS NULL OR c.status = $2)
             AND ($3::text IS NULL OR ($3 = 'SCORED' AND s.status = 'SCORED')
                  OR ($3 = 'UNSCORED' AND s.status <> 'SCORED'))
             AND ($4::text IS NULL OR t.payload_masked->>'category' = $4)
             AND ($5::double precision IS NULL OR s.calibrated_prob >= $5)
             AND ($6::double precision IS NULL OR s.calibrated_prob < $6)
             AND ($7::timestamptz IS NULL OR c.created_at >= $7::timestamptz)
             AND ($8::timestamptz IS NULL OR c.created_at < $8::timestamptz)
             AND ($9::text IS NULL OR ($9 = 'NONE' AND b.case_id IS NULL) OR b.status = $9)
           ORDER BY CASE WHEN $10 = 'newest' THEN c.created_at END DESC,
                    CASE WHEN $10 = 'oldest' THEN c.created_at END ASC,
                    CASE WHEN $10 = 'highest_score' THEN s.calibrated_prob END DESC NULLS LAST,
                    CASE WHEN $10 = 'lowest_score' THEN s.calibrated_prob END ASC NULLS LAST,
                    c.created_at DESC, c.case_id DESC
           LIMIT $11 OFFSET $12""",
        risk_level, status, stream, merchant_type, score_min, score_max,
        received_from, received_before, evidence_status, sort, limit, offset)
    total = await state.db.rfetchval(
        """SELECT count(*)
           FROM cases c
           JOIN scoring_results s ON s.txn_id = c.txn_id
           JOIN transactions t ON t.txn_id = c.txn_id
           LEFT JOIN case_briefs b ON b.case_id = c.case_id
           WHERE ($1::text IS NULL OR c.risk_level = $1)
             AND ($2::text IS NULL OR c.status = $2)
             AND ($3::text IS NULL OR ($3 = 'SCORED' AND s.status = 'SCORED')
                  OR ($3 = 'UNSCORED' AND s.status <> 'SCORED'))
             AND ($4::text IS NULL OR t.payload_masked->>'category' = $4)
             AND ($5::double precision IS NULL OR s.calibrated_prob >= $5)
             AND ($6::double precision IS NULL OR s.calibrated_prob < $6)
             AND ($7::timestamptz IS NULL OR c.created_at >= $7::timestamptz)
             AND ($8::timestamptz IS NULL OR c.created_at < $8::timestamptz)
             AND ($9::text IS NULL OR ($9 = 'NONE' AND b.case_id IS NULL) OR b.status = $9)""",
        risk_level, status, stream, merchant_type, score_min, score_max,
        received_from, received_before, evidence_status)
    summary = await _case_summary()
    decision_summary = await _decision_summary()
    return {
        "items": [dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
        "total": total,
        "decision_summary": decision_summary,
        **summary,
    }


@router.get("/analytics/transactions")
async def list_analytics_transactions(
    user: User = Depends(any_reviewer),
    risk_level: RiskLevel | None = None,
    status: CaseStatus | None = None,
    stream: ScoringStream | None = None,
    merchant_type: str | None = None,
    score_band: ScoreBand | None = None,
    received_from: datetime | None = None,
    received_before: datetime | None = None,
    evidence_status: EvidenceStatus | None = None,
    sort: SortOrder = "newest",
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    score_min, score_max = SCORE_BAND_BOUNDS[score_band] if score_band else (None, None)
    rows = await state.db.rfetch(
        """WITH numbered_cases AS (
               SELECT c.*,
                      row_number() OVER (ORDER BY c.created_at, c.case_id) AS run_number
               FROM cases c
           )
           SELECT c.case_id, c.run_number, c.txn_id, c.risk_level, c.status, c.created_at,
                  s.calibrated_prob, s.model_version, b.status AS brief_status
           FROM numbered_cases c
           JOIN scoring_results s ON s.txn_id = c.txn_id
           JOIN transactions t ON t.txn_id = c.txn_id
           LEFT JOIN case_briefs b ON b.case_id = c.case_id
           WHERE (($1::text IS NULL AND c.risk_level IN ('HIGH', 'MEDIUM'))
                  OR ($1::text IS NOT NULL AND c.risk_level = $1))
             AND ($2::text IS NULL OR c.status = $2)
             AND ($3::text IS NULL OR ($3 = 'SCORED' AND s.status = 'SCORED')
                  OR ($3 = 'UNSCORED' AND s.status <> 'SCORED'))
             AND ($4::text IS NULL OR t.payload_masked->>'category' = $4)
             AND ($5::double precision IS NULL OR s.calibrated_prob >= $5)
             AND ($6::double precision IS NULL OR s.calibrated_prob < $6)
             AND ($7::timestamptz IS NULL OR c.created_at >= $7::timestamptz)
             AND ($8::timestamptz IS NULL OR c.created_at < $8::timestamptz)
             AND ($9::text IS NULL OR ($9 = 'NONE' AND b.case_id IS NULL) OR b.status = $9)
             AND NOT EXISTS (
              SELECT 1
              FROM case_actions ca
              WHERE ca.case_id = c.case_id
                AND ca.action IN (
                    'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                    'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE',
                    'NEEDS_MORE_INFO', 'CONFIRM_SIMULATED'
                )
          )
           ORDER BY CASE WHEN $10 = 'newest' THEN c.created_at END DESC,
                    CASE WHEN $10 = 'oldest' THEN c.created_at END ASC,
                    CASE WHEN $10 = 'highest_score' THEN s.calibrated_prob END DESC NULLS LAST,
                    CASE WHEN $10 = 'lowest_score' THEN s.calibrated_prob END ASC NULLS LAST,
                    c.created_at DESC, c.case_id DESC
           LIMIT $11 OFFSET $12""",
        risk_level, status, stream, merchant_type, score_min, score_max,
        received_from, received_before, evidence_status, sort, limit, offset)
    total = await state.db.rfetchval(
        """SELECT count(*)
           FROM cases c
           JOIN scoring_results s ON s.txn_id = c.txn_id
           JOIN transactions t ON t.txn_id = c.txn_id
           LEFT JOIN case_briefs b ON b.case_id = c.case_id
           WHERE (($1::text IS NULL AND c.risk_level IN ('HIGH', 'MEDIUM'))
                  OR ($1::text IS NOT NULL AND c.risk_level = $1))
             AND ($2::text IS NULL OR c.status = $2)
             AND ($3::text IS NULL OR ($3 = 'SCORED' AND s.status = 'SCORED')
                  OR ($3 = 'UNSCORED' AND s.status <> 'SCORED'))
             AND ($4::text IS NULL OR t.payload_masked->>'category' = $4)
             AND ($5::double precision IS NULL OR s.calibrated_prob >= $5)
             AND ($6::double precision IS NULL OR s.calibrated_prob < $6)
             AND ($7::timestamptz IS NULL OR c.created_at >= $7::timestamptz)
             AND ($8::timestamptz IS NULL OR c.created_at < $8::timestamptz)
             AND ($9::text IS NULL OR ($9 = 'NONE' AND b.case_id IS NULL) OR b.status = $9)
             AND NOT EXISTS (
                 SELECT 1
                 FROM case_actions ca
                 WHERE ca.case_id = c.case_id
                   AND ca.action IN (
                       'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                       'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE',
                       'NEEDS_MORE_INFO', 'CONFIRM_SIMULATED'
                   )
             )""",
        risk_level, status, stream, merchant_type, score_min, score_max,
        received_from, received_before, evidence_status)
    summary = await _case_summary()
    decision_summary = await _decision_summary()
    decision_breakdown = await state.db.rfetch(
        """SELECT c.risk_level, latest.action, count(*) AS decision_count
           FROM cases c
           JOIN LATERAL (
               SELECT ca.action
               FROM case_actions ca
               WHERE ca.case_id = c.case_id
                 AND ca.action IN (
                     'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                     'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE', 'NEEDS_MORE_INFO'
                 )
               ORDER BY ca.created_at DESC, ca.id DESC
               LIMIT 1
           ) latest ON true
           WHERE c.risk_level IN ('HIGH', 'MEDIUM', 'LOW')
           GROUP BY c.risk_level, latest.action"""
    )
    return {
        "items": [dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
        "total": total,
        "decision_summary": dict(decision_summary),
        "decision_breakdown": [dict(r) for r in decision_breakdown],
        **summary,
    }


@router.get("/analytics/dashboard")
async def get_dashboard_analytics(
    user: User = Depends(any_reviewer),
):
    summary = await state.db.rfetchrow(
        """SELECT count(*) FILTER (WHERE s.status = 'SCORED') AS total_scored,
                  count(*) FILTER (
                      WHERE s.status = 'SCORED' AND s.scored_at >= now() - interval '60 minutes'
                  ) AS scored_last_hour,
                  count(*) FILTER (
                      WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                  ) AS open_count,
                  count(*) FILTER (
                      WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                        AND c.risk_level = 'HIGH'
                  ) AS high_open,
                  count(*) FILTER (
                      WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                        AND c.risk_level = 'HIGH' AND c.created_at >= now() - interval '60 minutes'
                  ) AS high_open_last_hour,
                  count(*) FILTER (
                      WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                        AND c.risk_level = 'MEDIUM'
                  ) AS medium_open,
                  count(*) FILTER (
                      WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                        AND c.risk_level = 'MEDIUM' AND c.created_at >= now() - interval '60 minutes'
                  ) AS medium_open_last_hour,
                  count(*) FILTER (WHERE s.status <> 'SCORED') AS unscored_total,
                  count(*) FILTER (
                      WHERE s.status <> 'SCORED' AND s.scored_at >= now() - interval '24 hours'
                  ) AS unscored_last_24h,
                  coalesce(percentile_cont(0.95) WITHIN GROUP (ORDER BY s.latency_ms)
                      FILTER (WHERE s.status = 'SCORED' AND s.latency_ms IS NOT NULL
                                AND s.scored_at >= now() - interval '60 minutes'), 0) AS latency_p95_ms,
                  coalesce(avg(s.calibrated_prob) FILTER (WHERE s.status = 'SCORED'), 0) AS average_probability
           FROM scoring_results s
           LEFT JOIN cases c ON c.txn_id = s.txn_id"""
    )
    decision_breakdown = await state.db.rfetch(
        """SELECT c.risk_level, latest.action, count(*) AS decision_count
           FROM cases c
           JOIN LATERAL (
               SELECT ca.action
               FROM case_actions ca
               WHERE ca.case_id = c.case_id
                 AND ca.action IN (
                     'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                     'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE', 'NEEDS_MORE_INFO'
                 )
               ORDER BY ca.created_at DESC, ca.id DESC
               LIMIT 1
           ) latest ON true
           WHERE c.risk_level IN ('HIGH', 'MEDIUM', 'LOW')
           GROUP BY c.risk_level, latest.action"""
    )
    recent_cases = await state.db.rfetch(
        """SELECT c.case_id, c.txn_id, c.risk_level, c.status, c.created_at,
                  s.calibrated_prob,
                  t.payload_masked->>'merchant' AS merchant,
                  t.payload_masked->>'amt' AS amount
           FROM cases c
           JOIN scoring_results s ON s.txn_id = c.txn_id
           JOIN transactions t ON t.txn_id = c.txn_id
           WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
           ORDER BY s.calibrated_prob DESC NULLS LAST, c.created_at DESC
           LIMIT 6"""
    )
    top_features = await state.db.rfetch(
        """SELECT item.data->>'feature' AS feature,
                  count(*)::int AS occurrences,
                  coalesce(avg(abs((item.data->>'contribution')::double precision)), 0)::double precision
                      AS average_impact
           FROM scoring_results s
           CROSS JOIN LATERAL jsonb_array_elements(
               CASE WHEN jsonb_typeof(s.explanations) = 'array'
                    THEN s.explanations ELSE '[]'::jsonb END
           ) AS item(data)
           WHERE s.status = 'SCORED'
             AND s.scored_at >= now() - interval '24 hours'
             AND item.data->>'feature' IS NOT NULL
             AND item.data->>'feature' <> 'model_probability'
             AND item.data->>'contribution' IS NOT NULL
           GROUP BY item.data->>'feature'
           ORDER BY occurrences DESC, average_impact DESC
           LIMIT 5"""
    )
    validation_issues = await state.db.rfetch(
        """SELECT item.issue->>'field' AS field, item.issue->>'msg' AS message,
                  count(*)::int AS occurrences
           FROM scoring_results s
           CROSS JOIN LATERAL jsonb_array_elements(
               CASE WHEN jsonb_typeof(s.validation_errors) = 'array'
                    THEN s.validation_errors ELSE '[]'::jsonb END
           ) AS item(issue)
           WHERE s.status <> 'SCORED'
             AND s.scored_at >= now() - interval '24 hours'
             AND item.issue->>'field' IS NOT NULL
           GROUP BY item.issue->>'field', item.issue->>'msg'
           ORDER BY occurrences DESC, field
           LIMIT 5"""
    )
    dead_queue_names = (QUEUE_ML_JOBS, QUEUE_CASE_EVENTS, QUEUE_LLM_JOBS)
    if state.redis is None:
        raise HTTPException(status_code=503, detail="Redis is unavailable")
    dead_counts = await asyncio.gather(
        *(state.redis.llen(f"{queue}:dead") for queue in dead_queue_names)
    )
    return {
        "summary": dict(summary),
        "decision_breakdown": [dict(row) for row in decision_breakdown],
        "recent_cases": [dict(row) for row in recent_cases],
        "signal_health": {
            "window_hours": 24,
            "top_features": [dict(row) for row in top_features],
            "validation_issues": [dict(row) for row in validation_issues],
            "unscored_last_24h": summary["unscored_last_24h"],
            "dead_letters": [
                {"queue": queue, "count": count}
                for queue, count in zip(dead_queue_names, dead_counts)
            ],
        },
    }


async def _query_prometheus_range(
    client: httpx.AsyncClient,
    query: str,
    start: float,
    end: float,
    step: int,
) -> list[dict[str, float]]:
    try:
        response = await client.get(
            "/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.exception("prometheus_range_query_failed")
        raise HTTPException(status_code=503, detail="ML metrics are unavailable") from exc

    if not isinstance(payload, dict) or payload.get("status") != "success":
        error = payload.get("error", "unknown error") if isinstance(payload, dict) else "invalid response"
        log.error("prometheus_range_query_rejected: %s", error)
        raise HTTPException(status_code=503, detail="ML metrics are unavailable")

    data = payload.get("data")
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        log.error("prometheus_range_query_invalid_response")
        raise HTTPException(status_code=503, detail="ML metrics are unavailable")

    points = []
    try:
        for series in result:
            for timestamp, value in series["values"]:
                point = {"timestamp": float(timestamp), "value": float(value)}
                if math.isfinite(point["timestamp"]) and math.isfinite(point["value"]):
                    points.append(point)
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        log.exception("prometheus_range_query_invalid_samples")
        raise HTTPException(status_code=503, detail="ML metrics are unavailable") from exc
    return sorted(points, key=lambda point: point["timestamp"])


@router.get("/analytics/ml-operations")
async def get_ml_operations(
    user: User = Depends(any_reviewer),
    window_minutes: int = Query(60, ge=15, le=1440),
):
    end = time.time()
    start = end - window_minutes * 60
    step = max(15, window_minutes * 60 // 60)
    settings = get_settings()

    try:
        async with httpx.AsyncClient(base_url=settings.prometheus_url, timeout=5) as client:
            latency, throughput = await asyncio.gather(
                _query_prometheus_range(
                    client,
                    "histogram_quantile(0.95, sum by (le) "
                    "(rate(fraud_inference_seconds_bucket[5m]))) * 1000",
                    start,
                    end,
                    step,
                ),
                _query_prometheus_range(
                    client,
                    "sum(rate(fraud_inference_requests_total[5m]))",
                    start,
                    end,
                    step,
                ),
            )
    except httpx.HTTPError as exc:
        log.exception("prometheus_unavailable")
        raise HTTPException(status_code=503, detail="ML metrics are unavailable") from exc

    return {
        "window_minutes": window_minutes,
        "latency_p95_ms": latency,
        "throughput_rps": throughput,
    }


@router.get("/classifications")
async def list_classifications(
    user: User = Depends(any_reviewer),
    decision: str | None = Query(
        None,
        pattern="^(CONFIRMED_FRAUD|DISMISSED|INSUFFICIENT_EVIDENCE|ESCALATE|NEEDS_MORE_INFO|CONFIRM_SIMULATED)$",
    ),
    search: str | None = Query(None, max_length=200),
    status: CaseStatus | None = None,
    stream: ScoringStream | None = None,
    merchant_type: str | None = None,
    score_band: ScoreBand | None = None,
    sort: SortOrder = "newest",
    limit: int = Query(50, ge=1, le=50),
    offset: int = Query(0, ge=0),
):
    """List the latest human classification or handoff action for each case."""
    score_min, score_max = SCORE_BAND_BOUNDS[score_band] if score_band else (None, None)
    action_filter = f"OUTCOME_{decision}" if decision in {
        "CONFIRMED_FRAUD", "DISMISSED", "INSUFFICIENT_EVIDENCE"
    } else decision
    rows = await state.db.rfetch(
        """WITH numbered_cases AS (
               SELECT c.*,
                      row_number() OVER (ORDER BY c.created_at, c.case_id) AS run_number
               FROM cases c
           ), latest_actions AS (
               SELECT DISTINCT ON (ca.case_id)
                      ca.case_id, ca.action, ca.actor, ca.role, ca.note, ca.created_at
               FROM case_actions ca
               WHERE ca.action IN (
                   'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                   'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE',
                   'NEEDS_MORE_INFO', 'CONFIRM_SIMULATED'
               )
               ORDER BY ca.case_id, ca.created_at DESC, ca.id DESC
           )
           SELECT c.case_id, c.run_number, c.txn_id, c.risk_level, c.status,
                  s.calibrated_prob, latest.action, latest.actor, latest.role,
                  latest.note, latest.created_at AS decided_at,
                  t.payload_masked->>'merchant' AS merchant,
                  t.payload_masked->>'category' AS category,
                  t.payload_masked->>'amt' AS amount,
                  t.payload_masked->>'city' AS city,
                  t.payload_masked->>'state' AS region
           FROM numbered_cases c
           JOIN latest_actions latest ON latest.case_id = c.case_id
           JOIN transactions t ON t.txn_id = c.txn_id
           JOIN scoring_results s ON s.txn_id = c.txn_id
           WHERE ($1::text IS NULL OR latest.action = $1)
             AND ($2::text IS NULL OR position(lower($2) in lower(concat_ws(
                 ' ', c.case_id::text, c.txn_id, latest.actor,
                 t.payload_masked->>'merchant', t.payload_masked->>'category'
             ))) > 0)
             AND ($3::text IS NULL OR c.status = $3)
             AND ($4::text IS NULL OR ($4 = 'SCORED' AND s.status = 'SCORED')
                  OR ($4 = 'UNSCORED' AND s.status <> 'SCORED'))
             AND ($5::text IS NULL OR t.payload_masked->>'category' = $5)
             AND ($6::double precision IS NULL OR s.calibrated_prob >= $6)
             AND ($7::double precision IS NULL OR s.calibrated_prob < $7)
           ORDER BY CASE WHEN $8 = 'newest' THEN latest.created_at END DESC,
                    CASE WHEN $8 = 'oldest' THEN latest.created_at END ASC,
                    CASE WHEN $8 = 'highest_score' THEN s.calibrated_prob END DESC NULLS LAST,
                    CASE WHEN $8 = 'lowest_score' THEN s.calibrated_prob END ASC NULLS LAST,
                    latest.created_at DESC, c.case_id DESC
           LIMIT $9 OFFSET $10""",
        action_filter, search.strip() if search and search.strip() else None,
        status, stream, merchant_type, score_min, score_max, sort, limit, offset)
    total = await state.db.rfetchval(
        """WITH latest_actions AS (
               SELECT DISTINCT ON (ca.case_id) ca.case_id, ca.action, ca.actor
               FROM case_actions ca
               WHERE ca.action IN (
                   'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                   'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE',
                   'NEEDS_MORE_INFO', 'CONFIRM_SIMULATED'
               )
               ORDER BY ca.case_id, ca.created_at DESC, ca.id DESC
           )
           SELECT count(*)
           FROM latest_actions latest
           JOIN cases c ON c.case_id = latest.case_id
           JOIN transactions t ON t.txn_id = c.txn_id
           JOIN scoring_results s ON s.txn_id = c.txn_id
           WHERE ($1::text IS NULL OR latest.action = $1)
             AND ($2::text IS NULL OR position(lower($2) in lower(concat_ws(
                 ' ', c.case_id::text, c.txn_id, latest.actor,
                 t.payload_masked->>'merchant', t.payload_masked->>'category'
             ))) > 0)
             AND ($3::text IS NULL OR c.status = $3)
             AND ($4::text IS NULL OR ($4 = 'SCORED' AND s.status = 'SCORED')
                  OR ($4 = 'UNSCORED' AND s.status <> 'SCORED'))
             AND ($5::text IS NULL OR t.payload_masked->>'category' = $5)
             AND ($6::double precision IS NULL OR s.calibrated_prob >= $6)
             AND ($7::double precision IS NULL OR s.calibrated_prob < $7)""",
        action_filter, search.strip() if search and search.strip() else None,
        status, stream, merchant_type, score_min, score_max)
    decision_summary = await _decision_summary()
    return {
        "items": [dict(row) for row in rows],
        "limit": limit,
        "offset": offset,
        "total": total,
        "decision_summary": decision_summary,
    }


@router.get("/{case_id}")
async def get_case(case_id: int, user: User = Depends(any_reviewer)):
    row = await state.db.rfetchrow(
        """WITH numbered_cases AS (
               SELECT c.*,
                      row_number() OVER (ORDER BY c.created_at, c.case_id) AS run_number
               FROM cases c
           )
           SELECT c.case_id, c.run_number, c.txn_id, c.risk_level, c.status,
                  c.assigned_to, c.created_at, c.updated_at,
                  t.payload_masked AS transaction, t.data_version, t.trace_id,
                  s.raw_model_score, s.calibrated_prob, s.model_version, s.threshold_config_version,
                  s.features, s.explanations,
                  b.status AS brief_status, b.brief, b.citations, b.attempts AS brief_attempts, b.last_error,
                  b.model_name AS brief_model, b.prompt_version, b.playbook_version
           FROM numbered_cases c
           JOIN transactions t ON t.txn_id = c.txn_id
           JOIN scoring_results s ON s.txn_id = c.txn_id
           LEFT JOIN case_briefs b ON b.case_id = c.case_id
           WHERE c.case_id = $1""", case_id)
    if row is None:
        raise HTTPException(404, "Không tìm thấy case")
    actions = await state.db.rfetch(
        "SELECT actor, role, action, note, created_at FROM case_actions WHERE case_id=$1 ORDER BY id", case_id)
    return {**dict(row), "actions": [dict(a) for a in actions]}


@router.post("/manual", status_code=201)
async def create_manual_case(body: ManualCaseRequest, request: Request,
                             user: User = Depends(require_roles(Role.ANALYST, Role.RISK_LEAD))):
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    async with state.db.write_tx() as conn:
        txn = await conn.fetchrow(
            "SELECT txn_id FROM transactions WHERE txn_id=$1", body.txn_id)
        if txn is None:
            raise HTTPException(404, "Không tìm thấy giao dịch")
        existing = await conn.fetchrow(
            "SELECT case_id FROM cases WHERE txn_id=$1", body.txn_id)
        if existing is not None:
            raise HTTPException(409, f"Giao dịch đã có case #{existing['case_id']}")
        case_id = await conn.fetchval(
            """INSERT INTO cases (txn_id, risk_level, assigned_to)
               VALUES ($1,$2,$3) RETURNING case_id""",
            body.txn_id, body.risk_level.value, user.username)
        await write_audit(
            conn, actor=user.username, event="case.manual_created", entity="case",
            entity_id=case_id, detail={"txn_id": body.txn_id, "reason": body.reason},
            trace_id=trace_id)
    if body.risk_level != RiskLevel.LOW:
        await enqueue_brief(case_id=case_id, txn_id=body.txn_id, level=body.risk_level,
                            trace_id=trace_id, requested_by=user.username)
    return {"case_id": case_id, "txn_id": body.txn_id, "status": CaseStatus.OPEN}


@router.post("/{case_id}/notes", status_code=201)
async def add_case_note(case_id: int, body: ActionRequest, request: Request,
                        user: User = Depends(require_roles(Role.ANALYST, Role.RISK_LEAD))):
    if not body.note:
        raise HTTPException(422, "Note không được để trống")
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    async with state.db.write_tx() as conn:
        exists = await conn.fetchval("SELECT 1 FROM cases WHERE case_id=$1", case_id)
        if exists is None:
            raise HTTPException(404, "Không tìm thấy case")
        await conn.execute(
            """INSERT INTO case_actions (case_id, actor, role, action, note)
               VALUES ($1,$2,$3,'NOTE',$4)""",
            case_id, user.username, user.role.value, body.note)
        await write_audit(conn, actor=user.username, event="case.note_added",
                          entity="case", entity_id=case_id, detail={"note": body.note},
                          trace_id=trace_id)
    return {"case_id": case_id, "action": "NOTE", "note": body.note}


@router.put("/{case_id}/assign")
async def assign_case(case_id: int, body: AssignmentRequest, request: Request,
                      user: User = Depends(require_roles(Role.ANALYST, Role.RISK_LEAD))):
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    async with state.db.write_tx() as conn:
        row = await conn.fetchrow(
            "UPDATE cases SET assigned_to=$2, updated_at=now() WHERE case_id=$1 RETURNING case_id, assigned_to",
            case_id, body.assigned_to)
        if row is None:
            raise HTTPException(404, "Không tìm thấy case")
        await write_audit(conn, actor=user.username, event="case.assigned",
                          entity="case", entity_id=case_id,
                          detail={"assigned_to": body.assigned_to}, trace_id=trace_id)
    return dict(row)


@router.put("/{case_id}/outcome")
async def set_case_outcome(case_id: int, body: OutcomeRequest, request: Request,
                           user: User = Depends(require_roles(Role.ANALYST, Role.RISK_LEAD))):
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    new_status = OUTCOME_TO_STATUS[body.outcome]
    async with state.db.write_tx() as conn:
        current = await conn.fetchrow(
            "SELECT status FROM cases WHERE case_id=$1 FOR UPDATE", case_id)
        if current is None:
            raise HTTPException(404, "Không tìm thấy case")
        previous_outcome = await conn.fetchval(
            """SELECT action FROM case_actions
               WHERE case_id=$1 AND action = ANY($2::text[])
               ORDER BY id DESC LIMIT 1""",
            case_id, list(OUTCOME_ACTIONS))
        if not can_update_outcome(current["status"], previous_outcome is not None):
            raise HTTPException(409, "Case đã có kết quả cuối cùng")
        await conn.execute(
            """UPDATE cases SET status=$2, updated_at=now(), assigned_to=COALESCE(assigned_to,$3)
               WHERE case_id=$1""",
            case_id, new_status.value, user.username)
        await conn.execute(
            """INSERT INTO case_actions (case_id, actor, role, action, note)
               VALUES ($1,$2,$3,$4,$5)""",
            case_id, user.username, user.role.value, f"OUTCOME_{body.outcome.value}", body.note)
        await write_audit(conn, actor=user.username,
                          event="case.outcome_revised" if previous_outcome else "case.outcome_set",
                          entity="case", entity_id=case_id,
                          detail={"outcome": body.outcome.value, "previous_outcome": previous_outcome,
                                  "status": new_status.value},
                          trace_id=trace_id)
    return {"case_id": case_id, "outcome": body.outcome.value, "status": new_status}


@router.post("/{case_id}/actions")
async def act_on_case(case_id: int, body: ActionRequest, request: Request,
                      user: User = Depends(require_roles(Role.ANALYST))):
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    new_status = ACTION_TO_STATUS[body.action]
    async with state.db.write_tx() as conn:
        row = await conn.fetchrow(
            """UPDATE cases SET status=$2, updated_at=now(), assigned_to=COALESCE(assigned_to,$3)
               WHERE case_id=$1
                 AND status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
               RETURNING case_id""",
            case_id, new_status.value, user.username)
        if row is None:
            raise HTTPException(409, "Case không tồn tại hoặc đã đóng")
        await conn.execute(
            "INSERT INTO case_actions (case_id, actor, role, action, note) VALUES ($1,$2,$3,$4,$5)",
            case_id, user.username, user.role.value, body.action.value, body.note)
        await write_audit(conn, actor=user.username, event=f"case.{body.action.value.lower()}",
                          entity="case", entity_id=case_id, detail={"note": body.note, "new_status": new_status},
                          trace_id=trace_id)
    return {"case_id": case_id, "status": new_status}


@router.post("/{case_id}/resolve")
async def resolve_case(case_id: int, body: ResolveRequest, request: Request,
                       user: User = Depends(require_roles(Role.RISK_LEAD))):
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    new_status = CaseStatus.CLOSED if body.decision == RiskLeadDecision.CLOSE else CaseStatus.RETURNED
    async with state.db.write_tx() as conn:
        row = await conn.fetchrow(
            "UPDATE cases SET status=$2, updated_at=now() WHERE case_id=$1 AND status='ESCALATED' RETURNING case_id",
            case_id, new_status.value)
        if row is None:
            raise HTTPException(409, "Chỉ xử lý được case đang ở trạng thái ESCALATED")
        await conn.execute(
            "INSERT INTO case_actions (case_id, actor, role, action, note) VALUES ($1,$2,$3,$4,$5)",
            case_id, user.username, user.role.value, body.decision.value, body.note)
        await write_audit(conn, actor=user.username, event=f"case.{body.decision.value.lower()}",
                          entity="case", entity_id=case_id, detail={"note": body.note}, trace_id=trace_id)
    return {"case_id": case_id, "status": new_status}


@router.post("/{case_id}/brief:regenerate", status_code=202)
async def regenerate_brief(case_id: int, request: Request,
                           user: User = Depends(require_roles(Role.ANALYST, Role.RISK_LEAD))):
    """Analyst tạo lại brief. Giới hạn số lần/giờ để bảo vệ ngân sách token."""
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    key = f"regen:{case_id}"
    count = await state.redis.incr(key)
    if count == 1:
        await state.redis.expire(key, 3600)
    if count > state.settings.regen_limit_per_hour:
        raise HTTPException(429, "Đã vượt giới hạn tạo lại brief cho case này")

    row = await state.db.wfetchrow("SELECT txn_id, risk_level FROM cases WHERE case_id=$1", case_id)
    if row is None:
        raise HTTPException(404, "Không tìm thấy case")
    env = await enqueue_brief(case_id=case_id, txn_id=row["txn_id"], level=RiskLevel(row["risk_level"]),
                              trace_id=trace_id, regenerate=True, requested_by=user.username)
    async with state.db.write_tx() as conn:
        await write_audit(conn, actor=user.username, event="brief.regenerate", entity="case",
                          entity_id=case_id, detail={"job_id": env.job_id}, trace_id=trace_id)
    return {"case_id": case_id, "job_id": env.job_id, "brief_status": "PENDING"}
