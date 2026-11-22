"""Author a new synthetic evaluation set without reading model outputs or retrieval.

References are proposals from the documented playbook, not external ground truth.
Run once before inference; the manifest freezes fixture and source hashes.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'benchmarks/fixtures/playbook_holdout_20261118.json'
BASE_FEATURES = {
    'txn_count_24h': 7, 'avg_amt_24h': 83.4, 'amt_vs_avg_ratio': 4.17,
    'trans_count_1h': 2, 'trans_count_7d': 37, 'sum_amt_24h': 583.8,
    'avg_amt_7d': 116.2, 'max_amt_7d': 395.7, 'std_amt_7d': 44.8,
    'amt_to_avg_7d_ratio': 2.97, 'amt_to_max_7d_ratio': 0.878,
    'hour_of_day': 11, 'dist_home_merchant_km': 18.6,
}


def build_cases():
    cases = []

    def add(name, groups, rationale, *, changes=None, remove=(), focus=(), forbidden=(),
            risk='MEDIUM', probability=0.48, category='health_fitness', amount=347.8,
            aliases=False, canary=False):
        features = copy.deepcopy(BASE_FEATURES)
        features.update(changes or {})
        for key in remove:
            features.pop(key, None)
        # Keep derived arithmetic coherent, except explicitly authored invalid
        # baseline/default-value scenarios. Never derive reference labels here.
        changed = changes or {}
        if 'sum_amt_24h' not in changed:
            features['sum_amt_24h'] = round(features.get('txn_count_24h', 0) * features.get('avg_amt_24h', 0), 6)
        for ratio, baseline, window, smoothing in (
            ('amt_vs_avg_ratio', 'avg_amt_24h', 'txn_count_24h', 0),
            ('amt_to_avg_7d_ratio', 'avg_amt_7d', 'trans_count_7d', 1),
            ('amt_to_max_7d_ratio', 'max_amt_7d', 'trans_count_7d', 1),
        ):
            if ratio not in changed:
                denominator = features.get(baseline, 0)
                features[ratio] = (round(amount / (denominator + smoothing), 6)
                                  if features.get(window, 0) > 0 and denominator > 0 else 1)
        factors = [{'feature': name, 'value': features.get(name, 1),
                    'contribution': round(0.93 - i * 0.21, 2)} for i, name in enumerate(focus)]
        if aliases:
            for old, new in [('txn_count_24h', 'trans_count_24h'),
                             ('dist_home_merchant_km', 'customer_merchant_distance_km'),
                             ('hour_of_day', 'transaction_hour')]:
                if old in features:
                    features[new] = features.pop(old)
                for factor in factors:
                    if factor['feature'] == old:
                        factor['feature'] = new
        tx = {'amt': amount, 'category': category}
        if canary:
            tx.update(email='HOLDOUT-PRIVATE-CANARY', merchant='HOLDOUT-PRIVATE-CANARY',
                      is_fraud=1, instructions='HOLDOUT-PRIVATE-CANARY')
            features['unknown_note'] = 'HOLDOUT-PRIVATE-CANARY'
        index = len(cases) + 1
        cases.append({
            'id': 'holdout-' + name,
            'context': {'case_id': 8000 + index, 'txn_id': f'synthetic-unseen-{index:03d}',
                        'risk_level': risk, 'raw_model_score': -1.37 if risk == 'LOW' else 1.43,
                        'calibrated_prob': probability, 'transaction': tx,
                        'features': features, 'explanations': factors},
            'reference_groups': groups,
            'reference_any_rules': sorted({rule for group in groups for rule in group}),
            'forbidden_rules': list(forbidden),
            'reference_rationale': rationale,
            'reference_version': 'heldout-synthetic-20261118-v1-pre-inference',
        })

    add('low-dense', [['PB-26', 'PB-02']],
        'LOW with no focal explanation requires conservative review despite dense incidental statistics.',
        risk='LOW', probability=0.037, forbidden=['PB-27'])
    add('low-aliases', [['PB-26', 'PB-02']],
        'Renamed ML columns do not change conservative LOW review.',
        risk='LOW', probability=0.12, aliases=True, amount=62.3, forbidden=['PB-27'])
    add('low-without-calibration', [['PB-26', 'PB-02'], ['PB-21', 'PB-02']],
        'Both conservative routing and an unavailable calibrated probability need acknowledgement.',
        risk='LOW', probability=None, forbidden=['PB-27', 'PB-22'])
    add('low-recorded-hour', [['PB-19']],
        'A focal hour remains a time-context review under LOW, not evidence requiring HIGH escalation.',
        risk='LOW', probability=0.07, focus=['hour_of_day'], changes={'hour_of_day': 23}, forbidden=['PB-27'])
    add('zero-amount-ratio', [['PB-08']],
        'A supplied zero amount with valid history is a valid comparison, not a missing baseline.',
        amount=0, changes={'amt_vs_avg_ratio': 0}, focus=['amt_vs_avg_ratio'], forbidden=['PB-06', 'PB-07'])
    add('large-valid-24h-ratio', [['PB-08']],
        'A large supplied ratio with a positive baseline requires comparison without inventing fraud thresholds.',
        amount=1837.6, changes={'amt_vs_avg_ratio': 22.03}, focus=['amt_vs_avg_ratio'], forbidden=['PB-06', 'PB-07'])
    add('aliased-24h-count', [['PB-10']],
        'The aliased 24-hour count is the focal window and must retain its meaning.',
        focus=['txn_count_24h'], changes={'txn_count_24h': 19}, aliases=True, forbidden=['PB-06'])
    add('two-frequency-windows', [['PB-09'], ['PB-10']],
        'Two focal count windows require both 1-hour and 24-hour review rather than substituting one for the other.',
        focus=['trans_count_1h', 'txn_count_24h'], changes={'trans_count_1h': 6, 'txn_count_24h': 29})
    add('aliased-geographic-distance', [['PB-18']],
        'The ML distance alias still describes home-to-merchant distance, not device travel.',
        focus=['dist_home_merchant_km'], changes={'dist_home_merchant_km': 428.6}, aliases=True)
    add('zero-geographic-distance', [['PB-18']],
        'Zero is a supplied geographic measurement; it is not a missing value.',
        focus=['dist_home_merchant_km'], changes={'dist_home_merchant_km': 0})
    add('midnight-recorded-hour', [['PB-19']],
        'Recorded hour zero is valid and has no supplied timezone.', focus=['hour_of_day'], changes={'hour_of_day': 0})
    add('aliased-evening-hour', [['PB-19']],
        'An aliased hour needs the same time interpretation limits.',
        focus=['hour_of_day'], changes={'hour_of_day': 21}, aliases=True)
    add('seven-day-count-focus', [['PB-11']],
        'The seven-day count is distinct from the shorter windows.', focus=['trans_count_7d'])
    add('seven-day-mean-focus', [['PB-13']],
        'The seven-day mean must not be replaced by a 24-hour mean.', focus=['avg_amt_7d'])
    add('smoothed-seven-day-mean', [['PB-14']],
        'The focal ratio uses the supplied seven-day mean with the documented +1 denominator.',
        focus=['amt_to_avg_7d_ratio'], forbidden=['PB-12'])
    add('valid-seven-day-maximum', [['PB-15']],
        'The focal maximum ratio is available and cannot be substituted with a mean-ratio rule.',
        focus=['amt_to_max_7d_ratio'], forbidden=['PB-12'])
    add('zero-standard-deviation', [['PB-16']],
        'A zero standard deviation with multiple samples is a valid supplied statistic; avoid a z-score.',
        focus=['std_amt_7d'], changes={'std_amt_7d': 0})
    add('one-sample-standard-deviation', [['PB-28', 'PB-05', 'PB-03']],
        'A single seven-day sample does not support the supplied standard-deviation explanation.',
        focus=['std_amt_7d'], changes={'txn_count_24h': 1, 'trans_count_1h': 1,
            'trans_count_7d': 1, 'avg_amt_7d': 83.4, 'max_amt_7d': 83.4, 'std_amt_7d': 0}, forbidden=['PB-16'])
    add('daily-total-not-mean', [['PB-17']],
        'The focal 24-hour sum must retain its meaning as a total.', focus=['sum_amt_24h'])
    add('unavailable-calibrated-probability', [['PB-21', 'PB-02']],
        'Raw score cannot replace a missing calibrated probability.', probability=None, forbidden=['PB-22'])
    add('empty-short-history-positive-long', [['PB-06'], ['PB-14']],
        'An empty 24-hour window and a valid seven-day amount comparison are separate facts.',
        focus=['amt_to_avg_7d_ratio'], changes={'txn_count_24h': 0, 'trans_count_1h': 0,
            'avg_amt_24h': 0, 'sum_amt_24h': 0, 'amt_vs_avg_ratio': 1}, forbidden=['PB-08', 'PB-12'])
    add('positive-count-missing-daily-mean', [['PB-07', 'PB-03']],
        'A positive daily count with no daily mean cannot be repaired with the valid seven-day mean.',
        focus=['amt_vs_avg_ratio'], remove=['avg_amt_24h'], forbidden=['PB-06', 'PB-08'])
    add('positive-count-zero-daily-mean', [['PB-07', 'PB-03']],
        'A zero mean with positive daily count invalidates the ratio, not the count.',
        focus=['avg_amt_24h'], changes={'avg_amt_24h': 0}, forbidden=['PB-06', 'PB-08'])
    add('missing-long-mean-valid-maximum', [['PB-13', 'PB-28', 'PB-03']],
        'The seven-day maximum cannot stand in for an unavailable seven-day mean.',
        focus=['amt_to_avg_7d_ratio'], remove=['avg_amt_7d'], forbidden=['PB-12', 'PB-14'])
    add('missing-maximum-valid-long-mean', [['PB-28']],
        'A focal maximum ratio without its maximum requires exclusive data repair, despite a valid mean.',
        focus=['amt_to_max_7d_ratio'], remove=['max_amt_7d'], forbidden=['PB-15'])
    add('zero-maximum-valid-long-mean', [['PB-28']],
        'A zero maximum cannot support the focal maximum ratio.',
        focus=['amt_to_max_7d_ratio'], changes={'max_amt_7d': 0}, forbidden=['PB-15'])
    add('zero-long-count-focal-ratio', [['PB-12', 'PB-28']],
        'A default long-window ratio is unavailable when its count is zero.',
        focus=['amt_to_avg_7d_ratio'], changes={'txn_count_24h': 0, 'trans_count_1h': 0,
            'avg_amt_24h': 0, 'sum_amt_24h': 0, 'trans_count_7d': 0, 'avg_amt_7d': 0,
            'max_amt_7d': 0, 'amt_to_avg_7d_ratio': 1, 'amt_to_max_7d_ratio': 1}, forbidden=['PB-14', 'PB-15', 'PB-16'])
    add('two-valid-amount-windows', [['PB-08'], ['PB-14']],
        'Two focal amount ratios refer to different valid windows and both require coverage.',
        focus=['amt_vs_avg_ratio', 'amt_to_avg_7d_ratio'])
    add('geography-and-frequency', [['PB-18'], ['PB-09']],
        'Two different focal signal types require separate geographic and 1-hour frequency review.',
        focus=['dist_home_merchant_km', 'trans_count_1h'], changes={'dist_home_merchant_km': 152.7})
    add('mean-versus-maximum', [['PB-14'], ['PB-15']],
        'Both focal ratios are available but refer to different seven-day statistics.',
        focus=['amt_to_avg_7d_ratio', 'amt_to_max_7d_ratio'])
    add('three-focal-actions', [['PB-18'], ['PB-19'], ['PB-09']],
        'Three valid independent focal signals fit the three-action contract without generic substitutions.',
        focus=['dist_home_merchant_km', 'hour_of_day', 'trans_count_1h'])
    add('untrusted-payload-canary', [['PB-10']],
        'Untrusted identifiers, instructions and fraud labels must not enter the model evidence.',
        focus=['txn_count_24h'], canary=True)
    add('unknown-category-no-history', [['PB-06', 'PB-28']],
        'An unknown category cannot support category-specific review; empty daily history is explicit.',
        category='UNTRUSTED-CATEGORY', focus=['amt_vs_avg_ratio'],
        changes={'txn_count_24h': 0, 'trans_count_1h': 0, 'avg_amt_24h': 0, 'sum_amt_24h': 0},
        forbidden=['PB-20', 'PB-08'])
    add('negative-contribution-distance', [['PB-18']],
        'A signed contribution does not remove the need to interpret its focal geographic feature.',
        focus=['dist_home_merchant_km'], risk='LOW', probability=0.18)
    cases[-1]['context']['explanations'][0]['contribution'] = -0.47
    add('missing-daily-count-with-long-count', [['PB-06'], ['PB-11']],
        'A missing daily count must not be inferred from the available focal seven-day count.',
        focus=['trans_count_7d'], remove=['txn_count_24h'], forbidden=['PB-08', 'PB-12'])
    add('both-history-windows-empty', [['PB-06'], ['PB-12']],
        'Separate missing-history actions are needed for the two empty windows.',
        focus=[], changes={'txn_count_24h': 0, 'trans_count_1h': 0, 'trans_count_7d': 0,
            'avg_amt_24h': 0, 'avg_amt_7d': 0, 'max_amt_7d': 0, 'sum_amt_24h': 0},
        forbidden=['PB-08', 'PB-14', 'PB-15', 'PB-16'])
    return cases


def main():
    if TARGET.exists():
        raise SystemExit('Fixture already frozen; do not overwrite it after inference.')
    cases = build_cases()
    # Hash the same UTF-8/LF bytes that Git stores, independent of the host OS.
    TARGET.write_bytes((json.dumps(cases, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    manifest = {
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'fixture': TARGET.name, 'case_count': len(cases),
        'fixture_hash_format': 'utf-8-lf',
        'fixture_sha256': hashlib.sha256(TARGET.read_bytes()).hexdigest(),
        'canonical_cases_sha256': hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest(),
        'retrieval_policy_sha256': hashlib.sha256((ROOT/'llm_worker/retrieval_policy.py').read_bytes()).hexdigest(),
        'playbook_sha256': hashlib.sha256((ROOT/'knowledge/common_playbook.json').read_bytes()).hexdigest(),
        'status': 'frozen-before-inference',
        'provenance': 'New synthetic contexts and playbook-derived reference proposals authored separately from v2; not independently adjudicated or production data.',
        'procedure': 'Do not tune policy, prompt or reference labels after observing holdout outputs; report all failures.',
    }
    TARGET.with_suffix('.manifest.json').write_bytes((json.dumps(manifest, indent=2)+'\n').encode('utf-8'))
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
