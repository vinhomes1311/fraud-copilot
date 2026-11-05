import pytest

from analyst_api.auth import User
from analyst_api.routes.cases import list_analytics_transactions, list_cases
from analyst_api.state import state
from fraud_common.schemas import Role


class FakeDatabase:
    def __init__(self):
        self.summary_query = ""

    async def rfetch(self, sql, *args):
        return []

    async def rfetchval(self, sql, *args):
        return 0

    async def rfetchrow(self, sql, *args):
        self.summary_query = sql
        return {
            "open_count": 35356,
            "high_count": 119,
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

    metric_names = ("open_count", "high_count", "average_probability", "scoring_latency_ms")
    assert {name: review[name] for name in metric_names} == {
        name: analytics[name] for name in metric_names
    }
    assert review["open_count"] == 35356
    open_count_filter = database.summary_query.split(") AS open_count")[0]
    assert "risk_level" not in open_count_filter
