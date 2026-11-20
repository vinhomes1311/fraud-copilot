import pytest

from benchmarks.operational_validation import SyntheticFaultSuccess, validate_test_service_urls


@pytest.mark.parametrize('db,redis', [
    ('', ''),
    ('postgresql://u:p@127.0.0.1:5432/fraud','redis://127.0.0.1:16379/0'),
    ('postgresql://u:p@prod.example:15432/validation','redis://127.0.0.1:16379/0'),
    ('postgresql://u:p@127.0.0.1:15432/validation','redis://127.0.0.1:6379/0'),
])
def test_operational_runner_rejects_non_disposable_endpoints(monkeypatch, db, redis):
    monkeypatch.setenv('FRAUD_TEST_DB_URL',db)
    monkeypatch.setenv('FRAUD_TEST_REDIS_URL',redis)
    with pytest.raises(ValueError,match='dedicated validation'):
        validate_test_service_urls()


def test_operational_runner_accepts_dedicated_local_services(monkeypatch):
    db='postgresql://validation:validation-local-only@127.0.0.1:15432/validation'
    redis='redis://127.0.0.1:16379/0'
    monkeypatch.setenv('FRAUD_TEST_DB_URL',db)
    monkeypatch.setenv('FRAUD_TEST_REDIS_URL',redis)
    assert validate_test_service_urls()==(db,redis)


@pytest.mark.asyncio
async def test_fault_success_fixture_uses_current_explained_brief_contract():
    from benchmarks.compare_llm import load_cases
    from llm_worker.grounding import check_grounding
    from llm_worker.rag import ApprovedPlaybookRetriever

    ctx = load_cases()[0]['context']
    chunks = await ApprovedPlaybookRetriever().retrieve_context(ctx)
    result = await SyntheticFaultSuccess().generate(ctx, chunks)
    check_grounding(result.brief, chunks)
    assert result.tokens_used == 120
    assert all(item.rationale and item.evidence_ids for item in result.brief.checklist)
