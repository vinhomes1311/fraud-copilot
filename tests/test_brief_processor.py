"""Worker lifecycle tests with mocked external services; requires requirements.txt."""
from dataclasses import replace
import unittest
from unittest.mock import AsyncMock

from fraud_common.schemas import JobEnvelope, BriefContent
from fraud_common.worker import NonRetryableError
from llm_worker.llm_client import LLMResult
from llm_worker.processor import BriefProcessor, CaseNotReady


class ProcessorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = AsyncMock()
        self.db.wexec.return_value = "UPDATE 1"
        self.db.rfetchrow.return_value = {
            "case_id": 1, "txn_id": "txn", "risk_level": "HIGH",
            "transaction": {"amt": 100}, "explanations": [],
        }
        self.processor = BriefProcessor(self.db)
        self.processor.retriever = AsyncMock()
        self.processor.retriever.retrieve.return_value = []
        self.processor.llm = AsyncMock()
        self.result = LLMResult(BriefContent(summary="test", key_signals=[], checklist=[], citations=[]),
                                tokens_used=10, model_name="test")
        self.processor.llm.generate.return_value = self.result
        self.env = JobEnvelope(job_type="generate_brief", trace_id="test", job_id="generation",
                               payload={"case_id": 1, "txn_id": "txn", "risk_level": "HIGH"})

    async def test_stale_job_does_not_call_model(self):
        self.db.wexec.return_value = "UPDATE 0"
        await self.processor.handle(self.env)
        self.db.rfetchrow.assert_not_awaited()
        self.processor.llm.generate.assert_not_awaited()

    async def test_replica_lag_propagates_for_retry(self):
        self.db.rfetchrow.return_value = None
        with self.assertRaises(CaseNotReady):
            await self.processor.handle(self.env)
        self.processor.llm.generate.assert_not_awaited()

    async def test_retrieval_timeout_propagates_for_retry(self):
        self.processor.retriever.retrieve.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.processor.handle(self.env)
        self.processor.llm.generate.assert_not_awaited()

    async def test_generation_timeout_propagates_for_retry(self):
        self.processor.llm.generate.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.processor.handle(self.env)
        self.assertEqual(self.db.wexec.await_count, 1)

    async def test_token_budget_records_terminal_fallback(self):
        self.processor.s = replace(self.processor.s, brief_token_budget=1)
        with self.assertRaises(NonRetryableError):
            await self.processor.handle(self.env)
        args = self.db.wexec.await_args.args
        self.assertEqual(args[2], "FAILED")
        self.assertEqual(args[3]["source"], "rule_based")
        self.assertEqual(args[-1], "generation")

    async def test_visibility_timeout_is_classified_and_null_signals_supported(self):
        self.db.rfetchrow.return_value["explanations"] = None
        await self.processor.handle_dead(self.env, "visibility timeout expired")
        args = self.db.wexec.await_args.args
        self.assertEqual(args[2], "FAILED_TIMEOUT")
        self.assertEqual(args[3]["source"], "rule_based")


if __name__ == "__main__":
    unittest.main()
