"""Xác thực + RBAC. Simulator dùng X-API-Key; UI dùng JWT (Bearer)."""
from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fraud_common.config import get_settings
from fraud_common.schemas import Role

from .state import state

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class User:
    username: str
    role: Role


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


async def authenticate(username: str, password: str) -> User | None:
    row = await state.db.rfetchrow(
        "SELECT username, role, password_hash, email_verified FROM users WHERE username=$1 AND active=true",
        username)
    if row and row["email_verified"] and verify_password(password, row["password_hash"]):
        return User(row["username"], Role(row["role"]))
    return None


def create_token(user: User) -> str:
    s = get_settings()
    now = int(time.time())
    return jwt.encode(
        {"sub": user.username, "role": user.role.value, "iat": now, "exp": now + s.jwt_ttl_s},
        s.jwt_secret, algorithm="HS256")


async def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    if creds is None:
        raise HTTPException(401, "Thiếu token", headers={"WWW-Authenticate": "Bearer"})
    try:
        data = jwt.decode(creds.credentials, get_settings().jwt_secret, algorithms=["HS256"])
        return User(data["sub"], Role(data["role"]))
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(401, "Token không hợp lệ hoặc hết hạn", headers={"WWW-Authenticate": "Bearer"})


def require_roles(*roles: Role):
    async def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "Không đủ quyền")
        return user
    return dep


async def require_simulator(x_api_key: str | None = Header(default=None)) -> str:
    expected = get_settings().simulator_api_key
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "API key không hợp lệ")
    return "simulator"


# Cho phép cả simulator (API key) lẫn người dùng đã đăng nhập (JWT)
async def simulator_or_user(
    x_api_key: str | None = Header(default=None),
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    if x_api_key:
        return await require_simulator(x_api_key)
    return await current_user(creds)
