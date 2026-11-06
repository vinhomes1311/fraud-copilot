"""Opt-in real PostgreSQL/Redis workflow; LLM transport is always offline.

Set FRAUD_TEST_DB_URL and FRAUD_TEST_REDIS_URL to disposable services.
Exercises HTTP routes through ASGI, real SQL, Redis Lua, and background consumers.
"""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
from redis.asyncio import Redis

import analyst_api.auth as auth_module
import analyst_api.main as api_module
import fraud_common.config as config_module
import llm_worker.api_client as api_client_module
import llm_worker.llm_client as client_module
import llm_worker.processor as brief_module
import scoring_worker.pipeline.pii as pii_module
import scoring_worker.pipeline.routing as routing_module
import scoring_worker.processor as scoring_module
from analyst_api.state import state
from fraud_common.db import Database, _init_conn
from fraud_common.queues import ReliableQueue
from fraud_common.schemas import Explanation, ScoringOutput
from fraud_common.worker import run_consumer


pytestmark = pytest.mark.skipif(
    not os.getenv('FRAUD_TEST_DB_URL') or not os.getenv('FRAUD_TEST_REDIS_URL'),
    reason='Set disposable PostgreSQL and Redis URLs for the end-to-end workflow')


async def eventually(operation, predicate, description, timeout=12):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await operation()
        if predicate(last):
            return last
        await asyncio.sleep(0.03)
    raise AssertionError(f'Timed out waiting for {description}; last value: {last}')


@pytest.mark.parametrize('provider', ['stub', 'api'])
@pytest.mark.asyncio
async def test_entire_workflow(monkeypatch, provider):
    db_url = os.environ['FRAUD_TEST_DB_URL']
    redis_url = os.environ['FRAUD_TEST_REDIS_URL']
    prefix = 'workflow_' + uuid4().hex
    schema = prefix
    admin = await asyncpg.connect(db_url, timeout=5)
    redis_cleanup = Redis.from_url(redis_url, decode_responses=True)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    await admin.execute(f'SET search_path TO "{schema}"')
    root = Path(__file__).resolve().parents[2]
    await admin.execute((root / 'db/init.sql').read_text(encoding='utf-8'))

    class ScopedDatabase(Database):
        async def connect(self):
            self.write = await asyncpg.create_pool(
                db_url, min_size=1, max_size=6, statement_cache_size=0,
                init=_init_conn, server_settings={'search_path': schema})
            self.read = await asyncpg.create_pool(
                db_url, min_size=1, max_size=6, statement_cache_size=0,
                init=_init_conn, server_settings={'search_path': schema})

    settings = replace(
        config_module.get_settings(), redis_url=redis_url,
        db_write_url=db_url, db_read_url=db_url, llm_provider=provider,
        simulator_api_key='synthetic-simulator-key', jwt_secret='workflow-test-secret-at-least-32-characters',
        llm_stub_latency_s=0, llm_stub_fail_rate=0, ml_job_timeout_s=5,
        llm_job_timeout_s=5, llm_visibility_timeout_s=30,
        llm_max_attempts=2, threshold_cache_ttl_s=0)
    for module in (config_module, api_module, auth_module, client_module, brief_module,
                   scoring_module, pii_module, routing_module, api_client_module):
        monkeypatch.setattr(module, 'get_settings', lambda: settings)
    monkeypatch.setattr(api_module, 'Database', ScopedDatabase)
    for constant in ('QUEUE_ML_JOBS', 'QUEUE_CASE_EVENTS', 'QUEUE_LLM_JOBS'):
        monkeypatch.setattr(api_module, constant, prefix + ':' + constant.lower())
    # This workflow test covers queue/API orchestration. Model artifact quality
    # is checked separately, so the E2E must not depend on a local joblib file.
    def fake_predict(features):
        amount = float(features['amt'])
        probability = 0.1 if amount < 100 else 0.5 if amount < 1400 else 0.9
        return ScoringOutput(
            raw_model_score=probability,
            calibrated_prob=probability,
            model_version='workflow-test-model',
            explanations=[Explanation(feature='amt', value=amount, contribution=probability)],
        )

    monkeypatch.setattr(scoring_module, 'predict', fake_predict)
    monkeypatch.setenv('LLM_RETRIEVER', 'playbook')
    monkeypatch.setenv('LLM_API_BASE_URL', 'https://offline-provider.example/v1')
    monkeypatch.setenv('LLM_API_MODEL', 'offline-workflow-model')
    monkeypatch.setenv('LLM_API_KEY', 'synthetic-api-key')
    monkeypatch.setenv('LLM_API_REASONING_EFFORT', '')
    captured = []
    fail_llm = False

    def fake_provider(request):
        assert request.url.host == 'offline-provider.example'
        captured.append(request)
        if fail_llm:
            raise httpx.ReadTimeout('synthetic timeout')
        payload = json.loads(request.content)
        request_data = json.loads(payload['messages'][1]['content'])
        evidence = request_data['evidence']
        allowed_rules = payload['response_format']['json_schema']['schema']['properties'][
            'checklist_ids']['items']['enum']
        # Mirror a compliant model: choose an ID allowed by this request's actual
        # retrieved playbook and any mandatory-gap schema constraint.
        selection = {'evidence_ids': list(evidence), 'checklist_ids': [allowed_rules[0]]}
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(selection)}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})

    # Only the provider module is replaced. HTTP requests to ASGI use real httpx.
    monkeypatch.setattr(api_client_module, 'httpx', SimpleNamespace(
        AsyncClient=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(fake_provider), **kw),
        TimeoutException=httpx.TimeoutException, RequestError=httpx.RequestError))
    stop = asyncio.Event()
    worker_tasks = []
    ids = {}
    try:
        async with api_module.app.router.lifespan_context(api_module.app):
            scoring = scoring_module.ScoringProcessor(state.db, state.events_queue)
            brief = brief_module.BriefProcessor(state.db)
            ml_queue = ReliableQueue(state.redis, state.ml_queue.name,
                                     max_attempts=2, on_dead=scoring.handle_dead)
            llm_queue = ReliableQueue(state.redis, state.llm_queue.name,
                                      max_attempts=2, on_dead=brief.handle_dead)
            worker_tasks = [
                asyncio.create_task(run_consumer(ml_queue, scoring.handle, concurrency=1,
                                                stop=stop, poll_interval_s=0.01)),
                asyncio.create_task(run_consumer(llm_queue, brief.handle, concurrency=1,
                                                stop=stop, poll_interval_s=0.01)),
            ]
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app),
                                             base_url='http://workflow.test') as http:
                    assert (await http.get('/healthz')).status_code == 200
                    assert (await http.get('/api/v1/cases')).status_code == 401
                    assert (await http.post('/api/v1/transactions', json={})).status_code == 401
                    assert (await http.post('/api/v1/auth/login', json={
                        'username': 'analyst1', 'password': 'wrongpass'})).status_code == 401
                    headers = {}
                    for role, user, password in (('analyst', 'analyst1', 'analyst123'),
                                                 ('lead', 'lead1', 'lead123'),
                                                 ('admin', 'admin', 'admin123')):
                        response = await http.post('/api/v1/auth/login', json={
                            'username': user, 'password': password})
                        assert response.status_code == 200, response.text
                        headers[role] = {'Authorization': 'Bearer ' + response.json()['access_token']}
                    simulator = {'X-API-Key': settings.simulator_api_key}

                    async def get_result(txn_id):
                        response = await http.get(f'/api/v1/transactions/{txn_id}/result', headers=simulator)
                        assert response.status_code in (200, 202), response.text
                        return response.json()

                    async def get_case(case_id, role='analyst'):
                        response = await http.get(f'/api/v1/cases/{case_id}', headers=headers[role])
                        assert response.status_code == 200, response.text
                        return response.json()

                    for level, amount, merchant_lat, merchant_lon in (
                        ('LOW', 10, 10, 20), ('MEDIUM', 1100, 10, 20),
                        ('HIGH', 1500, 40, 50), ('INVALID', -1, 10, 20)):
                        txn_id = prefix + '-' + level
                        ids[level] = txn_id
                        transaction = {
                            'trans_num': txn_id, 'trans_date_trans_time': '2026-11-04 12:00:00',
                            'cc_num': '4111111111111111', 'first': 'PRIVATE-FIRST',
                            'last': 'PRIVATE-LAST', 'street': 'PRIVATE-STREET',
                            'merchant': 'synthetic merchant', 'category': 'synthetic',
                            'amt': amount, 'lat': 10, 'long': 20,
                            'merch_lat': merchant_lat, 'merch_long': merchant_lon, 'is_fraud': 1}
                        response = await http.post('/api/v1/transactions', headers=simulator, json=transaction)
                        assert response.status_code == 202, response.text
                        duplicate = await http.post('/api/v1/transactions', headers=simulator, json=transaction)
                        assert duplicate.status_code == 200 and duplicate.json()['status'] == 'DUPLICATE'
                        result = await eventually(lambda: get_result(txn_id),
                            lambda row: row.get('status') != 'PENDING', level + ' scoring')
                        if level == 'INVALID':
                            assert result['status'] == 'MANUAL_FALLBACK_UNSCORED', result
                            assert await state.db.rfetchval(
                                'SELECT count(*) FROM unscored_queue WHERE txn_id=$1', txn_id) == 1
                            continue
                        assert result['status'] == 'SCORED' and result['risk_level'] == level, result
                        if level == 'LOW':
                            assert result['case_id'] is not None
                            assert await state.db.rfetchval(
                                'SELECT count(*) FROM cases WHERE txn_id=$1', txn_id) == 1
                            low_case = await get_case(result['case_id'])
                            assert low_case['risk_level'] == 'LOW'
                            assert low_case['brief_status'] is None
                            assert await state.db.rfetchval(
                                'SELECT count(*) FROM case_briefs WHERE case_id=$1',
                                result['case_id']) == 0
                            continue
                        case_id = result['case_id']
                        ids[level + '_case'] = case_id
                        case = await eventually(lambda: get_case(case_id),
                            lambda row: row['brief_status'] in ('READY', 'FAILED', 'FAILED_TIMEOUT'),
                            level + ' brief')
                        assert case['brief_status'] == 'READY', case
                        assert case['brief']['citations'] and case['brief']['checklist']
                        assert case['brief']['source'] == 'llm'
                        assert 'cc_num' not in case['transaction'] and 'is_fraud' not in case['transaction']
                        assert 'PRIVATE' not in json.dumps(case['transaction'])

                    listed = await http.get('/api/v1/cases', headers=headers['analyst'])
                    items = listed.json()['items']
                    assert [row['risk_level'] for row in items] == ['HIGH', 'MEDIUM', 'LOW']
                    high_case = ids['HIGH_case']
                    medium_case = ids['MEDIUM_case']
                    response = await http.post(f'/api/v1/cases/{high_case}/resolve',
                        headers=headers['analyst'], json={'decision': 'CLOSE'})
                    assert response.status_code == 403
                    response = await http.post(f'/api/v1/cases/{high_case}/actions',
                        headers=headers['analyst'], json={'action': 'ESCALATE', 'note': 'Synthetic review'})
                    assert response.status_code == 200, response.text
                    lead_queue = await http.get('/api/v1/cases', headers=headers['lead'])
                    assert [row['case_id'] for row in lead_queue.json()['items']] == [high_case]
                    response = await http.post(f'/api/v1/cases/{high_case}/resolve',
                        headers=headers['lead'], json={'decision': 'CLOSE', 'note': 'Synthetic close'})
                    assert response.status_code == 200, response.text
                    assert (await get_case(high_case))['status'] == 'CLOSED'
                    assert len((await get_case(high_case))['actions']) == 2

                    assert (await http.get('/api/v1/admin/queues', headers=headers['analyst'])).status_code == 403
                    thresholds = await http.put('/api/v1/admin/threshold-config', headers=headers['admin'],
                                                 json={'low_max': 0.2, 'high_min': 0.8})
                    assert thresholds.status_code == 200, thresholds.text
                    assert thresholds.json()['version'] == 2
                    active = await http.get('/api/v1/admin/threshold-config', headers=headers['admin'])
                    assert float(active.json()['high_min']) == 0.8

                    # Regeneration success, then timeout retries/fallback on the same case.
                    response = await http.post(f'/api/v1/cases/{medium_case}/brief:regenerate',
                                               headers=headers['analyst'])
                    assert response.status_code == 202, response.text
                    case = await eventually(lambda: get_case(medium_case),
                        lambda row: row['brief_status'] == 'READY', 'brief regeneration')
                    saved_score = (case['calibrated_prob'], case['risk_level'])
                    if provider == 'api':
                        fail_llm = True
                    else:
                        async def timeout(*args):
                            raise TimeoutError('synthetic timeout')
                        monkeypatch.setattr(brief.llm, 'generate', timeout)
                    response = await http.post(f'/api/v1/cases/{medium_case}/brief:regenerate',
                                               headers=headers['analyst'])
                    assert response.status_code == 202
                    failed = await eventually(lambda: get_case(medium_case),
                        lambda row: row['brief_status'] == 'FAILED_TIMEOUT', 'retry exhaustion/fallback')
                    assert failed['brief']['source'] == 'rule_based'
                    assert failed['brief_attempts'] == 2
                    assert (failed['calibrated_prob'], failed['risk_level']) == saved_score
                    assert (await llm_queue.depth())['dead'] == 1
                    assert await state.db.rfetchval('SELECT count(*) FROM audit_log') >= 8

                    if provider == 'api':
                        assert len(captured) == 5  # 2 briefs + regeneration + 2 timeout attempts.
                        for request in captured:
                            text = request.content.decode()
                            assert '4111111111111111' not in text
                            assert 'PRIVATE' not in text and prefix not in text
                    else:
                        assert captured == []
                    print(f'WORKFLOW_OK provider={provider}: LOW/MEDIUM/HIGH/INVALID, duplicate, '
                          'brief, auth/RBAC, escalate/close, admin, regenerate, retry/fallback')
            finally:
                stop.set()
                await asyncio.wait_for(asyncio.gather(*worker_tasks), timeout=10)
    finally:
        keys = [key async for key in redis_cleanup.scan_iter(match=prefix + '*')]
        keys += ['idem:txn:' + value for key, value in ids.items() if not key.endswith('_case')]
        keys += ['regen:' + str(value) for key, value in ids.items() if key.endswith('_case')]
        if keys:
            await redis_cleanup.delete(*keys)
        await redis_cleanup.aclose()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
