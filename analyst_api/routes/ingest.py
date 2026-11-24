"""Luồng 1 (Hot Path), phía tiếp nhận: Simulator -> Nginx -> API -> Redis ml_jobs."""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from fraud_common.data_version import normalize_data_version
from fraud_common.metrics import LABEL_LEAK_DROPPED
from fraud_common.schemas import JOB_SCORE_TRANSACTION, JobEnvelope, ScoreTransactionPayload

from ..auth import current_user, require_simulator, simulator_or_user
from ..state import state

router = APIRouter(tags=["ingest"])


async def _enqueue_transaction(body: dict, trace_id: str | None = None,
                               data_version: str | None = None):
    txn_id = str(body.get("trans_num") or uuid4().hex)
    trace_id = trace_id or uuid4().hex

    depth = await state.redis.zcard(state.ml_queue.ready)
    if depth >= state.settings.max_ml_queue_depth:
        return JSONResponse({"detail": "ML queue đầy, thử lại sau"}, status_code=429,
                            headers={"Retry-After": "1"})

    if not await state.redis.set(f"idem:txn:{txn_id}", "1", nx=True, ex=3600):
        return JSONResponse({"txn_id": txn_id, "status": "DUPLICATE"}, status_code=200)
    env = JobEnvelope(
        job_type=JOB_SCORE_TRANSACTION, priority=0, trace_id=trace_id,
        payload=ScoreTransactionPayload(txn_id=txn_id, raw=body,
                                        data_version=data_version).model_dump(mode="json"))
    try:
        await state.ml_queue.publish(env)
    except Exception:
        await state.redis.delete(f"idem:txn:{txn_id}")
        raise HTTPException(503, "Không đẩy được job vào hàng đợi")
    return {"txn_id": txn_id, "job_id": env.job_id, "trace_id": trace_id, "status": "QUEUED"}


@router.post("/transactions", status_code=202, dependencies=[Depends(require_simulator)])
async def ingest_transaction(request: Request):
    """Nhận giao dịch thô, KHÔNG validate nghiệp vụ ở đây (Scoring Module làm việc đó - FR-01).
    API chỉ: chặn lộ nhãn, chống trùng, backpressure, gắn trace_id rồi đẩy vào Redis."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body không phải JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body phải là JSON object")

    # Chống lộ nhãn: nhãn thật không được đi vào luồng suy luận
    if "is_fraud" in body:
        body.pop("is_fraud")
        LABEL_LEAK_DROPPED.inc()

    data_version = normalize_data_version(request.headers.get("x-data-version"))
    return await _enqueue_transaction(
        body,
        request.headers.get("x-request-id"),
        data_version=data_version,
    )


@router.get("/fault-lab/config")
async def fault_lab_config():
    return {"enabled": state.settings.enable_fault_lab}

@router.post("/fault-lab/transactions", status_code=202, dependencies=[Depends(current_user)])
async def ingest_fault_lab_transaction(request: Request):
    if not state.settings.enable_fault_lab:
        raise HTTPException(404, "Fault Lab chưa được bật")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Body không phải JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Body phải là JSON object")
    if "is_fraud" in body:
        body.pop("is_fraud")
        LABEL_LEAK_DROPPED.inc()

    txn_id = f"fault-lab-{uuid4().hex}"
    body["trans_num"] = txn_id
    return await _enqueue_transaction(body, request.headers.get("x-request-id"))


@router.get("/transactions/{txn_id}/result", dependencies=[Depends(simulator_or_user)])
async def get_result(txn_id: str):
    """Hỏi kết quả chấm điểm (bất đồng bộ). 202 nếu chưa xong."""
    row = await state.db.rfetchrow(
        """SELECT s.txn_id, s.status, s.risk_level, s.calibrated_prob, s.model_version,
                  s.threshold_config_version, s.latency_ms, s.validation_errors, s.trace_id, c.case_id
           FROM scoring_results s LEFT JOIN cases c ON c.txn_id = s.txn_id WHERE s.txn_id = $1""", txn_id)
    if row is None:
        return JSONResponse({"txn_id": txn_id, "status": "PENDING"}, status_code=202)
    return dict(row)
