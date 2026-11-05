"""Demo controls for feeding the test dataset into the normal scoring queue."""
from __future__ import annotations

import asyncio
import csv
import logging
import secrets
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from fraud_common.schemas import JOB_SCORE_TRANSACTION, JobEnvelope, ScoreTransactionPayload, Role

from ..auth import User, require_roles
from ..state import state

router = APIRouter(prefix="/demo", tags=["demo"])
demo_user = require_roles(Role.ANALYST, Role.RISK_LEAD)
logger = logging.getLogger(__name__)
DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "fraudTest.csv"
SIMULATOR_STATE_KEY = "demo:simulator:state"
SIMULATOR_LOCK_KEY = "demo:simulator:producer-lock"
SIMULATOR_MAX_TPS = 1000
NUMERIC_FIELDS = {
    "amt": float,
    "lat": float,
    "long": float,
    "merch_lat": float,
    "merch_long": float,
    "city_pop": int,
    "unix_time": int,
}


class SimulatorControl(BaseModel):
    action: Literal["start", "stop", "set_speed"]
    tps: int | None = Field(default=None, ge=1, le=SIMULATOR_MAX_TPS)


@router.post("/test-batch")
async def enqueue_test_batch(
    batch_size: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    _: User = Depends(demo_user),
) -> dict[str, int | bool]:
    return await _enqueue_test_batch(batch_size, offset)


async def _enqueue_test_batch(batch_size: int, offset: int) -> dict[str, int | bool]:
    if not DATASET_PATH.exists():
        raise HTTPException(503, "Không tìm thấy data/fraudTest.csv")
    if state.ml_queue is None or state.redis is None:
        raise HTTPException(503, "Hệ thống chưa sẵn sàng")

    if offset > 0:
        existing = await state.db.rfetchrow("SELECT count(*) AS total FROM transactions")
        if existing is not None and existing["total"] == 0:
            offset = 0

    rows = await asyncio.to_thread(_read_batch, offset, batch_size)
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


@router.get("/simulator")
async def get_simulator_state(
    initial_offset: int = Query(0, ge=0),
    initial_tps: int = Query(10, ge=1, le=SIMULATOR_MAX_TPS),
    _: User = Depends(demo_user),
) -> dict[str, int | bool | str]:
    if state.redis is None:
        raise HTTPException(503, "Hệ thống chưa sẵn sàng")

    await _initialize_simulator_state(initial_offset, initial_tps)
    return await _read_simulator_state()


@router.post("/simulator")
async def control_simulator(
    command: SimulatorControl,
    _: User = Depends(demo_user),
) -> dict[str, int | bool | str]:
    if state.redis is None:
        raise HTTPException(503, "Hệ thống chưa sẵn sàng")
    if command.action == "set_speed" and command.tps is None:
        raise HTTPException(422, "Cần chọn tốc độ từ 1 đến 1000 giao dịch/giây")

    await _initialize_simulator_state(0, command.tps or 10)
    if command.action == "start":
        await state.redis.hset(
            SIMULATOR_STATE_KEY,
            mapping={"running": "1", "ever_started": "1", "error": ""},
        )
    elif command.action == "stop":
        await state.redis.hset(
            SIMULATOR_STATE_KEY, mapping={"running": "0", "ever_started": "1"}
        )
    else:
        await state.redis.hset(SIMULATOR_STATE_KEY, mapping={"tps": str(command.tps)})
    await state.redis.hincrby(SIMULATOR_STATE_KEY, "version", 1)
    return await _read_simulator_state()


async def _initialize_simulator_state(initial_offset: int, initial_tps: int) -> None:
    is_new_state = await state.redis.hsetnx(SIMULATOR_STATE_KEY, "running", "0")
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "offset", str(initial_offset))
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "tps", str(initial_tps))
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "ever_started", "0" if is_new_state else "1")
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "error", "")
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "version", "0")


async def _read_simulator_state() -> dict[str, int | bool | str]:
    stored = await state.redis.hgetall(SIMULATOR_STATE_KEY)
    return {
        "running": stored.get("running") == "1",
        "ever_started": stored.get("ever_started") == "1",
        "offset": int(stored.get("offset", "0")),
        "tps": int(stored.get("tps", "10")),
        "error": stored.get("error", ""),
        "version": int(stored.get("version", "0")),
    }


async def run_simulator(stop: asyncio.Event) -> None:
    while not stop.is_set():
        if state.redis is None:
            await _wait(stop, 0.5)
            continue

        lock_token = secrets.token_urlsafe(16)
        acquired = await state.redis.set(SIMULATOR_LOCK_KEY, lock_token, nx=True, ex=30)
        if not acquired:
            await _wait(stop, 0.25)
            continue

        heartbeat = asyncio.create_task(_renew_simulator_lock(lock_token))
        cycle_started = asyncio.get_running_loop().time()
        try:
            simulator = await _read_simulator_state()
            if not simulator["running"]:
                await _wait(stop, 0.25)
                continue
            result = await _enqueue_test_batch(
                int(simulator["tps"]), int(simulator["offset"])
            )
            updates = {
                "offset": str(result["next_offset"]),
                "error": "",
            }
            if not result["has_more"]:
                updates["running"] = "0"
            await state.redis.hset(SIMULATOR_STATE_KEY, mapping=updates)
            await state.redis.hincrby(SIMULATOR_STATE_KEY, "version", 1)
        except Exception as error:
            message = error.detail if isinstance(error, HTTPException) else str(error)
            logger.exception("Shared demo simulator stopped after a batch error")
            await state.redis.hset(
                SIMULATOR_STATE_KEY,
                mapping={"running": "0", "error": message[:500]},
            )
            await state.redis.hincrby(SIMULATOR_STATE_KEY, "version", 1)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await state.redis.eval(
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end",
                1,
                SIMULATOR_LOCK_KEY,
                lock_token,
            )

        elapsed = asyncio.get_running_loop().time() - cycle_started
        await _wait(stop, max(0, 1 - elapsed))


async def _renew_simulator_lock(lock_token: str) -> None:
    while True:
        await asyncio.sleep(5)
        renewed = await state.redis.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('expire', KEYS[1], 30) else return 0 end",
            1,
            SIMULATOR_LOCK_KEY,
            lock_token,
        )
        if not renewed:
            return


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


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
