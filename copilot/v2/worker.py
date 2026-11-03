"""Redis notification worker with PostgreSQL outbox and execution ledger."""
import argparse
import json
import os
import time
from uuid import UUID
from ..contracts import ContractError, fields
from ..providers import MockProvider, OllamaProvider
from .knowledge import load
from .vectors import OllamaEmbeddings, SemanticRetriever, FixtureRetriever
from .service import generate
from .storage import Repository, connect, register, dispatch_once, failed_result


def validate_job(job):
    fields(job, ("case_id", "transaction_id", "generation", "priority"))
    if not isinstance(job["case_id"], str):
        raise ContractError("Invalid case ID")
    UUID(job["case_id"])
    if type(job["generation"]) is not int or job["generation"] < 1:
        raise ContractError("Invalid generation")
    if job["priority"] not in ("high", "medium") or not isinstance(job["transaction_id"], str):
        raise ContractError("Invalid job")
    return job


def process(job, repository, generator):
    validate_job(job)
    claim = repository.claim(job)
    if claim is None:
        return "SKIPPED_DUPLICATE_OR_STALE"
    try:
        result = generator(claim["snapshot"])
    except ContractError:
        result = failed_result("INVALID_CONTEXT")
    except Exception:
        result = failed_result("WORKER_ERROR")
    return result["brief_status"] if repository.finish(job, claim, result) else "STALE_RESULT_DISCARDED"


def configured_generator(case):
    kb = load(os.environ.get("PLAYBOOK_PATH", "knowledge/common_playbook.json"))
    mode = os.environ.get("COPILOT_PROVIDER", "ollama")
    if mode not in ("ollama", "mock"):
        raise ValueError("Unknown provider")
    provider = MockProvider() if mode == "mock" else OllamaProvider(timeout=60)
    retrieval = os.environ.get("COPILOT_RETRIEVER", "semantic")
    if retrieval not in ("fixture", "semantic"):
        raise ValueError("Unknown retriever")
    retriever = FixtureRetriever(kb) if retrieval == "fixture" else SemanticRetriever(
        kb, OllamaEmbeddings(), os.environ.get("VECTOR_INDEX_PATH", "artifacts/playbook-index.json"))
    return generate(case, kb, provider, retriever)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("worker", "dispatch", "submit", "show"))
    p.add_argument("--case")
    p.add_argument("--case-id")
    p.add_argument("--regenerate", action="store_true")
    p.add_argument("--once", action="store_true")
    args = p.parse_args()
    if args.command == "submit":
        if not args.case:
            p.error("submit requires --case")
        with open(args.case, encoding="utf-8") as f:
            case = json.load(f)
        with connect() as conn:
            generation = register(conn, case, args.regenerate)
        print(json.dumps({"case_id":case["case_id"],"generation":generation,"status":"PENDING"}))
        return
    if args.command == "show":
        if not args.case_id:
            p.error("show requires --case-id")
        UUID(args.case_id)
        with connect() as conn:
            row = conn.execute("SELECT * FROM copilot_dev.investigation_cases WHERE case_id=%s", (args.case_id,)).fetchone()
        print(json.dumps(row, default=str, ensure_ascii=False, indent=2))
        return
    from redis import Redis
    redis = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3,
                           socket_timeout=7, decode_responses=True)
    while True:
        try:
            if args.command == "dispatch":
                count = dispatch_once(redis)
                if count:
                    print(json.dumps({"event":"dispatched","count":count}), flush=True)
            else:
                message = redis.blpop(["copilot:high", "copilot:medium"], timeout=5)
                if message:
                    if len(message[1]) > 4096:
                        raise ContractError("Oversized job")
                    status = process(json.loads(message[1]), Repository(), configured_generator)
                    print(json.dumps({"event":"brief_processed","status":status}), flush=True)
        except Exception as exc:
            # Outbox re-publishes lost/dequeued jobs; no case data/credentials in logs.
            print(json.dumps({"event":"worker_error","error_type":type(exc).__name__}), flush=True)
        if args.once:
            break
        if args.command == "dispatch":
            time.sleep(5)


if __name__ == "__main__":
    main()
