"""Unit & Integration tests for Authentication, Authorization (RBAC) and Auth Routes.

Covers:
- analyst_api/auth.py:
  * hash_password and verify_password edge cases (invalid format, wrong algorithm)
  * authenticate (user not found, email unverified, bad password, successful auth)
  * create_token and current_user (missing token, invalid/expired JWT, missing claims, invalid role)
  * require_roles (403 when role unauthorized, success when role matches)
  * require_simulator (401 when key missing or wrong, success when valid)
  * simulator_or_user (handles both X-API-Key and Bearer JWT)
- analyst_api/routes/auth_routes.py:
  * login (invalid credentials 401, valid credentials 200)
  * register (invalid username chars 422, duplicate username 409)
  * verify_email (valid token, expired/used token 400)
  * forgot_password (email exists & sends mail, email not found, SMTP unconfigured 503)
  * reset_password (valid token, invalid/expired token 400)
  * google_login (unconfigured 503, configured redirect)
  * google_callback (existing user, new user provisioning)
"""
from __future__ import annotations

import hashlib
import inspect
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import asyncpg
import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from analyst_api.auth import (
    User,
    authenticate,
    create_token,
    current_user,
    hash_password,
    require_roles,
    require_simulator,
    simulator_or_user,
    verify_password,
)
from analyst_api.routes.auth_routes import (
    EmailRequest,
    LoginRequest,
    RegisterRequest,
    ResetRequest,
    forgot_password,
    google_callback,
    google_login,
    login,
    register,
    reset_password,
    verify_email,
)
from analyst_api.state import state
from fraud_common.config import get_settings
from fraud_common.schemas import Role


# ---------------------------------------------------------------------------
# Mock Database Helpers
# ---------------------------------------------------------------------------

class MockDb:
    def __init__(self):
        self.rfetchrow_handler = None
        self.wfetchrow_handler = None
        self.wexec_handler = None
        self.tx_conn = None

    async def rfetchrow(self, sql, *args):
        if self.rfetchrow_handler:
            res = self.rfetchrow_handler(sql, *args)
            if inspect.isawaitable(res):
                return await res
            return res
        return None

    async def wfetchrow(self, sql, *args):
        if self.wfetchrow_handler:
            res = self.wfetchrow_handler(sql, *args)
            if inspect.isawaitable(res):
                return await res
            return res
        return None

    async def wexec(self, sql, *args):
        if self.wexec_handler:
            res = self.wexec_handler(sql, *args)
            if inspect.isawaitable(res):
                return await res
            return res
        return None

    @asynccontextmanager
    async def write_tx(self):
        yield self.tx_conn


# ===========================================================================
# 1. Tests for analyst_api/auth.py
# ===========================================================================

def test_verify_password_edge_cases():
    hashed = hash_password("secret123")
    assert verify_password("secret123", hashed) is True
    assert verify_password("wrong_password", hashed) is False

    # Invalid string formats that could crash split("$") or hex decoding
    assert verify_password("secret123", "invalid_format_no_dollar") is False
    assert verify_password("secret123", "md5$salt$hash") is False  # wrong algorithm
    assert verify_password("secret123", "scrypt$nothex$nothex") is False
    assert verify_password("secret123", "") is False


@pytest.mark.asyncio
async def test_authenticate_scenarios():
    db = MockDb()
    prev_db = state.db
    state.db = db

    try:
        # Scenario 1: User does not exist
        db.rfetchrow_handler = lambda sql, username: None
        res = await authenticate("unknown_user", "pass123")
        assert res is None

        # Scenario 2: User exists but email is not verified
        password_hash = hash_password("pass123")
        db.rfetchrow_handler = lambda sql, username: {
            "username": "unverified",
            "role": "analyst",
            "password_hash": password_hash,
            "email_verified": False,
        }
        res = await authenticate("unverified", "pass123")
        assert res is None

        # Scenario 3: User exists and verified, but wrong password
        db.rfetchrow_handler = lambda sql, username: {
            "username": "valid_user",
            "role": "analyst",
            "password_hash": password_hash,
            "email_verified": True,
        }
        res = await authenticate("valid_user", "wrong_pass")
        assert res is None

        # Scenario 4: User exists, verified, correct password
        res = await authenticate("valid_user", "pass123")
        assert res is not None
        assert res.username == "valid_user"
        assert res.role == Role.ANALYST
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_current_user_valid_and_missing_token():
    user = User(username="analyst_bob", role=Role.ANALYST)
    token = create_token(user)

    # 1. Valid token
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    authed_user = await current_user(creds)
    assert authed_user.username == "analyst_bob"
    assert authed_user.role == Role.ANALYST

    # 2. Missing credentials (creds is None) -> 401 "Thiếu token"
    with pytest.raises(HTTPException) as exc_info:
        await current_user(None)
    assert exc_info.value.status_code == 401
    assert "Thiếu token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_current_user_invalid_and_expired_token():
    s = get_settings()

    # 1. Malformed token
    with pytest.raises(HTTPException) as exc_info:
        await current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad.token.here"))
    assert exc_info.value.status_code == 401
    assert "Token không hợp lệ hoặc hết hạn" in exc_info.value.detail

    # 2. Expired token
    now = int(time.time())
    expired_payload = {"sub": "alice", "role": "analyst", "iat": now - 7200, "exp": now - 3600}
    expired_token = jwt.encode(expired_payload, s.jwt_secret, algorithm="HS256")
    with pytest.raises(HTTPException) as exc_info:
        await current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=expired_token))
    assert exc_info.value.status_code == 401
    assert "Token không hợp lệ hoặc hết hạn" in exc_info.value.detail

    # 3. Token with invalid role
    bad_role_payload = {"sub": "alice", "role": "super_admin_does_not_exist", "iat": now, "exp": now + 3600}
    bad_role_token = jwt.encode(bad_role_payload, s.jwt_secret, algorithm="HS256")
    with pytest.raises(HTTPException) as exc_info:
        await current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=bad_role_token))
    assert exc_info.value.status_code == 401

    # 4. Token missing "sub"
    no_sub_payload = {"role": "analyst", "iat": now, "exp": now + 3600}
    no_sub_token = jwt.encode(no_sub_payload, s.jwt_secret, algorithm="HS256")
    with pytest.raises(HTTPException) as exc_info:
        await current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=no_sub_token))
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_roles_enforcement():
    lead_or_admin = require_roles(Role.RISK_LEAD, Role.ADMIN)

    analyst_user = User(username="staff", role=Role.ANALYST)
    lead_user = User(username="boss", role=Role.RISK_LEAD)

    # Authorized role -> passes
    res = await lead_or_admin(lead_user)
    assert res == lead_user

    # Unauthorized role -> 403 Forbidden
    with pytest.raises(HTTPException) as exc_info:
        await lead_or_admin(analyst_user)
    assert exc_info.value.status_code == 403
    assert "Không đủ quyền" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_simulator():
    valid_key = get_settings().simulator_api_key

    # Valid key -> passes
    assert await require_simulator(valid_key) == "simulator"

    # Missing header -> 401
    with pytest.raises(HTTPException) as exc_info:
        await require_simulator(None)
    assert exc_info.value.status_code == 401
    assert "API key không hợp lệ" in exc_info.value.detail

    # Wrong key -> 401
    with pytest.raises(HTTPException) as exc_info:
        await require_simulator("wrong-simulator-key")
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_simulator_or_user():
    valid_key = get_settings().simulator_api_key
    user = User(username="analyst_carol", role=Role.ANALYST)
    token = create_token(user)
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    # 1. Simulator with valid API key
    assert await simulator_or_user(x_api_key=valid_key, creds=None) == "simulator"

    # 2. Simulator with wrong API key -> 401
    with pytest.raises(HTTPException):
        await simulator_or_user(x_api_key="wrong_key", creds=None)

    # 3. User with JWT bearer
    res = await simulator_or_user(x_api_key=None, creds=creds)
    assert res.username == "analyst_carol"

    # 4. Neither API key nor Bearer token -> 401
    with pytest.raises(HTTPException):
        await simulator_or_user(x_api_key=None, creds=None)


# ===========================================================================
# 2. Tests for analyst_api/routes/auth_routes.py
# ===========================================================================

@pytest.mark.asyncio
async def test_login_route_success_and_failure():
    db = MockDb()
    prev_db = state.db
    state.db = db

    password_hash = hash_password("correct-pass")
    db.rfetchrow_handler = lambda sql, username: {
        "username": username,
        "role": "analyst",
        "password_hash": password_hash,
        "email_verified": True,
    }

    try:
        # Success
        req = LoginRequest(username="analyst1", password="correct-pass")
        res = await login(req)
        assert res["access_token"]
        assert res["token_type"] == "bearer"
        assert res["role"] == Role.ANALYST

        # Failure: bad password
        req_bad = LoginRequest(username="analyst1", password="wrong-pass")
        with pytest.raises(HTTPException) as exc_info:
            await login(req_bad)
        assert exc_info.value.status_code == 401
        assert "Sai tài khoản hoặc mật khẩu" in exc_info.value.detail
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_register_invalid_username_format_and_duplicate():
    db = MockDb()
    prev_db = state.db
    state.db = db

    try:
        # 1. Invalid username regex format (has spaces or special characters)
        req_invalid = RegisterRequest(username="invalid user!", password="password123")
        with pytest.raises(HTTPException) as exc_info:
            await register(req_invalid)
        assert exc_info.value.status_code == 422
        assert "Tên đăng nhập chỉ gồm" in exc_info.value.detail

        # 2. Duplicate username in DB -> UniqueViolationError -> 409
        class ConnDuplicate:
            async def fetchrow(self, sql, *args):
                raise asyncpg.UniqueViolationError("Duplicate key")

        db.tx_conn = ConnDuplicate()
        req_dup = RegisterRequest(username="duplicate-user", password="password123")
        with pytest.raises(HTTPException) as exc_info:
            await register(req_dup)
        assert exc_info.value.status_code == 409
        assert "Tên đăng nhập đã tồn tại" in exc_info.value.detail
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_verify_email_success_and_failure():
    db = MockDb()
    prev_db = state.db
    state.db = db

    try:
        # 1. Valid token
        db.wfetchrow_handler = lambda sql, token_hash: {"user_id": 42}
        wexec_called = []
        db.wexec_handler = lambda sql, user_id: wexec_called.append((sql, user_id))

        res = await verify_email(token="valid_verification_token_1234567890")
        assert res == {"message": "Email đã được xác thực"}
        assert len(wexec_called) == 1
        assert wexec_called[0][1] == 42

        # 2. Invalid or expired token (returns None from update)
        db.wfetchrow_handler = lambda sql, token_hash: None
        with pytest.raises(HTTPException) as exc_info:
            await verify_email(token="invalid_or_expired_token_1234567890")
        assert exc_info.value.status_code == 400
        assert "Liên kết xác thực không hợp lệ hoặc đã hết hạn" in exc_info.value.detail
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_forgot_password_scenarios():
    db = MockDb()
    prev_db = state.db
    prev_settings = state.settings
    state.db = db

    mock_settings = SimpleNamespace(
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="user@example.com",
        smtp_password="pwd",
        smtp_from="noreply@example.com",
        app_base_url="http://localhost:8080",
    )
    state.settings = mock_settings

    try:
        # 1. Email not found in DB -> returns success message (anti-enumeration)
        db.rfetchrow_handler = lambda sql, email: None
        res = await forgot_password(EmailRequest(email="nonexistent@example.com"))
        assert res == {"message": "Nếu email tồn tại, hướng dẫn khôi phục đã được gửi."}

        # 2. Email exists -> inserts token and sends email
        db.rfetchrow_handler = lambda sql, email: {"user_id": 10, "email": "user@example.com"}
        wexec_calls = []
        db.wexec_handler = lambda sql, *args: wexec_calls.append((sql, args))

        with patch("smtplib.SMTP") as mock_smtp:
            server_inst = MagicMock()
            mock_smtp.return_value.__enter__.return_value = server_inst

            res = await forgot_password(EmailRequest(email="user@example.com"))
            assert res == {"message": "Nếu email tồn tại, hướng dẫn khôi phục đã được gửi."}
            assert len(wexec_calls) == 1
            assert server_inst.send_message.called

        # 3. SMTP not configured -> raises 503
        mock_settings.smtp_host = None
        with pytest.raises(HTTPException) as exc_info:
            await forgot_password(EmailRequest(email="user@example.com"))
        assert exc_info.value.status_code == 503
        assert "SMTP chưa được cấu hình" in exc_info.value.detail
    finally:
        state.db = prev_db
        state.settings = prev_settings


@pytest.mark.asyncio
async def test_reset_password_success_and_failure():
    db = MockDb()
    prev_db = state.db
    state.db = db

    try:
        # 1. Valid reset token
        db.rfetchrow_handler = lambda sql, token_hash: {"user_id": 99}
        wexec_calls = []
        db.wexec_handler = lambda sql, *args: wexec_calls.append(args)

        req = ResetRequest(token="reset_token_longer_than_20_chars", password="new-password-123")
        res = await reset_password(req)
        assert res == {"message": "Mật khẩu đã được cập nhật"}
        assert len(wexec_calls) == 1
        assert wexec_calls[0][0] == 99  # user_id updated

        # 2. Invalid or expired token
        db.rfetchrow_handler = lambda sql, token_hash: None
        with pytest.raises(HTTPException) as exc_info:
            await reset_password(req)
        assert exc_info.value.status_code == 400
        assert "Liên kết đặt lại mật khẩu không hợp lệ hoặc đã hết hạn" in exc_info.value.detail
    finally:
        state.db = prev_db


@pytest.mark.asyncio
async def test_google_login_routes():
    prev_settings = state.settings
    prev_db = state.db

    db = MockDb()
    state.db = db

    # 1. google_login: unconfigured client_id -> 503
    state.settings = SimpleNamespace(google_client_id=None)
    with pytest.raises(HTTPException) as exc_info:
        await google_login()
    assert exc_info.value.status_code == 503

    # 2. google_login: configured -> RedirectResponse to Google
    state.settings = SimpleNamespace(
        google_client_id="test-client-id",
        google_redirect_uri="http://localhost:8080/callback",
        google_client_secret="test-secret",
        app_base_url="http://localhost:8080",
        jwt_secret="jwt-secret",
        jwt_ttl_s=3600,
    )
    redirect = await google_login()
    assert "https://accounts.google.com/o/oauth2/v2/auth" in redirect.headers["location"]
    assert "client_id=test-client-id" in redirect.headers["location"]

    # 3. google_callback: existing user
    db.rfetchrow_handler = lambda sql, email: {"username": "existing_google_user", "role": "analyst"}

    class MockHttpxResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class MockHttpxClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            return MockHttpxResponse({"access_token": "google_fake_access_token"})

        async def get(self, url, **kwargs):
            return MockHttpxResponse({"email": "alice@gmail.com", "name": "Alice Google"})

    with patch("httpx.AsyncClient", return_value=MockHttpxClient()):
        resp = await google_callback(code="auth_code_from_google")
        assert resp.status_code == 307
        assert "access_token=" in resp.headers["location"]

    # 4. google_callback: new user provisioning
    db.rfetchrow_handler = lambda sql, email: None
    db.wfetchrow_handler = lambda sql, *args: {"username": args[0], "role": "analyst"}

    with patch("httpx.AsyncClient", return_value=MockHttpxClient()):
        resp_new = await google_callback(code="auth_code_new_user")
        assert resp_new.status_code == 307
        assert "access_token=" in resp_new.headers["location"]

    state.settings = prev_settings
    state.db = prev_db
