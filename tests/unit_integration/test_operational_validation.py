import pytest

from benchmarks.operational_validation import validate_test_service_urls


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
