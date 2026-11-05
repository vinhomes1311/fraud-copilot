import asyncio

import pytest

from analyst_api.auth import User
from analyst_api.routes import demo
from analyst_api.state import state
from fraud_common.schemas import Role


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.values = {}

    async def hsetnx(self, key, field, value):
        entry = self.hashes.setdefault(key, {})
        if field in entry:
            return 0
        entry[field] = value
        return 1

    async def hgetall(self, key):
        return self.hashes.get(key, {}).copy()

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def hincrby(self, key, field, amount):
        entry = self.hashes.setdefault(key, {})
        entry[field] = str(int(entry.get(field, "0")) + amount)
        return int(entry[field])

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, _script, _key_count, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


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
            demo.SimulatorControl(action="start", tps=5), _=user
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
        "error": "",
        "version": 0,
    }
    assert revisited == initial
    assert started["running"] is True
    assert started["ever_started"] is True
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
    assert fake_redis.hashes[demo.SIMULATOR_STATE_KEY]["version"] == "1"
    assert demo.SIMULATOR_LOCK_KEY not in fake_redis.values
