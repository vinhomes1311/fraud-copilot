import copy
import json
from unittest.mock import patch

import pytest

from llm_worker.api_client import APIClient
from llm_worker.grounding import check_grounding
from llm_worker.local_integration import KB_PATH, OllamaClient, project_evidence
from llm_worker.narrative import prompt_payload, validate_rationales
from llm_worker.rag import Chunk


def context():
    return {
        'case_id': 1, 'txn_id': 'PRIVATE-CANARY', 'risk_level': 'HIGH',
        'transaction': {'amt': 1500, 'category': 'shopping_net', 'merchant': 'PRIVATE-CANARY'},
        'raw_model_score': 1.7, 'calibrated_prob': 0.8,
        'features': {'txn_count_24h': 10, 'avg_amt_24h': 100, 'amt_vs_avg_ratio': 15,
                     'trans_count_7d': 40, 'avg_amt_7d': 300, 'max_amt_7d': 2000},
        'explanations': [{'feature': 'amt', 'value': 1500, 'contribution': 0.3}],
    }


def rationale():
    return {'rule_code': 'PB-08', 'evidence_ids': ['EV-AMOUNT', 'EV-FEATURE-avg_amt_24h'],
            'text': 'The current amount of {{EV-AMOUNT}} exceeds the average amount of '
                    '{{EV-FEATURE-avg_amt_24h}} in the previous 24 hours. This contrast '
                    'makes the size of the purchase a relevant review signal, while '
                    'the recorded transaction pattern still requires an analyst assessment '
                    'before deciding whether the activity is fraudulent.'}


@pytest.mark.asyncio
@pytest.mark.parametrize('client_type', [APIClient, OllamaClient])
async def test_generated_explanation_reaches_saved_contract_with_verbatim_source_and_values(client_type):
    ctx = context()
    before = copy.deepcopy(ctx)
    chunks = [Chunk(section_id=c['section_id'], rule_code=c['rule_code'], text=c['text'])
              for c in json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']]
    selection = {'checklist_ids': ['PB-08'], 'rationales': [rationale()]}
    response = {'done': True, 'prompt_eval_count': 120, 'eval_count': 140,
                'message': {'content': json.dumps(selection)}}
    with patch.dict('os.environ', {'LLM_API_KEY': 'synthetic-key', 'OLLAMA_MODEL': 'test'}):
        client = client_type()
        with patch.object(client, 'chat', return_value=response) as transport:
            result = await client.generate(ctx, chunks)
    assert transport.call_count == 1
    check_grounding(result.brief, chunks)
    item = result.brief.checklist[0]
    assert '1500 exceeds the average amount of 100' in item.rationale
    assert '{{' not in item.rationale and 'PRIVATE-CANARY' not in item.rationale
    assert item.evidence_ids == rationale()['evidence_ids']
    assert result.brief.model_dump()['checklist'][0]['rationale'] == item.rationale
    assert ctx == before and result.tokens_used == 260


def test_prompt_comparisons_keep_mean_maximum_and_windows_separate():
    ctx = context()
    payload = prompt_payload(project_evidence(ctx), [])
    assert payload['verified_comparisons'] == [
        {'left': 'EV-AMOUNT', 'right': 'EV-FEATURE-avg_amt_24h', 'relation': 'greater_than'},
        {'left': 'EV-AMOUNT', 'right': 'EV-FEATURE-avg_amt_7d', 'relation': 'greater_than'},
        {'left': 'EV-AMOUNT', 'right': 'EV-FEATURE-max_amt_7d', 'relation': 'less_than'},
    ]
    assert 'PRIVATE-CANARY' not in json.dumps(payload)
    ctx['features'].update(txn_count_24h=0, trans_count_7d=0)
    payload = prompt_payload(project_evidence(ctx), [])
    assert payload['verified_comparisons'] == []
    assert 'EV-FEATURE-avg_amt_24h' not in payload['case_facts']


@pytest.mark.parametrize('extra', [
    ' The timezone is not supplied.',
    ' The currency is unknown.',
    ' Record the missing baseline.',
    ' The customer was contacted.',
    ' The amount is 999999.',
    ' The model reports an 80% probability.',
    ' The amount is {{EV-PRIVATE}}.',
    ' The mean is {{EV-FEATURE-avg_amt_7d}}.',
    ' <script>alert()</script>',
])
def test_generated_prose_cannot_introduce_notes_uncited_values_or_invented_numbers(extra):
    item = rationale()
    item['text'] += extra
    with pytest.raises(ValueError):
        validate_rationales([item], ['PB-08'], project_evidence(context()))


@pytest.mark.parametrize('change', ['missing', 'extra', 'unknown_rule', 'unknown_evidence', 'policy_evidence', 'empty', 'short'])
def test_rationale_contract_rejects_missing_or_invalid_support(change):
    items = [rationale()]
    if change == 'missing':
        items = []
    elif change == 'extra':
        items *= 2
    elif change == 'unknown_rule':
        items[0]['rule_code'] = 'PB-19'
    elif change == 'unknown_evidence':
        items[0]['evidence_ids'] = ['EV-UNKNOWN']
    elif change == 'policy_evidence':
        items[0]['evidence_ids'] = ['EV-LIMIT']
    elif change == 'empty':
        items[0]['text'] = ''
    else:
        items[0]['text'] = 'The amount is high.'
    with pytest.raises(ValueError):
        validate_rationales(items, ['PB-08'], project_evidence(context()))


@pytest.mark.asyncio
async def test_old_selection_only_reply_no_longer_silently_returns_a_terse_brief():
    ctx = context()
    chunks = [Chunk(section_id=c['section_id'], rule_code=c['rule_code'], text=c['text'])
              for c in json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']]
    with patch.dict('os.environ', {'OLLAMA_MODEL': 'test'}), patch.object(
            OllamaClient, 'chat', return_value={'done': True, 'prompt_eval_count': 1,
                'eval_count': 1, 'message': {'content': '{"checklist_ids":["PB-08"]}'}}):
        with pytest.raises(ValueError):
            await OllamaClient().generate(ctx, chunks)
