"""Luồng 3: Analyst / Risk Lead đọc hàng đợi, xem hồ sơ, ra quyết định."""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from fraud_common.schemas import AnalystAction, CaseOutcome, CaseStatus, RiskLeadDecision, RiskLevel, Role

from ..audit import write_audit
from ..auth import User, require_roles
from ..events import enqueue_brief
from ..state import state

router = APIRouter(prefix="/cases", tags=["cases"])
any_reviewer = require_roles(Role.ANALYST, Role.RISK_LEAD)

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
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Review Queue: newest transactions first. Risk Lead defaults to ESCALATED."""
    if user.role == Role.RISK_LEAD and status is None:
        status = CaseStatus.ESCALATED
    rows = await state.db.rfetch(
        """SELECT c.case_id, c.txn_id, c.risk_level, c.status, c.assigned_to, c.created_at,
                  s.calibrated_prob, b.status AS brief_status
           FROM cases c
           JOIN scoring_results s ON s.txn_id = c.txn_id
           LEFT JOIN case_briefs b ON b.case_id = c.case_id
           WHERE ($1::text IS NULL OR c.risk_level = $1) AND ($2::text IS NULL OR c.status = $2)
           ORDER BY c.created_at DESC, c.case_id DESC
           LIMIT $3 OFFSET $4""",
        risk_level, status, limit, offset)
    total = await state.db.rfetchval(
        """SELECT count(*)
           FROM cases c
           WHERE ($1::text IS NULL OR c.risk_level = $1) AND ($2::text IS NULL OR c.status = $2)""",
        risk_level, status)
    summary = await state.db.rfetchrow(
        """SELECT count(*) FILTER (WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                                     AND c.risk_level IN ('MEDIUM', 'HIGH')) AS open_count,
                 count(*) FILTER (WHERE c.status NOT IN ('CLOSED', 'DISMISSED', 'CONFIRMED_FRAUD', 'CONFIRMED_SIMULATED')
                                     AND c.risk_level = 'HIGH') AS high_count,
                 coalesce(avg(s.calibrated_prob), 0) AS average_probability
                 ,coalesce(avg(s.latency_ms) FILTER (WHERE s.status = 'SCORED'), 0) AS scoring_latency_ms
          FROM cases c
          JOIN scoring_results s ON s.txn_id = c.txn_id""")
    return {
        "items": [dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
        "total": total,
        "open_count": summary["open_count"],
        "high_count": summary["high_count"],
        "average_probability": summary["average_probability"],
        "scoring_latency_ms": summary["scoring_latency_ms"],
    }


@router.get("/analytics/transactions")
async def list_analytics_transactions(
    user: User = Depends(any_reviewer),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = await state.db.rfetch(
        """SELECT c.case_id, c.txn_id, c.risk_level, c.status, c.created_at,
                  s.calibrated_prob, s.model_version, b.status AS brief_status
           FROM cases c
           JOIN scoring_results s ON s.txn_id = c.txn_id
           LEFT JOIN case_briefs b ON b.case_id = c.case_id
        WHERE c.risk_level IN ('HIGH', 'MEDIUM')
        ORDER BY s.calibrated_prob DESC, c.created_at
           LIMIT $1 OFFSET $2""",
        limit, offset)
    total = await state.db.rfetchval(
        "SELECT count(*) FROM cases WHERE risk_level IN ('HIGH', 'MEDIUM')")
    return {"items": [dict(r) for r in rows], "limit": limit, "offset": offset, "total": total}


@router.get("/classifications")
async def list_classifications(
    user: User = Depends(any_reviewer),
    decision: str | None = Query(
        None,
        pattern="^(CONFIRMED_FRAUD|DISMISSED|INSUFFICIENT_EVIDENCE|ESCALATE|NEEDS_MORE_INFO|CONFIRM_SIMULATED)$",
    ),
    search: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List the latest human classification or handoff action for each case."""
    action_filter = f"OUTCOME_{decision}" if decision in {
        "CONFIRMED_FRAUD", "DISMISSED", "INSUFFICIENT_EVIDENCE"
    } else decision
    rows = await state.db.rfetch(
        """SELECT c.case_id, c.txn_id, c.risk_level, c.status,
                  s.calibrated_prob, latest.action, latest.actor, latest.role,
                  latest.note, latest.created_at AS decided_at,
                  t.payload_masked->>'merchant' AS merchant,
                  t.payload_masked->>'category' AS category,
                  t.payload_masked->>'amt' AS amount,
                  t.payload_masked->>'city' AS city,
                  t.payload_masked->>'state' AS region
           FROM cases c
           JOIN LATERAL (
               SELECT ca.action, ca.actor, ca.role, ca.note, ca.created_at
               FROM case_actions ca
               WHERE ca.case_id = c.case_id
                 AND ca.action IN (
                     'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                     'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE',
                     'NEEDS_MORE_INFO', 'CONFIRM_SIMULATED'
                 )
               ORDER BY ca.created_at DESC, ca.id DESC
               LIMIT 1
           ) latest ON true
           JOIN transactions t ON t.txn_id = c.txn_id
           JOIN scoring_results s ON s.txn_id = c.txn_id
           WHERE ($1::text IS NULL OR latest.action = $1)
             AND ($2::text IS NULL OR position(lower($2) in lower(concat_ws(
                 ' ', c.case_id::text, c.txn_id, latest.actor,
                 t.payload_masked->>'merchant', t.payload_masked->>'category'
             ))) > 0)
           ORDER BY latest.created_at DESC, c.case_id DESC
           LIMIT $3 OFFSET $4""",
        action_filter, search.strip() if search and search.strip() else None, limit, offset)
    total = await state.db.rfetchval(
        """SELECT count(*)
           FROM cases c
           JOIN LATERAL (
               SELECT ca.action, ca.actor
               FROM case_actions ca
               WHERE ca.case_id = c.case_id
                 AND ca.action IN (
                     'OUTCOME_CONFIRMED_FRAUD', 'OUTCOME_DISMISSED',
                     'OUTCOME_INSUFFICIENT_EVIDENCE', 'ESCALATE',
                     'NEEDS_MORE_INFO', 'CONFIRM_SIMULATED'
                 )
               ORDER BY ca.created_at DESC, ca.id DESC
               LIMIT 1
           ) latest ON true
           JOIN transactions t ON t.txn_id = c.txn_id
           WHERE ($1::text IS NULL OR latest.action = $1)
             AND ($2::text IS NULL OR position(lower($2) in lower(concat_ws(
                 ' ', c.case_id::text, c.txn_id, latest.actor,
                 t.payload_masked->>'merchant', t.payload_masked->>'category'
             ))) > 0)""",
        action_filter, search.strip() if search and search.strip() else None)
    return {"items": [dict(row) for row in rows], "limit": limit, "offset": offset, "total": total}


@router.get("/{case_id}")
async def get_case(case_id: int, user: User = Depends(any_reviewer)):
    row = await state.db.rfetchrow(
        """SELECT c.case_id, c.txn_id, c.risk_level, c.status, c.assigned_to, c.created_at, c.updated_at,
                  t.payload_masked AS transaction,
                  s.raw_model_score, s.calibrated_prob, s.model_version, s.threshold_config_version,
                  s.features, s.explanations,
                  b.status AS brief_status, b.brief, b.citations, b.attempts AS brief_attempts, b.last_error
           FROM cases c
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
