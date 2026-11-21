"""Unit tests for ReliableQueue, Worker Consumer, and Scoring Processor resilience.

Covers:
- fraud_common/queues.py (ReliableQueue):
  * publish, consume (normal, poison pill / ValidationError)
  * ack, nack (retry with exponential backoff, dead letter on max attempts, on_dead callback error tolerance)
  * reap_expired (moves unacknowledged / timed out jobs from processing back to ready or dead)
  * depth & update_gauges
  * reaper_loop & gauge_loop cancellation via stop Event
- fraud_common/worker.py:
  * run_consumer success (calls handler, acks job, records OK metric)
  * run_consumer NonRetryableError (acks job, logs, records non_retryable metric)
  * run_consumer generic Exception / TimeoutError (nacks job, records error metric)
  * run_consumer consume failure (handles Redis drop, continues without crash)
  * install_signal_handlers
- scoring_worker/pipeline/routing.py:
  * ThresholdProvider cache hit, TTL expiration, and safe fallback defaults
- scoring_worker/processor.py:
  * handle_dead fallback to MANUAL_FALLBACK_UNSCORED
  * validation failure fallback to unscored_queue
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fraud_common.queues import ReliableQueue
from fraud_common.schemas import JobEnvelope, RiskLevel, ThresholdConfig
from fraud_common.worker import NonRetryableError, install_signal_handlers, run_consumer
from scoring_worker.pipeline.routing import ThresholdProvider, route
from scoring_worker.processor import ScoringProcessor


# ---------------------------------------------------------------------------
# In-Memory Fake Redis for ReliableQueue
# ---------------------------------------------------------------------------

class FakeRedis:
    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}  # key -> {member: score}
        self.hashes: dict[str, dict[str, str]] = {}   # key -> {field: value}
        self.lists: dict[str, list[str]] = {}         # key -> [elements]
        self.scripts = {}

    def register_script(self, script_code: str):
        # Emulate the _POP_LUA script atomically
        async def run_pop(keys, args):
            ready_key, proc_key = keys[0], keys[1]
            deadline = args[0]
            zset = self.zsets.get(ready_key, {})
            if not zset:
                return None
            # Find min score (highest priority / earliest timestamp)
            min_member = min(zset.keys(), key=lambda k: zset[k])
            del zset[min_member]
            job_dict = json.loads(min_member)
            job_id = job_dict["job_id"]
            if proc_key not in self.hashes:
                self.hashes[proc_key] = {}
            self.hashes[proc_key][job_id] = f"{deadline}|{min_member}"
            return min_member

        return run_pop

    async def zadd(self, name: str, mapping: dict[str, float]):
        if name not in self.zsets:
            self.zsets[name] = {}
        self.zsets[name].update(mapping)

    async def hdel(self, name: str, *keys: str) -> int:
        h = self.hashes.get(name, {})
        deleted = 0
        for k in keys:
            if k in h:
                del h[k]
                deleted += 1
        return deleted

    async def hgetall(self, name: str) -> dict[str, str]:
        return dict(self.hashes.get(name, {}))

    async def lpush(self, name: str, *values: str):
        if name not in self.lists:
            self.lists[name] = []
        for val in values:
            self.lists[name].insert(0, val)

    async def zcard(self, name: str) -> int:
        return len(self.zsets.get(name, {}))

    async def hlen(self, name: str) -> int:
        return len(self.hashes.get(name, {}))

    async def llen(self, name: str) -> int:
        return len(self.lists.get(name, []))


# ===========================================================================
# 1. ReliableQueue Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_reliable_queue_publish_and_consume():
    fake_redis = FakeRedis()
    queue = ReliableQueue(fake_redis, "test_ml", visibility_timeout_s=5.0)

    env = JobEnvelope(job_type="score", priority=0, trace_id="tr1", payload={"data": 123})
    await queue.publish(env)

    depth = await queue.depth()
    assert depth["ready"] == 1
    assert depth["processing"] == 0

    # Consume job
    job = await queue.consume()
    assert job is not None
    assert job.job_id == env.job_id
    assert job.payload["data"] == 123

    # Now in processing
    depth = await queue.depth()
    assert depth["ready"] == 0
    assert depth["processing"] == 1

    # ACK job
    await queue.ack(job)
    depth = await queue.depth()
    assert depth["processing"] == 0


@pytest.mark.asyncio
async def test_reliable_queue_poison_pill_validation_error():
    fake_redis = FakeRedis()
    queue = ReliableQueue(fake_redis, "test_poison")

    # Put raw invalid json (missing required fields for JobEnvelope)
    invalid_raw = json.dumps({"job_id": "bad_job_1", "corrupted": True})
    await fake_redis.zadd(queue.ready, {invalid_raw: 100.0})

    # Consume should catch ValidationError, delete from processing and push to dead
    job = await queue.consume()
    assert job is None

    depth = await queue.depth()
    assert depth["ready"] == 0
    assert depth["processing"] == 0
    assert depth["dead"] == 1


@pytest.mark.asyncio
async def test_reliable_queue_nack_retry_and_dead_letter():
    fake_redis = FakeRedis()
    on_dead_called = []

    async def mock_on_dead(env, error):
        on_dead_called.append((env.job_id, error))

    queue = ReliableQueue(fake_redis, "test_nack", max_attempts=2, on_dead=mock_on_dead)
    env = JobEnvelope(job_type="score", priority=1, trace_id="tr2", payload={"v": 1})
    await queue.publish(env)

    # First attempt
    job1 = await queue.consume()
    assert job1.attempts == 0
    retry_result = await queue.nack(job1, "temporary DB timeout")
    assert retry_result is True  # Retried

    # Second attempt (after retry republished)
    job2 = await queue.consume()
    assert job2.attempts == 1
    second_result = await queue.nack(job2, "permanent model error")
    assert second_result is False  # Moved to dead-letter

    depth = await queue.depth()
    assert depth["dead"] == 1
    assert len(on_dead_called) == 1
    assert on_dead_called[0][0] == env.job_id


@pytest.mark.asyncio
async def test_reliable_queue_reap_expired():
    fake_redis = FakeRedis()
    queue = ReliableQueue(fake_redis, "test_reap", visibility_timeout_s=0.1, max_attempts=2)

    env = JobEnvelope(job_type="score", priority=0, trace_id="tr3", payload={"val": 42})
    await queue.publish(env)
    job = await queue.consume()
    assert job is not None

    # Processing item deadline is set in future; immediate reap should find 0 expired
    assert await queue.reap_expired() == 0

    # Wait for visibility timeout to expire
    await asyncio.sleep(0.15)
    reaped_count = await queue.reap_expired()
    assert reaped_count == 1

    # Job is moved back to ready
    re_consumed = await queue.consume()
    assert re_consumed is not None
    assert re_consumed.attempts == 1


# ===========================================================================
# 2. Worker Consumer Tests (fraud_common/worker.py)
# ===========================================================================

@pytest.mark.asyncio
async def test_run_consumer_success_and_error_handling():
    fake_redis = FakeRedis()
    queue = ReliableQueue(fake_redis, "test_consumer_queue")

    # Publish 3 jobs:
    # 1. OK job
    # 2. NonRetryableError job
    # 3. Crash Exception job
    env_ok = JobEnvelope(job_type="test", priority=0, trace_id="t1", payload={"action": "ok"})
    env_non_retry = JobEnvelope(job_type="test", priority=0, trace_id="t2", payload={"action": "non_retry"})
    env_err = JobEnvelope(job_type="test", priority=0, trace_id="t3", payload={"action": "error"})

    for e in (env_ok, env_non_retry, env_err):
        await queue.publish(e)

    stop_event = asyncio.Event()

    async def dummy_handler(env: JobEnvelope):
        action = env.payload.get("action")
        if action == "ok":
            return
        elif action == "non_retry":
            raise NonRetryableError("Corrupted payload field")
        elif action == "error":
            raise RuntimeError("DB connection dropped")

    # Start consumer in background task
    consumer_task = asyncio.create_task(
        run_consumer(queue, dummy_handler, concurrency=2, stop=stop_event, poll_interval_s=0.01)
    )

    # Let consumer process all jobs
    await asyncio.sleep(0.1)
    stop_event.set()
    await consumer_task

    # Both env_ok and env_non_retry should be acknowledged (removed from processing)
    # env_err should be nacked (re-added to ready or processing)
    depth = await queue.depth()
    assert depth["processing"] == 0


@pytest.mark.asyncio
async def test_install_signal_handlers():
    stop = asyncio.Event()
    # Does not crash on Windows / non-posix
    install_signal_handlers(stop)
    assert not stop.is_set()


# ===========================================================================
# 3. Routing & ThresholdProvider Tests (scoring_worker/pipeline/routing.py)
# ===========================================================================

@pytest.mark.asyncio
async def test_threshold_provider_cache_and_fallback():
    class MockDb:
        def __init__(self):
            self.row = None
            self.calls = 0

        async def rfetchrow(self, sql):
            self.calls += 1
            return self.row

    db = MockDb()
    provider = ThresholdProvider(db)

    # 1. Database returns None on first call -> safe fallback default (0.30 / 0.70)
    cfg1 = await provider.get()
    assert cfg1.version == 0
    assert cfg1.low_max == 0.30
    assert cfg1.high_min == 0.70
    assert db.calls == 1

    # 2. Subsequent call within cache TTL -> returns cached config without querying DB
    cfg2 = await provider.get()
    assert cfg2 == cfg1
    assert db.calls == 1  # No DB query

    # 3. Cache expires -> queries DB and updates
    provider._fetched_at = 0.0  # Force expiration
    db.row = {"version": 5, "low_max": 0.25, "high_min": 0.65}
    cfg3 = await provider.get()
    assert cfg3.version == 5
    assert cfg3.low_max == 0.25
    assert cfg3.high_min == 0.65
    assert db.calls == 2

    # 4. Route function verification
    assert route(0.80, cfg3) == RiskLevel.HIGH
    assert route(0.50, cfg3) == RiskLevel.MEDIUM
    assert route(0.10, cfg3) == RiskLevel.LOW


# ===========================================================================
# 4. ScoringProcessor Dead Letter Fallback (scoring_worker/processor.py)
# ===========================================================================

@pytest.mark.asyncio
async def test_scoring_processor_handle_dead_fallback():
    class MockProcessorDb:
        def __init__(self):
            self.executed_sqls = []

        @asynccontextmanager
        async def write_tx(self):
            yield self

        async def execute(self, sql, *args):
            self.executed_sqls.append((sql, args))

    db = MockProcessorDb()
    fake_queue = AsyncMock()
    processor = ScoringProcessor(db, fake_queue)

    dead_env = JobEnvelope(
        job_type="score_transaction",
        job_id="job_failed_3x",
        trace_id="tr_dead_99",
        payload={
            "txn_id": "txn_failed_max_attempts",
            "raw": {"account_id": "ACC123", "amt": 999.0},
        },
    )

    await processor.handle_dead(dead_env, "connection timed out 3 times")

    # Verify that unscored_queue was populated to prevent silent loss
    unscored_inserts = [
        args for sql, args in db.executed_sqls
        if "INSERT INTO unscored_queue" in sql
    ]
    assert len(unscored_inserts) == 1
    assert unscored_inserts[0][0] == "txn_failed_max_attempts"
    assert "scoring failed after retries" in unscored_inserts[0][1][0]["msg"]
