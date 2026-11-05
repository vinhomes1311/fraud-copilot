# End-to-end smoke

Job tương ứng: `e2e-smoke` trong `.github/workflows/ci.yml`.

File [test_transaction_e2e.py](./test_transaction_e2e.py) là test transaction
thật qua simulator, API, Redis, Scoring Worker và PostgreSQL. Test cần
`FRAUD_E2E_URL`.

Workflow `e2e-smoke` khởi động Redis, PostgreSQL, PgBouncer, Analyst API,
Scoring Worker, LLM Worker và Nginx, sau đó chạy file pytest này với
`FRAUD_E2E_URL=http://localhost:8080`.

## Workflow với LLM mock

[test_workflow_e2e.py](./test_workflow_e2e.py) kiểm tra API/queue/scoring/brief và
các thao tác review với PostgreSQL/Redis thật, model và LLM transport được mock.
Test cần `FRAUD_TEST_DB_URL` và `FRAUD_TEST_REDIS_URL` trỏ tới dịch vụ kiểm thử riêng.
Job `tests` đã cung cấp hai biến này và thu thập thư mục `tests/e2e_smoke`.
Test này không cần `FRAUD_E2E_URL`, API key thật hoặc gọi API LLM ngoài.
