import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import analyst_api.routes.cases as case_routes
from analyst_api.auth import User
from analyst_api.routes.cases import (
    get_dashboard_analytics,
    get_ml_operations,
    list_analytics_transactions,
    list_cases,
    list_classifications,
)
from analyst_api.state import state
from fraud_common.schemas import CaseStatus, RiskLevel, Role


class FakeDatabase:
    def __init__(self):
        self.summary_query = ""
        self.decision_query = ""
        self.queries = []

    async def rfetch(self, sql, *args):
        self.queries.append(("fetch", sql, args))
        return []

    async def rfetchval(self, sql, *args):
        self.queries.append(("fetchval", sql, args))
        return 0

    async def rfetchrow(self, sql, *args):
        if "latest.action" in sql:
            self.decision_query = sql
            return {
                "confirmed_fraud": 4,
                "dismissed": 3,
                "insufficient_evidence": 2,
                "escalated": 1,
                "needs_more_info": 5,
            }
        self.summary_query = sql
        return {
            "open_count": 35356,
            "high_count": 119,
            "medium_count": 287,
            "average_probability": 0.004376305132256544,
            "scoring_latency_ms": 189.33410010636823,
        }


@pytest.mark.asyncio
async def test_review_and_analytics_return_consistent_dashboard_metrics():
    """#sym:test_review_and_analytics_return_consistent_dashboard_metrics
    Kiem tra: metrics dashboard giua review va analytics.
    @param Khong co tham so.
    Dung khi: open/high/average/latency giong nhau va open_count khong loc risk_level.
    Sai khi: metrics lech hoac query loc sai.
    """
    database = FakeDatabase()
    previous_db = state.db
    state.db = database
    user = User("analyst1", Role.ANALYST)
    try:
        review = await list_cases(user=user, limit=1, offset=0)
        analytics = await list_analytics_transactions(user=user, limit=1, offset=0)
        classifications = await list_classifications(
            user=user, decision=None, search=None, limit=1, offset=0
        )
    finally:
        state.db = previous_db

    metric_names = ("open_count", "high_count", "medium_count", "average_probability", "scoring_latency_ms")
    assert {name: review[name] for name in metric_names} == {
        name: analytics[name] for name in metric_names
    }
    assert review["open_count"] == 35356
    assert review["medium_count"] == 287
    assert review["decision_summary"] == analytics["decision_summary"] == classifications["decision_summary"]
    review_query = database.queries[0][1]
    assert "CASE WHEN $10 = 'newest' THEN c.created_at END DESC" in review_query
    assert "row_number() OVER (ORDER BY c.created_at, c.case_id) AS run_number" in review_query
    assert analytics["decision_summary"] == {
        "confirmed_fraud": 4,
        "dismissed": 3,
        "insufficient_evidence": 2,
        "escalated": 1,
        "needs_more_info": 5,
    }
    assert "ORDER BY ca.case_id, ca.created_at DESC, ca.id DESC" in database.decision_query
    open_count_filter = database.summary_query.split(") AS open_count")[0]
    assert "risk_level" not in open_count_filter


@pytest.mark.asyncio
async def test_analytics_excludes_cases_with_manual_decisions():
    database = FakeDatabase()
    previous_db = state.db
    state.db = database
    try:
        result = await list_analytics_transactions(
            user=User("analyst1", Role.ANALYST), limit=10, offset=0
        )
    finally:
        state.db = previous_db

    transactions_query = database.queries[0][1]
    total_query = database.queries[1][1]
    assert "row_number() OVER (ORDER BY c.created_at, c.case_id) AS run_number" in transactions_query
    decision_actions = (
        "OUTCOME_CONFIRMED_FRAUD",
        "OUTCOME_DISMISSED",
        "OUTCOME_INSUFFICIENT_EVIDENCE",
        "ESCALATE",
        "NEEDS_MORE_INFO",
        "CONFIRM_SIMULATED",
    )
    for query in (transactions_query, total_query):
        assert "NOT EXISTS" in query
        assert "FROM case_actions ca" in query
        for action in decision_actions:
            assert f"'{action}'" in query
    assert result["total"] == 0
    assert result["decision_summary"]["confirmed_fraud"] == 4


@pytest.mark.asyncio
async def test_analytics_returns_latest_decisions_grouped_by_model_risk():
    database = FakeDatabase()
    expected = [
        {"risk_level": "HIGH", "action": "OUTCOME_CONFIRMED_FRAUD", "decision_count": 8},
        {"risk_level": "LOW", "action": "OUTCOME_DISMISSED", "decision_count": 2},
    ]

    async def grouped_fetch(sql, *args):
        database.queries.append(("fetch", sql, args))
        return expected if "decision_count" in sql else []

    database.rfetch = grouped_fetch
    previous_db = state.db
    state.db = database
    try:
        result = await list_analytics_transactions(
            user=User("analyst1", Role.ANALYST), limit=10, offset=0
        )
    finally:
        state.db = previous_db

    assert result["decision_breakdown"] == expected
    query = next(sql for kind, sql, _ in database.queries if kind == "fetch" and "decision_count" in sql)
    assert "latest.action" in query
    assert "ORDER BY ca.created_at DESC, ca.id DESC" in query
    assert "GROUP BY c.risk_level, latest.action" in query


@pytest.mark.asyncio
async def test_review_queue_keeps_all_cases_for_risk_lead_when_status_is_unfiltered():
    database = FakeDatabase()
    previous_db = state.db
    state.db = database
    try:
        await list_cases(user=User("lead1", Role.RISK_LEAD), limit=10, offset=0)
    finally:
        state.db = previous_db

    _, query, args = database.queries[0]
    assert args[1] is None
    assert "($2::text IS NULL OR c.status = $2)" in query


@pytest.mark.asyncio
async def test_table_filters_reach_review_analytics_and_decision_history_queries():
    database = FakeDatabase()
    previous_db = state.db
    state.db = database
    user = User("analyst1", Role.ANALYST)
    filters = {
        "status": CaseStatus.OPEN,
        "stream": "UNSCORED",
        "merchant_type": "grocery_pos",
        "score_band": "75-100",
        "sort": "oldest",
    }
    try:
        await list_cases(
            user=user, risk_level=RiskLevel.HIGH, limit=10, offset=0, **filters
        )
        await list_analytics_transactions(
            user=user, risk_level=RiskLevel.HIGH, limit=10, offset=0, **filters
        )
        await list_classifications(
            user=user, decision=None, search=None, limit=10, offset=0, **filters
        )
    finally:
        state.db = previous_db

    filter_queries = [
        (sql, args)
        for kind, sql, args in database.queries
        if kind == "fetch" and "numbered_cases" in sql
    ]
    total_queries = [
        (sql, args)
        for kind, sql, args in database.queries
        if kind == "fetchval"
    ]
    assert len(filter_queries) == 3
    assert len(total_queries) == 3
    assert filter_queries[0][1] == (
        RiskLevel.HIGH, CaseStatus.OPEN, "UNSCORED", "grocery_pos",
        0.75, None, None, None, None, "oldest", 10, 0,
    )
    assert filter_queries[1][1] == filter_queries[0][1]
    assert filter_queries[2][1] == (
        None, None, CaseStatus.OPEN, "UNSCORED", "grocery_pos",
        0.75, None, "oldest", 10, 0,
    )
    assert all("s.calibrated_prob >= " in sql for sql, _ in filter_queries)
    assert all("payload_masked->>'category'" in sql for sql, _ in filter_queries)
    assert all("s.status" in sql and "c.status" in sql for sql, _ in filter_queries)
    assert all(
        "CASE WHEN $10 = 'newest'" in sql or "CASE WHEN $8 = 'newest'" in sql
        for sql, _ in filter_queries
    )
    assert total_queries[0][1] == total_queries[1][1] == (
        RiskLevel.HIGH, CaseStatus.OPEN, "UNSCORED", "grocery_pos",
        0.75, None, None, None, None
    )
    assert total_queries[2][1] == (
        None, None, CaseStatus.OPEN, "UNSCORED", "grocery_pos", 0.75, None
    )


@pytest.mark.asyncio
async def test_received_and_evidence_filters_reach_review_and_analytics_queries():
    database = FakeDatabase()
    previous_db = state.db
    state.db = database
    filters = {
        "user": User("analyst1", Role.ANALYST),
        "risk_level": RiskLevel.HIGH,
        "status": CaseStatus.OPEN,
        "score_band": "75-100",
        "received_from": datetime(2026, 11, 1, tzinfo=timezone.utc),
        "received_before": datetime(2026, 11, 18, tzinfo=timezone.utc),
        "evidence_status": "READY",
        "sort": "oldest",
        "limit": 10,
        "offset": 0,
    }
    try:
        await list_cases(**filters)
        await list_analytics_transactions(**filters)
    finally:
        state.db = previous_db

    row_queries = [
        (sql, args)
        for kind, sql, args in database.queries
        if kind == "fetch" and "numbered_cases" in sql
    ]
    total_queries = [
        (sql, args) for kind, sql, args in database.queries if kind == "fetchval"
    ]
    expected_filters = (
        RiskLevel.HIGH, CaseStatus.OPEN, None, None, 0.75, None,
        datetime(2026, 11, 1, tzinfo=timezone.utc),
        datetime(2026, 11, 18, tzinfo=timezone.utc), "READY",
    )
    assert len(row_queries) == len(total_queries) == 2
    assert row_queries[0][1] == row_queries[1][1] == (*expected_filters, "oldest", 10, 0)
    assert total_queries[0][1] == total_queries[1][1] == expected_filters
    for sql, _ in row_queries + total_queries:
        assert "c.created_at >= $7::timestamptz" in sql
        assert "c.created_at < $8::timestamptz" in sql
        assert "b.status = $9" in sql
        assert "b.case_id IS NULL" in sql


@pytest.mark.asyncio
async def test_dashboard_returns_metrics_decisions_and_signal_health():
    class DashboardDatabase(FakeDatabase):
        async def rfetchrow(self, sql, *args):
            self.queries.append(("fetchrow", sql, args))
            self.summary_query = sql
            return {
                "total_scored": 100,
                "scored_last_hour": 5,
                "open_count": 10,
                "high_open": 3,
                "high_open_last_hour": 1,
                "medium_open": 7,
                "medium_open_last_hour": 2,
                "unscored_total": 4,
                "unscored_last_24h": 2,
                "latency_p95_ms": 120.5,
                "average_probability": 0.12,
            }

        async def rfetch(self, sql, *args):
            self.queries.append(("fetch", sql, args))
            if "GROUP BY c.risk_level, latest.action" in sql:
                return [{"risk_level": "HIGH", "action": "OUTCOME_CONFIRMED_FRAUD", "decision_count": 3}]
            if "average_impact" in sql:
                return [{"feature": "amt", "occurrences": 7, "average_impact": 0.42}]
            if "validation_errors" in sql:
                return [{"field": "amt", "message": "Input should be a valid number", "occurrences": 2}]
            return [{"case_id": 1, "risk_level": "HIGH", "calibrated_prob": 0.9}]

    class DashboardRedis:
        async def llen(self, key):
            return {"ml_jobs:dead": 1, "case_events:dead": 2, "llm_jobs:dead": 3}[key]

    database = DashboardDatabase()
    previous_db = state.db
    previous_redis = state.redis
    state.db = database
    state.redis = DashboardRedis()
    try:
        result = await get_dashboard_analytics(user=User("analyst1", Role.ANALYST))
    finally:
        state.db = previous_db
        state.redis = previous_redis

    assert result["summary"]["total_scored"] == 100
    assert result["summary"]["latency_p95_ms"] == 120.5
    assert result["decision_breakdown"][0]["decision_count"] == 3
    assert result["recent_cases"][0]["case_id"] == 1
    assert result["signal_health"]["top_features"] == [
        {"feature": "amt", "occurrences": 7, "average_impact": 0.42}
    ]
    assert result["signal_health"]["validation_issues"][0]["field"] == "amt"
    assert result["signal_health"]["unscored_last_24h"] == 2
    assert [item["count"] for item in result["signal_health"]["dead_letters"]] == [1, 2, 3]
    assert len(database.queries) == 5
    decision_query = next(sql for kind, sql, _ in database.queries if kind == "fetch" and "decision_count" in sql)
    assert "ORDER BY ca.created_at DESC, ca.id DESC" in decision_query
    assert "s.scored_at >= now() - interval '60 minutes'" in database.summary_query


@pytest.mark.asyncio
async def test_ml_operations_queries_prometheus_latency_and_throughput(monkeypatch):
    observed_queries = []

    class FakeResponse:
        def __init__(self, timestamp):
            self.timestamp = timestamp

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": "success",
                "data": {"result": [{"values": [[self.timestamp, "12.5"]]}]},
            }

    class FakeAsyncClient:
        def __init__(self, base_url, timeout):
            assert base_url == "http://prometheus.test"
            assert timeout == 5

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            pass

        async def get(self, path, params):
            assert path == "/api/v1/query_range"
            observed_queries.append(params["query"])
            return FakeResponse(params["start"])

    monkeypatch.setattr(case_routes, "get_settings", lambda: SimpleNamespace(prometheus_url="http://prometheus.test"))
    monkeypatch.setattr(case_routes.httpx, "AsyncClient", FakeAsyncClient)

    result = await get_ml_operations(user=User("analyst1", Role.ANALYST), window_minutes=60)

    assert result["window_minutes"] == 60
    assert result["latency_p95_ms"][0]["value"] == 12.5
    assert result["throughput_rps"][0]["value"] == 12.5
    assert any("fraud_inference_seconds_bucket" in query for query in observed_queries)
    assert any("fraud_inference_requests_total" in query for query in observed_queries)


@pytest.mark.parametrize(
    "endpoint",
    [list_cases, list_analytics_transactions, list_classifications],
)
def test_case_lists_are_limited_to_50_per_page(endpoint):
    limit = inspect.signature(endpoint).parameters["limit"].default
    assert limit.default == 50
    assert any(getattr(constraint, "le", None) == 50 for constraint in limit.metadata)
