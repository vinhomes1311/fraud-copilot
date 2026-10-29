import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from llm_worker.api_client import APIClient
from llm_worker.local_integration import OllamaClient, SemanticRetriever, project_evidence
from llm_worker.llm_client import get_llm_client, StubLLMClient
from llm_worker.rag import get_retriever, ApprovedPlaybookRetriever
from llm_worker.grounding import check_grounding


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            'LLM_API_BASE_URL': 'https://provider.example/v1',
            'LLM_API_MODEL': 'test-model', 'LLM_API_KEY': 'SECRET-KEY',
            'LLM_API_RESPONSE_FORMAT': 'json_schema',
            'LLM_API_TOKEN_PARAMETER': 'max_completion_tokens',
            'LLM_API_MAX_COMPLETION_TOKENS': '1024',
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ctx = {'case_id': 1, 'txn_id': 'PRIVATE-ID', 'risk_level': 'HIGH',
            'raw_model_score': 1.7, 'calibrated_prob': 0.8,
            'transaction': {'amt': 100, 'email': 'PRIVATE-EMAIL'},
            'features': {'txn_count_24h': 0, 'avg_amt_24h': 0},
            'explanations': [{'feature': 'amt', 'value': 100, 'contribution': 0.2}]}
        self.chunks = await ApprovedPlaybookRetriever().retrieve(['amt'], 'HIGH')
        self.selection = {'evidence_ids': list(project_evidence(self.ctx)), 'checklist_ids': ['PB-03']}

    def reply(self):
        return {'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(self.selection)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}

    async def run_api(self, reply=None, status=200, failure=None):
        captured = []
        def handler(request):
            captured.append(request)
            if failure:
                raise failure
            return httpx.Response(status, json=reply if reply is not None else self.reply())
        real_client = httpx.AsyncClient
        def factory(**kwargs):
            self.assertFalse(kwargs['follow_redirects'])
            self.assertGreater(kwargs['timeout'], 0)
            return real_client(transport=httpx.MockTransport(handler), **kwargs)
        with patch('llm_worker.api_client.httpx.AsyncClient', side_effect=factory):
            result = await APIClient().generate(self.ctx, self.chunks)
        return result, captured

    async def test_success_privacy_contract_usage_and_unchanged_input(self):
        before = copy.deepcopy(self.ctx)
        result, requests = await self.run_api()
        self.assertEqual(before, self.ctx)
        self.assertEqual(len(requests), 1)
        req = requests[0]
        self.assertEqual(str(req.url), 'https://provider.example/v1/chat/completions')
        self.assertEqual(req.headers['authorization'], 'Bearer SECRET-KEY')
        body = json.loads(req.content)
        self.assertNotIn('PRIVATE', req.content.decode())
        self.assertNotIn('SECRET-KEY', req.content.decode())
        self.assertEqual(body['response_format']['type'], 'json_schema')
        self.assertTrue(body['response_format']['json_schema']['strict'])
        self.assertEqual(body['max_completion_tokens'], 1024)
        self.assertEqual(result.tokens_used, 120)
        self.assertEqual(result.model_name, 'test-model')
        self.assertEqual(result.brief.citations[0].rule_code, 'PB-03')
        check_grounding(result.brief, self.chunks)

    async def test_json_mode_and_legacy_token_parameter(self):
        with patch.dict(os.environ, {'LLM_API_RESPONSE_FORMAT': 'json_object',
                                    'LLM_API_TOKEN_PARAMETER': 'max_tokens'}):
            _, requests = await self.run_api()
        body = json.loads(requests[0].content)
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['max_tokens'], 1024)
        self.assertNotIn('max_completion_tokens', body)

    async def test_missing_duplicate_unknown_and_extra_selection_rejected(self):
        bad = [dict(self.selection, checklist_ids=['FAKE']),
               dict(self.selection, evidence_ids=self.selection['evidence_ids'][:-1]),
               dict(self.selection, checklist_ids=['PB-03', 'PB-03']),
               dict(self.selection, summary='invented')]
        for selection in bad:
            with self.subTest(selection=selection):
                reply = self.reply()
                reply['choices'][0]['message']['content'] = json.dumps(selection)
                with self.assertRaises(ValueError):
                    await self.run_api(reply)

    async def test_invalid_envelope_refusal_truncation_and_usage(self):
        variants = []
        for reason in ('length', 'content_filter', 'tool_calls'):
            r = self.reply(); r['choices'][0]['finish_reason'] = reason; variants.append(r)
        r = self.reply(); r['choices'][0]['message']['refusal'] = 'refused'; variants.append(r)
        r = self.reply(); r['choices'][0]['message']['content'] = None; variants.append(r)
        r = self.reply(); r['choices'] = []; variants.append(r)
        for counts in (None, {}, {'prompt_tokens': True, 'completion_tokens': 20, 'total_tokens': 21},
                       {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 999}):
            r = self.reply(); r['usage'] = counts; variants.append(r)
        for reply in variants:
            with self.subTest(reply=reply), self.assertRaises(ValueError):
                await self.run_api(reply)

    async def test_malformed_selection_json_rejected(self):
        reply = self.reply(); reply['choices'][0]['message']['content'] = '{'
        with self.assertRaises(ValueError):
            await self.run_api(reply)

    async def test_unapproved_chunks_rejected_before_http(self):
        self.chunks[0].text = 'unapproved'
        with patch('llm_worker.api_client.httpx.AsyncClient') as transport:
            with self.assertRaises(ValueError):
                await APIClient().generate(self.ctx, self.chunks)
            transport.assert_not_called()

    async def test_http_errors_sanitized(self):
        for status in (301, 400, 401, 429, 500):
            with self.subTest(status=status):
                with self.assertRaisesRegex(RuntimeError, f'^LLM API HTTP {status}$'):
                    await self.run_api({'error': 'SECRET-KEY PRIVATE-EMAIL'}, status)

    async def test_timeout_is_classified_and_network_error_sanitized(self):
        with self.assertRaisesRegex(TimeoutError, '^LLM API timeout$'):
            await self.run_api(failure=httpx.ReadTimeout('SECRET-KEY'))
        with self.assertRaisesRegex(RuntimeError, '^LLM API connection failed$'):
            await self.run_api(failure=httpx.ConnectError('SECRET-KEY'))

    def test_invalid_configuration(self):
        for name, value in (
            ('LLM_API_KEY', ''), ('LLM_API_MODEL', ''), ('LLM_API_BASE_URL', ''),
            ('LLM_API_BASE_URL', 'http://provider.example/v1'),
            ('LLM_API_BASE_URL', 'https://user:secret@provider.example/v1'),
            ('LLM_API_BASE_URL', 'https://provider.example/v1?key=secret'),
            ('LLM_API_RESPONSE_FORMAT', 'text'), ('LLM_API_TOKEN_PARAMETER', 'bad'),
            ('LLM_API_MAX_COMPLETION_TOKENS', '0'),
        ):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaises(ValueError):
                    APIClient()

    async def test_factories_preserve_ollama_and_use_approved_api_playbook(self):
        for provider, cls in [('stub', StubLLMClient), ('ollama', OllamaClient), ('api', APIClient)]:
            with patch('llm_worker.llm_client.get_settings', return_value=SimpleNamespace(llm_provider=provider)):
                self.assertIsInstance(get_llm_client(), cls)
        with patch('fraud_common.config.get_settings', return_value=SimpleNamespace(llm_provider='api')):
            self.assertIsInstance(get_retriever(), ApprovedPlaybookRetriever)
            self.assertEqual(len(await get_retriever().retrieve(['amt'], 'HIGH')), 5)
            with patch.dict(os.environ, {'LLM_RETRIEVER': 'ollama'}):
                self.assertIsInstance(get_retriever(), SemanticRetriever)
            with patch.dict(os.environ, {'LLM_RETRIEVER': 'typo'}):
                with self.assertRaises(ValueError):
                    get_retriever()
        with patch('fraud_common.config.get_settings', return_value=SimpleNamespace(llm_provider='ollama')):
            self.assertIsInstance(get_retriever(), SemanticRetriever)
