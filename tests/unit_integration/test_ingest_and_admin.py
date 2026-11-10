"""Tests for analyst_api/routes/ingest.py and analyst_api/routes/admin.py."""
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from analyst_api.auth import User
from analyst_api.routes.admin import ThresholdUpdate, get_threshold, put_threshold, queue_stats
from analyst_api.routes.ingest import get_result, ingest_transaction
from analyst_api.state import state
from fraud_common.schemas import Role


# ---------------------------------------------------------------------------
# Helpers & Mocks
# ---------------------------------------------------------------------------

def make_request(json_data=None, raw_body=None, headers=None):
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    async def receive():
        if raw_body is not None:
            return {"type": "http.request", "body": raw_body}
        if json_data is not None:
            return {"type": "http.request", "body": json.dumps(json_data).encode()}
        return {"type": "http.request", "body": b""}
    return Request(scope, receive)


# ---------------------------------------------------------------------------
# Ingest Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_rejects_invalid_json():
    req = make_request(raw_body=b"invalid-json{")
    with pytest.raises(HTTPException) as exc_info:
        await ingest_transaction(req)
    assert exc_info.value.status_code == 400
    assert "Body không phải JSON" in exc_info.value.detail


@pytest.mark.asyncio
async def test_ingest_rejects_non_dict_json():
    req = make_request(json_data=["array", "not", "object"])
    with pytest.raises(HTTPException) as exc_info:
        await ingest_transaction(req)
    assert exc_info.value.status_code == 400
    assert "Body phải là JSON object" in exc_info.value.detail


@pytest.mark.asyncio
async def test_ingest_drops_is_fraud_label_leak():
    req = make_request(json_data={"trans_num": "tx123", "amt": 50.0, "is_fraud": 1})

    mock_redis = AsyncMock()
    mock_redis.zcard = AsyncMock(return_value=0)
    mock_redis.set = AsyncMock(return_value=True)

    mock_queue = MagicMock()
    mock_queue.ready = "ml_jobs:ready"
    mock_queue.publish = AsyncMock()

    prev_redis, prev_queue, prev_settings = state.redis, state.ml_queue, state.settings
    mock_settings = MagicMock()
    mock_settings.max_ml_queue_depth = 5000

    state.redis = mock_redis
    state.ml_queue = mock_queue
    state.settings = mock_settings

    try:
        res = await ingest_transaction(req)
        assert res["status"] == "QUEUED"
        assert res["txn_id"] == "tx123"

        # Check that is_fraud was dropped from the payload before publish
        published_env = mock_queue.publish.call_args[0][0]
        raw_payload = published_env.payload["raw"]
        assert "is_fraud" not in raw_payload
        assert raw_payload["amt"] == 50.0
    finally:
        state.redis, state.ml_queue, state.settings = prev_redis, prev_queue, prev_settings


@pytest.mark.asyncio
async def test_ingest_backpressure_when_queue_full():
    req = make_request(json_data={"trans_num": "tx-full", "amt": 100.0})

    mock_redis = AsyncMock()
    mock_redis.zcard = AsyncMock(return_value=5000)

    mock_queue = MagicMock()
    mock_queue.ready = "ml_jobs:ready"

    prev_redis, prev_queue, prev_settings = state.redis, state.ml_queue, state.settings
    mock_settings = MagicMock()
    mock_settings.max_ml_queue_depth = 5000

    state.redis = mock_redis
    state.ml_queue = mock_queue
    state.settings = mock_settings

    try:
        res = await ingest_transaction(req)
        assert res.status_code == 429
        assert res.headers["Retry-After"] == "1"
    finally:
        state.redis, state.ml_queue, state.settings = prev_redis, prev_queue, prev_settings


@pytest.mark.asyncio
async def test_ingest_detects_duplicate_transaction():
    req = make_request(json_data={"trans_num": "tx-dup", "amt": 20.0})

    mock_redis = AsyncMock()
    mock_redis.zcard = AsyncMock(return_value=10)
    mock_redis.set = AsyncMock(return_value=False)  # NX failed -> duplicate!

    mock_queue = MagicMock()
    mock_queue.ready = "ml_jobs:ready"

    prev_redis, prev_queue, prev_settings = state.redis, state.ml_queue, state.settings
    mock_settings = MagicMock()
    mock_settings.max_ml_queue_depth = 5000

    state.redis = mock_redis
    state.ml_queue = mock_queue
    state.settings = mock_settings

    try:
        res = await ingest_transaction(req)
        assert res.status_code == 200
        data = json.loads(res.body)
        assert data["status"] == "DUPLICATE"
        assert data["txn_id"] == "tx-dup"
    finally:
        state.redis, state.ml_queue, state.settings = prev_redis, prev_queue, prev_settings


@pytest.mark.asyncio
async def test_ingest_cleans_idem_on_publish_failure():
    req = make_request(json_data={"trans_num": "tx-fail", "amt": 10.0})

    mock_redis = AsyncMock()
    mock_redis.zcard = AsyncMock(return_value=0)
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.delete = AsyncMock()

    mock_queue = MagicMock()
    mock_queue.ready = "ml_jobs:ready"
    mock_queue.publish = AsyncMock(side_effect=RuntimeError("Redis down"))

    prev_redis, prev_queue, prev_settings = state.redis, state.ml_queue, state.settings
    mock_settings = MagicMock()
    mock_settings.max_ml_queue_depth = 5000

    state.redis = mock_redis
    state.ml_queue = mock_queue
    state.settings = mock_settings

    try:
        with pytest.raises(HTTPException) as exc_info:
            await ingest_transaction(req)
        assert exc_info.value.status_code == 503
        mock_redis.delete.assert_called_once_with("idem:txn:tx-fail")
    finally:
        state.redis, state.ml_queue, state.settings = prev_redis, prev_queue, prev_settings


@pytest.mark.asyncio
async def test_get_result_pending_and_found():
    mock_db = AsyncMock()
    prev_db = state.db
    state.db = mock_db

    try:
        # 1. Not found -> returns 202 PENDING
        mock_db.rfetchrow = AsyncMock(return_value=None)
        res = await get_result("tx-unknown")
        assert res.status_code == 202
        assert json.loads(res.body)["status"] == "PENDING"

        # 2. Found -> returns scoring result
        mock_db.rfetchrow = AsyncMock(return_value={
            "txn_id": "tx-done",
            "status": "SCORED",
            "risk_level": "HIGH",
            "calibrated_prob": 0.85,
            "case_id": 123,
        })
        res2 = await get_result("tx-done")
        assert res2["status"] == "SCORED"
        assert res2["risk_level"] == "HIGH"
        assert res2["calibrated_prob"] == 0.85
    finally:
        state.db = prev_db


# ---------------------------------------------------------------------------
# Admin Tests
# ---------------------------------------------------------------------------

def test_threshold_update_validator():
    # Valid
    t = ThresholdUpdate(low_max=0.3, high_min=0.7)
    assert t.low_max == 0.3
    assert t.high_min == 0.7

    # low_max <= 0
    with pytest.raises(ValidationError):
        ThresholdUpdate(low_max=0.0, high_min=0.7)

    # low_max > high_min
    with pytest.raises(ValidationError):
        ThresholdUpdate(low_max=0.8, high_min=0.5)

    # high_min >= 1
    with pytest.raises(ValidationError):
        ThresholdUpdate(low_max=0.3, high_min=1.0)


@pytest.mark.asyncio
async def test_admin_get_threshold():
    mock_db = AsyncMock()
    mock_db.rfetchrow = AsyncMock(return_value={
        "version": 1,
        "low_max": 0.30,
        "high_min": 0.70,
        "created_by": "admin",
    })
    prev_db = state.db
    state.db = mock_db

    try:
        admin_user = User(username="admin", role=Role.ADMIN)
        res = await get_threshold(admin_user)
        assert res["version"] == 1
        assert res["low_max"] == 0.30
        assert res["high_min"] == 0.70
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_admin_put_threshold_updates_active_version():
    class FakeConn:
        async def execute(self, sql, *args):
            pass

        async def fetchrow(self, sql, *args):
            return {"version": 2}

    class FakeDB:
        @asynccontextmanager
        async def write_tx(self):
            yield FakeConn()

    prev_db = state.db
    state.db = FakeDB()

    try:
        admin_user = User(username="admin", role=Role.ADMIN)
        body = ThresholdUpdate(low_max=0.25, high_min=0.75)
        res = await put_threshold(body, admin_user)

        assert res["version"] == 2
        assert res["low_max"] == 0.25
        assert res["high_min"] == 0.75
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_admin_queue_stats():
    mock_ml = AsyncMock()
    mock_ml.depth = AsyncMock(return_value=12)

    mock_llm = AsyncMock()
    mock_llm.depth = AsyncMock(return_value=3)

    mock_events = AsyncMock()
    mock_events.depth = AsyncMock(return_value=0)

    prev_ml, prev_llm, prev_events = state.ml_queue, state.llm_queue, state.events_queue
    state.ml_queue = mock_ml
    state.llm_queue = mock_llm
    state.events_queue = mock_events

    try:
        admin_user = User(username="admin", role=Role.ADMIN)
        res = await queue_stats(admin_user)
        assert res == {"ml_jobs": 12, "llm_jobs": 3, "case_events": 0}
    finally:
        state.ml_queue, state.llm_queue, state.events_queue = prev_ml, prev_llm, prev_events


@pytest.mark.asyncio
async def test_admin_queue_stats_503_when_unready():
    prev_ml = state.ml_queue
    state.ml_queue = None
    try:
        admin_user = User(username="admin", role=Role.ADMIN)
        with pytest.raises(HTTPException) as exc_info:
            await queue_stats(admin_user)
        assert exc_info.value.status_code == 503
    finally:
        state.ml_queue = prev_ml

