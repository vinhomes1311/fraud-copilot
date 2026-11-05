"""Score frozen playbook references independently of model generation."""
import argparse
import hashlib
import json
from pathlib import Path


def validate_references(cases, allowed):
    seen = set()
    for case in cases:
        if case['id'] in seen:
            raise ValueError('Duplicate reference case')
        seen.add(case['id'])
        groups = case.get('reference_groups')
        forbidden = case.get('forbidden_rules', [])
        if not isinstance(groups, list) or not groups:
            raise ValueError('Require reference groups')
        for group in groups:
            if not isinstance(group, list) or not group or any(not isinstance(x, str) for x in group):
                raise ValueError('Require nonempty rule alternatives')
            if len(group) != len(set(group)) or not set(group) <= allowed:
                raise ValueError('Invalid reference rule')
        if not isinstance(forbidden, list) or any(not isinstance(x, str) for x in forbidden):
            raise ValueError('Invalid forbidden rules')
        expected = {x for g in groups for x in g}
        if not set(forbidden) <= allowed or expected & set(forbidden):
            raise ValueError('Contradictory or unknown forbidden rule')
        if not case.get('reference_rationale') or not case.get('reference_version'):
            raise ValueError('Require rationale and reference version')


def score_case(case, sample):
    groups = case['reference_groups']
    retrieved = set(sample.get('retrieved_rules', []))
    selected = set(sample.get('checklist_rules', []))
    retrieval_hits = [bool(retrieved & set(g)) for g in groups]
    selection_hits = [bool(selected & set(g)) for g in groups]
    forbidden = sorted(selected & set(case.get('forbidden_rules', [])))
    valid = bool(sample['ok'])
    return {'case_id': case['id'], 'repeat': sample['repeat'], 'valid': valid,
            'retrieval_covered': all(retrieval_hits),
            'group_coverage': sum(selection_hits) / len(groups) if valid else 0,
            'strict_pass': valid and all(selection_hits) and not forbidden,
            'forbidden_selected': forbidden,
            'missing_groups': [g for g, hit in zip(groups, selection_hits) if not hit],
            'retrieved_rules': sorted(retrieved), 'selected_rules': sorted(selected),
            'failure_stage': sample.get('failure_stage')}


def evaluate(report, cases):
    expected_hash = hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest()
    if report['cases_sha256'] != expected_hash:
        raise ValueError('References differ from the cases frozen for this run')
    lookup = {c['id']: c for c in cases}
    rows = [score_case(lookup[s['case_id']], s) for s in report['samples'] if s['phase'] == 'measured']
    planned = len(cases) * report['config']['repeats'] * len(report['config']['providers'])
    if not planned:
        raise ValueError('No planned measured cases')
    covered = [r for r in rows if r['retrieval_covered'] and r['valid']]
    return {'mode': report['mode'], 'planned': planned, 'attempted': len(rows),
            'valid_rate': sum(r['valid'] for r in rows) / planned,
            'retrieval_coverage_rate': sum(r['retrieval_covered'] for r in rows) / planned,
            'strict_success_rate': sum(r['strict_pass'] for r in rows) / planned,
            'mean_group_coverage': sum(r['group_coverage'] for r in rows) / planned,
            'selection_success_given_retrieved_and_valid':
                sum(r['strict_pass'] for r in covered) / len(covered) if covered else None,
            'forbidden_selection_count': sum(bool(r['forbidden_selected']) for r in rows),
            'cases_sha256': expected_hash, 'samples': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--cases', type=Path, default=Path(__file__).parent / 'fixtures/playbook_v2_cases.json')
    args = parser.parse_args()
    from llm_worker.local_integration import KB_PATH
    cases = json.loads(args.cases.read_text(encoding='utf-8'))
    kb = json.loads(KB_PATH.read_text(encoding='utf-8'))
    validate_references(cases, {c['rule_code'] for c in kb['chunks']})
    report = json.loads(args.report.read_text(encoding='utf-8'))
    result = evaluate(report, cases)
    output = args.report.parent
    (output/'quality.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    lines = ['# Playbook v2 evaluation', '',
             'References authored before inference; team proposals, not independently adjudicated ground truth.',
             'Strict pass requires every reference group and no forbidden rule, plus a valid brief.',
             'Denominator includes all planned measured calls; warm-ups excluded.', '']
    for key, value in result.items():
        if key != 'samples':
            lines.append(f'- {key}: {value}')
    lines += ['', '| Case | Repeat | Valid | Retrieval covers groups | Strict pass | Selected | Missing groups | Forbidden |',
              '|---|---:|---|---|---|---|---|---|']
    for row in result['samples']:
        lines.append('| ' + ' | '.join(str(row[k]) for k in
            ('case_id','repeat','valid','retrieval_covered','strict_pass','selected_rules','missing_groups','forbidden_selected')) + ' |')
    (output/'quality.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k != 'samples'}, indent=2))


if __name__ == '__main__':
    main()
