import pytest
from copilot.v2.knowledge import load
from llm_worker.local_integration import KB_PATH
from llm_worker.rag import ApprovedPlaybookRetriever

def test_expanded_rules_have_unique_citations_and_bounded_chunks():
    kb = load(KB_PATH)
    assert len(kb['chunks']) >= 29
    assert len({c['rule_code'] for c in kb['chunks']}) == len(kb['chunks'])
    assert all(len(c['text']) <= 2000 for c in kb['chunks'])

@pytest.mark.asyncio
async def test_lexical_retrieval_reaches_detailed_rules_without_prompting_whole_kb():
    chunks = await ApprovedPlaybookRetriever().retrieve(['amt_to_avg_7d_ratio'], 'HIGH')
    assert len(chunks) == 5
    assert any(c.rule_code == 'PB-14' for c in chunks)
    assert any('mean' in c.text or 'trung bình' in c.text for c in chunks)
