import copy
import hashlib
import json

import pytest

from benchmarks.compare_llm import load_cases, ROOT
from benchmarks.playbook_eval import evaluate, score_case, validate_references
from llm_worker.local_integration import KB_PATH, project_evidence


def fixtures():
    return load_cases(ROOT/'benchmarks/playbook_v2_cases.json')


def test_references_valid_and_cover_detailed_rules():
    cases = fixtures()
    allowed = {c['rule_code'] for c in json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']}
    validate_references(cases, allowed)
    assert len(cases) >= 20
    assert len({r for c in cases for g in c['reference_groups'] for r in g}) >= 20
    for case in cases:
        evidence = json.dumps(project_evidence(case['context']))
        assert case['reference_rationale'] not in evidence
        assert case['context']['txn_id'] not in evidence


def test_partial_group_match_does_not_count_as_strict_success():
    case = {'id':'example','reference_groups':[['PB-18'],['PB-24','PB-06']], 'forbidden_rules':['PB-12']}
    row = {'ok':True,'repeat':0,'retrieved_rules':['PB-18','PB-24'],'checklist_rules':['PB-18']}
    score = score_case(case, row)
    assert score['retrieval_covered'] and not score['strict_pass']
    assert score['group_coverage'] == .5
    row['checklist_rules'] += ['PB-24']
    assert score_case(case,row)['strict_pass']
    row['checklist_rules'] += ['PB-12']
    assert not score_case(case,row)['strict_pass']
    row['ok'] = False
    assert score_case(case,row)['group_coverage'] == 0


def test_references_cannot_change_after_run_and_skipped_calls_count():
    cases = fixtures()[:1]
    report = {'cases_sha256':hashlib.sha256(json.dumps(cases,sort_keys=True).encode()).hexdigest(),
              'mode':'mock','config':{'repeats':2,'providers':['ollama']},
              'samples':[{'phase':'measured','ok':True,'repeat':0,'case_id':cases[0]['id'],
                          'retrieved_rules':['PB-08'],'checklist_rules':['PB-08']}]}
    assert evaluate(report,cases)['strict_success_rate'] == .5
    changed = copy.deepcopy(cases)
    changed[0]['reference_rationale'] += ' changed'
    with pytest.raises(ValueError, match='frozen'):
        evaluate(report,changed)


def test_invalid_and_conflicting_references_fail():
    cases = fixtures()[:1]
    with pytest.raises(ValueError):
        validate_references(cases, set())
    cases[0]['forbidden_rules'] = cases[0]['reference_groups'][0][:]
    allowed = {c['rule_code'] for c in json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']}
    with pytest.raises(ValueError, match='Contradictory'):
        validate_references(cases,allowed)
