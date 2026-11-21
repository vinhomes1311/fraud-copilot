from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from analyst_api.auth import User, verify_password
from analyst_api.routes.auth_routes import (
    LoginRequest,
    RegisterRequest,
    active_day_streak,
    get_profile,
    profile_badges,
    register,
)
from fraud_common.schemas import Role
from analyst_api.state import state


class FakeConnection:
    def __init__(self):
        self.sql = ""
        self.args = ()

    async def fetchrow(self, sql, *args):
        self.sql = sql
        self.args = args
        return {"username": args[0], "role": "analyst"}


class FakeDatabase:
    def __init__(self):
        self.connection = FakeConnection()

    @asynccontextmanager
    async def write_tx(self):
        yield self.connection


class FakeProfileDatabase:
    async def rfetchrow(self, sql, *args):
        assert "FROM case_actions" in sql
        assert args == ("analyst1",)
        today = datetime.now(timezone(timedelta(hours=7))).date()
        return {
            "display_name": "Analyst One",
            "role": "analyst",
            "cases_reviewed": 25,
            "active_days": 3,
            "activity_days": [today, today - timedelta(days=1), today - timedelta(days=3)],
        }


@pytest.mark.asyncio
async def test_register_creates_email_optional_user_and_returns_login_token():
    """#sym:test_register_creates_email_optional_user_and_returns_login_token
    Kiem tra: dang ky user va tao login token.
    @param Khong co tham so.
    Dung khi: user duoc insert, password hash dung va token co role bearer.
    Sai khi: SQL, hash, token hoac role sai.
    """
    database = FakeDatabase()
    previous_db = state.db
    state.db = database
    try:
        assert set(RegisterRequest.model_fields) == {"username", "password"}
        body = RegisterRequest(username="new-analyst", password="secure-pass-123")
        result = await register(body)
    finally:
        state.db = previous_db

    assert "VALUES ($1, NULL, $1, $2, 'analyst', true)" in database.connection.sql
    assert database.connection.args[0] == "new-analyst"
    assert verify_password(body.password, database.connection.args[1])
    assert result["access_token"]
    assert result["token_type"] == "bearer"
    assert result["role"] == "analyst"


def test_login_accepts_legacy_short_password_but_registration_enforces_minimum():
    login = LoginRequest(username="lead1", password="lead123")
    assert login.password == "lead123"

    with pytest.raises(ValidationError):
        RegisterRequest(username="new-analyst", password="short7")


def test_active_day_streak_accepts_today_or_yesterday_and_stops_at_gaps():
    today = date(2026, 11, 17)
    assert active_day_streak([today, today - timedelta(days=1)], today) == 2
    assert active_day_streak([today - timedelta(days=1), today - timedelta(days=2)], today) == 2
    assert active_day_streak([today - timedelta(days=3)], today) == 0


def test_profile_badges_show_milestone_progress_and_unlocks():
    badges = profile_badges(25)
    assert [(badge["name"], badge["earned"], badge["progress"]) for badge in badges] == [
        ("Expert", True, 25),
        ("Master", False, 25),
    ]


@pytest.mark.asyncio
async def test_profile_returns_activity_and_badges_for_authenticated_user():
    previous_db = state.db
    state.db = FakeProfileDatabase()
    try:
        profile = await get_profile(User("analyst1", Role.ANALYST))
    finally:
        state.db = previous_db

    assert profile["display_name"] == "Analyst One"
    assert profile["cases_reviewed"] == 25
    assert profile["active_days"] == 3
    assert profile["active_day_streak"] == 2
    assert profile["badges"][0]["earned"] is True
    assert profile["badges"][1]["earned"] is False
