"""Demo controls for feeding the test dataset into the normal scoring queue."""
from __future__ import annotations

import csv
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from fraud_common.schemas import JOB_SCORE_TRANSACTION, JobEnvelope, ScoreTransactionPayload, Role

from ..auth import User, require_roles
from ..state import state

router = APIRouter(prefix="/demo", tags=["demo"])
demo_user = require_roles(Role.ANALYST, Role.RISK_LEAD)
DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "fraudTest.csv"
NUMERIC_FIELDS = {
    "amt": float,
    "lat": float,
    "long": float,
    "merch_lat": float,
    "merch_long": float,
    "city_pop": int,
    "unix_time": int,
}


@router.post("/test-batch")
async def enqueue_test_batch(
    batch_size: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: User = Depends(demo_user),
) -> dict[str, int | bool]:
    if not DATASET_PATH.exists():
        raise HTTPException(503, "Không tìm thấy data/fraudTest.csv")
    if state.ml_queue is None or state.redis is None:
        raise HTTPException(503, "Hệ thống chưa sẵn sàng")

    if offset > 0:
        existing = await state.db.rfetchrow("SELECT count(*) AS total FROM transactions")
        if existing is not None and existing["total"] == 0:
            offset = 0

    rows = _read_batch(offset, batch_size)
    queued = 0
    skipped = 0
    for row in rows:
        txn_id = str(row.get("trans_num") or "")
        if not txn_id:
            skipped += 1
            continue
        row.pop("is_fraud", None)
        if not await state.redis.set(f"idem:txn:{txn_id}", "1", nx=True, ex=3600):
            skipped += 1
            continue
        envelope = JobEnvelope(
            job_type=JOB_SCORE_TRANSACTION,
            priority=1,
            trace_id=txn_id,
            payload=ScoreTransactionPayload(txn_id=txn_id, raw=row).model_dump(mode="json"),
        )
        try:
            await state.ml_queue.publish(envelope)
        except Exception:
            await state.redis.delete(f"idem:txn:{txn_id}")
            raise HTTPException(503, "Không đẩy được batch vào hàng đợi")
        queued += 1

    next_offset = offset + len(rows)
    return {
        "queued": queued,
        "skipped": skipped,
        "next_offset": next_offset,
        "has_more": len(rows) == batch_size,
    }


def _read_batch(offset: int, batch_size: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with DATASET_PATH.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        for index, raw in enumerate(reader):
            if index < offset:
                continue
            if len(rows) >= batch_size:
                break
            row: dict[str, object] = {key: value for key, value in raw.items() if key and value != ""}
            for key, cast in NUMERIC_FIELDS.items():
                if key in row:
                    row[key] = cast(row[key])  # type: ignore[arg-type]
            rows.append(row)
    return rows
