"""Render case facts for analysts, separately from model-facing instructions."""
from __future__ import annotations

from fraud_common.schemas import BriefContent, ChecklistItem, Citation


FEATURE_LABELS = {
    'amt': 'Transaction amount',
    'dist_home_merchant_km': 'Home-to-merchant distance',
    'txn_count_24h': 'Transaction count in the previous 24 hours',
    'avg_amt_24h': 'Average transaction amount in the previous 24 hours',
    'amt_vs_avg_ratio': 'Amount-to-average ratio for the previous 24 hours',
    'hour_of_day': 'Transaction hour',
    'trans_count_1h': 'Transaction count in the previous hour',
    'trans_count_7d': 'Transaction count in the previous 7 days',
    'sum_amt_24h': 'Total transaction amount in the previous 24 hours',
    'avg_amt_7d': 'Average transaction amount in the previous 7 days',
    'std_amt_7d': 'Transaction amount standard deviation in the previous 7 days',
    'max_amt_7d': 'Maximum transaction amount in the previous 7 days',
    'amt_to_avg_7d_ratio': 'Amount-to-average ratio for the previous 7 days',
    'amt_to_max_7d_ratio': 'Amount-to-maximum ratio for the previous 7 days',
}


def display_number(value):
    """Keep validated source precision; do not infer a currency or unit."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def case_facts(evidence):
    """Return display text keyed by projected evidence ID, never by raw payload."""
    facts = {}
    for key, item in evidence.items():
        if key == 'EV-AMOUNT':
            facts[key] = f"Transaction amount: {display_number(item['value'])}."
        elif key == 'EV-CATEGORY':
            facts[key] = f"Transaction category: {item['value']}."
        elif key == 'EV-RAW':
            facts[key] = f"Raw ML score: {display_number(item['value'])}."
        elif key == 'EV-PROB':
            percent = format(item['value'] * 100, '.6f').rstrip('0').rstrip('.')
            facts[key] = f'Calibrated probability: {percent}%.'
        elif key == 'EV-ROUTE':
            facts[key] = f"ML risk routing: {item['value']}."
        elif key == 'EV-HISTORY':
            facts[key] = f"Transaction count in the previous 24 hours: {display_number(item['value'])}."
        elif key.startswith('EV-FEATURE-'):
            name = key.removeprefix('EV-FEATURE-')
            label = FEATURE_LABELS[name]
            value = display_number(item['value'])
            unit = ' km' if name == 'dist_home_merchant_km' else ''
            facts[key] = f'{label}: {value}{unit}.'
        elif key.startswith('EV-FACTOR-'):
            factor = item
            if not {'feature', 'value', 'contribution'} <= factor.keys():
                raise ValueError('Missing verified factor values for case rendering')
            label = FEATURE_LABELS[factor['feature']]
            if factor.get('reason'):
                # Readable factor from ml_pipeline.explain: original value, card-history
                # comparison and share of the model's evidence.
                value = factor.get('display_value') or display_number(factor['value'])
                share = factor.get('share_pct')
                suffix = f" ({display_number(share)}% of model evidence)" if share is not None else ''
                facts[key] = f"ML factor: {label.lower()} = {value}; {factor['reason']}{suffix}."
            else:
                facts[key] = (f"ML factor: {label.lower()} = {display_number(factor['value'])}; "
                              f"contribution {display_number(factor['contribution'])}.")
        elif key == 'EV-HISTORY-GAP':
            count = evidence.get('EV-FEATURE-txn_count_24h', {}).get('value')
            if count == 0:
                facts[key] = 'No transactions recorded in the previous 24 hours.'
        elif key == 'EV-HISTORY-7D-GAP':
            count = evidence.get('EV-FEATURE-trans_count_7d', {}).get('value')
            if count == 0:
                facts[key] = 'No transactions recorded in the previous 7 days.'
        # Missing-data notes, EV-LIMIT and evidence.text stay out of case prose.
        # Missing values remain absent, not zero or a fabricated baseline.
    return facts


def _feature(name):
    return 'EV-FEATURE-' + name


# These choose which verified facts explain a selected rule. The model still
# selects the rule IDs; the renderer never reads expected benchmark answers.
RULE_FACTS = {
    'PB-01': ['EV-AMOUNT', 'EV-CATEGORY', _feature('hour_of_day'), _feature('dist_home_merchant_km')],
    'PB-02': ['EV-RAW', 'EV-PROB'],
    'PB-03': ['EV-HISTORY', _feature('avg_amt_24h'), 'EV-HISTORY-GAP', 'EV-BASELINE-GAP'],
    'PB-04': ['EV-ROUTE', 'EV-PROB'],
    'PB-06': ['EV-HISTORY-GAP'],
    'PB-07': ['EV-HISTORY', 'EV-BASELINE-GAP'],
    'PB-08': ['EV-AMOUNT', _feature('avg_amt_24h'), _feature('amt_vs_avg_ratio')],
    'PB-09': [_feature('trans_count_1h'), _feature('txn_count_24h')],
    'PB-10': [_feature('txn_count_24h'), _feature('trans_count_7d')],
    'PB-11': [_feature('trans_count_7d'), _feature('avg_amt_7d')],
    'PB-12': ['EV-HISTORY-7D-GAP'],
    'PB-13': [_feature('avg_amt_7d'), 'EV-BASELINE-7D-GAP'],
    'PB-14': ['EV-AMOUNT', _feature('avg_amt_7d'), _feature('amt_to_avg_7d_ratio')],
    'PB-15': ['EV-AMOUNT', _feature('max_amt_7d'), _feature('amt_to_max_7d_ratio')],
    'PB-16': [_feature('trans_count_7d'), _feature('std_amt_7d')],
    'PB-17': [_feature('sum_amt_24h'), _feature('txn_count_24h')],
    'PB-18': [_feature('dist_home_merchant_km')],
    'PB-19': [_feature('hour_of_day')],
    'PB-20': ['EV-CATEGORY'],
    'PB-21': ['EV-RAW', 'EV-PROB'],
    'PB-22': ['EV-PROB', 'EV-ROUTE'],
    'PB-24': ['EV-HISTORY', _feature('trans_count_7d'), 'EV-HISTORY-GAP',
              'EV-BASELINE-GAP', 'EV-HISTORY-7D-GAP', 'EV-BASELINE-7D-GAP'],
    'PB-26': ['EV-ROUTE', 'EV-PROB', 'EV-AMOUNT'],
    'PB-27': ['EV-ROUTE', 'EV-PROB', 'EV-AMOUNT'],
    'PB-29': ['EV-ROUTE', 'EV-PROB'],
}


def render_brief(evidence, selected_chunks, rationales=None, *, rationale_origin='llm'):
    facts = case_facts(evidence)
    gaps = [key for key in facts if key.endswith('-GAP')]
    factor_ids = [key for key in facts if key.startswith('EV-FACTOR-')]
    checklist = []
    for index, chunk in enumerate(selected_chunks, 1):
        rule = chunk['rule_code']
        if rule not in RULE_FACTS and rule not in ('PB-05', 'PB-23', 'PB-25', 'PB-28'):
            raise ValueError('Missing case-fact renderer for playbook rule')
        if rule in ('PB-05', 'PB-28'):
            keys = gaps
        elif rule in ('PB-23', 'PB-25'):
            keys = factor_ids
        else:
            keys = RULE_FACTS.get(rule, [])
        statements = [facts[key] for key in keys if key in facts]
        # Broad principles can be selected even on sparse evidence. Render
        # actual transaction facts rather than copying generic playbook prose.
        if not statements:
            statements = [facts[key] for key in ('EV-AMOUNT', 'EV-ROUTE') if key in facts]
        rationale = (rationales or {}).get(rule, {})
        checklist.append(ChecklistItem(
            step=index, action=' '.join(dict.fromkeys(statements)), rule_code=rule,
            rationale=rationale.get('text'), evidence_ids=rationale.get('evidence_ids', [])))
    summary = list(dict.fromkeys(facts.values()))
    return BriefContent(
        summary=' '.join(summary),
        key_signals=[text for key, text in facts.items() if key.startswith(('EV-FACTOR-', 'EV-FEATURE-'))],
        checklist=checklist,
        citations=[Citation(section_id=c['section_id'], rule_code=c['rule_code'], quote=c['text'])
                   for c in selected_chunks],
        source='llm', rationale_origin=rationale_origin,
    )
