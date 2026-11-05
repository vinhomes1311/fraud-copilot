import inspect

import pytest

from analyst_api.auth import User
from analyst_api.routes.cases import list_analytics_transactions, list_cases, list_classifications
from analyst_api.state import state
from fraud_common.schemas import Role


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
    finally:
        state.db = previous_db

    metric_names = ("open_count", "high_count", "medium_count", "average_probability", "scoring_latency_ms")
    assert {name: review[name] for name in metric_names} == {
        name: analytics[name] for name in metric_names
    }
    assert review["open_count"] == 35356
    assert review["medium_count"] == 287
    review_query = database.queries[0][1]
    assert "ORDER BY c.created_at DESC, c.case_id DESC" in review_query
    assert "row_number() OVER (ORDER BY c.created_at, c.case_id) AS run_number" in review_query
    assert analytics["decision_summary"] == {
        "confirmed_fraud": 4,
        "dismissed": 3,
        "insufficient_evidence": 2,
        "escalated": 1,
        "needs_more_info": 5,
    }
    assert "ORDER BY ca.created_at DESC, ca.id DESC" in database.decision_query
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


@pytest.mark.parametrize(
    "endpoint",
    [list_cases, list_analytics_transactions, list_classifications],
)
def test_case_lists_are_limited_to_50_per_page(endpoint):
    limit = inspect.signature(endpoint).parameters["limit"].default
    assert limit.default == 50
    assert any(getattr(constraint, "le", None) == 50 for constraint in limit.metadata)
