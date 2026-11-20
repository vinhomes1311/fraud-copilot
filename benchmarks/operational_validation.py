"""Opt-in live worker validation against dedicated disposable PostgreSQL/Redis.

Uses production enqueue, queue consumer, processor, grounding and SQL. Fault
scenarios are explicitly injected; they are not claimed as provider outages.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import httpx
from redis.asyncio import Redis

from analyst_api.events import enqueue_brief
import llm_worker.api_client as api_module
import llm_worker.local_integration as local_module
from benchmarks.compare_llm import RequestPacer, load_cases, ROOT
from benchmarks.mock_llm import explanatory_selection
from fraud_common.db import Database, _init_conn
from fraud_common.queues import ReliableQueue
from fraud_common.resilience import CircuitBreaker
from fraud_common.schemas import BriefContent, RiskLevel
from fraud_common.worker import run_consumer
from llm_worker.grounding import check_grounding
from llm_worker.processor import BriefProcessor
from llm_worker.rag import ApprovedPlaybookRetriever
from llm_worker.retrieval_policy import retrieve_context


def validate_test_service_urls():
    db_url = os.getenv('FRAUD_TEST_DB_URL', '')
    redis_url = os.getenv('FRAUD_TEST_REDIS_URL', '')
    db, redis = urlsplit(db_url), urlsplit(redis_url)
    if (db.scheme != 'postgresql' or db.hostname not in ('localhost', '127.0.0.1')
            or db.port != 15432 or db.path != '/validation'
            or redis.scheme != 'redis' or redis.hostname not in ('localhost', '127.0.0.1')
            or redis.port != 16379):
        raise ValueError('Use the dedicated validation services on localhost:15432/16379')
    return db_url, redis_url


class ObservedRetriever:
    def __init__(self, inner):
        self.inner = inner
        self.chunks = {}

    async def retrieve_context(self, ctx, top_k=5):
        chunks = await retrieve_context(self.inner, ctx, top_k=top_k)
        self.chunks[ctx['case_id']] = chunks
        return chunks


class ObservedLLM:
    def __init__(self, inner, interval=0):
        self.inner = inner
        self.pacer = RequestPacer(interval)
        self.samples = []
        self.attempts = 0

    async def generate(self, ctx, chunks):
        self.attempts += 1
        await self.pacer.wait()
        started = time.perf_counter()
        result = await self.inner.generate(ctx, chunks)
        self.samples.append({'case_id': ctx['case_id'], 'latency_s': time.perf_counter()-started,
                             'tokens': result.tokens_used, 'model': result.model_name})
        return result


class SyntheticFaultSuccess(local_module.OllamaClient):
    """Grounded synthetic response used only to isolate injected resilience checks."""
    def model_name(self):
        return 'synthetic-fault-success'

    async def chat(self, model, schema, evidence, selected_chunks):
        allowed = schema['properties']['checklist_ids']['items']['enum']
        return {'done': True, 'prompt_eval_count': 100, 'eval_count': 20,
                'message': {'content': json.dumps(explanatory_selection(allowed[:1]))}}


async def validate(output, interval):
    db_url, redis_url = validate_test_service_urls()
    os.environ['OLLAMA_BASE_URL'] = 'http://127.0.0.1:11434'
    cases = {c['id']: c for c in load_cases(ROOT/'benchmarks/fixtures/playbook_holdout_20261118.json')}
    schema = 'ops_validation_' + uuid4().hex
    admin = await asyncpg.connect(db_url)
    redis = Redis.from_url(redis_url, decode_responses=True)
    db = Database('', '')
    queues = []
    snapshots = {}
    checks = []
    model_samples = {}
    requests = []
    schema_created = False
    report = {'mode': 'live-worker-with-real-PostgreSQL-Redis',
              'created_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'production enqueue -> Redis consumer -> retrieval -> LLM -> grounding -> PostgreSQL',
              'limitations': ['Disposable test services; not production traffic, HA or replica-lag acceptance.',
                              'Fault checks inject timeout/connection/429 errors and label them separately.',
                              'API pacing is a validation harness setting, not automatic production rate limiting.'],
              'retrieval_policy_sha256': hashlib.sha256((ROOT/'llm_worker/retrieval_policy.py').read_bytes()).hexdigest(),
              'checks': checks, 'model_samples': model_samples, 'transport_requests': requests}

    def record(name, provider, ok, **details):
        row = dict(name=name, provider=provider, passed=bool(ok), **details)
        checks.append(row)
        (output/'operations.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(row), flush=True)

    async def seed(case, suffix, *, high=False):
        ctx = copy.deepcopy(case['context'])
        if high:
            ctx.update(risk_level='HIGH', calibrated_prob=0.91)
        txn = schema + '_' + suffix
        await db.wexec('INSERT INTO transactions (txn_id,payload_masked) VALUES ($1,$2)', txn, ctx['transaction'])
        await db.wexec("""INSERT INTO scoring_results (txn_id,status,raw_model_score,calibrated_prob,
            risk_level,features,explanations) VALUES ($1,'SCORED',$2,$3,$4,$5,$6)""",
            txn,ctx['raw_model_score'],ctx['calibrated_prob'],ctx['risk_level'],ctx['features'],ctx['explanations'])
        row = await db.wfetchrow('INSERT INTO cases (txn_id,risk_level) VALUES ($1,$2) RETURNING case_id', txn,ctx['risk_level'])
        ctx.update(case_id=row['case_id'],txn_id=txn)
        snapshots[txn] = dict(await db.wfetchrow('SELECT raw_model_score,calibrated_prob,risk_level,features,explanations FROM scoring_results WHERE txn_id=$1',txn))
        return ctx

    async def batch(name, provider, processor, contexts, concurrency=1, attempts=2, existing=None, queue=None):
        queue = queue or ReliableQueue(redis, schema+'_'+name, visibility_timeout_s=300,
                                       max_attempts=attempts, on_dead=processor.handle_dead)
        if queue not in queues:
            queues.append(queue)
        state = SimpleNamespace(db=db,llm_queue=queue)
        published = time.perf_counter()
        envs = []
        if existing:
            envs = existing
        else:
            for ctx in contexts:
                with patch('analyst_api.events.state', state):
                    envs.append(await enqueue_brief(case_id=ctx['case_id'],txn_id=ctx['txn_id'],
                        level=RiskLevel(ctx['risk_level']),trace_id='ops-validation',regenerate=True))
        starts = []
        active = [0]

        async def handle(env):
            active[0] += 1
            starts.append({'case_id':env.payload['case_id'],'attempt':env.attempts+1,
                           'start_after_publish_s':time.perf_counter()-published,
                           'active_handlers':active[0]})
            try:
                await processor.handle(env)
            finally:
                active[0] -= 1

        stop = asyncio.Event()
        task = asyncio.create_task(run_consumer(queue,handle,concurrency=concurrency,stop=stop))
        try:
            async with asyncio.timeout(360):
                while True:
                    rows = [await db.wfetchrow('SELECT * FROM case_briefs WHERE case_id=$1', c['case_id']) for c in contexts]
                    depth = await queue.depth()
                    if all(r and r['status'] in ('READY','FAILED','FAILED_TIMEOUT') for r in rows) and not depth['ready'] and not depth['processing']:
                        break
                    await asyncio.sleep(0.1)
        finally:
            stop.set()
            await asyncio.wait_for(task,20)
        return rows, envs, starts, queue

    real_client = httpx.AsyncClient
    transport_entries = {}

    async def audit_request(request):
        body = request.content.decode(errors='replace')
        if 'HOLDOUT-PRIVATE-CANARY' in body or schema in body:
            raise ValueError('Private test canary reached model transport')
        entry = {'kind':'embedding' if request.url.path.endswith('/api/embed') else 'generation'}
        requests.append(entry)
        transport_entries[id(request)] = entry

    async def audit_response(response):
        transport_entries[id(response.request)]['http_status'] = response.status_code

    def client_factory(**kwargs):
        kwargs['event_hooks'] = {'request':[audit_request], 'response':[audit_response]}
        return real_client(**kwargs)

    transport = SimpleNamespace(AsyncClient=client_factory,TimeoutException=httpx.TimeoutException,RequestError=httpx.RequestError)
    try:
        await redis.ping()
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        schema_created = True
        await admin.execute(f'SET search_path TO "{schema}"')
        await admin.execute((ROOT/'db/init.sql').read_text(encoding='utf-8'))
        db.write = await asyncpg.create_pool(db_url,min_size=1,max_size=5,init=_init_conn,
                                             server_settings={'search_path':schema},statement_cache_size=0)
        db.read = db.write
        with patch.object(api_module,'httpx',transport), patch.object(local_module,'httpx',transport):
            for provider in ('api','ollama'):
                processor = BriefProcessor(db)
                processor.s = replace(processor.s,llm_job_timeout_s=120,llm_visibility_timeout_s=300)
                processor.retriever = ObservedRetriever(ApprovedPlaybookRetriever())
                base = api_module.APIClient() if provider=='api' else local_module.OllamaClient(max_tokens=1024)
                live = ObservedLLM(base,interval if provider=='api' else 0)
                processor.llm = live
                model_samples[provider] = live.samples
                normal = [await seed(cases['holdout-low-dense'],provider+'_low'),
                          await seed(cases['holdout-missing-maximum-valid-long-mean'],provider+'_high',high=True)]
                rows,envs,starts,queue = await batch(provider+'_sequential',provider,processor,normal)
                record('sequential_ready_and_priority',provider,
                       all(r['status']=='READY' for r in rows) and starts[0]['case_id']==normal[1]['case_id'],
                       statuses=[r['status'] for r in rows], attempts=[r['attempts'] for r in rows])
                for row in rows:
                    if row['status']=='READY':
                        check_grounding(BriefContent.model_validate(row['brief']),processor.retriever.chunks[row['case_id']])
                calls = live.attempts
                await processor.handle(envs[0])
                record('completed_duplicate_makes_no_model_call',provider,live.attempts==calls)
                state = SimpleNamespace(db=db,llm_queue=queue)
                with patch('analyst_api.events.state',state):
                    new = await enqueue_brief(case_id=normal[0]['case_id'],txn_id=normal[0]['txn_id'],
                        level=RiskLevel.LOW,trace_id='ops-regeneration',regenerate=True)
                await processor.handle_dead(envs[0],'TimeoutError(injected stale generation)')
                await processor.handle(envs[0])
                pending = await db.wfetchrow('SELECT status,generation_id FROM case_briefs WHERE case_id=$1',normal[0]['case_id'])
                record('stale_generation_cannot_overwrite_new_pending',provider,
                       pending['status']=='PENDING' and pending['generation_id']==new.job_id and live.attempts==calls)
                regen,_,_,_ = await batch(provider+'_regeneration',provider,processor,normal[:1],existing=[new],queue=queue)
                record('new_generation_completes',provider,regen[0]['status']=='READY')
                parallel = [await seed(cases[name],provider+'_parallel_'+str(i),high=i==1) for i,name in enumerate((
                    'holdout-two-frequency-windows','holdout-untrusted-payload-canary',
                    'holdout-mean-versus-maximum','holdout-three-focal-actions'))]
                rows,_,starts,_ = await batch(provider+'_parallel',provider,processor,parallel,concurrency=2)
                peak = max(s['active_handlers'] for s in starts)
                record('two_consumers_all_jobs_ready',provider,all(r['status']=='READY' for r in rows) and peak==2,
                       statuses=[r['status'] for r in rows], peak_active_handlers=peak, starts=starts)
                for row in rows:
                    if row['status']=='READY':
                        check_grounding(BriefContent.model_validate(row['brief']),processor.retriever.chunks[row['case_id']])
                processor.retriever = ObservedRetriever(local_module.SemanticRetriever())
                semantic = [await seed(cases['holdout-low-aliases'],provider+'_semantic')]
                rows,_,_,_ = await batch(provider+'_semantic',provider,processor,semantic)
                record('real_semantic_embedding_then_generation',provider,rows[0]['status']=='READY',status=rows[0]['status'])
                if rows[0]['status']=='READY':
                    check_grounding(BriefContent.model_validate(rows[0]['brief']),processor.retriever.chunks[rows[0]['case_id']])
                processor.retriever = ObservedRetriever(ApprovedPlaybookRetriever())
                transient = [await seed(cases['holdout-zero-geographic-distance'],provider+'_transient')]
                attempts = [0]

                async def rate_limit_once(ctx,chunks):
                    attempts[0]+=1
                    if attempts[0]==1:
                        raise RuntimeError('LLM API HTTP 429 (injected)')
                    return await live.generate(ctx,chunks)

                processor.llm = SimpleNamespace(generate=rate_limit_once)
                rows,_,_,_ = await batch(provider+'_retry',provider,processor,transient)
                record('injected_429_retried_then_live_provider_ready',provider,
                       rows[0]['status']=='READY' and rows[0]['attempts']==2,status=rows[0]['status'],attempts=rows[0]['attempts'])
                processor.llm = live
                report.setdefault('model_attempt_counts',{})[provider] = live.attempts

            # Fault isolation uses real queue/SQL and synthetic model-call failures.
            processor = BriefProcessor(db)
            processor.retriever = ObservedRetriever(ApprovedPlaybookRetriever())
            processor.s = replace(processor.s,llm_job_timeout_s=0.03)

            async def slow(ctx,chunks):
                await asyncio.sleep(0.2)
                raise AssertionError('Timeout should have cancelled this call')

            processor.llm = SimpleNamespace(generate=slow)
            timeout_ctx=[await seed(cases['holdout-zero-geographic-distance'],'timeout')]
            rows,_,_,queue=await batch('timeout','injected',processor,timeout_ctx)
            record('deadline_retry_dead_letter_and_fallback','injected',
                   rows[0]['status']=='FAILED_TIMEOUT' and rows[0]['brief']['source']=='rule_based'
                   and rows[0]['attempts']==2 and (await queue.depth())['dead']==1,
                   status=rows[0]['status'],attempts=rows[0]['attempts'])
            processor.s = replace(processor.s,llm_job_timeout_s=120)
            processor.breaker = CircuitBreaker(5,30)

            async def disconnected(ctx,chunks):
                raise RuntimeError('LLM API connection failed')

            processor.llm=SimpleNamespace(generate=disconnected)
            disconnected_ctx=[await seed(cases['holdout-zero-geographic-distance'],'connection')]
            rows,_,_,_=await batch('connection','injected',processor,disconnected_ctx)
            record('connection_failure_is_bounded_with_fallback','injected',
                   rows[0]['status']=='FAILED' and rows[0]['brief']['source']=='rule_based' and rows[0]['attempts']==2,
                   status=rows[0]['status'],attempts=rows[0]['attempts'])
            processor.breaker=CircuitBreaker(2,60)
            calls=[0]

            async def fail_for_breaker(ctx,chunks):
                calls[0]+=1
                raise TimeoutError('injected timeout')

            processor.llm=SimpleNamespace(generate=fail_for_breaker)
            breaker_ctx=[await seed(cases['holdout-zero-geographic-distance'],'breaker_'+str(i)) for i in range(3)]
            rows,_,_,_=await batch('breaker','injected',processor,breaker_ctx,attempts=1)
            record('circuit_open_suppresses_third_model_call','injected',
                   calls[0]==2 and processor.breaker.is_open and all(r['status']=='FAILED_TIMEOUT' for r in rows),
                   model_calls=calls[0],jobs=3)
            processor.breaker.reset_after_s=0
            processor.llm=SyntheticFaultSuccess(max_tokens=1024)
            recovery=[await seed(cases['holdout-zero-geographic-distance'],'breaker_recovery')]
            rows,_,_,_=await batch('breaker_recovery','injected',processor,recovery)
            record('half_open_success_closes_breaker','injected',rows[0]['status']=='READY' and not processor.breaker.is_open)
            lease=[await seed(cases['holdout-zero-geographic-distance'],'expired_lease')]
            lease_queue=ReliableQueue(redis,schema+'_lease',visibility_timeout_s=0.05,max_attempts=2,on_dead=processor.handle_dead)
            queues.append(lease_queue)
            with patch('analyst_api.events.state',SimpleNamespace(db=db,llm_queue=lease_queue)):
                await enqueue_brief(case_id=lease[0]['case_id'],txn_id=lease[0]['txn_id'],level=RiskLevel.MEDIUM,trace_id='lease')
            stuck=await lease_queue.consume()
            await asyncio.sleep(0.08)
            reaped=await lease_queue.reap_expired()
            lease_queue.visibility_timeout_s=300
            rows,_,_,_=await batch('lease','injected',processor,lease,existing=[stuck],queue=lease_queue)
            record('expired_visibility_lease_requeued_and_ready','injected',reaped==1 and rows[0]['status']=='READY' and rows[0]['attempts']==2)
            unchanged=True
            for txn,before in snapshots.items():
                score=dict(await db.wfetchrow('SELECT raw_model_score,calibrated_prob,risk_level,features,explanations FROM scoring_results WHERE txn_id=$1',txn))
                unchanged &= score==before
            record('all_ml_scores_and_features_unchanged','all',unchanged,cases=len(snapshots))
            record('live_model_transport_excludes_private_canaries','all',len(requests)>0,
                   generation_requests=sum(x['kind']=='generation' for x in requests),
                   embedding_requests=sum(x['kind']=='embedding' for x in requests))
        report['passed']=all(c['passed'] for c in checks)
    except Exception as exc:
        report.update(passed=False,error_type=type(exc).__name__)
        raise
    finally:
        for queue in queues:
            await redis.delete(queue.ready,queue.processing,queue.dead)
        await redis.aclose()
        if db.write:
            await db.write.close()
        if schema_created:
            await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()
        report['isolated_schema_and_queues_removed']=True
        (output/'operations.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--api-min-interval',type=float,default=15)
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    if not args.live:
        print('Plan only: --live uses dedicated test services, about 9 real generations/provider, plus injected fault scenarios.')
        return 0
    if not 0 <= args.api_min_interval <= 60:
        raise ValueError('API interval must be between 0 and 60 seconds')
    output=args.output_dir or ROOT/'benchmark_results'/('operations-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    output.mkdir(parents=True,exist_ok=False)
    print('OUTPUT_DIR='+str(output),flush=True)
    try:
        report=asyncio.run(validate(output,args.api_min_interval))
    except Exception as exc:
        print('Operational validation failed: '+type(exc).__name__,flush=True)
        return 1
    print('OPERATIONS_PASSED='+str(report['passed']),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
