"""Playbook applicability rules; independent of benchmark references and case IDs."""
FEATURE_RULES = {
    'PB-08': 'amt_vs_avg_ratio', 'PB-09': 'trans_count_1h', 'PB-10': 'txn_count_24h',
    'PB-11': 'trans_count_7d', 'PB-13': 'avg_amt_7d', 'PB-14': 'amt_to_avg_7d_ratio',
    'PB-15': 'amt_to_max_7d_ratio', 'PB-16': 'std_amt_7d', 'PB-17': 'sum_amt_24h',
    'PB-18': 'dist_home_merchant_km', 'PB-19': 'hour_of_day',
}


def applicability(ctx):
    from .local_integration import canonical_features, FEATURE_ALIASES, project_evidence
    features = canonical_features(ctx.get('features'))
    evidence = project_evidence(ctx)
    known = {k.removeprefix('EV-FEATURE-') for k in evidence if k.startswith('EV-FEATURE-')}
    explained = {FEATURE_ALIASES.get(e['feature'], e['feature']) for e in ctx.get('explanations', [])}
    count = features.get('txn_count_24h', 0) or 0
    count7 = features.get('trans_count_7d', 0) or 0
    priorities = {}
    for rule, feature in FEATURE_RULES.items():
        if feature in known:
            priorities[rule] = 4 if feature in explained else 1
    if count <= 0:
        priorities['PB-06'] = 3
    elif 'EV-BASELINE-GAP' in evidence:
        priorities['PB-07'] = 3
    if 'trans_count_7d' in features and count7 <= 0:
        priorities['PB-12'] = 3
    if 'EV-BASELINE-7D-GAP' in evidence:
        priorities['PB-13'] = 3
    if 'trans_count_7d' in features and (count <= 0 < count7 or 'EV-BASELINE-GAP' in evidence):
        priorities['PB-24'] = 2
    if 'EV-CATEGORY' in evidence:
        priorities['PB-20'] = 1
    priorities['PB-21'] = 2 if ctx.get('calibrated_prob') is None else 0
    if ctx.get('calibrated_prob') is not None:
        priorities['PB-22'] = 0
    if explained:
        priorities['PB-23'] = 0
    priorities['PB-26' if ctx['risk_level'] == 'LOW' else 'PB-27'] = 0
    if any(k.endswith('-GAP') for k in evidence):
        priorities['PB-28'] = 1
    if (explained & set(FEATURE_RULES.values())) - known:
        priorities['PB-28'] = 3
    # Broad principles remain available, but specific applicable cards rank first.
    priorities.update({f'PB-{i:02d}': 0 for i in range(1, 6)})
    priorities['PB-25'] = 0
    priorities['PB-29'] = 0
    return priorities


def required_review_rules(ctx):
    """Require data repair when the main maximum-ratio signal has no baseline."""
    from .local_integration import project_evidence
    explained = {e['feature'] for e in ctx.get('explanations', [])}
    if ('amt_to_max_7d_ratio' in explained and
            'EV-MAX-7D-GAP' in project_evidence(ctx)):
        return {'PB-28'}
    return set()


def rank_applicable(ranked, ctx):
    priorities = applicability(ctx)
    for rule in required_review_rules(ctx):
        priorities[rule] = 5
    allowed = [c for c in ranked if c['rule_code'] in priorities]
    return sorted(allowed, key=lambda c: -priorities[c['rule_code']])


async def retrieve_context(retriever, ctx, top_k=5):
    signals = [e['feature'] for e in ctx.get('explanations', [])]
    method = getattr(type(retriever), 'retrieve_context', None)
    if method:
        return await method(retriever, ctx, top_k=top_k)
    return await retriever.retrieve(signals, ctx['risk_level'], top_k=top_k)
