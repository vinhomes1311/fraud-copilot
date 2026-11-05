from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import hashlib
import re
import secrets
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode

import asyncpg
import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse

from ..auth import User, authenticate, create_token, hash_password
from fraud_common.schemas import Role
from ..state import state

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    # Existing accounts may predate the current password policy. Enforce the
    # minimum for new credentials in RegisterRequest and ResetRequest instead.
    password: str = Field(max_length=128)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    password: str = Field(min_length=8, max_length=128)


class EmailRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)


class ResetRequest(BaseModel):
    token: str = Field(min_length=20)
    password: str = Field(min_length=8, max_length=128)


def _token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


async def _send_email(to: str, subject: str, body: str) -> None:
    s = state.settings
    if not s.smtp_host or not s.smtp_from:
        raise HTTPException(503, "SMTP chưa được cấu hình")
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = s.smtp_from, to, subject
    message.set_content(body)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as server:
        server.starttls()
        if s.smtp_username:
            server.login(s.smtp_username, s.smtp_password)
        server.send_message(message)


@router.post("/auth/login")
async def login(body: LoginRequest):
    user = await authenticate(body.username.strip().lower(), body.password)
    if not user:
        raise HTTPException(401, "Sai tài khoản hoặc mật khẩu")
    return {"access_token": create_token(user), "token_type": "bearer", "role": user.role}


@router.post("/auth/register", status_code=201)
async def register(body: RegisterRequest):
    username = body.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,49}", username):
        raise HTTPException(422, "Tên đăng nhập chỉ gồm chữ thường, số, ., _, -")
    async with state.db.write_tx() as conn:
        try:
            row = await conn.fetchrow(
                """INSERT INTO users (username, email, display_name, password_hash, role, email_verified)
                   VALUES ($1, NULL, $1, $2, 'analyst', true) RETURNING username, role""",
                username, hash_password(body.password))
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(409, "Tên đăng nhập đã tồn tại") from exc
    user = User(row["username"], Role(row["role"]))
    return {"access_token": create_token(user), "token_type": "bearer", "role": user.role}


@router.get("/auth/verify")
async def verify_email(token: str = Query(min_length=20)):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = await state.db.wfetchrow(
        """UPDATE auth_tokens SET used_at=now() WHERE token_hash=$1 AND purpose='verify_email'
           AND used_at IS NULL AND expires_at > now() RETURNING user_id""", token_hash)
    if row is None:
        raise HTTPException(400, "Liên kết xác thực không hợp lệ hoặc đã hết hạn")
    await state.db.wexec("UPDATE users SET email_verified=true WHERE user_id=$1", row["user_id"])
    return {"message": "Email đã được xác thực"}


@router.post("/auth/forgot-password")
async def forgot_password(body: EmailRequest):
    row = await state.db.rfetchrow("SELECT user_id, email FROM users WHERE email=$1 AND active=true", body.email.strip().lower())
    if row is not None:
        raw, token_hash = _token()
        await state.db.wexec(
            "INSERT INTO auth_tokens (token_hash,user_id,purpose,expires_at) VALUES ($1,$2,'reset_password',now()+interval '1 hour')",
            token_hash, row["user_id"])
        await _send_email(row["email"], "Reset your Fraud Copilot password",
                          f"Open {state.settings.app_base_url}/?reset={raw} to choose a new password.")
    return {"message": "Nếu email tồn tại, hướng dẫn khôi phục đã được gửi."}


@router.post("/auth/reset-password")
async def reset_password(body: ResetRequest):
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    row = await state.db.rfetchrow(
        """UPDATE auth_tokens SET used_at=now() WHERE token_hash=$1 AND purpose='reset_password'
           AND used_at IS NULL AND expires_at > now() RETURNING user_id""", token_hash)
    if row is None:
        raise HTTPException(400, "Liên kết đặt lại mật khẩu không hợp lệ hoặc đã hết hạn")
    await state.db.wexec("UPDATE users SET password_hash=$2 WHERE user_id=$1", row["user_id"], hash_password(body.password))
    return {"message": "Mật khẩu đã được cập nhật"}


@router.get("/auth/google")
async def google_login():
    s = state.settings
    if not s.google_client_id:
        raise HTTPException(503, "Google OAuth chưa được cấu hình")
    params = urlencode({"client_id": s.google_client_id, "redirect_uri": s.google_redirect_uri,
                        "response_type": "code", "scope": "openid email profile", "access_type": "offline"})
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")


@router.get("/auth/google/callback")
async def google_callback(code: str = Query(min_length=1)):
    s = state.settings
    async with httpx.AsyncClient(timeout=10) as client:
        token_response = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": s.google_client_id, "client_secret": s.google_client_secret,
            "redirect_uri": s.google_redirect_uri, "grant_type": "authorization_code"})
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]
        profile = (await client.get("https://openidconnect.googleapis.com/v1/userinfo",
                                    headers={"Authorization": f"Bearer {access_token}"})).json()
    email = profile["email"].lower()
    row = await state.db.rfetchrow("SELECT username, role FROM users WHERE email=$1", email)
    if row is None:
        username = re.sub(r"[^a-z0-9._-]", "-", email.split("@")[0])[:40]
        row = await state.db.wfetchrow(
            """INSERT INTO users (username,email,display_name,password_hash,email_verified)
               VALUES ($1,$2,$3,$4,true) RETURNING username,role""",
            username, email, profile.get("name", username), hash_password(secrets.token_urlsafe(32)))
    jwt_token = create_token(User(row["username"], Role(row["role"])))
    return RedirectResponse(f"{s.app_base_url}/#access_token={jwt_token}")
