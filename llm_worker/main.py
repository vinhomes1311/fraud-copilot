from __future__ import annotations

import asyncio
import logging

from prometheus_client import start_http_server
from redis.asyncio import Redis

from fraud_common.config import get_settings
from fraud_common.db import Database
from fraud_common.logging_utils import setup_logging
from fraud_common.queues import QUEUE_LLM_JOBS, ReliableQueue
from fraud_common.worker import install_signal_handlers, run_consumer

from .processor import BriefProcessor

log = logging.getLogger(__name__)


async def main() -> None:
    s = get_settings()
    s.validate_llm_timeouts()
    setup_logging("llm-worker")
    start_http_server(s.metrics_port)

    redis = Redis.from_url(s.redis_url, decode_responses=True)
    db = Database(s.db_write_url, s.db_read_url)
    await db.connect()

    processor = BriefProcessor(db)
    llm_queue = ReliableQueue(
        redis, QUEUE_LLM_JOBS, visibility_timeout_s=s.llm_visibility_timeout_s,
        max_attempts=s.llm_max_attempts, on_dead=processor.handle_dead)

    stop = asyncio.Event()
    install_signal_handlers(stop)
    log.info("llm-worker sẵn sàng")
    await asyncio.gather(
        run_consumer(llm_queue, processor.handle, concurrency=s.llm_concurrency, stop=stop),
        llm_queue.reaper_loop(stop),
    )
    await db.close()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
