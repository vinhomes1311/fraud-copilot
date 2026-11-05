"""Compare the project's Ollama/API brief adapters; default is a no-request plan."""
from __future__ import annotations
from llm_worker.retrieval_policy import retrieve_context

import argparse
import asyncio
import copy
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import statistics
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx

import llm_worker.api_client as api_module
import llm_worker.local_integration as local_module
from copilot.providers import SYSTEM_PROMPT
from fraud_common.config import get_settings
from llm_worker.grounding import check_grounding
from llm_worker.rag import ApprovedPlaybookRetriever

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = Path(__file__).with_name('cases.json')
ARCHITECTURE = json.loads(Path(__file__).with_name('llm_architecture.json').read_text(encoding='utf-8'))


def brief_budget():
    return min(get_settings().brief_token_budget, ARCHITECTURE['requirements']['brief_token_limit'])


@dataclass(frozen=True)
class Config:
    providers: tuple[str, ...] = ('ollama', 'api')
    repeats: int = 3
    warmup: int = 1
    concurrency: int = 1
    timeout_s: float = 120
    completion_cap: int = 1024
    max_requests: int = 100
    seed: int = 42
    api_input_price_per_million: float | None = None
    api_output_price_per_million: float | None = None
    scope: str = 'generation'
    retriever: str = 'ollama'

    def validate(self, case_count):
        if self.scope not in ('generation', 'rag-llm') or self.retriever not in ('playbook', 'ollama'):
            raise ValueError('Choose generation or rag-llm scope, with playbook or ollama retrieval')
        if not self.providers or len(set(self.providers)) != len(self.providers):
            raise ValueError('Choose unique providers')
        if any(p not in ('ollama', 'api') for p in self.providers):
            raise ValueError('Providers must be ollama or api')
        if self.repeats < 1 or self.warmup < 0 or not 1 <= self.concurrency <= 64:
            raise ValueError('Require repeats >= 1, warmup >= 0, concurrency in 1..64')
        if not math.isfinite(self.timeout_s) or not 0 < self.timeout_s <= 3600:
            raise ValueError('Timeout must be in (0, 3600] seconds')
        if self.completion_cap <= 0 or not 1 <= self.max_requests <= 10000:
            raise ValueError('Require a positive completion cap and max_requests in 1..10000')
        if case_count < 1 or self.request_count(case_count) > self.max_requests:
            raise ValueError('Planned requests exceed --max-requests, or no cases selected')
        prices = (self.api_input_price_per_million, self.api_output_price_per_million)
        if (prices[0] is None) != (prices[1] is None):
            raise ValueError('Provide both API input and output prices, or neither')
        if any(p is not None and (not math.isfinite(p) or p < 0) for p in prices):
            raise ValueError('Prices must be finite and non-negative')

    def request_count(self, n):
        return self.llm_request_count(n) + self.embedding_request_count(n)

    def llm_request_count(self, n):
        return len(self.providers) * (self.warmup + self.repeats * n)

    def embedding_request_count(self, n):
        return self.llm_request_count(n) if self.scope == 'rag-llm' and self.retriever == 'ollama' else 0


def load_cases(path=DEFAULT_CASES, selected=None):
    cases = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(cases, list) or not cases:
        raise ValueError('Case file must contain a non-empty JSON array')
    ids = set()
    allowed_rules = {c['rule_code'] for c in json.loads(local_module.KB_PATH.read_text(encoding='utf-8'))['chunks']}
    for case in cases:
        name = case.get('id') if isinstance(case, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', name) or name in ids:
            raise ValueError('Case IDs must be unique and contain only letters, digits, underscore or dash')
        ids.add(name)
        local_module.project_evidence(case['context'])
        reference = case.get('reference_any_rules', [])
        if not isinstance(reference, list) or not set(reference) <= allowed_rules:
            raise ValueError('Invalid reference rules in case file')
    if selected:
        if not set(selected) <= ids:
            raise ValueError('Unknown --case-ids')
        cases = [case for case in cases if case['id'] in selected]
    return cases


class Recorder:
    usage = None
    native = None

    async def chat(self, *args):
        reply = await super().chat(*args)
        counts = (reply.get('prompt_eval_count'), reply.get('eval_count'))
        if all(type(n) is int and n >= 0 for n in counts):
            self.usage = {'input_tokens': counts[0], 'output_tokens': counts[1],
                          'total_tokens': sum(counts)}
        self.native = {key: reply[key] / 1e9 for key in
                       ('load_duration', 'prompt_eval_duration', 'eval_duration', 'total_duration')
                       if type(reply.get(key)) is int and reply[key] >= 0}
        return reply


class MeasuredAPI(Recorder, api_module.APIClient):
    pass


class MeasuredOllama(Recorder, local_module.OllamaClient):
    def __init__(self, model, cap):
        super().__init__(max_tokens=cap)
        self.model = model

    def model_name(self):
        return self.model


def classify_error(exc):
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return 'timeout', False
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    if status is None:
        match = re.search(r'LLM API HTTP (\d{3})', str(exc))
        status = int(match[1]) if match else None
    if status == 429:
        billing = ('insufficient_quota', 'credit_balance_exhausted', 'spend_limit_exceeded',
                   'organization_usage_limit_exceeded')
        return ('quota' if any(code in str(exc) for code in billing) else 'rate_limit'), True
    if status is not None:
        return f'http_{status}', 400 <= status < 500
    if isinstance(exc, httpx.RequestError) or str(exc) == 'LLM API connection failed':
        return 'connection', True
    if isinstance(exc, ValueError):
        return 'invalid_response', False
    return 'generation_error', False


async def measure(provider, factory, case, chunks, config, phase, repeat, retriever=None):
    started = time.perf_counter()
    client = None
    row = {'provider': provider, 'case_id': case['id'], 'phase': phase, 'repeat': repeat,
           'ok': False, 'error_kind': None, 'fatal': False, 'model': None,
           'input_tokens': None, 'output_tokens': None, 'total_tokens': None,
           'reference_match': None, 'checklist_rules': [], 'native_seconds': {},
           'ollama_eval_tokens_per_s': None, 'retrieval_started': False, 'generation_started': False,
           'retrieval_s': None, 'generation_s': None, 'validation_s': None,
           'failure_stage': None, 'within_brief_budget': None}
    stage = 'configuration'
    try:
        client = factory()
        row['model'] = client.model_name()
        context = copy.deepcopy(case['context'])
        active_chunks = chunks
        if retriever is not None:
            stage = 'retrieval'
            row['retrieval_started'] = True
            timer = time.perf_counter()
            try:
                signals = [e['feature'] for e in context.get('explanations', [])]
                active_chunks = await asyncio.wait_for(
                    retrieve_context(retriever, context, top_k=5), config.timeout_s)
            finally:
                row['retrieval_s'] = time.perf_counter() - timer
        row['retrieved_rules'] = [c.rule_code for c in active_chunks]
        stage = 'generation'
        row['generation_started'] = True
        timer = time.perf_counter()
        try:
            result = await asyncio.wait_for(client.generate(context, active_chunks), config.timeout_s)
        finally:
            row['generation_s'] = time.perf_counter() - timer
        stage = 'validation'
        timer = time.perf_counter()
        check_grounding(result.brief, active_chunks)
        row['within_brief_budget'] = result.tokens_used <= brief_budget()
        if not row['within_brief_budget']:
            row['failure_stage'] = 'budget'
            row['error_kind'] = 'token_budget'
        else:
            row['ok'] = True
            row['checklist_rules'] = [item.rule_code for item in result.brief.checklist]
            reference = case.get('reference_any_rules', [])
            if reference:
                row['reference_match'] = bool(set(reference) & set(row['checklist_rules']))
        row['validation_s'] = time.perf_counter() - timer
    except Exception as exc:
        row['error_kind'], row['fatal'] = classify_error(exc)
        row['failure_stage'] = stage
    row['latency_s'] = time.perf_counter() - started
    if client is not None:
        row.update(client.usage or {})
        row['native_seconds'] = client.native or {}
        duration = row['native_seconds'].get('eval_duration')
        if provider == 'ollama' and duration and row['output_tokens'] is not None:
            row['ollama_eval_tokens_per_s'] = row['output_tokens'] / duration
    return row


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def latency_stats(rows):
    values = [r['latency_s'] for r in rows]
    return {'mean': statistics.mean(values) if values else None,
            'p50': percentile(values, .5), 'p95': percentile(values, .95),
            'min': min(values) if values else None, 'max': max(values) if values else None}


def summarize(rows, planned, elapsed, config):
    measured = [row for row in rows if row['phase'] == 'measured']
    valid = [row for row in measured if row['ok']]
    known = [row for row in rows if row['total_tokens'] is not None]
    references = [row['reference_match'] for row in valid if row['reference_match'] is not None]
    totals = {key: sum(row[key] for row in known) for key in
              ('input_tokens', 'output_tokens', 'total_tokens')}
    cost = None
    if rows and rows[0]['provider'] == 'api' and config.api_input_price_per_million is not None:
        cost = (totals['input_tokens'] * config.api_input_price_per_million +
                totals['output_tokens'] * config.api_output_price_per_million) / 1e6
    return {'planned_measured': planned, 'attempted_measured': len(measured),
            'skipped_measured': planned - len(measured), 'valid_briefs': len(valid),
            'success_rate': len(valid) / len(measured) if measured else None,
            'latency_success_s': latency_stats(valid), 'latency_all_attempts_s': latency_stats(measured),
            'measured_wall_s': elapsed, 'valid_briefs_per_s': len(valid) / elapsed if elapsed else None,
            'attempts_per_s': len(measured) / elapsed if elapsed else None,
            'reported_output_tokens_per_wall_s': sum(row['output_tokens'] or 0 for row in valid) / elapsed if elapsed else None,
            'reference_match_rate': sum(references) / len(references) if references else None,
            'warmup_attempts': len(rows) - len(measured), 'known_usage_all_phases': totals,
            'attempts_with_unknown_usage': sum(r.get('generation_started', True) and r['total_tokens'] is None for r in rows),
            'generation_attempts_all_phases': sum(r.get('generation_started', True) for r in rows),
            'retrieval_attempts_all_phases': sum(r.get('retrieval_started', False) for r in rows),
            'retrieval_latency_s': latency_stats([dict(r, latency_s=r['retrieval_s']) for r in measured
                                                if r.get('retrieval_s') is not None]),
            'generation_latency_s': latency_stats([dict(r, latency_s=r['generation_s']) for r in measured
                                                 if r.get('generation_s') is not None]),
            'estimated_api_usd_known_usage_uncached': cost,
            'stopped_after': next((row['error_kind'] for row in rows if row['fatal']), None),
            'errors': {kind: sum(r['error_kind'] == kind for r in measured)
                       for kind in sorted({r['error_kind'] for r in measured if r['error_kind']})}}


async def run_benchmark(config, cases, factories, mode, retriever=None):
    config.validate(len(cases))
    chunks = await ApprovedPlaybookRetriever().retrieve([], 'MEDIUM')
    if config.scope == 'rag-llm' and retriever is None:
        retriever = local_module.SemanticRetriever() if config.retriever == 'ollama' else ApprovedPlaybookRetriever()
    jobs = [(repeat, case) for repeat in range(config.repeats) for case in cases]
    random.Random(config.seed).shuffle(jobs)
    report = {'mode': mode, 'created_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': ('brief generation with shared approved playbook; excludes embeddings, queues and DB'
                        if config.scope == 'generation' else 'retrieval -> LLM -> grounding; excludes queue and DB'),
              'brief_token_budget': brief_budget(),
              'configured_brief_token_budget': get_settings().brief_token_budget,
              'architecture_reference': copy.deepcopy(ARCHITECTURE),
              'architecture_coverage': {
                  'semantic_retrieval_timed': config.scope == 'rag-llm' and config.retriever == 'ollama',
                  'worker_retry_fallback_persistence_timed': False,
                  'hot_path_500ms_sla_applied': False,
                  'llm_latency_sla_s': None,
                  'evaluation': 'partial Cold Path only; not full architecture acceptance'},
              'retrieval_options': {
                  'kind': config.retriever if config.scope == 'rag-llm' else 'shared-playbook',
                  'embedding_model': (os.getenv('OLLAMA_EMBED_MODEL', 'embeddinggemma:300m')
                                      if config.scope == 'rag-llm' and config.retriever == 'ollama' else None),
                    'top_k': 5,
                    'document_cache': 'per-retriever memory; endpoint/model/text keyed; queries always embedded',
                    'document_cache_ttl_s': float(os.getenv('OLLAMA_EMBED_CACHE_TTL_S', '300'))},
              'config': asdict(config), 'case_ids': [case['id'] for case in cases],
              'cases_sha256': hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
              'playbook_sha256': hashlib.sha256(local_module.KB_PATH.read_bytes()).hexdigest(),
              'base_system_prompt_sha256': hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest(),
              'ollama_system_prompt_sha256': hashlib.sha256(local_module.OLLAMA_SELECTION_PROMPT.encode()).hexdigest(),
              'adapter_source_sha256': {
                  'api': hashlib.sha256(Path(api_module.__file__).read_bytes()).hexdigest(),
                  'ollama': hashlib.sha256(Path(local_module.__file__).read_bytes()).hexdigest()},
              'host': {'platform': platform.system(), 'machine': platform.machine(),
                       'processor': platform.processor(), 'cpu_count': os.cpu_count(),
                       'python': platform.python_version()}, 'providers': {}, 'samples': [],
              'provider_options': {}}
    for provider in config.providers:
        prototype = factories[provider]()
        options = {'model': prototype.model_name(), 'completion_cap': config.completion_cap}
        if provider == 'api':
            options.update(selection_contract='evidence-and-checklist-v1',
                           evidence_source='validated-model-selection',
                           response_format=prototype.response_format,
                           token_parameter=prototype.token_parameter,
                           reasoning_effort=prototype.reasoning_effort or 'provider_default',
                           temperature='provider_default')
        else:
            options.update(selection_contract='checklist-only-v1', evidence_source='backend-all-evidence',
                             response_format='json_schema', temperature=0,
                             keep_alive=os.getenv('OLLAMA_KEEP_ALIVE', '10m'))
        report['provider_options'][provider] = options
    for provider in config.providers:
        rows = []
        stopped = False
        for index in range(config.warmup):
            row = await measure(provider, factories[provider], cases[index % len(cases)],
                                chunks, config, 'warmup', index, retriever)
            rows.append(row)
            if row['fatal']:
                stopped = True
                break
        started = time.perf_counter()
        queue = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)

        async def worker():
            nonlocal stopped
            while not stopped:
                try:
                    repeat, case = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                row = await measure(provider, factories[provider], case, chunks, config, 'measured', repeat, retriever)
                rows.append(row)
                if row['fatal']:
                    stopped = True

        await asyncio.gather(*(worker() for _ in range(config.concurrency)))
        elapsed = time.perf_counter() - started
        report['providers'][provider] = summarize(rows, len(jobs), elapsed, config)
        report['samples'].extend(rows)
    return report


def mock_response(request):
    payload = json.loads(request.content)
    if request.url.path.endswith('/api/embed'):
        return httpx.Response(200, json={'embeddings': [[1.0, float(i + 1)] for i in range(len(payload['input']))]})
    evidence = json.loads(payload['messages'][1]['content'])['evidence']
    ids = ['PB-03', 'PB-05'] if 'EV-HISTORY-GAP' in evidence else ['PB-01', 'PB-03']
    available = [c['id'] for c in json.loads(payload['messages'][1]['content'])['playbook']]
    ids = [i for i in ids if i in available] or available[:1]
    content = json.dumps({'evidence_ids': list(evidence), 'checklist_ids': ids})
    if request.url.path.endswith('/chat/completions'):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})
    content = json.dumps({'checklist_ids': ids})
    return httpx.Response(200, json={'done': True, 'message': {'content': content},
        'prompt_eval_count': 100, 'eval_count': 20, 'load_duration': 1000000,
        'eval_duration': 2000000, 'prompt_eval_duration': 1000000, 'total_duration': 4000000})


def factories_for(config, mode, ollama_model, api_model):
    if ('ollama' in config.providers and not ollama_model.strip() or
            'api' in config.providers and not api_model.strip()):
        raise ValueError('Model names must not be empty')
    if mode == 'live' and ('ollama' in config.providers or
                           config.scope == 'rag-llm' and config.retriever == 'ollama'):
        url = urlsplit(os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434'))
        if (url.scheme not in ('http', 'https') or url.hostname not in
                ('127.0.0.1', 'localhost', '::1', 'host.docker.internal') or
                url.username or url.password or url.query or url.fragment):
            raise ValueError('Ollama benchmark requires a local OLLAMA_BASE_URL without credentials or query')
    def make_api():
        overrides = {'LLM_API_MODEL': api_model}
        if mode == 'mock':
            overrides.update(LLM_API_BASE_URL='https://mock.example/v1', LLM_API_KEY='synthetic-benchmark-key')
        with patch.dict(os.environ, overrides):
            client = MeasuredAPI()
        client.max_tokens = config.completion_cap
        return client
    factories = {'ollama': lambda: MeasuredOllama(ollama_model, config.completion_cap), 'api': make_api}
    # Check credentials/configuration before sending any real request to either provider.
    for provider in config.providers:
        factories[provider]()
    return factories


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'results.json').write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
    fields = ['provider', 'model', 'case_id', 'phase', 'repeat', 'ok', 'error_kind', 'latency_s',
              'input_tokens', 'output_tokens', 'total_tokens', 'reference_match', 'ollama_eval_tokens_per_s',
              'retrieval_s', 'generation_s', 'validation_s', 'failure_stage', 'within_brief_budget']
    with (output / 'samples.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(report['samples'])
    lines = [f"# LLM benchmark ({report['mode']})", '', report['scope'], '',
             '| Provider | Valid / attempted | Skipped | Success | p50 (s) | p95 (s) | Valid briefs/s | Stop reason |',
             '|---|---:|---:|---:|---:|---:|---:|---|']
    def fmt(value):
        return '-' if value is None else f'{value:.4f}'
    for provider, result in report['providers'].items():
        lat = result['latency_success_s']
        rate = '-' if result['success_rate'] is None else f"{result['success_rate']:.1%}"
        lines.append(f"| {provider} | {result['valid_briefs']} / {result['attempted_measured']} | "
                     f"{result['skipped_measured']} | {rate} | {fmt(lat['p50'])} | {fmt(lat['p95'])} | "
                     f"{fmt(result['valid_briefs_per_s'])} | {result['stopped_after'] or '-'} |")
    lines += ['', 'Warm-up is excluded from latency/throughput but included in known usage/cost.',
              ('Mock timings are synthetic and cannot establish which model is faster.' if report['mode'] == 'mock'
               else 'Live results require multiple samples/runs before drawing performance conclusions.'),
              'Cost is an uncached-price estimate of known usage, not an invoice; failed requests may have unknown usage.',
              'Tokenizers/reasoning differ. Reference-rule matches are fixture heuristics, not fraud accuracy.',
              'Local hardware/energy cost, streaming TTFT and queue/DB latency are not measured.',
              'Retrieval is timed only in rag-llm scope. The architecture has no numeric LLM latency SLA; 500 ms belongs to Hot Path.',
              '', 'See results.json for failures, skipped requests, native Ollama durations and full configuration.']
    (output / 'summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true', help='Call real models; API requests can be billable.')
    mode.add_argument('--mock', action='store_true', help='Exercise both adapters with offline MockTransport.')
    parser.add_argument('--providers', nargs='+', choices=('ollama', 'api'), default=['ollama', 'api'])
    parser.add_argument('--scope', choices=('generation', 'rag-llm'), default='generation')
    parser.add_argument('--retriever', choices=('playbook', 'ollama'), default='ollama',
                        help='Shared retriever for rag-llm scope; ollama uses semantic embeddings.')
    parser.add_argument('--cases', type=Path, default=DEFAULT_CASES)
    parser.add_argument('--case-ids', nargs='+')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=1)
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--completion-cap', type=int, default=1024)
    parser.add_argument('--max-requests', type=int, default=100)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--ollama-model', default=os.getenv('OLLAMA_MODEL') or 'qwen2.5:3b')
    parser.add_argument('--api-model', default=os.getenv('LLM_API_MODEL') or 'gpt-5.4-mini')
    parser.add_argument('--api-input-price-per-million', type=float)
    parser.add_argument('--api-output-price-per-million', type=float)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args(argv)
    config = Config(tuple(args.providers), args.repeats, args.warmup, args.concurrency,
                    args.timeout, args.completion_cap, args.max_requests, args.seed,
                    args.api_input_price_per_million, args.api_output_price_per_million,
                    args.scope, args.retriever)
    try:
        cases = load_cases(args.cases, args.case_ids)
        config.validate(len(cases))
        if not args.live and not args.mock:
            print(json.dumps({'mode': 'plan', 'requests': config.request_count(len(cases)),
                'llm_requests': config.llm_request_count(len(cases)),
                'embedding_requests': config.embedding_request_count(len(cases)),
                'scope': config.scope,
                'effective_retriever': config.retriever if config.scope == 'rag-llm' else 'shared-playbook',
                'embedding_model': (os.getenv('OLLAMA_EMBED_MODEL', 'embeddinggemma:300m')
                                    if config.scope == 'rag-llm' and config.retriever == 'ollama' else None),
                'architecture_source': ARCHITECTURE['source_url'],
                'brief_token_limit': brief_budget(), 'llm_latency_sla_s': None,
                'measured_per_provider': config.repeats * len(cases), 'warmup_per_provider': config.warmup,
                'case_ids': [case['id'] for case in cases], 'config': asdict(config),
                'models': {'ollama': args.ollama_model, 'api': args.api_model},
                'note': 'No requests sent. Use --mock offline or --live for real model measurements.'}, indent=2))
            return 0
        output = args.output_dir or ROOT / 'benchmark_results' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        if output.exists():
            raise ValueError('Output directory already exists; choose a new directory')
        mode_name = 'live' if args.live else 'mock'
        factories = factories_for(config, mode_name, args.ollama_model, args.api_model)
        # Verify output access before a potentially billable request is sent.
        output.mkdir(parents=True, exist_ok=False)
        probe = output / '.write-check'
        probe.write_text('benchmark output check', encoding='utf-8')
        probe.unlink()
        real_client = httpx.AsyncClient
        def http_factory(**kwargs):
            kwargs['timeout'] = config.timeout_s
            if mode_name == 'mock':
                kwargs['transport'] = httpx.MockTransport(mock_response)
            return real_client(**kwargs)
        wrapper = SimpleNamespace(AsyncClient=http_factory,
            TimeoutException=httpx.TimeoutException, RequestError=httpx.RequestError)
        with patch.object(api_module, 'httpx', wrapper), patch.object(local_module, 'httpx', wrapper):
            report = asyncio.run(run_benchmark(config, cases, factories, mode_name))
        write_report(report, output)
        print((output / 'summary.md').read_text(encoding='utf-8'))
        print('Report: ' + str(output.resolve()))
        return 0 if all(r['success_rate'] == 1 for r in report['providers'].values()) else 1
    except Exception:
        # Do not expose exception messages that may contain provider URLs or credentials.
        print('Benchmark setup/report failed. Check arguments, case schema, provider configuration and output permissions.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
