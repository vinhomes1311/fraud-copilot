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
from .routes import admin, auth_routes, cases, ingest
from .state import state


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    setup_logging("analyst-api")
    state.settings = s
    state.redis = Redis.from_url(s.redis_url, decode_responses=True)
    state.db = Database(s.db_write_url, s.db_read_url)
    await state.db.connect()

    state.ml_queue = ReliableQueue(state.redis, QUEUE_ML_JOBS)
    state.llm_queue = ReliableQueue(state.redis, QUEUE_LLM_JOBS)
    state.events_queue = ReliableQueue(state.redis, QUEUE_CASE_EVENTS, visibility_timeout_s=30, max_attempts=5)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(run_consumer(state.events_queue, handle_case_created, concurrency=4, stop=stop)),
        asyncio.create_task(state.events_queue.reaper_loop(stop)),
        asyncio.create_task(state.ml_queue.gauge_loop(stop)),
        asyncio.create_task(state.llm_queue.gauge_loop(stop)),
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
app.mount("/metrics", make_asgi_app())
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz():
    await state.redis.ping()
    return {"status": "ok"}
