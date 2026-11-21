"""The LLM boundary accepts current scoring-worker features without changing ML."""
import copy
from unittest.mock import patch

import pytest

from llm_worker.local_integration import SemanticRetriever, project_evidence


def context():
    return {'transaction': {'amt': 100}, 'raw_model_score': .7,
            'calibrated_prob': .6, 'risk_level': 'MEDIUM',
            'features': {'amt': 100, 'trans_count_24h': 3, 'transaction_hour': 2,
                         'customer_merchant_distance_km': 40, 'trans_count_7d': 9,
                         'avg_amt_7d': 50, 'max_amt_7d': 200, 'std_amt_7d': 10,
                         'amt_to_avg_7d_ratio': 100 / 51, 'amt_to_max_7d_ratio': 100 / 201},
            'explanations': [{'feature': 'customer_merchant_distance_km', 'value': 40, 'contribution': .1}]}


def test_current_ml_names_preserve_facts_and_separate_history_windows():
    ctx = context()
    before = copy.deepcopy(ctx)
    evidence = project_evidence(ctx)
    assert ctx == before
    assert evidence['EV-FEATURE-txn_count_24h']['value'] == 3
    assert evidence['EV-FEATURE-hour_of_day']['value'] == 2
    assert evidence['EV-FEATURE-dist_home_merchant_km']['value'] == 40
    assert 'dist_home_merchant_km' in evidence['EV-FACTOR-0']['text']
    assert 'EV-HISTORY-GAP' not in evidence
    assert 'EV-BASELINE-GAP' in evidence
    assert 'EV-FEATURE-avg_amt_24h' not in evidence
    assert evidence['EV-FEATURE-avg_amt_7d']['value'] == 50
    assert '7-day' in evidence['EV-FEATURE-avg_amt_7d']['text']
    assert 'mean + 1' in evidence['EV-FEATURE-amt_to_avg_7d_ratio']['text']


def test_missing_7_day_baseline_omits_fallback_ratios_and_explanations():
    ctx = context()
    ctx['features'].update(trans_count_24h=0, trans_count_7d=0, avg_amt_7d=0, max_amt_7d=0)
    ctx['explanations'] = [{'feature': 'amt_to_avg_7d_ratio', 'value': 100, 'contribution': .9}]
    evidence = project_evidence(ctx)
    for feature in ('avg_amt_7d', 'max_amt_7d', 'std_amt_7d', 'amt_to_avg_7d_ratio', 'amt_to_max_7d_ratio'):
        assert 'EV-FEATURE-' + feature not in evidence
    assert 'EV-FACTOR-0' not in evidence
    assert 'EV-HISTORY-7D-GAP' in evidence


def test_7_day_history_does_not_invent_24_hour_history():
    ctx = context()
    ctx['features']['trans_count_24h'] = 0
    evidence = project_evidence(ctx)
    assert '24-hour' in evidence['EV-HISTORY-GAP']['text']
    assert evidence['EV-FEATURE-avg_amt_7d']['value'] == 50
    assert 'EV-HISTORY-7D-GAP' not in evidence


def test_conflicting_aliases_fail_instead_of_silently_choosing():
    ctx = context()
    ctx['features']['txn_count_24h'] = 99
    with pytest.raises(ValueError, match='Conflicting'):
        project_evidence(ctx)


@pytest.mark.asyncio
async def test_retrieval_understands_current_ml_explanation_names():
    async def fake(path, payload):
        query = payload['input'][-1]
        assert 'Home-to-merchant distance' in query
        assert 'recorded transaction hour' in query
        assert '7-day mean + 1' in query
        return {'embeddings': [[1., .5] for _ in payload['input']]}
    with patch('llm_worker.local_integration.request', side_effect=fake):
        assert len(await SemanticRetriever().retrieve(
            ['customer_merchant_distance_km', 'transaction_hour', 'amt_to_avg_7d_ratio'], 'HIGH')) == 5


def test_readable_factor_reason_and_scale_reach_llm_evidence():
    ctx = context()
    ctx['explanations'] = [{'feature': 'customer_merchant_distance_km', 'value': 40, 'contribution': 1.25,
                            'label': 'Home-to-merchant distance', 'display_value': '40.0 km',
                            'reason': "Merchant is 40.0 km from the cardholder's address",
                            'share_pct': 31.5, 'probability_without': .42}]
    text = project_evidence(ctx)['EV-FACTOR-0']['text']
    assert "Merchant is 40.0 km from the cardholder's address" in text
    assert '31.5% of this transaction' in text
    assert 'log-odds' in text and 'method and scale not supplied' not in text
    assert 'does not confirm fraud' in text


def test_key_signals_use_reason_and_keep_legacy_format():
    from llm_worker.fallback import describe_signal, rule_based_brief
    readable = {'feature': 'amt', 'value': 780.52, 'contribution': 2.1, 'label': 'Transaction amount',
                'reason': "780.52 is 6.2× this card's average", 'share_pct': 40.0}
    assert describe_signal(readable) == "Transaction amount: 780.52 is 6.2× this card's average (40% of model evidence)"
    assert describe_signal({'feature': 'amt', 'value': 1, 'contribution': 0.2}) == 'amt đóng góp 0.20'
    brief = rule_based_brief({'txn_id': 't1', 'transaction': {'amt': 1}, 'risk_level': 'HIGH',
                              'explanations': [readable]})
    assert brief.key_signals == [describe_signal(readable)]
