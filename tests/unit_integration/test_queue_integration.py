"""Cần Redis đang chạy ở localhost:6379 (docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis).
Tự bỏ qua nếu không kết nối được."""
import asyncio
import os
import uuid

import pytest
from redis.asyncio import Redis

from fraud_common.queues import ReliableQueue
from fraud_common.schemas import JobEnvelope


async def _redis_or_skip():
    r = Redis.from_url(os.getenv("FRAUD_TEST_REDIS_URL", "redis://localhost:6379/0"),
                       decode_responses=True, socket_connect_timeout=3)
    try:
        await r.ping()
    except Exception:
        await r.aclose()
        pytest.skip("Redis không chạy")
    return r


def _env(priority: int, n: int = 0) -> JobEnvelope:
    return JobEnvelope(job_type="t", priority=priority, trace_id="tr", payload={"n": n})


def test_priority_order_ack_and_dead_letter():
    """#sym:test_priority_order_ack_and_dead_letter
    Kiem tra: queue priority, ack, retry, dead-letter.
    @param Khong co tham so.
    Dung khi: priority cao ra truoc, ack xoa job, retry roi dead-letter.
    Sai khi: queue sai thu tu hoac retry/dead sai.
    """
    async def run():
        r = await _redis_or_skip()
        q = ReliableQueue(r, f"test_{uuid.uuid4().hex}", visibility_timeout_s=0.2, max_attempts=2)
        await q.publish(_env(1, 1))     # Medium
        await q.publish(_env(0, 2))     # High, đến sau nhưng phải ra trước
        first = await q.consume()
        assert first.payload["n"] == 2
        await q.ack(first)

        second = await q.consume()
        assert second.payload["n"] == 1
        assert await q.nack(second, "boom") is True       # lần 1: thử lại
        again = await q.consume()
        assert again.attempts == 1
        assert await q.nack(again, "boom") is False       # lần 2: dead-letter
        assert (await q.depth())["dead"] == 1

        # worker chết: job treo quá visibility timeout được reaper trả về hàng đợi
        await q.publish(_env(0, 3))
        stuck = await q.consume()
        assert stuck is not None
        await asyncio.sleep(0.3)
        assert await q.reap_expired() == 1
        assert (await q.consume()).payload["n"] == 3
        await r.delete(q.ready, q.processing, q.dead)
        await r.aclose()

    asyncio.run(run())
