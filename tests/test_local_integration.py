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
        self.assertIn('mean amount', evidence['EV-FEATURE-avg_amt_24h']['text'])
        self.assertIn('not a median', evidence['EV-FEATURE-avg_amt_24h']['text'])
    def test_merged_history_summary_preserves_baseline_validation(self):
        for average in (0, 12):
            with self.subTest(average=average):
                self.ctx['features'].update(txn_count_24h=3, avg_amt_24h=average)
                evidence = project_evidence(self.ctx)
                self.assertEqual(evidence['EV-HISTORY']['value'], 3)
                self.assertIn('3 transactions', evidence['EV-HISTORY']['text'])
                self.assertNotIn('EV-HISTORY-GAP', evidence)
                if average:
                    self.assertIn('12.00', evidence['EV-HISTORY']['text'])
                    self.assertNotIn('EV-BASELINE-GAP', evidence)
                else:
                    self.assertNotIn('historical average', evidence['EV-HISTORY']['text'])
                    self.assertIn('EV-BASELINE-GAP', evidence)
                    self.assertNotIn('EV-FEATURE-avg_amt_24h', evidence)

    async def test_generated_contract_and_unchanged_scores(self):
        before=copy.deepcopy(self.ctx)
        async def fake(path,payload):
            data=json.loads(payload['messages'][1]['content'])
            self.assertNotIn('SECRET',json.dumps(payload))
            self.assertEqual(payload['format']['required'], ['checklist_ids'])
            self.assertEqual(set(payload['format']['properties']), {'checklist_ids'})
            return {'done':True,'prompt_eval_count':100,'eval_count':20,
                    'message':{'content':json.dumps({'checklist_ids':['PB-02']})}}
        with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}), patch('llm_worker.local_integration.request',side_effect=fake):
            result=await OllamaClient().generate(self.ctx,self.chunks)
        self.assertEqual(self.ctx,before)
        self.assertEqual(result.tokens_used,120)
        self.assertEqual(result.brief.citations[0].rule_code,'PB-02')
        self.assertEqual(len(result.brief.checklist),1)
        for item in project_evidence(self.ctx).values():
            self.assertIn(item['text'], result.brief.summary)
    async def test_timeout_propagates(self):
        with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}), patch('llm_worker.local_integration.request',side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                await OllamaClient().generate(self.ctx,self.chunks)
    async def test_invalid_citation_rejected(self):
        result={'done':True,'prompt_eval_count':1,'eval_count':1,'message':{'content':json.dumps({'checklist_ids':['FAKE']})}}
        with patch.dict(os.environ,{'OLLAMA_MODEL':'test'}), patch('llm_worker.local_integration.request',return_value=result):
            with self.assertRaises(ValueError):
                await OllamaClient().generate(self.ctx,self.chunks)
    async def test_embedding_dimension_rejected(self):
        with patch('llm_worker.local_integration.request',return_value={'embeddings':[[1.0]]}):
            with self.assertRaises(ValueError):
                await SemanticRetriever().retrieve(['amt'],'HIGH')

    async def test_all_benchmark_evidence_is_preserved_without_model_copying(self):
        from benchmarks.compare_llm import load_cases
        from llm_worker.grounding import check_grounding
        reply = {'done': True, 'prompt_eval_count': 100, 'eval_count': 10,
                 'message': {'content': '{"checklist_ids":["PB-03"]}'}}
        for case in load_cases():
            with self.subTest(case=case['id']), patch.dict(os.environ, {'OLLAMA_MODEL': 'test'}), \
                 patch('llm_worker.local_integration.request', return_value=reply):
                before = copy.deepcopy(case['context'])
                result = await OllamaClient().generate(case['context'], self.chunks)
                self.assertEqual(case['context'], before)
                expected = ' '.join(item['text'] for item in project_evidence(before).values())
                self.assertEqual(result.brief.summary, expected)
                check_grounding(result.brief, self.chunks)
                self.assertEqual(result.tokens_used, 110)

    async def test_invalid_local_checklists_and_extra_output_are_rejected(self):
        invalid = [{'checklist_ids': []}, {'checklist_ids': ['FAKE']},
                   {'checklist_ids': ['PB-03', 'PB-03']},
                   {'checklist_ids': ['PB-01', 'PB-02', 'PB-03', 'PB-04']},
                   {'checklist_ids': 'PB-03'}, {'checklist_ids': [1]}, {},
                   {'checklist_ids': ['PB-03'], 'summary': 'invented'},
                   {'checklist_ids': ['PB-03'], 'evidence_ids': ['EV-AMOUNT']}]
        for selection in invalid:
            reply = {'done': True, 'prompt_eval_count': 100, 'eval_count': 10,
                     'message': {'content': json.dumps(selection)}}
            with self.subTest(selection=selection), patch.dict(os.environ, {'OLLAMA_MODEL': 'test'}), \
                 patch('llm_worker.local_integration.request', return_value=reply), self.assertRaises(ValueError):
                await OllamaClient().generate(self.ctx, self.chunks)

    async def test_local_truncation_and_malformed_json_remain_failures(self):
        for reason, content in [('length', '{"checklist_ids":["PB-03"]}'), ('stop', '{')]:
            reply = {'done': True, 'done_reason': reason, 'prompt_eval_count': 100, 'eval_count': 10,
                     'message': {'content': content}}
            with self.subTest(reason=reason), patch.dict(os.environ, {'OLLAMA_MODEL': 'test'}), \
                 patch('llm_worker.local_integration.request', return_value=reply), self.assertRaises(ValueError):
                await OllamaClient().generate(self.ctx, self.chunks)

if __name__=='__main__': unittest.main()
