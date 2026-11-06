import copy
import json
from unittest.mock import patch
import pytest
from benchmarks.compare_llm import ROOT, load_cases
from benchmarks.playbook_eval import validate_references
from llm_worker.local_integration import SemanticRetriever, KB_PATH
from llm_worker.retrieval_policy import applicability, retrieve_context

def cases():
    return load_cases(ROOT/'benchmarks/fixtures/playbook_v2_cases.json')

def test_applicability_excludes_invalid_rules_for_all_contexts():
    for c in cases():
        assert not set(c['forbidden_rules']) & applicability(c['context']).keys(), c['id']

def test_context_and_identifiers_do_not_change_policy():
    ctx=copy.deepcopy(cases()[0]['context'])
    before=copy.deepcopy(ctx)
    expected=applicability(ctx)
    assert ctx==before
    ctx.update(case_id=999,txn_id='PRIVATE-CANARY')
    ctx['transaction']['email']='PRIVATE-CANARY'
    ctx['features']['unknown']='PRIVATE-CANARY'
    assert applicability(ctx)==expected

@pytest.mark.asyncio
async def test_semantic_query_includes_history_state_but_not_identifiers():
    ctx=copy.deepcopy(next(c['context'] for c in cases() if c['id']=='ml-current-no-history'))
    ctx['transaction']['email']='PRIVATE-CANARY'
    async def fake(path,payload):
        query=payload['input'][-1]
        assert 'no 24-hour behavioral baseline' in query
        assert 'PRIVATE-CANARY' not in query and ctx['txn_id'] not in query
        return {'embeddings':[[1.,.5] for _ in payload['input']]}
    with patch('llm_worker.local_integration.request',side_effect=fake):
        chunks=await retrieve_context(SemanticRetriever(),ctx)
    selected={c.rule_code for c in chunks}
    assert selected & {'PB-06','PB-12'}
    assert not selected & {'PB-08','PB-14','PB-15'}

def test_boundary_references_and_conditions_are_consistent():
    allowed={c['rule_code'] for c in json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']}
    data=load_cases(ROOT/'benchmarks/fixtures/playbook_v2_boundary_cases.json')
    validate_references(data,allowed)
    for c in data:
        assert not set(c['forbidden_rules']) & applicability(c['context']).keys()

@pytest.mark.asyncio
async def test_generation_rejects_known_but_inapplicable_rule():
    import os
    from llm_worker.local_integration import OllamaClient
    from llm_worker.rag import Chunk
    ctx=copy.deepcopy(cases()[0]['context'])
    kb=json.loads(KB_PATH.read_text(encoding='utf-8'))
    rule=next(c for c in kb['chunks'] if c['id']=='PB-06')
    chunk=Chunk(section_id=rule['section_id'],rule_code=rule['rule_code'],text=rule['text'])
    reply={'done':True,'prompt_eval_count':100,'eval_count':10,
           'message':{'content':'{"checklist_ids":["PB-06"]}'}}
    with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}), patch('llm_worker.local_integration.request',return_value=reply):
        with pytest.raises(ValueError,match='not applicable'):
            await OllamaClient().generate(ctx,[chunk])
