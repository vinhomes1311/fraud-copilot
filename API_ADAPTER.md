# API adapter alongside Ollama

`LLM_PROVIDER=api` selects an OpenAI-compatible Chat Completions endpoint.
`LLM_PROVIDER=ollama` preserves the existing Ollama adapter. `stub` is unchanged.
The API client reuses the Ollama adapter's evidence allowlist, approved-playbook
validation, ID selection validator, and extractive brief rendering. Scores and
routing are not recalculated. No provider SDK or new dependency is required.

## Host worker (PowerShell)

Configure these before starting Python; application settings are loaded at import.
The host worker does not automatically read `.env`.

```powershell
$env:LLM_PROVIDER = 'api'
$env:LLM_API_BASE_URL = 'https://api.openai.com/v1'
$env:LLM_API_MODEL = '<model enabled for your account>'
$env:LLM_API_KEY = '<set your key locally; never commit it>'
$env:LLM_RETRIEVER = 'playbook'
$env:LLM_JOB_TIMEOUT_S = '120'
$env:LLM_VISIBILITY_TIMEOUT_S = '300'
.\.venv\Scripts\python.exe -m llm_worker.main
```

Use a provider-specific HTTPS base URL (without `/chat/completions`). Configure
DB_WRITE_URL, DB_READ_URL, REDIS_URL and apply the existing generation migration
as described in LLM_INTEGRATION.md before using the queue worker.

The default `playbook` retriever supplies all five approved KB entries without
semantic ranking or embeddings; this mode has no Ollama dependency. This is
appropriate only for the current tiny KB, not a scalable semantic retriever.
Set `LLM_RETRIEVER=ollama` to use existing semantic retrieval; then Ollama and
embeddinggemma are still required. Ollama generation defaults to Ollama retrieval.

## Compatibility

The endpoint must support non-streaming Chat Completions, Bearer authentication,
and report integer prompt_tokens, completion_tokens, total_tokens usage.
Default response format is strict `json_schema`. If the model only supports JSON
mode, explicitly set `LLM_API_RESPONSE_FORMAT=json_object`; local ID validation
remains mandatory. There is no automatic downgrade or provider fallback.

Output cap defaults to 1024 via `max_completion_tokens`; set
`LLM_API_MAX_COMPLETION_TOKENS` to adjust it, or set
`LLM_API_TOKEN_PARAMETER=max_tokens` for compatible endpoints requiring the older
parameter. Model-specific capabilities must be checked with the provider.
Reasoning models may need a higher cap; validate latency, completion and cost.

Refusals, truncation, invalid IDs, missing evidence and invalid usage fail the job.
HTTP errors are sanitized; no response body or API key is saved to last_error.
Timeouts map to TimeoutError. The client makes one attempt: existing queue retries,
circuit breaker, token budget and terminal fallback remain responsible for jobs.
HTTP 401/429/5xx currently use the existing bounded retry policy; Retry-After is not
scheduled by this adapter. A token-budget check occurs after inference and cannot
undo API charges. Tokens are recorded per successful attempt, not total retry cost.

## Docker

Set the same variables in the shell or untracked `.env`, then use the API override
instead of the Ollama override:

```powershell
docker compose -f docker-compose.yml -f docker-compose.api.yml up --build -d
```

## Tests and smoke run

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api_client.py tests/test_local_integration.py tests/test_brief_processor.py tests/test_generation_sql.py -v
```

The API tests use httpx.MockTransport, with no external requests or charges.
For a live smoke test, set the API variables above and reuse the synthetic-case
script that calls `get_retriever()` and `get_llm_client()`; no DB is needed for that
script. Expect the selected API model name, valid citations and usage. A successful
mock test does not establish compatibility, quality or latency of a real model.

Protocol references:
- https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create
- https://developers.openai.com/api/docs/guides/structured-outputs
