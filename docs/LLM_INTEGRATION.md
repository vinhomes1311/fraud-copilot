# Local LLM integration

Source snapshot: vinhomes1311/fraud-copilot main at 38274b9f1a61d60acccf01ce7dd09d1aa1105e78.

The Ollama adapter uses the local approved five-principle playbook and local
selection validator. It keeps the shared JobEnvelope, integer case IDs, database,
retry, fallback and BriefContent contracts. It never recalculates scoring/routing.
No synthetic UUID, explanation method, contribution scale or historical median is invented.
Only allowlisted numeric features enter the model; arbitrary masked payload fields
and identifiers are excluded. Unknown explanation features are omitted until reviewed.
The summary is extractive; checklist actions are identified as team proposals.

## Run

Install the repository requirements. Ensure qwen2.5:3b and embeddinggemma:300m
are installed in Ollama (or set OLLAMA_MODEL/OLLAMA_EMBED_MODEL).

Host worker (PowerShell):

    $env:LLM_PROVIDER = 'ollama'
    $env:OLLAMA_MODEL = 'qwen2.5:3b'
    $env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
    python -m llm_worker.main

Set DB_WRITE_URL, DB_READ_URL and REDIS_URL as in the team's dev setup.

Docker:

    docker compose -f docker-compose.yml -f docker-compose.ollama.yml up --build -d

The container must be able to reach Ollama on the configured endpoint. This change
does not configure the host firewall or Ollama binding. Default Compose still uses
the stub; the override explicitly enables the real adapter.

## Validation and limits

    python -m unittest discover -s tests -p test_local_integration.py -v

Each retrieval and generation stage has a total asyncio deadline. Keep queue
visibility timeout greater than their combined deadlines plus database overhead.
Ollama prompt_eval_count + eval_count are recorded for the existing token budget.
HTTP timeouts are converted to TimeoutError for the team's FAILED_TIMEOUT handling.
Network and model/output errors propagate to the existing retry/circuit breaker.
The five document embeddings are recomputed with each query to avoid stale model
or playbook indexes; this can increase cold-start latency. Profile before increasing concurrency.

Each enqueue stores its job ID as the active brief generation. RUNNING, READY and
failure writes are conditional on that generation; a superseded job cannot replace
the newer brief. Completion also checks the attempt number, and terminal rows reject
duplicate deliveries. New generations clear previous content and usage metadata.
Token-budget failures now persist a rule-based fallback before the job is acknowledged.

For an existing database, stop API and LLM workers, apply
`db/migrations/001_brief_generation.sql`, then restart both with this code. Fresh
databases include the column in `db/init.sql`. Do not mix old and new workers.
Legacy PENDING/RUNNING rows with a NULL generation can be claimed by a queued job.
This migration has been prepared but has not been applied to a running database here.

Worker startup requires LLM_VISIBILITY_TIMEOUT_S > 2 * LLM_JOB_TIMEOUT_S + 10,
covering retrieval and generation deadlines plus a database allowance. Database
stalls can still exceed that allowance; this is not a lease renewal mechanism.

Remaining queue limitations: no transactional outbox between PostgreSQL and Redis;
an enqueue can commit PENDING before Redis publication fails. Duplicate case-created
events can request another generation. Queue ack/nack ownership is still keyed by
job ID, not a per-delivery lease, and retry publication is not atomic with removal
from processing. Generation fencing protects brief writes across regenerations but
does not solve these delivery guarantees. Do not claim exactly-once execution.

Tests with mocked Ollama exercise contracts, grounding, usage and failure paths;
they do not establish quality or latency of the installed model.

## Validation added on 2026-10-28

    python -m unittest discover -s tests -p test_generation_sql.py -v
    python -m unittest discover -s tests -p test_brief_processor.py -v
    python -m pytest -q

The 9 generation/timeout tests passed. They execute production conditional SQL in
SQLite after translating placeholders and now(); they are not PostgreSQL tests.
All 61 project Python files passed AST parsing. The 6 added processor tests and
the existing Ollama adapter tests could not run in this environment because
pydantic/httpx and other requirements are unavailable; dependency installation
failed. The Ollama localhost connection was denied by the execution environment.

An opt-in integration test is also provided:

    $env:FRAUD_TEST_DB_URL = 'postgresql://USER:PASSWORD@localhost:5432/TEST_DATABASE'
    $env:FRAUD_TEST_REDIS_URL = 'redis://localhost:6379/15'
    python -m pytest tests/test_brief_pipeline_integration.py -v

Use disposable test services and a direct PostgreSQL connection. The test creates
and removes a uniquely named schema and Redis queue, exercising enqueue, READY,
duplicate delivery, stale generation, timeout/dead-letter fallback, and unchanged
ML risk/probability. It uses the stub LLM; it does not validate Ollama quality.
This live integration test has not been run here.
