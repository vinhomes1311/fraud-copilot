"""Semantic retrieval caching must preserve ranking and reject invalid vectors."""
import asyncio
import os
from unittest.mock import patch

import pytest

from llm_worker.local_integration import OllamaClient, SemanticRetriever

DOC_BATCH = len(SemanticRetriever().kb["chunks"]) + 1


def embeddings(path, payload):
    return {'embeddings': [[1.0, 0.5] for _ in payload['input']]}


@pytest.mark.asyncio
async def test_only_documents_are_cached_and_queries_always_run():
    retriever = SemanticRetriever()
    with patch('llm_worker.local_integration.request', side_effect=embeddings) as http:
        first = await retriever.retrieve(['amt'], 'HIGH', top_k=3)
        second = await retriever.retrieve(['amt'], 'HIGH', top_k=3)
        await retriever.retrieve(['txn_count_24h'], 'MEDIUM')
    assert first == second
    assert [len(call.args[1]['input']) for call in http.call_args_list] == [DOC_BATCH, 1, 1]
    assert 'prior account transactions' in http.call_args_list[-1].args[1]['input'][0]


@pytest.mark.asyncio
async def test_model_endpoint_and_document_changes_invalidate_cache():
    retriever = SemanticRetriever()
    with patch('llm_worker.local_integration.request', side_effect=embeddings) as http:
        await retriever.retrieve([], 'HIGH')
        with patch.dict(os.environ, {'OLLAMA_EMBED_MODEL': 'another-model'}):
            await retriever.retrieve([], 'HIGH')
            with patch.dict(os.environ, {'OLLAMA_BASE_URL': 'http://another-host:11434'}):
                await retriever.retrieve([], 'HIGH')
                retriever.kb['chunks'][0]['text'] += ' Updated principle.'
                await retriever.retrieve([], 'HIGH')
    assert [len(call.args[1]['input']) for call in http.call_args_list] == [DOC_BATCH] * 4


@pytest.mark.asyncio
async def test_cache_expiry_and_disable_rebuild_documents():
    with patch('llm_worker.local_integration.time') as clock:
        clock.monotonic.return_value = 0
        retriever = SemanticRetriever()
        with patch('llm_worker.local_integration.request', side_effect=embeddings) as http:
            await retriever.retrieve([], 'HIGH')
            clock.monotonic.return_value = retriever.cache_ttl_s + 1
            await retriever.retrieve([], 'HIGH')
            retriever.cache_ttl_s = 0
            retriever._document_cache = None
            await retriever.retrieve([], 'HIGH')
            await retriever.retrieve([], 'HIGH')
    assert all(len(call.args[1]['input']) == DOC_BATCH for call in http.call_args_list)


@pytest.mark.asyncio
async def test_concurrent_jobs_share_document_fill():
    async def slow_embeddings(path, payload):
        await asyncio.sleep(0)
        return embeddings(path, payload)
    retriever = SemanticRetriever()
    with patch('llm_worker.local_integration.request', side_effect=slow_embeddings) as http:
        await asyncio.gather(retriever.retrieve(['amt'], 'HIGH'), retriever.retrieve([], 'MEDIUM'))
    assert [len(call.args[1]['input']) for call in http.call_args_list] == [DOC_BATCH, 1]


@pytest.mark.asyncio
async def test_dimension_change_rejects_result_and_rebuilds_next_time():
    retriever = SemanticRetriever()
    with patch('llm_worker.local_integration.request', side_effect=embeddings):
        await retriever.retrieve([], 'HIGH')
    with patch('llm_worker.local_integration.request', return_value={'embeddings': [[1, 0, 0]]}):
        with pytest.raises(ValueError, match='dimensions/count'):
            await retriever.retrieve([], 'HIGH')
    with patch('llm_worker.local_integration.request', side_effect=embeddings) as http:
        await retriever.retrieve([], 'HIGH')
    assert len(http.call_args.args[1]['input']) == DOC_BATCH


@pytest.mark.asyncio
async def test_invalid_fill_and_transport_failure_do_not_poison_cache():
    retriever = SemanticRetriever()
    for failure in ({'embeddings': []}, TimeoutError('synthetic timeout')):
        kwargs = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
        with patch('llm_worker.local_integration.request', **kwargs):
            with pytest.raises((ValueError, TimeoutError)):
                await retriever.retrieve([], 'HIGH')
    with patch('llm_worker.local_integration.request', side_effect=embeddings) as http:
        await retriever.retrieve([], 'HIGH')
    assert len(http.call_args.args[1]['input']) == DOC_BATCH


def test_invalid_cache_configuration_is_rejected(monkeypatch):
    for value in ('-1', 'nan', 'inf'):
        monkeypatch.setenv('OLLAMA_EMBED_CACHE_TTL_S', value)
        with pytest.raises(ValueError):
            SemanticRetriever()


def test_completion_budget_is_configurable_and_explicit_cap_wins(monkeypatch):
    monkeypatch.setenv('OLLAMA_MAX_COMPLETION_TOKENS', '96')
    assert OllamaClient().max_tokens == 96
    assert OllamaClient(max_tokens=256).max_tokens == 256
    monkeypatch.setenv('OLLAMA_MAX_COMPLETION_TOKENS', '0')
    with pytest.raises(ValueError):
        OllamaClient()
