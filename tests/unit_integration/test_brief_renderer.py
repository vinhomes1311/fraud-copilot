from benchmarks.mock_llm import explanatory_selection
import copy
import json
from unittest.mock import patch

import pytest

from benchmarks.compare_llm import ROOT, load_cases
from llm_worker.api_client import APIClient
from llm_worker.brief_renderer import case_facts, render_brief, RULE_FACTS
from llm_worker.grounding import check_grounding
from llm_worker.local_integration import KB_PATH, OllamaClient, project_evidence
from llm_worker.rag import ApprovedPlaybookRetriever, Chunk
from llm_worker.retrieval_policy import retrieve_context


def context():
    return {
        'case_id': 123, 'txn_id': 'PRIVATE-CANARY', 'risk_level': 'HIGH',
        'raw_model_score': 1.7, 'calibrated_prob': 0.8,
        'transaction': {'amt': 1250.5, 'category': 'shopping_net', 'merchant': 'PRIVATE-CANARY'},
        'features': {'hour_of_day': 13, 'txn_count_24h': 0, 'avg_amt_24h': 987654,
                     'amt_vs_avg_ratio': 876543, 'trans_count_7d': 0,
                     'avg_amt_7d': 765432, 'max_amt_7d': 654321,
                     'amt_to_avg_7d_ratio': 543210, 'amt_to_max_7d_ratio': 432109},
        'explanations': [{'feature': 'hour_of_day', 'value': 13, 'contribution': -0.27},
                         {'feature': 'amt_vs_avg_ratio', 'value': 876543, 'contribution': 0.5}],
    }


def body(brief):
    return ' '.join([brief.summary, *brief.key_signals,
                     *(c.action for c in brief.checklist),
                     *(c.rationale or '' for c in brief.checklist)])


async def generate(ctx, rules, client_type=OllamaClient):
    kb = json.loads(KB_PATH.read_text(encoding='utf-8'))
    chunks = [Chunk(section_id=c['section_id'],rule_code=c['rule_code'],text=c['text']) for c in kb['chunks']]
    with patch.dict('os.environ', {'OLLAMA_MODEL': 'test', 'LLM_API_KEY': 'synthetic-key'}):
        client = client_type()
        reply = {'done': True, 'prompt_eval_count': 100, 'eval_count': 20,
                 'message': {'content': json.dumps(explanatory_selection(rules))}}
        with patch.object(client, 'chat', return_value=reply):
            result = await client.generate(ctx, chunks)
    check_grounding(result.brief, chunks)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize('client_type', [OllamaClient, APIClient])
async def test_hour_and_empty_history_are_case_facts_for_both_backends(client_type):
    ctx = context()
    before = copy.deepcopy(ctx)
    result = await generate(ctx, ['PB-19', 'PB-06', 'PB-12'], client_type)
    assert [item.action for item in result.brief.checklist] == [
        'Transaction hour: 13.',
        'No transactions recorded in the previous 24 hours.',
        'No transactions recorded in the previous 7 days.',
    ]
    assert [c.rule_code for c in result.brief.checklist] == ['PB-19', 'PB-06', 'PB-12']
    text = body(result.brief)
    for instruction in ('Ghi rõ', 'Bỏ qua', 'Nếu có', 'không tự', 'provided feature',
                        'Final decision belongs', 'implementation_action', 'PRIVATE-CANARY'):
        assert instruction not in text
    for unavailable in ('987654', '876543', '765432', '654321', '543210', '432109'):
        assert unavailable not in text
    assert '1250.5' in text and '80%' in text and 'contribution -0.27' in text
    assert text.isascii()
    for note in ('timezone', 'currency', 'baseline', 'unavailable', 'not supplied'):
        assert note not in text.lower()
    assert ctx == before and result.tokens_used == 120
    # Source quotes remain verbatim, separately from the analyst-facing body.
    kb = {c['rule_code']: c for c in json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']}
    assert all(c.quote == kb[c.rule_code]['text'] for c in result.brief.citations)


@pytest.mark.asyncio
async def test_rendered_action_changes_with_case_and_distinguishes_missing_from_zero():
    ctx = context()
    ctx['features']['hour_of_day'] = 2
    ctx['explanations'][0]['value'] = 2
    ctx['features'].pop('txn_count_24h')
    result = await generate(ctx, ['PB-19', 'PB-06'])
    assert result.brief.checklist[0].action == 'Transaction hour: 2.'
    assert 'No transactions recorded in the previous 24 hours.' not in body(result.brief)
    assert 'EV-HISTORY-GAP' not in case_facts(project_evidence(ctx))
    assert 'missing' not in body(result.brief).lower()
    assert 'unavailable' not in body(result.brief).lower()


@pytest.mark.asyncio
async def test_history_windows_and_mean_maximum_stay_distinct():
    ctx = context()
    ctx['features'].update(txn_count_24h=0, trans_count_7d=17, avg_amt_7d=108,
                           max_amt_7d=403, amt_to_avg_7d_ratio=11.47, amt_to_max_7d_ratio=3.09)
    result = await generate(ctx, ['PB-06', 'PB-14', 'PB-15'])
    assert result.brief.checklist[0].action == 'No transactions recorded in the previous 24 hours.'
    assert '108' in result.brief.checklist[1].action and '11.47' in result.brief.checklist[1].action
    assert '403' in result.brief.checklist[2].action and '3.09' in result.brief.checklist[2].action
    assert 'No transactions recorded in the previous 7 days.' not in body(result.brief)


@pytest.mark.asyncio
async def test_missing_probability_and_insufficient_std_are_omitted_without_notes():
    ctx = context()
    ctx['calibrated_prob'] = None
    ctx['features'].update(trans_count_7d=1, avg_amt_7d=94, max_amt_7d=94, std_amt_7d=0)
    ctx['explanations'] = [{'feature':'std_amt_7d','value':0,'contribution':0.2}]
    result = await generate(ctx, ['PB-21', 'PB-28'])
    text = body(result.brief)
    assert result.brief.checklist[0].action == 'Raw ML score: 1.7.'
    assert 'Transaction count in the previous 7 days: 1.' in text
    assert 'Calibrated probability:' not in text
    assert 'standard deviation' not in text.lower()
    for note in ('missing', 'not supplied', 'unavailable', 'insufficient', 'baseline', 'timezone', 'currency'):
        assert note not in text.lower()


def test_renderer_ignores_model_prose_and_missing_data_notes():
    evidence = project_evidence(context())
    for item in evidence.values():
        item['text'] = 'INTERNAL-PROSE-CANARY'
    facts = case_facts(evidence)
    assert set(facts) == set(evidence) - {'EV-LIMIT', 'EV-MAX-7D-GAP'}
    assert 'INTERNAL-PROSE-CANARY' not in ' '.join(facts.values())
    kb = json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']
    assert set(RULE_FACTS) | {'PB-05','PB-23','PB-25','PB-28'} == {c['id'] for c in kb}
    with pytest.raises(ValueError, match='Missing case-fact renderer'):
        render_brief(evidence,[dict(kb[0],rule_code='PB-UNKNOWN')])


@pytest.mark.asyncio
async def test_all_fixtures_keep_selection_grounding_and_numeric_signal_completeness():
    for name in ('cases.json','playbook_v2_cases.json','playbook_v2_boundary_cases.json',
                 'playbook_holdout_20261118.json'):
        for case in load_cases(ROOT/'benchmarks/fixtures'/name):
            ctx = case['context']
            chunks = await retrieve_context(ApprovedPlaybookRetriever(), ctx)
            with patch.dict('os.environ', {'OLLAMA_MODEL':'test'}):
                client = OllamaClient()

                async def compliant_reply(model, schema, evidence, selected_chunks):
                    ids = schema['properties']['checklist_ids']['items']['enum'][:1]
                    return {'done':True,'prompt_eval_count':100,'eval_count':20,
                            'message':{'content':json.dumps(explanatory_selection(ids))}}

                with patch.object(client,'chat',side_effect=compliant_reply):
                    result = await client.generate(ctx, chunks)
            check_grounding(result.brief,chunks)
            evidence = project_evidence(ctx)
            assert len(result.brief.key_signals) == sum(k.startswith(('EV-FEATURE-','EV-FACTOR-')) for k in evidence)
            assert 'provided feature' not in body(result.brief)
            assert 'HOLDOUT-PRIVATE-CANARY' not in body(result.brief)
            assert body(result.brief).isascii()
            assert all(c.quote.isascii() for c in result.brief.citations)
            for note in ('timezone', 'currency', 'baseline', 'unavailable', 'not supplied'):
                assert note not in body(result.brief).lower()
