"""Demo controls for feeding the test dataset into the normal scoring queue."""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import secrets
import time
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
OPEN_CASE_RATE_WINDOW_SECONDS = 5
OPEN_CASE_RATE_CACHE_SECONDS = 1
NUMERIC_FIELDS = {
    "amt": float,
    "lat": float,
    "long": float,
    "merch_lat": float,
    "merch_long": float,
    "city_pop": int,
    "unix_time": int,
}
_RESET_DEMO_QUEUES_LUA = """
local function is_demo_job(raw)
  local ok, job = pcall(cjson.decode, raw)
  if not ok or type(job) ~= 'table' or type(job['payload']) ~= 'table' then
    return false
  end
  return job['trace_id'] ~= nil
     and job['payload']['txn_id'] ~= nil
     and job['trace_id'] == job['payload']['txn_id']
end

local active = 0
local processing = redis.call('HVALS', KEYS[2])
for _, value in ipairs(processing) do
  local separator = string.find(value, '|', 1, true)
  if separator and is_demo_job(string.sub(value, separator + 1)) then
    active = active + 1
  end
end

local removed = 0
local jobs = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, raw in ipairs(jobs) do
  if is_demo_job(raw) then
    removed = removed + redis.call('ZREM', KEYS[1], raw)
  end
end
return {removed, active}
"""


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
) -> dict[str, int | float | bool | str]:
    if state.redis is None:
        raise HTTPException(503, "Hệ thống chưa sẵn sàng")

    await _initialize_simulator_state(initial_offset, initial_tps)
    return await _read_simulator_state()


@router.post("/reset")
async def reset_demo_data(_: User = Depends(demo_user)) -> dict[str, int | float | bool | str]:
    if state.redis is None or state.db is None:
        raise HTTPException(503, "Hệ thống chưa sẵn sàng")
    queues = (state.ml_queue, state.events_queue, state.llm_queue)
    if any(queue is None for queue in queues):
        raise HTTPException(503, "Hàng đợi chưa sẵn sàng")

    await _initialize_simulator_state(0, 10)
    await state.redis.hset(SIMULATOR_STATE_KEY, mapping={"running": "0"})
    await state.redis.hincrby(SIMULATOR_STATE_KEY, "version", 1)

    lock_token = secrets.token_urlsafe(16)
    await _acquire_simulator_lock(lock_token)

    heartbeat = asyncio.create_task(_renew_simulator_lock(lock_token))
    try:
        async with state.db.write_tx() as conn:
            demo_transactions = await conn.fetch(
                "SELECT txn_id FROM transactions WHERE trace_id = txn_id"
            )

        txn_ids = {row["txn_id"] for row in demo_transactions}
        queue_keys = (
            state.ml_queue.ready, state.ml_queue.processing,
            state.events_queue.ready, state.events_queue.processing,
            state.llm_queue.ready, state.llm_queue.processing,
        )
        for ready_key, processing_key in zip(queue_keys[::2], queue_keys[1::2]):
            ready_jobs = await state.redis.zrange(ready_key, 0, -1)
            processing_values = await state.redis.hvals(processing_key)
            txn_ids.update(_queued_demo_transaction_ids(ready_jobs))
            txn_ids.update(
                _queued_demo_transaction_ids(
                    [value.split("|", 1)[1] for value in processing_values if "|" in value]
                )
            )
            while True:
                _removed, active = await state.redis.eval(
                    _RESET_DEMO_QUEUES_LUA,
                    2,
                    ready_key,
                    processing_key,
                )
                if active == 0:
                    break
                await asyncio.sleep(0.1)

        async with state.db.write_tx() as conn:
            demo_transactions = await conn.fetch(
                "SELECT txn_id FROM transactions WHERE trace_id = txn_id"
            )
            deleted_case_count = await conn.fetchval(
                """SELECT count(*)
                   FROM cases c
                   JOIN transactions t ON t.txn_id = c.txn_id
                   WHERE t.trace_id = t.txn_id"""
            )
        txn_ids.update(row["txn_id"] for row in demo_transactions)
        if txn_ids:
            await state.redis.delete(*(f"idem:txn:{txn_id}" for txn_id in txn_ids))

        async with state.db.write_tx() as conn:
            await conn.execute(
                """DELETE FROM case_actions ca
                   USING cases c, transactions t
                   WHERE ca.case_id = c.case_id
                     AND c.txn_id = t.txn_id
                     AND t.trace_id = t.txn_id"""
            )
            await conn.execute(
                """DELETE FROM case_briefs cb
                   USING cases c, transactions t
                   WHERE cb.case_id = c.case_id
                     AND c.txn_id = t.txn_id
                     AND t.trace_id = t.txn_id"""
            )
            await conn.execute(
                """DELETE FROM cases c
                   USING transactions t
                   WHERE c.txn_id = t.txn_id
                     AND t.trace_id = t.txn_id"""
            )
            await conn.execute(
                """DELETE FROM unscored_queue uq
                   USING transactions t
                   WHERE uq.txn_id = t.txn_id
                     AND t.trace_id = t.txn_id"""
            )
            await conn.execute(
                """DELETE FROM scoring_results sr
                   USING transactions t
                   WHERE sr.txn_id = t.txn_id
                     AND t.trace_id = t.txn_id"""
            )
            deleted_transactions = await conn.execute(
                "DELETE FROM transactions WHERE trace_id = txn_id"
            )

        await state.redis.hset(
            SIMULATOR_STATE_KEY,
            mapping={
                "running": "1",
                "ever_started": "1",
                "offset": "0",
                "open_cases_per_second": "0",
                "open_case_rate_sampled_at": "0",
                "error": "",
            },
        )
        await state.redis.hincrby(SIMULATOR_STATE_KEY, "version", 1)
        return {
            "deleted_cases": int(deleted_case_count or 0),
            "deleted_transactions": int(deleted_transactions.rsplit(" ", 1)[-1]),
            **(await _read_simulator_state()),
        }
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


async def _acquire_simulator_lock(lock_token: str, timeout_s: float = 35) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not await state.redis.set(SIMULATOR_LOCK_KEY, lock_token, nx=True, ex=30):
        if asyncio.get_running_loop().time() >= deadline:
            raise HTTPException(
                409, "Không thể dừng simulator an toàn; chưa xóa dữ liệu, vui lòng thử lại"
            )
        await asyncio.sleep(0.1)


@router.post("/simulator")
async def control_simulator(
    command: SimulatorControl,
    _: User = Depends(demo_user),
) -> dict[str, int | float | bool | str]:
    if state.redis is None:
        raise HTTPException(503, "Hệ thống chưa sẵn sàng")
    if command.action == "set_speed" and command.tps is None:
        raise HTTPException(422, "Cần chọn tốc độ từ 1 đến 1000 giao dịch/giây")

    await _initialize_simulator_state(0, command.tps or 10)
    if command.action == "start":
        updates = {"running": "1", "ever_started": "1", "error": ""}
        if command.tps is not None:
            updates["tps"] = str(command.tps)
        await state.redis.hset(SIMULATOR_STATE_KEY, mapping=updates)
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
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "open_cases_per_second", "0")
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "open_case_rate_sampled_at", "0")
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "ever_started", "0" if is_new_state else "1")
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "error", "")
    await state.redis.hsetnx(SIMULATOR_STATE_KEY, "version", "0")


async def _read_simulator_state() -> dict[str, int | float | bool | str]:
    stored = await state.redis.hgetall(SIMULATOR_STATE_KEY)
    open_cases_per_second = float(stored.get("open_cases_per_second", "0"))
    sampled_at = float(stored.get("open_case_rate_sampled_at", "0"))
    now = time.time()
    if state.db is not None and now - sampled_at >= OPEN_CASE_RATE_CACHE_SECONDS:
        recent_open_cases = await state.db.rfetchval(
            """SELECT count(*)
               FROM cases
               WHERE status IN ('OPEN', 'IN_REVIEW', 'NEEDS_MORE_INFO', 'ESCALATED', 'RETURNED')
                 AND risk_level IN ('LOW', 'MEDIUM', 'HIGH')
                 AND created_at >= now() - make_interval(secs => $1)""",
            OPEN_CASE_RATE_WINDOW_SECONDS,
        )
        open_cases_per_second = round(
            int(recent_open_cases or 0) / OPEN_CASE_RATE_WINDOW_SECONDS, 1
        )
        await state.redis.hset(
            SIMULATOR_STATE_KEY,
            mapping={
                "open_cases_per_second": str(open_cases_per_second),
                "open_case_rate_sampled_at": str(now),
            },
        )
    return {
        "running": stored.get("running") == "1",
        "ever_started": stored.get("ever_started") == "1",
        "offset": int(stored.get("offset", "0")),
        "tps": int(stored.get("tps", "10")),
        "open_cases_per_second": open_cases_per_second,
        "error": stored.get("error", ""),
        "version": int(stored.get("version", "0")),
    }


async def run_simulator(stop: asyncio.Event) -> None:
    while not stop.is_set():
        if state.redis is None:
            await _wait(stop, 0.5)
            continue
        if not (await _read_simulator_state())["running"]:
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


def _queued_demo_transaction_ids(jobs: list[str]) -> set[str]:
    transaction_ids = set()
    for raw in jobs:
        try:
            job = json.loads(raw)
        except (TypeError, ValueError):
            continue
        payload = job.get("payload", {})
        txn_id = payload.get("txn_id") if isinstance(payload, dict) else None
        if txn_id and job.get("trace_id") == txn_id:
            transaction_ids.add(txn_id)
    return transaction_ids
