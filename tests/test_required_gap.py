import copy
import json
import os
from unittest.mock import patch
import pytest
from benchmarks.compare_llm import ROOT
from llm_worker.local_integration import OllamaClient, KB_PATH
from llm_worker.retrieval_policy import required_review_rules, rank_applicable
from llm_worker.rag import Chunk

def context():
    data=json.loads((ROOT/'benchmarks/playbook_v2_boundary_cases.json').read_text())
    return copy.deepcopy(next(c['context'] for c in data if c['id']=='boundary-4'))

def test_requirement_depends_on_evidence_not_case_id_and_ranks_first():
    ctx=context(); ctx['case_id']=9999;ctx['txn_id']='changed'
    kb=json.loads(KB_PATH.read_text(encoding='utf-8'))
    assert required_review_rules(ctx)=={'PB-28'}
    assert rank_applicable(kb['chunks'],ctx)[0]['id']=='PB-28'
    ctx['features']['max_amt_7d']=120
    assert required_review_rules(ctx)==set()
    ctx=context();ctx['explanations']=[]
    assert required_review_rules(ctx)==set()

@pytest.mark.asyncio
@pytest.mark.parametrize('ids,valid',[(['PB-11','PB-13'],False),(['PB-28'],True),(['PB-28','PB-13'],False)])
async def test_model_must_include_required_gap_without_output_repair(ids,valid):
    ctx=context()
    kb=json.loads(KB_PATH.read_text(encoding='utf-8'))
    chunks=[Chunk(section_id=c['section_id'],rule_code=c['rule_code'],text=c['text']) for c in kb['chunks'] if c['id'] in {'PB-11','PB-13','PB-28'}]
    async def fake(path,payload):
        assert payload['format']['properties']['checklist_ids']['items']['enum']==['PB-28']
        assert payload['format']['properties']['checklist_ids']['maxItems']==1
        entries=json.loads(payload['messages'][1]['content'])['playbook']
        assert next(c for c in entries if c['id']=='PB-28')['selection_role']=='mandatory_gap'
        return {'done':True,'prompt_eval_count':100,'eval_count':15,'message':{'content':json.dumps({'checklist_ids':ids})}}
    with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}),patch('llm_worker.local_integration.request',side_effect=fake):
        if valid:
            result=await OllamaClient().generate(ctx,chunks)
            assert [c.rule_code for c in result.brief.checklist]==ids
        else:
            with pytest.raises(ValueError,match='Missing required'):
                await OllamaClient().generate(ctx,chunks)
