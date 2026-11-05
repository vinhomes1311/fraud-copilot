"""Keep all scored risks in review; only MEDIUM/HIGH request automatic briefs."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fraud_common.schemas import JobEnvelope, RiskLevel, ScoringOutput, ThresholdConfig
import scoring_worker.processor as scoring


@pytest.mark.parametrize('probability,level', [
    (0.1, RiskLevel.LOW), (0.5, RiskLevel.MEDIUM), (0.9, RiskLevel.HIGH)])
@pytest.mark.asyncio
async def test_scored_risks_create_review_cases_and_only_non_low_publish_brief_events(
        monkeypatch, probability, level):
    conn = AsyncMock()
    conn.fetchrow.return_value = {'txn_id': 'review-test'}
    conn.fetchval.return_value = 42

    @asynccontextmanager
    async def write_tx():
        yield conn

    db = SimpleNamespace(write_tx=write_tx)
    events = AsyncMock()
    processor = scoring.ScoringProcessor(db, events)
    processor.thresholds.get = AsyncMock(return_value=ThresholdConfig(
        version=1, low_max=0.3, high_min=0.7))
    monkeypatch.setattr(scoring, 'compute_features', AsyncMock(return_value={'amt': 10}))
    monkeypatch.setattr(scoring, 'predict', lambda features: ScoringOutput(
        raw_model_score=probability, calibrated_prob=probability,
        model_version='test', explanations=[]))
    env = JobEnvelope(job_type='score_transaction', trace_id='review-test', payload={
        'txn_id': 'review-test', 'raw': {
            'trans_num': 'review-test', 'trans_date_trans_time': '2026-11-04T12:00:00Z',
            'cc_num': '4111111111111111', 'merchant': 'test', 'category': 'grocery_pos',
            'amt': 10, 'lat': 10, 'long': 20, 'merch_lat': 10, 'merch_long': 20}})

    await processor.handle(env)

    conn.fetchval.assert_awaited_once()
    sql, txn_id, saved_level = conn.fetchval.call_args.args
    assert 'INSERT INTO cases' in sql
    assert txn_id == 'review-test' and saved_level == level.value
    audit_detail = conn.execute.call_args.args[5]
    assert audit_detail['case_id'] == 42
    if level == RiskLevel.LOW:
        events.publish.assert_not_awaited()
    else:
        events.publish.assert_awaited_once()
        event = events.publish.call_args.args[0]
        assert event.payload['case_id'] == 42
        assert event.payload['risk_level'] == level.value
