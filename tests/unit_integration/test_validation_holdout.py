import copy
import hashlib
import json

import pytest

from benchmarks.build_validation_holdout import ROOT, TARGET, build_cases
import benchmarks.build_validation_holdout as holdout_builder
from benchmarks.compare_llm import load_cases
from benchmarks.playbook_eval import validate_references
from llm_worker.local_integration import KB_PATH, canonical_features, project_evidence
from llm_worker.rag import ApprovedPlaybookRetriever
from llm_worker.retrieval_policy import retrieve_context


def test_holdout_is_frozen_new_and_arithmetically_coherent():
    cases = load_cases(TARGET)
    assert cases == build_cases() and len(cases) == 36
    manifest = json.loads(TARGET.with_suffix('.manifest.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(TARGET.read_bytes()).hexdigest() == manifest['fixture_sha256']
    assert hashlib.sha256(json.dumps(cases, sort_keys=True).encode()).hexdigest() == manifest['canonical_cases_sha256']
    old = []
    for name in ('cases.json', 'playbook_v2_cases.json', 'playbook_v2_boundary_cases.json'):
        old += load_cases(ROOT / 'benchmarks/fixtures' / name)

    def evidence_context(case):
        ctx = copy.deepcopy(case['context'])
        ctx.pop('case_id'); ctx.pop('txn_id')
        return json.dumps(ctx, sort_keys=True)

    assert not {evidence_context(c) for c in cases} & {evidence_context(c) for c in old}
    allowed = {c['rule_code'] for c in json.loads(KB_PATH.read_text(encoding='utf-8'))['chunks']}
    validate_references(cases, allowed)
    for case in cases:
        ctx = case['context']
        features = canonical_features(ctx['features'])
        count = features.get('txn_count_24h', 0)
        assert features['trans_count_1h'] >= 0
        if 'txn_count_24h' in features:
            assert features['trans_count_1h'] <= count
        assert count <= features['trans_count_7d']
        if 'avg_amt_24h' in features:
            assert features['sum_amt_24h'] == pytest.approx(count * features['avg_amt_24h'])
        assert 'HOLDOUT-PRIVATE-CANARY' not in json.dumps(project_evidence(ctx))


def test_holdout_writer_uses_portable_bytes_and_does_not_overwrite(tmp_path, monkeypatch):
    target = tmp_path / 'holdout.json'
    monkeypatch.setattr(holdout_builder, 'TARGET', target)
    holdout_builder.main()

    fixture = target.read_bytes()
    manifest_bytes = target.with_suffix('.manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest['fixture_hash_format'] == 'utf-8-lf'
    assert b'\r' not in fixture and b'\r' not in manifest_bytes
    assert fixture.endswith(b'\n') and manifest_bytes.endswith(b'\n')
    assert json.loads(fixture) == build_cases()
    assert hashlib.sha256(fixture).hexdigest() == manifest['fixture_sha256']

    with pytest.raises(SystemExit, match='already frozen'):
        holdout_builder.main()
    assert target.read_bytes() == fixture


@pytest.mark.asyncio
async def test_report_context_coverage_without_tuning_holdout_references():
    cases = load_cases(TARGET)
    # These checks measure coverage; references never feed the retriever.
    covered = 0
    for case in cases:
        before = copy.deepcopy(case['context'])
        chunks = await retrieve_context(ApprovedPlaybookRetriever(), case['context'], top_k=5)
        ids = {c.rule_code for c in chunks}
        covered += all(ids.intersection(g) for g in case['reference_groups'])
        assert len(chunks) <= 5 and case['context'] == before
        assert not ids.intersection(case['forbidden_rules'])
    assert covered > 0  # Do not force 100% by adapting holdout labels or policy.
