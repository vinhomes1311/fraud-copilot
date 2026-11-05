import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from analyst_api.auth import User
from analyst_api.routes import demo
from analyst_api.state import state
from fraud_common.schemas import Role


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.values = {}
        self.deleted = []
        self.lock_conflicts = 0

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hsetnx(self, key, field, value):
        entry = self.hashes.setdefault(key, {})
        if field in entry:
            return 0
        entry[field] = value
        return 1

    async def hgetall(self, key):
        return self.hashes.get(key, {}).copy()

    async def hvals(self, _key):
        return []

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hincrby(self, key, field, amount):
        entry = self.hashes.setdefault(key, {})
        entry[field] = str(int(entry.get(field, "0")) + amount)
        return int(entry[field])

    async def set(self, key, value, nx=False, ex=None):
        if key == demo.SIMULATOR_LOCK_KEY and self.lock_conflicts:
            self.lock_conflicts -= 1
            return False
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, _key_count, *args):
        if "is_demo_job" in script:
            return [0, 0]
        key, token = args[-2:]
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0

    async def zrange(self, _key, _start, _stop):
        return []

    async def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


class FakeResetConnection:
    def __init__(self):
        self.executed = []

    async def fetch(self, _query):
        return [{"txn_id": "demo-transaction"}]

    async def fetchval(self, _query):
        return 2

    async def execute(self, query):
        self.executed.append(query)
        return "DELETE 3" if query.startswith("DELETE FROM transactions") else "DELETE 2"


class FakeResetDatabase:
    def __init__(self):
        self.connection = FakeResetConnection()

    @asynccontextmanager
    async def write_tx(self):
        yield self.connection

    async def rfetchval(self, _query, *_args):
        return 0


class FakeCaseRateDatabase:
    def __init__(self, recent_case_count):
        self.recent_case_count = recent_case_count
        self.queries = []

    async def rfetchval(self, query, *args):
        self.queries.append((query, args))
        return self.recent_case_count


class FakeQueue:
    def __init__(self, name):
        self.ready = f"{name}:ready"
        self.processing = f"{name}:processing"

@pytest.mark.asyncio
async def test_simulator_controls_share_state_and_preserve_progress():
    fake_redis = FakeRedis()
    previous_redis = state.redis
    state.redis = fake_redis
    user = User("analyst1", Role.ANALYST)
    try:
        initial = await demo.get_simulator_state(initial_offset=42, initial_tps=5, _=user)
        revisited = await demo.get_simulator_state(initial_offset=100, initial_tps=90, _=user)
        started = await demo.control_simulator(
            demo.SimulatorControl(action="start", tps=7), _=user
        )
        updated = await demo.control_simulator(
            demo.SimulatorControl(action="set_speed", tps=20), _=user
        )
        stopped = await demo.control_simulator(
            demo.SimulatorControl(action="stop"), _=user
        )
    finally:
        state.redis = previous_redis

    assert initial == {
        "running": False,
        "ever_started": False,
        "offset": 42,
        "tps": 5,
        "open_cases_per_second": 0.0,
        "error": "",
        "version": 0,
    }
    assert revisited == initial
    assert started["running"] is True
    assert started["ever_started"] is True
    assert started["tps"] == 7
    assert started["version"] == 1
    assert updated["tps"] == 20
    assert updated["version"] == 2
    assert stopped["running"] is False
    assert stopped["ever_started"] is True
    assert stopped["offset"] == 42
    assert stopped["version"] == 3


@pytest.mark.asyncio
async def test_simulator_runner_publishes_one_shared_batch(monkeypatch):
    fake_redis = FakeRedis()
    fake_redis.hashes[demo.SIMULATOR_STATE_KEY] = {
        "running": "1",
        "offset": "12",
        "tps": "7",
        "open_cases_per_second": "0",
        "error": "",
    }
    previous_redis = state.redis
    state.redis = fake_redis
    stop = asyncio.Event()
    calls = []

    async def enqueue(batch_size, offset):
        calls.append((batch_size, offset))
        stop.set()
        return {"queued": batch_size, "skipped": 0, "next_offset": offset + batch_size, "has_more": True}

    monkeypatch.setattr(demo, "_enqueue_test_batch", enqueue)
    try:
        await demo.run_simulator(stop)
    finally:
        state.redis = previous_redis

    assert calls == [(7, 12)]
    assert fake_redis.hashes[demo.SIMULATOR_STATE_KEY]["offset"] == "19"
    assert fake_redis.hashes[demo.SIMULATOR_STATE_KEY]["offset"] == "19"
    assert fake_redis.hashes[demo.SIMULATOR_STATE_KEY]["version"] == "1"
    assert demo.SIMULATOR_LOCK_KEY not in fake_redis.values


@pytest.mark.asyncio
async def test_reset_demo_data_clears_only_simulator_records_and_restarts():
    fake_redis = FakeRedis()
    fake_redis.hashes[demo.SIMULATOR_STATE_KEY] = {
        "running": "0",
        "offset": "150",
        "tps": "25",
        "open_cases_per_second": "10",
        "ever_started": "1",
        "error": "",
        "version": "4",
    }
    fake_redis.lock_conflicts = 2
    database = FakeResetDatabase()
    previous = (state.redis, state.db, state.ml_queue, state.events_queue, state.llm_queue)
    state.redis = fake_redis
    state.db = database
    state.ml_queue = FakeQueue("ml_jobs")
    state.events_queue = FakeQueue("case_events")
    state.llm_queue = FakeQueue("llm_jobs")
    try:
        result = await demo.reset_demo_data(_=User("analyst1", Role.ANALYST))
    finally:
        state.redis, state.db, state.ml_queue, state.events_queue, state.llm_queue = previous

    assert result["deleted_cases"] == 2
    assert result["deleted_transactions"] == 3
    assert result["offset"] == 0
    assert result["tps"] == 25
    assert result["running"] is True
    assert result["open_cases_per_second"] == 0
    assert fake_redis.deleted == ["idem:txn:demo-transaction"]
    assert len(database.connection.executed) == 6
    assert all("audit_log" not in query for query in database.connection.executed)


def test_queued_demo_transaction_ids_rejects_non_demo_jobs():
    demo_job = {
        "trace_id": "demo-transaction",
        "payload": {"txn_id": "demo-transaction"},
    }
    unrelated_job = {
        "trace_id": "request-id",
        "payload": {"txn_id": "unrelated-transaction"},
    }
    assert demo._queued_demo_transaction_ids(
        [json.dumps(demo_job), json.dumps(unrelated_job), "invalid-json"]
    ) == {"demo-transaction"}


@pytest.mark.asyncio
async def test_simulator_runner_does_not_take_lock_while_stopped(monkeypatch):
    fake_redis = FakeRedis()
    fake_redis.hashes[demo.SIMULATOR_STATE_KEY] = {
        "running": "0",
        "offset": "12",
        "tps": "7",
        "open_cases_per_second": "0",
        "error": "",
    }
    previous_redis = state.redis
    state.redis = fake_redis
    stop = asyncio.Event()

    async def stop_after_idle_wait(_stop, _seconds):
        stop.set()

    monkeypatch.setattr(demo, "_wait", stop_after_idle_wait)
    try:
        await demo.run_simulator(stop)
    finally:
        state.redis = previous_redis

    assert fake_redis.values == {}


@pytest.mark.asyncio
async def test_simulator_reports_open_cases_created_per_second_and_caches_sample():
    fake_redis = FakeRedis()
    database = FakeCaseRateDatabase(recent_case_count=18)
    previous = (state.redis, state.db)
    state.redis = fake_redis
    state.db = database
    try:
        first = await demo.get_simulator_state(initial_offset=0, initial_tps=10, _=User("analyst1", Role.ANALYST))
        second = await demo.get_simulator_state(initial_offset=0, initial_tps=10, _=User("analyst1", Role.ANALYST))
    finally:
        state.redis, state.db = previous

    assert first["open_cases_per_second"] == 3.6
    assert second["open_cases_per_second"] == 3.6
    assert len(database.queries) == 1
    query, args = database.queries[0]
    assert "FROM cases" in query
    assert "created_at >=" in query
    assert "status IN ('OPEN', 'IN_REVIEW', 'NEEDS_MORE_INFO', 'ESCALATED', 'RETURNED')" in query
    assert args == (demo.OPEN_CASE_RATE_WINDOW_SECONDS,)
