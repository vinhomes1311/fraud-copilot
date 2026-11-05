import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from llm_worker.api_client import APIClient, _http_error
from llm_worker.local_integration import OllamaClient, SemanticRetriever, project_evidence
from llm_worker.llm_client import get_llm_client, StubLLMClient
from llm_worker.rag import get_retriever, ApprovedPlaybookRetriever
from llm_worker.grounding import check_grounding
from llm_worker.processor import BriefProcessor
from fraud_common.schemas import JobEnvelope


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
        self.assertEqual(set(body['response_format']['json_schema']['schema']['required']),
                         {'evidence_ids', 'checklist_ids'})
        self.assertEqual(body['max_completion_tokens'], 1024)
        self.assertFalse(body['store'])
        self.assertNotIn('reasoning_effort', body)
        self.assertEqual(result.tokens_used, 120)
        self.assertEqual(result.model_name, 'test-model')
        self.assertEqual(result.brief.citations[0].rule_code, 'PB-03')
        check_grounding(result.brief, self.chunks)

    def test_online_defaults_require_only_key(self):
        with patch.dict(os.environ, {'LLM_API_KEY': 'SECRET-KEY'}, clear=True):
            client = APIClient()
        self.assertEqual(client.base_url, 'https://api.openai.com/v1')
        self.assertEqual(client.model_name(), 'gpt-5.4-mini')

    async def test_reasoning_effort_is_explicit_and_model_can_change(self):
        with patch.dict(os.environ, {'LLM_API_REASONING_EFFORT': 'low',
                                    'LLM_API_MODEL': 'another-online-model'}):
            result, requests = await self.run_api()
        body = json.loads(requests[0].content)
        self.assertEqual(body['reasoning_effort'], 'low')
        self.assertEqual(body['model'], 'another-online-model')
        self.assertEqual(result.model_name, 'another-online-model')

    async def test_api_result_reaches_ready_with_grounded_citations(self):
        db = AsyncMock()
        db.wexec.return_value = 'UPDATE 1'
        db.rfetchrow.return_value = self.ctx
        real_client = httpx.AsyncClient
        captured = []

        def handler(request):
            captured.append(request)
            reply = self.reply()
            selection = dict(self.selection, checklist_ids=['PB-06'])
            reply['choices'][0]['message']['content'] = json.dumps(selection)
            return httpx.Response(200, json=reply)

        with patch('llm_worker.processor.get_llm_client', return_value=APIClient()), \
             patch('llm_worker.processor.get_retriever', return_value=ApprovedPlaybookRetriever()), \
             patch('llm_worker.api_client.httpx.AsyncClient',
                   side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
            processor = BriefProcessor(db)
            env = JobEnvelope(job_type='generate_brief', trace_id='api-test',
                              job_id='generation-api',
                              payload={'case_id': 1, 'txn_id': 'PRIVATE-ID', 'risk_level': 'HIGH'})
            await processor.handle(env)
        self.assertEqual(len(captured), 1)
        self.assertNotIn('PRIVATE', captured[0].content.decode())
        sql, *args = db.wexec.await_args.args
        self.assertIn("status='READY'", sql)
        self.assertEqual(args[4], 'test-model')
        self.assertEqual(args[6], 120)
        self.assertEqual(args[7], 'generation-api')
        self.assertEqual(args[1]['citations'][0]['rule_code'], 'PB-06')

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

    async def test_429_identifies_quota_and_rate_limits_without_provider_text(self):
        hints = {
            'insufficient_quota': 'Check API billing',
            'credit_balance_exhausted': 'No API credits remain',
            'organization_spend_limit_exceeded': 'organization API spend limit',
            'project_spend_limit_exceeded': 'API project spend limit',
            'organization_usage_limit_exceeded': 'organization API usage limit',
            'rate_limit_exceeded': 'Request/token rate limit',
            'slow_down': 'Request traffic increased too quickly',
        }
        for code, hint in hints.items():
            with self.subTest(code=code):
                reply = {'error': {'code': code, 'type': 'insufficient_quota',
                                   'message': 'SECRET-KEY PRIVATE-EMAIL'}}
                with self.assertRaises(RuntimeError) as caught:
                    await self.run_api(reply, status=429)
                message = str(caught.exception)
                self.assertIn(f'HTTP 429 ({code})', message)
                self.assertIn(hint, message)
                self.assertNotIn('SECRET', message)
                self.assertNotIn('PRIVATE', message)

    def test_429_unknown_malformed_errors_and_type_fallback_are_safe(self):
        bodies = [None, [], {'error': None}, {'error': []},
                  {'error': {'code': 'SECRET-KEY', 'message': 'PRIVATE-EMAIL'}},
                  {'error': {'code': [], 'type': {}}}]
        for body in bodies:
            with self.subTest(body=body):
                error = _http_error(httpx.Response(429, json=body))
                self.assertEqual(str(error), 'LLM API HTTP 429')
        self.assertEqual(str(_http_error(httpx.Response(429, text='SECRET-KEY'))),
                         'LLM API HTTP 429')
        for kind in ('insufficient_quota', 'rate_limit_error'):
            error = _http_error(httpx.Response(429, json={'error': {'type': kind}}))
            self.assertIn(f'HTTP 429 ({kind})', str(error))

    def test_invalid_configuration(self):
        for name, value in (
            ('LLM_API_KEY', ''), ('LLM_API_MODEL', ''), ('LLM_API_BASE_URL', ''),
            ('LLM_API_BASE_URL', 'http://provider.example/v1'),
            ('LLM_API_BASE_URL', 'https://user:secret@provider.example/v1'),
            ('LLM_API_BASE_URL', 'https://provider.example/v1?key=secret'),
            ('LLM_API_RESPONSE_FORMAT', 'text'), ('LLM_API_TOKEN_PARAMETER', 'bad'),
            ('LLM_API_MAX_COMPLETION_TOKENS', '0'),
            ('LLM_API_REASONING_EFFORT', 'unsupported'),
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
            with patch.dict(os.environ, {'LLM_RETRIEVER': ''}):
                self.assertIsInstance(get_retriever(), ApprovedPlaybookRetriever)
            self.assertEqual(len(await get_retriever().retrieve(['amt'], 'HIGH')), 5)
            with patch.dict(os.environ, {'LLM_RETRIEVER': 'ollama'}):
                self.assertIsInstance(get_retriever(), SemanticRetriever)
            with patch.dict(os.environ, {'LLM_RETRIEVER': 'typo'}):
                with self.assertRaises(ValueError):
                    get_retriever()
        with patch('fraud_common.config.get_settings', return_value=SimpleNamespace(llm_provider='ollama')):
            self.assertIsInstance(get_retriever(), SemanticRetriever)
            with patch.dict(os.environ, {'LLM_RETRIEVER': ''}):
                self.assertIsInstance(get_retriever(), SemanticRetriever)
