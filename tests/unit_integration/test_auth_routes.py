from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from analyst_api.auth import verify_password
from analyst_api.routes.auth_routes import LoginRequest, RegisterRequest, register
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
