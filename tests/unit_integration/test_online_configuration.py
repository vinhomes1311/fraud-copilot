"""Check .env startup behavior and the synthetic smoke command without real APIs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from llm_worker.smoke_test import check


def test_dotenv_loads_before_settings_and_shell_takes_precedence(tmp_path):
    package = tmp_path / 'fraud_common'
    package.mkdir()
    source = Path(__file__).resolve().parents[2] / 'fraud_common/config.py'
    shutil.copyfile(source, package / 'config.py')
    (tmp_path / '.env').write_text(
        'LLM_PROVIDER=api\nLLM_API_MODEL=from-dotenv\nLLM_JOB_TIMEOUT_S=120\n'
        'LLM_VISIBILITY_TIMEOUT_S=300\n', encoding='utf-8')
    code = (
        "import os, runpy, sys; m=runpy.run_path(sys.argv[1]); s=m['get_settings'](); "
        "s.validate_llm_timeouts(); print(s.llm_provider, os.environ['LLM_API_MODEL'])"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith('LLM_')}
    result = subprocess.run([sys.executable, '-B', '-c', code, str(package / 'config.py')],
                            env=env, cwd=tmp_path, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == 'api from-dotenv'
    env['LLM_PROVIDER'] = 'stub'
    env['LLM_API_MODEL'] = 'from-shell'
    result = subprocess.run([sys.executable, '-B', '-c', code, str(package / 'config.py')],
                            env=env, cwd=tmp_path, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == 'stub from-shell'


def api_settings():
    return SimpleNamespace(llm_provider='api', llm_job_timeout_s=30,
                           validate_llm_timeouts=lambda: None)


@pytest.mark.asyncio
async def test_config_check_does_not_make_a_request():
    with patch.dict(os.environ, {'LLM_API_KEY': 'synthetic-key'}, clear=True), \
         patch('llm_worker.smoke_test.get_settings', return_value=api_settings()), \
         patch('llm_worker.api_client.httpx.AsyncClient') as http:
        report = await check()
    http.assert_not_called()
    assert report == {'provider': 'api', 'model': 'gpt-5.4-mini', 'status': 'configuration_valid'}
    assert 'synthetic-key' not in json.dumps(report)


@pytest.mark.asyncio
async def test_live_smoke_uses_synthetic_evidence_and_validates_grounding():
    real_client = httpx.AsyncClient
    captured = []

    def handler(request):
        captured.append(request)
        evidence = json.loads(request.content)['messages'][1]['content']
        evidence = json.loads(evidence)['evidence']
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(
                {'evidence_ids': list(evidence), 'checklist_ids': ['PB-03']})}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}})

    with patch.dict(os.environ, {'LLM_API_KEY': 'synthetic-key'}, clear=True), \
         patch('llm_worker.smoke_test.get_settings', return_value=api_settings()), \
         patch('llm_worker.api_client.httpx.AsyncClient',
               side_effect=lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)):
        report = await check(live=True)
    assert len(captured) == 1
    assert report['status'] == 'api_ok'
    assert report['tokens_used'] == 120
    assert report['brief']['citations'][0]['rule_code'] == 'PB-03'
    assert 'synthetic-smoke' not in captured[0].content.decode()


@pytest.mark.asyncio
async def test_worker_invalid_api_configuration_fails_before_database_connect():
    from llm_worker.main import main
    from fraud_common.config import get_settings
    settings = get_settings()
    from dataclasses import replace
    with patch.dict(os.environ, {'LLM_API_KEY': ''}, clear=True), \
         patch('llm_worker.main.get_settings', return_value=replace(settings, llm_provider='api')), \
         patch('llm_worker.llm_client.get_settings', return_value=replace(settings, llm_provider='api')), \
         patch('llm_worker.main.Database') as database, \
         patch('llm_worker.main.start_http_server') as metrics:
        database.return_value.connect = AsyncMock()
        with pytest.raises(ValueError, match='LLM_API_KEY'):
            await main()
        database.return_value.connect.assert_not_awaited()
        metrics.assert_not_called()
