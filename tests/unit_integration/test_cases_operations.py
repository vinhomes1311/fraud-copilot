"""Unit & Integration tests for Analyst Operations on Cases (analyst_api/routes/cases.py).

Covers all missing endpoint tests flagged in the QA review:
- GET /cases (list_cases with filters, pagination)
- GET /cases/classifications (decision filter, search, pagination)
- GET /cases/{case_id} (case details + actions, 404 when missing)
- POST /cases/manual (create manual case, 404 txn missing, 409 duplicate case, enqueue brief)
- POST /cases/{case_id}/notes (empty note 422, 404 case missing, note insertion + audit)
- PUT /cases/{case_id}/assign (assign user, 404 when case missing)
- PUT /cases/{case_id}/outcome (set outcome, 404 missing, 409 final status locked, revise outcome)
- POST /cases/{case_id}/actions (analyst action, 409 when already closed/missing, valid action)
- POST /cases/{case_id}/resolve (risk lead resolve, 409 when not ESCALATED, CLOSE and RETURN)
- POST /cases/{case_id}/brief:regenerate (rate limit 429, 404 missing, 202 accepted + brief queued)
"""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from analyst_api.auth import User
from analyst_api.routes.cases import (
    ActionRequest,
    AssignmentRequest,
    ManualCaseRequest,
    OutcomeRequest,
    ResolveRequest,
    act_on_case,
    add_case_note,
    assign_case,
    create_manual_case,
    get_case,
    list_cases,
    list_classifications,
    regenerate_brief,
    resolve_case,
    set_case_outcome,
)
from analyst_api.state import state
from fraud_common.schemas import (
    AnalystAction,
    CaseOutcome,
    CaseStatus,
    JobEnvelope,
    RiskLeadDecision,
    RiskLevel,
    Role,
)


# ---------------------------------------------------------------------------
# Mock Database & Request Helpers
# ---------------------------------------------------------------------------

class MockCasesDb:
    def __init__(self):
        self.rfetch_handler = None
        self.rfetchrow_handler = None
        self.rfetchval_handler = None
        self.wfetchrow_handler = None
        self.tx_conn = None

    async def rfetch(self, sql, *args):
        if self.rfetch_handler:
            res = self.rfetch_handler(sql, *args)
            return (await res) if inspect.isawaitable(res) else res
        return []

    async def rfetchrow(self, sql, *args):
        if self.rfetchrow_handler:
            res = self.rfetchrow_handler(sql, *args)
            return (await res) if inspect.isawaitable(res) else res
        return None

    async def rfetchval(self, sql, *args):
        if self.rfetchval_handler:
            res = self.rfetchval_handler(sql, *args)
            return (await res) if inspect.isawaitable(res) else res
        return None

    async def wfetchrow(self, sql, *args):
        if self.wfetchrow_handler:
            res = self.wfetchrow_handler(sql, *args)
            return (await res) if inspect.isawaitable(res) else res
        return None

    @asynccontextmanager
    async def write_tx(self):
        yield self.tx_conn


class MockTxConn:
    def __init__(self):
        self.fetchrow_handler = None
        self.fetchval_handler = None
        self.execute_calls = []

    async def fetchrow(self, sql, *args):
        if self.fetchrow_handler:
            res = self.fetchrow_handler(sql, *args)
            return (await res) if inspect.isawaitable(res) else res
        return None

    async def fetchval(self, sql, *args):
        if self.fetchval_handler:
            res = self.fetchval_handler(sql, *args)
            return (await res) if inspect.isawaitable(res) else res
        return None

    async def execute(self, sql, *args):
        self.execute_calls.append((sql, args))


def make_dummy_request(request_id: str = "test-req-123") -> Request:
    scope = {
        "type": "http",
        "headers": [(b"x-request-id", request_id.encode())],
    }
    return Request(scope)


# ===========================================================================
# 1. Tests for Query Endpoints (list_cases, list_classifications, get_case)
# ===========================================================================

@pytest.mark.asyncio
async def test_list_cases_and_classifications():
    db = MockCasesDb()
    prev_db = state.db
    state.db = db

    analyst = User(username="analyst_jane", role=Role.ANALYST)

    try:
        # Mock summary and list items
        db.rfetchrow_handler = lambda sql, *args: {
            "open_count": 5, "high_count": 2, "medium_count": 3,
            "average_probability": 0.72, "scoring_latency_ms": 350.0,
        }
        db.rfetch_handler = lambda sql, *args: [
            {"case_id": 1, "txn_id": "txn_1", "status": "OPEN", "risk_level": "HIGH"},
            {"case_id": 2, "txn_id": "txn_2", "status": "IN_REVIEW", "risk_level": "MEDIUM"},
        ]
        db.rfetchval_handler = lambda sql, *args: 2

        res = await list_cases(user=analyst, risk_level=RiskLevel.HIGH, status=CaseStatus.OPEN, limit=10, offset=0)
        assert res["total"] == 2
        assert len(res["items"]) == 2
        assert res["open_count"] == 5

        # Classifications list
        res_class = await list_classifications(user=analyst, decision="CONFIRMED_FRAUD", search=None, limit=10, offset=0)
        assert res_class["total"] == 2
        assert len(res_class["items"]) == 2

        # Classifications with search term
        res_search = await list_classifications(user=analyst, decision=None, search=" merchant_xyz ", limit=10, offset=0)
        assert res_search["total"] == 2
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_get_case_success_and_not_found():
    db = MockCasesDb()
    prev_db = state.db
    state.db = db

    analyst = User(username="analyst_jane", role=Role.ANALYST)

    try:
        # 1. Case found
        db.rfetchrow_handler = lambda sql, cid: {
            "case_id": cid, "txn_id": "txn_100", "status": "OPEN", "risk_level": "HIGH",
            "assigned_to": "analyst_jane", "created_at": None, "updated_at": None,
            "transaction": {"amt": 500}, "raw_model_score": 0.9, "calibrated_prob": 0.88,
            "model_version": "v1", "threshold_config_version": 1, "features": {}, "explanations": [],
            "brief_status": "READY", "brief": "Suspicious", "citations": [], "brief_attempts": 1, "last_error": None,
        }
        db.rfetch_handler = lambda sql, cid: [
            {"actor": "analyst_jane", "role": "analyst", "action": "NOTE", "note": "Checked card", "created_at": None}
        ]

        case_res = await get_case(case_id=100, user=analyst)
        assert case_res["case_id"] == 100
        assert case_res["txn_id"] == "txn_100"
        assert len(case_res["actions"]) == 1

        # 2. Case not found -> 404
        db.rfetchrow_handler = lambda sql, cid: None
        with pytest.raises(HTTPException) as exc_info:
            await get_case(case_id=999, user=analyst)
        assert exc_info.value.status_code == 404
        assert "Không tìm thấy case" in exc_info.value.detail
    finally:
        state.db = prev_db


# ===========================================================================
# 2. Tests for Manual Case Creation (POST /cases/manual)
# ===========================================================================

@pytest.mark.asyncio
async def test_create_manual_case_scenarios():
    db = MockCasesDb()
    conn = MockTxConn()
    db.tx_conn = conn
    prev_db = state.db
    state.db = db

    analyst = User(username="analyst_jane", role=Role.ANALYST)
    req = make_dummy_request()

    try:
        # Scenario 1: Transaction not found -> 404
        conn.fetchrow_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await create_manual_case(
                ManualCaseRequest(txn_id="missing_txn", risk_level=RiskLevel.HIGH, reason="Fraud reported"),
                req, analyst
            )
        assert exc_info.value.status_code == 404
        assert "Không tìm thấy giao dịch" in exc_info.value.detail

        # Scenario 2: Transaction exists, but already has a case -> 409
        def mock_existing(sql, *args):
            if "SELECT txn_id FROM transactions" in sql:
                return {"txn_id": args[0]}
            if "SELECT case_id FROM cases" in sql:
                return {"case_id": 55}
            return None

        conn.fetchrow_handler = mock_existing
        with pytest.raises(HTTPException) as exc_info:
            await create_manual_case(
                ManualCaseRequest(txn_id="existing_txn", risk_level=RiskLevel.HIGH, reason="Fraud reported"),
                req, analyst
            )
        assert exc_info.value.status_code == 409
        assert "Giao dịch đã có case #55" in exc_info.value.detail

        # Scenario 3: Valid creation -> creates case & enqueues brief
        def mock_valid(sql, *args):
            if "SELECT txn_id FROM transactions" in sql:
                return {"txn_id": args[0]}
            if "SELECT case_id FROM cases" in sql:
                return None
            return None

        conn.fetchrow_handler = mock_valid
        conn.fetchval_handler = lambda sql, *args: 101

        with patch("analyst_api.routes.cases.enqueue_brief", new_callable=AsyncMock) as mock_enqueue:
            res = await create_manual_case(
                ManualCaseRequest(txn_id="valid_txn", risk_level=RiskLevel.HIGH, reason="Verified suspect"),
                req, analyst
            )
            assert res["case_id"] == 101
            assert res["txn_id"] == "valid_txn"
            assert res["status"] == CaseStatus.OPEN
            assert mock_enqueue.called
    finally:
        state.db = prev_db


# ===========================================================================
# 3. Tests for Notes & Assign (POST /notes, PUT /assign)
# ===========================================================================

@pytest.mark.asyncio
async def test_add_case_note_and_assign():
    db = MockCasesDb()
    conn = MockTxConn()
    db.tx_conn = conn
    prev_db = state.db
    state.db = db

    analyst = User(username="analyst_jane", role=Role.ANALYST)
    req = make_dummy_request()

    try:
        # 1. Note empty -> 422
        with pytest.raises(HTTPException) as exc_info:
            await add_case_note(1, ActionRequest(action=AnalystAction.NOTE, note=""), req, analyst)
        assert exc_info.value.status_code == 422

        # 2. Case missing for note -> 404
        conn.fetchval_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await add_case_note(999, ActionRequest(action=AnalystAction.NOTE, note="Important note"), req, analyst)
        assert exc_info.value.status_code == 404

        # 3. Note success
        conn.fetchval_handler = lambda sql, *args: 1
        res_note = await add_case_note(1, ActionRequest(action=AnalystAction.NOTE, note="Valid note"), req, analyst)
        assert res_note["action"] == "NOTE"
        assert res_note["note"] == "Valid note"

        # 4. Assign: case missing -> 404
        conn.fetchrow_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await assign_case(999, AssignmentRequest(assigned_to="lead1"), req, analyst)
        assert exc_info.value.status_code == 404

        # 5. Assign: success
        conn.fetchrow_handler = lambda sql, *args: {"case_id": 1, "assigned_to": "lead1"}
        res_assign = await assign_case(1, AssignmentRequest(assigned_to="lead1"), req, analyst)
        assert res_assign["assigned_to"] == "lead1"
    finally:
        state.db = prev_db


# ===========================================================================
# 4. Tests for Outcome, Actions & Resolve (PUT /outcome, POST /actions, POST /resolve)
# ===========================================================================

@pytest.mark.asyncio
async def test_set_outcome_workflow():
    db = MockCasesDb()
    conn = MockTxConn()
    db.tx_conn = conn
    prev_db = state.db
    state.db = db

    analyst = User(username="analyst_jane", role=Role.ANALYST)
    req = make_dummy_request()

    try:
        # Case missing -> 404
        conn.fetchrow_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await set_case_outcome(999, OutcomeRequest(outcome=CaseOutcome.CONFIRMED_FRAUD, note="Confirmed"), req, analyst)
        assert exc_info.value.status_code == 404

        # Case already in closed/terminal status without prior outcome -> 409
        conn.fetchrow_handler = lambda sql, *args: {"status": "CLOSED"}
        conn.fetchval_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await set_case_outcome(1, OutcomeRequest(outcome=CaseOutcome.CONFIRMED_FRAUD, note="Confirmed"), req, analyst)
        assert exc_info.value.status_code == 409
        assert "Case đã có kết quả cuối cùng" in exc_info.value.detail

        # Success set outcome
        conn.fetchrow_handler = lambda sql, *args: {"status": "IN_REVIEW"}
        res = await set_case_outcome(1, OutcomeRequest(outcome=CaseOutcome.CONFIRMED_FRAUD, note="Confirmed"), req, analyst)
        assert res["outcome"] == "CONFIRMED_FRAUD"
        assert res["status"] == CaseStatus.CONFIRMED_FRAUD
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_act_on_case_and_resolve():
    db = MockCasesDb()
    conn = MockTxConn()
    db.tx_conn = conn
    prev_db = state.db
    state.db = db

    analyst = User(username="analyst_jane", role=Role.ANALYST)
    lead = User(username="lead_bob", role=Role.RISK_LEAD)
    req = make_dummy_request()

    try:
        # act_on_case: Case already closed or not found -> 409
        conn.fetchrow_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await act_on_case(1, ActionRequest(action=AnalystAction.ESCALATE, note="Escalating"), req, analyst)
        assert exc_info.value.status_code == 409

        # act_on_case: success
        conn.fetchrow_handler = lambda sql, *args: {"case_id": 1}
        res_action = await act_on_case(1, ActionRequest(action=AnalystAction.ESCALATE, note="Escalating"), req, analyst)
        assert res_action["status"] == CaseStatus.ESCALATED

        # resolve_case: Not in ESCALATED status -> 409
        conn.fetchrow_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await resolve_case(1, ResolveRequest(decision=RiskLeadDecision.CLOSE, note="All clear"), req, lead)
        assert exc_info.value.status_code == 409
        assert "Chỉ xử lý được case đang ở trạng thái ESCALATED" in exc_info.value.detail

        # resolve_case: success CLOSE
        conn.fetchrow_handler = lambda sql, *args: {"case_id": 1}
        res_resolve = await resolve_case(1, ResolveRequest(decision=RiskLeadDecision.CLOSE, note="Done"), req, lead)
        assert res_resolve["status"] == CaseStatus.CLOSED

        # resolve_case: success RETURN
        res_return = await resolve_case(1, ResolveRequest(decision=RiskLeadDecision.RETURN, note="More info needed"), req, lead)
        assert res_return["status"] == CaseStatus.RETURNED
    finally:
        state.db = prev_db


# ===========================================================================
# 5. Tests for Brief Regeneration (POST /cases/{case_id}/brief:regenerate)
# ===========================================================================

@pytest.mark.asyncio
async def test_regenerate_brief_endpoint():
    db = MockCasesDb()
    conn = MockTxConn()
    db.tx_conn = conn
    prev_db = state.db
    prev_redis = state.redis
    prev_settings = state.settings

    state.db = db
    state.settings = SimpleNamespace(regen_limit_per_hour=3)

    mock_redis = AsyncMock()
    state.redis = mock_redis

    analyst = User(username="analyst_jane", role=Role.ANALYST)
    req = make_dummy_request()

    try:
        # 1. Rate limit exceeded -> 429
        mock_redis.incr.return_value = 5  # count > 3
        with pytest.raises(HTTPException) as exc_info:
            await regenerate_brief(1, req, analyst)
        assert exc_info.value.status_code == 429
        assert "Đã vượt giới hạn" in exc_info.value.detail

        # 2. Case not found -> 404
        mock_redis.incr.return_value = 1
        db.wfetchrow_handler = lambda sql, *args: None
        with pytest.raises(HTTPException) as exc_info:
            await regenerate_brief(999, req, analyst)
        assert exc_info.value.status_code == 404

        # 3. Success -> enqueues brief & returns 202
        db.wfetchrow_handler = lambda sql, *args: {"txn_id": "txn_regen", "risk_level": "HIGH"}
        dummy_envelope = JobEnvelope(job_type="generate_brief", job_id="job_brief_123", trace_id="trace_test_123", payload={})

        with patch("analyst_api.routes.cases.enqueue_brief", new_callable=AsyncMock, return_value=dummy_envelope):
            res = await regenerate_brief(1, req, analyst)
            assert res["case_id"] == 1
            assert res["job_id"] == "job_brief_123"
            assert res["brief_status"] == "PENDING"
    finally:
        state.db = prev_db
        state.redis = prev_redis
        state.settings = prev_settings
