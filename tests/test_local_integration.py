import copy
import json
import os
import unittest
from unittest.mock import patch
from llm_worker.local_integration import OllamaClient, SemanticRetriever, project_evidence
from llm_worker.rag import Chunk
from copilot.v2.knowledge import load
from llm_worker.local_integration import KB_PATH

class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx={'case_id':1,'txn_id':'tx-test','risk_level':'HIGH','raw_model_score':1.7,
            'calibrated_prob':0.7,'transaction':{'amt':100,'cc_num':'SECRET','email':'SECRET'},
            'features':{'txn_count_24h':0,'avg_amt_24h':0},
            'explanations':[{'feature':'amt','value':100,'contribution':0.2}]}
        self.chunks=[Chunk(section_id=c['section_id'],rule_code=c['rule_code'],text=c['text'])
                     for c in load(KB_PATH)['chunks']]
    def test_projection_privacy_and_history(self):
        evidence=project_evidence(self.ctx)
        self.assertNotIn('SECRET',json.dumps(evidence))
        self.assertNotIn('EV-FEATURE-avg_amt_24h',evidence)
        self.assertIn('EV-HISTORY-GAP',evidence)
        self.assertIn('method and scale not supplied',evidence['EV-FACTOR-0']['text'])
    def test_average_is_not_median(self):
        self.ctx['features'].update(txn_count_24h=3,avg_amt_24h=12)
        evidence=project_evidence(self.ctx)
        self.assertIn('avg_amt_24h',evidence['EV-FEATURE-avg_amt_24h']['text'])
        self.assertNotIn('median',json.dumps(evidence))
    async def test_generated_contract_and_unchanged_scores(self):
        before=copy.deepcopy(self.ctx)
        async def fake(path,payload):
            data=json.loads(payload['messages'][1]['content'])
            self.assertNotIn('SECRET',json.dumps(payload))
            return {'done':True,'prompt_eval_count':100,'eval_count':20,
                    'message':{'content':json.dumps({'evidence_ids':list(data['evidence']),
                                                    'checklist_ids':['PB-02']})}}
        with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}), patch('llm_worker.local_integration.request',side_effect=fake):
            result=await OllamaClient().generate(self.ctx,self.chunks)
        self.assertEqual(self.ctx,before)
        self.assertEqual(result.tokens_used,120)
        self.assertEqual(result.brief.citations[0].rule_code,'PB-02')
        self.assertEqual(len(result.brief.checklist),1)
    async def test_timeout_propagates(self):
        with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}), patch('llm_worker.local_integration.request',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                await OllamaClient().generate(self.ctx,self.chunks)
    async def test_invalid_citation_rejected(self):
        result={'done':True,'prompt_eval_count':1,'eval_count':1,'message':{'content':json.dumps({'evidence_ids':list(project_evidence(self.ctx)),'checklist_ids':['FAKE']})}}
        with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}), patch('llm_worker.local_integration.request',return_value=result):
            with self.assertRaises(ValueError):
                await OllamaClient().generate(self.ctx,self.chunks)
    async def test_embedding_dimension_rejected(self):
        with patch('llm_worker.local_integration.request',return_value={'embeddings':[[1.0]]}):
            with self.assertRaises(ValueError):
                await SemanticRetriever().retrieve(['amt'],'HIGH')

if __name__=='__main__': unittest.main()
