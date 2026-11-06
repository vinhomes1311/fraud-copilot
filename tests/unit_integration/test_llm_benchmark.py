import csv
import json
from pathlib import Path

import httpx
import pytest

from benchmarks import compare_llm as bench


def report_at(path):
    return json.loads((path / 'results.json').read_text(encoding='utf-8'))


def test_default_only_plans_without_key_or_transport(monkeypatch, capsys):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    def forbidden(*args, **kwargs):
        raise AssertionError('Plan must not construct clients')
    monkeypatch.setattr(bench, 'factories_for', forbidden)
    assert bench.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['mode'] == 'plan'
    assert plan['requests'] == 32
    assert plan['measured_per_provider'] == 15
    assert plan['warmup_per_provider'] == 1


def test_mock_both_transports_receive_same_case_playbook_and_cap(tmp_path, monkeypatch, capsys):
    output = tmp_path / 'report'
    original = bench.mock_response
    requests = []
    def capture(request):
        requests.append(request)
        return original(request)
    monkeypatch.setattr(bench, 'mock_response', capture)
    monkeypatch.setenv('LLM_API_KEY', 'DO-NOT-SAVE-THIS-KEY')
    assert bench.main(['--mock', '--case-ids', 'history-gap', '--repeats', '2', '--warmup', '1',
                       '--completion-cap', '700', '--output-dir', str(output)]) == 0
    report = report_at(output)
    assert report['mode'] == 'mock'
    assert len(requests) == len(report['samples']) == 6
    bodies = [json.loads(request.content) for request in requests]
    contexts = [json.loads(body['messages'][1]['content']) for body in bodies]
    assert all(context == contexts[0] for context in contexts)
    assert 'synthetic-history-gap' not in requests[0].content.decode()
    for body in bodies:
        if 'options' in body:
            assert body['options']['num_predict'] == 700
        else:
            assert body['max_completion_tokens'] == 700
    for provider in ('ollama', 'api'):
        summary = report['providers'][provider]
        assert summary['attempted_measured'] == 2 and summary['valid_briefs'] == 2
        assert summary['warmup_attempts'] == 1
        assert summary['known_usage_all_phases']['total_tokens'] == 360
        assert summary['reference_match_rate'] == 1
        assert summary['latency_success_s']['p95'] >= 0
        assert summary['estimated_api_usd_known_usage_uncached'] is None
    ollama = next(row for row in report['samples'] if row['provider'] == 'ollama')
    assert ollama['native_seconds']['eval_duration'] == .002
    with (output / 'samples.csv').open(encoding='utf-8', newline='') as handle:
        assert len(list(csv.DictReader(handle))) == 6
    assert 'Mock timings' in (output / 'summary.md').read_text()
    for artifact in output.iterdir():
        assert 'DO-NOT-SAVE-THIS-KEY' not in artifact.read_text()
    assert 'DO-NOT-SAVE-THIS-KEY' not in capsys.readouterr().out


@pytest.mark.parametrize('options', [
    ['--repeats', '0'], ['--warmup', '-1'], ['--concurrency', '0'],
    ['--timeout', 'nan'], ['--timeout', 'inf'], ['--completion-cap', '0'],
    ['--max-requests', '1'], ['--api-input-price-per-million', '1'],
    ['--api-input-price-per-million', '-1', '--api-output-price-per-million', '2'],
])
def test_invalid_limits_block_live_calls(options, monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid plan must not construct clients')
    monkeypatch.setattr(bench, 'factories_for', forbidden)
    assert bench.main(['--live', '--output-dir', str(tmp_path / 'report'), *options]) == 2
    assert not (tmp_path / 'report').exists()


def test_metrics_exclude_warmup_but_cost_includes_known_failed_usage():
    def row(phase, ok, latency, tokens=True):
        return {'provider': 'api', 'phase': phase, 'ok': ok, 'latency_s': latency,
                'input_tokens': 100 if tokens else None, 'output_tokens': 20 if tokens else None,
                'total_tokens': 120 if tokens else None, 'reference_match': True if ok else None,
                'error_kind': None if ok else 'invalid_response', 'fatal': False}
    rows = [row('warmup', True, 99), row('measured', True, 1), row('measured', True, 3),
            row('measured', False, .01)]
    config = bench.Config(api_input_price_per_million=1, api_output_price_per_million=2)
    result = bench.summarize(rows, 3, 4.5, config)
    assert result['success_rate'] == pytest.approx(2 / 3)
    assert result['latency_success_s']['p50'] == 2
    assert result['latency_success_s']['p95'] == pytest.approx(2.9)
    assert result['known_usage_all_phases']['total_tokens'] == 480
    assert result['estimated_api_usd_known_usage_uncached'] == pytest.approx(.00056)
    assert result['reported_output_tokens_per_wall_s'] == pytest.approx(40 / 4.5)
    assert result['errors'] == {'invalid_response': 1}


def test_quota_stops_api_without_stopping_ollama_or_leaking_errors(tmp_path, monkeypatch, capsys):
    output = tmp_path / 'quota'
    original = bench.mock_response
    api_requests = []
    def handler(request):
        if request.url.path.endswith('/chat/completions'):
            api_requests.append(request)
            return httpx.Response(429, json={'error': {'code': 'credit_balance_exhausted',
                'message': 'PRIVATE-CONTENT SECRET-KEY'}})
        return original(request)
    monkeypatch.setattr(bench, 'mock_response', handler)
    assert bench.main(['--mock', '--repeats', '2', '--output-dir', str(output)]) == 1
    report = report_at(output)
    assert len(api_requests) == 1
    api = report['providers']['api']
    assert api['stopped_after'] == 'quota'
    assert api['attempted_measured'] == 0 and api['skipped_measured'] == 10
    assert api['success_rate'] is None and api['latency_success_s']['p50'] is None
    assert api['attempts_with_unknown_usage'] == 1
    assert report['providers']['ollama']['success_rate'] == 1
    text = ''.join(path.read_text() for path in output.iterdir()) + capsys.readouterr().out
    assert 'PRIVATE-CONTENT' not in text and 'SECRET-KEY' not in text


def test_invalid_selection_accounts_for_usage_even_when_brief_fails(tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': '{"evidence_ids":[],"checklist_ids":["FAKE"]}'}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})
    monkeypatch.setattr(bench, 'mock_response', handler)
    output = tmp_path / 'invalid'
    assert bench.main(['--mock', '--providers', 'api', '--case-ids', 'history-gap',
                       '--repeats', '1', '--warmup', '0', '--output-dir', str(output)]) == 1
    report = report_at(output)
    sample = report['samples'][0]
    assert sample['error_kind'] == 'invalid_response' and not sample['ok']
    assert sample['total_tokens'] == 120
    assert report['providers']['api']['attempts_with_unknown_usage'] == 0


def test_timeout_is_recorded_without_automatic_retry(tmp_path, monkeypatch):
    import asyncio
    original = bench.mock_response
    called = 0
    async def handler(request):
        nonlocal called
        called += 1
        await asyncio.sleep(.05)
        return original(request)
    monkeypatch.setattr(bench, 'mock_response', handler)
    output = tmp_path / 'timeout'
    assert bench.main(['--mock', '--providers', 'api', '--case-ids', 'history-gap',
                       '--repeats', '1', '--warmup', '0', '--timeout', '.005',
                       '--output-dir', str(output)]) == 1
    assert called == 1
    assert report_at(output)['samples'][0]['error_kind'] == 'timeout'


def test_concurrency_is_bounded_and_all_cases_are_scheduled(tmp_path, monkeypatch):
    import asyncio
    original = bench.mock_response
    active = peak = 0
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.01)
            return original(request)
        finally:
            active -= 1
    monkeypatch.setattr(bench, 'mock_response', handler)
    output = tmp_path / 'parallel'
    assert bench.main(['--mock', '--providers', 'ollama', '--repeats', '1', '--warmup', '0',
                       '--concurrency', '2', '--output-dir', str(output)]) == 0
    assert peak == 2
    report = report_at(output)
    assert len(report['samples']) == 5
    assert {row['case_id'] for row in report['samples']} == set(report['case_ids'])


def test_existing_output_blocks_calls_and_local_endpoint_is_required(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Existing output must block before client construction')
    with monkeypatch.context() as patcher:
        patcher.setattr(bench, 'factories_for', forbidden)
        assert bench.main(['--live', '--output-dir', str(tmp_path)]) == 2
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://user:SECRET@remote.example/?key=SECRET')
    assert bench.main(['--live', '--providers', 'ollama', '--output-dir', str(tmp_path / 'new')]) == 2


def test_percentile_empty_single_and_interpolation():
    assert bench.percentile([], .95) is None
    assert bench.percentile([2], .95) == 2
    assert bench.percentile([3, 1], .5) == 2


def test_output_permissions_are_checked_before_model_requests(tmp_path, monkeypatch):
    original = Path.write_text
    def denied(path, *args, **kwargs):
        if path.name == '.write-check':
            raise PermissionError('synthetic output permission error')
        return original(path, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError('No model requests may run when output is unwritable')
    monkeypatch.setattr(Path, 'write_text', denied)
    monkeypatch.setattr(bench, 'run_benchmark', forbidden)
    assert bench.main(['--mock', '--output-dir', str(tmp_path / 'denied')]) == 2


def test_case_ids_are_unique_and_context_is_validated(tmp_path):
    fixture = bench.load_cases()
    with pytest.raises(ValueError, match='Unknown'):
        bench.load_cases(selected=['missing'])
    path = tmp_path / 'cases.json'
    path.write_text(json.dumps([fixture[0], fixture[0]]), encoding='utf-8')
    with pytest.raises(ValueError, match='unique'):
        bench.load_cases(path)


def test_rag_llm_times_semantic_retrieval_for_both_providers(tmp_path, monkeypatch):
    original = bench.mock_response
    requests = []
    def capture(request):
        requests.append(request)
        return original(request)
    monkeypatch.setattr(bench, 'mock_response', capture)
    output = tmp_path / 'rag'
    assert bench.main(['--mock', '--scope', 'rag-llm', '--case-ids', 'history-gap',
                       '--repeats', '1', '--warmup', '0', '--max-requests', '4',
                       '--output-dir', str(output)]) == 0
    report = report_at(output)
    assert len(requests) == 4
    assert sum(request.url.path.endswith('/api/embed') for request in requests) == 2
    assert report['architecture_coverage']['semantic_retrieval_timed'] is True
    assert report['retrieval_options']['kind'] == 'ollama'
    assert report['retrieval_options']['embedding_model']
    assert report['provider_options']['ollama']['selection_contract'] == 'checklist-only-v1'
    assert report['provider_options']['ollama']['evidence_source'] == 'backend-all-evidence'
    assert report['provider_options']['api']['selection_contract'] == 'evidence-and-checklist-v1'
    assert report['ollama_system_prompt_sha256']
    assert report['architecture_coverage']['hot_path_500ms_sla_applied'] is False
    assert report['architecture_coverage']['llm_latency_sla_s'] is None
    assert report['architecture_reference']['requirements']['brief_token_limit'] == 5000
    for row in report['samples']:
        assert row['ok'] and row['within_brief_budget']
        assert row['retrieval_s'] > 0 and row['generation_s'] > 0
        assert row['latency_s'] >= row['retrieval_s'] + row['generation_s']
    for provider in ('ollama', 'api'):
        assert report['providers'][provider]['retrieval_attempts_all_phases'] == 1
        assert report['providers'][provider]['generation_attempts_all_phases'] == 1


def test_rag_failure_never_calls_generation_or_marks_usage_unknown(tmp_path, monkeypatch):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        assert request.url.path.endswith('/api/embed')
        return httpx.Response(200, json={'embeddings': []})
    monkeypatch.setattr(bench, 'mock_response', handler)
    output = tmp_path / 'rag-failed'
    assert bench.main(['--mock', '--scope', 'rag-llm', '--providers', 'api', '--case-ids', 'history-gap',
                       '--repeats', '1', '--warmup', '0', '--output-dir', str(output)]) == 1
    report = report_at(output)
    assert paths == ['/api/embed']
    assert report['samples'][0]['failure_stage'] == 'retrieval'
    assert report['samples'][0]['error_kind'] == 'invalid_response'
    assert report['providers']['api']['generation_attempts_all_phases'] == 0
    assert report['providers']['api']['attempts_with_unknown_usage'] == 0


def test_rag_timeout_is_separate_from_generation_timeout(tmp_path, monkeypatch):
    import asyncio
    paths = []
    async def handler(request):
        paths.append(request.url.path)
        await asyncio.sleep(.05)
        return httpx.Response(200, json={'embeddings': []})
    monkeypatch.setattr(bench, 'mock_response', handler)
    output = tmp_path / 'rag-timeout'
    assert bench.main(['--mock', '--scope', 'rag-llm', '--providers', 'api', '--case-ids', 'history-gap',
                       '--repeats', '1', '--warmup', '0', '--timeout', '.005',
                       '--output-dir', str(output)]) == 1
    assert paths == ['/api/embed']
    row = report_at(output)['samples'][0]
    assert row['failure_stage'] == 'retrieval' and row['error_kind'] == 'timeout'
    assert not row['generation_started']


def test_architecture_budget_remains_5000_if_worker_setting_is_higher(tmp_path, monkeypatch):
    from types import SimpleNamespace
    original = bench.mock_response
    def handler(request):
        data = original(request).json()
        data['usage'] = {'prompt_tokens': 4981, 'completion_tokens': 20, 'total_tokens': 5001}
        return httpx.Response(200, json=data)
    monkeypatch.setattr(bench, 'mock_response', handler)
    monkeypatch.setattr(bench, 'get_settings', lambda: SimpleNamespace(brief_token_budget=10000))
    output = tmp_path / 'budget'
    assert bench.main(['--mock', '--providers', 'api', '--case-ids', 'history-gap',
                       '--repeats', '1', '--warmup', '0', '--output-dir', str(output)]) == 1
    report = report_at(output)
    assert report['brief_token_budget'] == 5000
    assert report['configured_brief_token_budget'] == 10000
    row = report['samples'][0]
    assert row['failure_stage'] == 'budget' and row['error_kind'] == 'token_budget'
    assert row['total_tokens'] == 5001 and row['within_brief_budget'] is False


def test_rag_plan_counts_embedding_calls_in_request_budget(monkeypatch, capsys):
    assert bench.main(['--scope', 'rag-llm']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['requests'] == 64
    assert plan['llm_requests'] == plan['embedding_requests'] == 32
    assert plan['llm_latency_sla_s'] is None
    def forbidden(*args, **kwargs):
        raise AssertionError('Request budget must be checked before clients are created')
    monkeypatch.setattr(bench, 'factories_for', forbidden)
    assert bench.main(['--mock', '--scope', 'rag-llm', '--providers', 'api',
                       '--case-ids', 'history-gap', '--repeats', '1', '--warmup', '0',
                       '--max-requests', '1']) == 2


def test_rag_playbook_mode_reports_semantic_retrieval_was_not_measured(tmp_path):
    output = tmp_path / 'playbook'
    assert bench.main(['--mock', '--scope', 'rag-llm', '--retriever', 'playbook',
                       '--case-ids', 'history-gap', '--repeats', '1', '--warmup', '0',
                       '--max-requests', '2', '--output-dir', str(output)]) == 0
    report = report_at(output)
    assert not report['architecture_coverage']['semantic_retrieval_timed']
    assert all(row['retrieval_started'] and row['ok'] for row in report['samples'])
