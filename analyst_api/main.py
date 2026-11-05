from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app
from redis.asyncio import Redis

from fraud_common.config import get_settings
from fraud_common.db import Database
from fraud_common.logging_utils import setup_logging
from fraud_common.queues import QUEUE_CASE_EVENTS, QUEUE_LLM_JOBS, QUEUE_ML_JOBS, ReliableQueue
from fraud_common.worker import run_consumer

from .events import handle_case_created
from .routes import admin, auth_routes, cases, demo, ingest
from .state import state
from .auth import hash_password


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging("analyst-api")
    state.settings = s
    state.redis = Redis.from_url(s.redis_url, decode_responses=True)
    state.db = Database(s.db_write_url, s.db_read_url)
    await state.db.connect()
    await state.db.wexec(
        """CREATE TABLE IF NOT EXISTS users (
             user_id BIGSERIAL PRIMARY KEY,
             username TEXT NOT NULL UNIQUE,
               email TEXT,
             display_name TEXT NOT NULL,
             password_hash TEXT NOT NULL,
             role TEXT NOT NULL DEFAULT 'analyst',
             active BOOLEAN NOT NULL DEFAULT true,
             email_verified BOOLEAN NOT NULL DEFAULT false,
             created_at TIMESTAMPTZ NOT NULL DEFAULT now()
           )""")
    await state.db.wexec("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT")
    await state.db.wexec("ALTER TABLE users ALTER COLUMN email DROP NOT NULL")
    await state.db.wexec("ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false")
    await state.db.wexec("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users (email) WHERE email IS NOT NULL")
    await state.db.wexec(
        """CREATE TABLE IF NOT EXISTS auth_tokens (
             token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
             purpose TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ
           )""")
    for username, display_name, password, role in (
        ("analyst1", "Analyst One", "analyst123", "analyst"),
        ("analyst2", "Analyst Two", "analyst123", "analyst"),
        ("lead1", "Risk Lead", "lead123", "risk_lead"),
        ("admin", "Administrator", "admin123", "admin"),
    ):
        await state.db.wexec(
            """INSERT INTO users (username, email, display_name, password_hash, role, email_verified)
               VALUES ($1, $2, $3, $4, $5, true)
               ON CONFLICT (username) DO UPDATE SET email=EXCLUDED.email, email_verified=true""",
            username, f"{username}@demo.local", display_name, hash_password(password), role)

    state.ml_queue = ReliableQueue(state.redis, QUEUE_ML_JOBS)
    state.llm_queue = ReliableQueue(state.redis, QUEUE_LLM_JOBS)
    state.events_queue = ReliableQueue(state.redis, QUEUE_CASE_EVENTS, visibility_timeout_s=30, max_attempts=5)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(run_consumer(state.events_queue, handle_case_created, concurrency=4, stop=stop)),
        asyncio.create_task(state.events_queue.reaper_loop(stop)),
        asyncio.create_task(state.ml_queue.gauge_loop(stop)),
        asyncio.create_task(state.llm_queue.gauge_loop(stop)),
        asyncio.create_task(demo.run_simulator(stop)),
    ]
    try:
        yield
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state.db.close()
        await state.redis.aclose()


app = FastAPI(title="Fraud Copilot - Analyst API", version="0.1.0", lifespan=lifespan)
app.include_router(auth_routes.router, prefix="/api/v1")
app.include_router(ingest.router, prefix="/api/v1")
app.include_router(cases.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(demo.router, prefix="/api/v1")
app.mount("/metrics", make_asgi_app())
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/static/dog-tracker.mp4", include_in_schema=False)
async def dog_tracker_video():
    video_path = Path(__file__).resolve().parent.parent / "static" / "media" / "dog-tracker.mp4"
    return FileResponse(video_path, media_type="video/mp4", headers={"Cache-Control": "public, max-age=86400"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz():
    await state.redis.ping()
    return {"status": "ok"}
