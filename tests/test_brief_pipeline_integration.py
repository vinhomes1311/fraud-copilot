"""Opt-in Redis -> worker -> PostgreSQL test using stub LLM (no model download)."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest
from redis.asyncio import Redis

from analyst_api.events import enqueue_brief
from fraud_common.db import Database, _init_conn
from fraud_common.queues import ReliableQueue
from fraud_common.schemas import RiskLevel
from llm_worker.llm_client import StubLLMClient
from llm_worker.processor import BriefProcessor
from llm_worker.rag import StubRetriever


@pytest.mark.skipif(not os.getenv("FRAUD_TEST_DB_URL") or not os.getenv("FRAUD_TEST_REDIS_URL"),
                    reason="Set explicit disposable PostgreSQL and Redis test URLs")
def test_brief_pipeline():
    async def run():
        schema = "brief_test_" + uuid4().hex
        admin = await asyncpg.connect(os.environ["FRAUD_TEST_DB_URL"])
        redis = Redis.from_url(os.environ["FRAUD_TEST_REDIS_URL"], decode_responses=True)
        db = Database("", "")
        queue = ReliableQueue(redis, schema, max_attempts=1)
        try:
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            await admin.execute(f'SET search_path TO "{schema}"')
            await admin.execute((Path(__file__).resolve().parents[1] / "db/init.sql").read_text(encoding="utf-8"))
            db.write = await asyncpg.create_pool(os.environ["FRAUD_TEST_DB_URL"], min_size=1, max_size=2,
                                                init=_init_conn, server_settings={"search_path": schema})
            db.read = db.write
            await db.wexec("INSERT INTO transactions (txn_id, payload_masked) VALUES ('txn', $1)",
                           {"amt": 100, "merchant": "test", "category": "test"})
            await db.wexec("""INSERT INTO scoring_results
                (txn_id, status, raw_model_score, calibrated_prob, risk_level, features, explanations)
                VALUES ('txn', 'SCORED', 1.2, 0.8, 'HIGH', $1, $2)""", {}, [])
            case = await db.wfetchrow("INSERT INTO cases (txn_id, risk_level) VALUES ('txn','HIGH') RETURNING case_id")
            case_id = case["case_id"]
            processor = BriefProcessor(db)
            processor.retriever = StubRetriever()
            processor.llm = StubLLMClient()
            queue.on_dead = processor.handle_dead
            test_state = SimpleNamespace(db=db, llm_queue=queue)

            async def enqueue():
                with patch("analyst_api.events.state", test_state):
                    return await enqueue_brief(case_id=case_id, txn_id="txn", level=RiskLevel.HIGH,
                                               trace_id="integration", regenerate=True)

            async def brief():
                return await db.wfetchrow("SELECT * FROM case_briefs WHERE case_id=$1", case_id)

            await enqueue()
            first = await queue.consume()
            await processor.handle(first)
            await queue.ack(first)
            assert (await brief())["status"] == "READY"
            await processor.handle(first)  # duplicate completed delivery
            assert (await brief())["status"] == "READY"

            # An old generation cannot overwrite a newly requested brief.
            await enqueue()
            await processor.handle_dead(first, "TimeoutError()")
            assert (await brief())["status"] == "PENDING"
            failed = await queue.consume()
            processor.llm = AsyncMock()
            processor.llm.generate.side_effect = TimeoutError("injected")
            with pytest.raises(TimeoutError):
                await processor.handle(failed)
            assert await queue.nack(failed, "TimeoutError()") is False
            final = await brief()
            assert final["status"] == "FAILED_TIMEOUT"
            assert final["brief"]["source"] == "rule_based"
            assert final["citations"] == []
            assert (await queue.depth())["dead"] == 1
            score = await db.wfetchrow("SELECT calibrated_prob, risk_level FROM scoring_results WHERE txn_id='txn'")
            assert score["calibrated_prob"] == 0.8 and score["risk_level"] == "HIGH"
        finally:
            await redis.delete(queue.ready, queue.processing, queue.dead)
            await redis.aclose()
            if db.write:
                await db.write.close()
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await admin.close()

    asyncio.run(run())
