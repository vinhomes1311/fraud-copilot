# End-to-end smoke

Job tương ứng: `e2e-smoke` trong `.github/workflows/ci.yml`.

File [test_transaction_e2e.py](./test_transaction_e2e.py) là test transaction
thật qua simulator, API, Redis, Scoring Worker và PostgreSQL. Test cần
`FRAUD_E2E_URL`.

Workflow `e2e-smoke` khởi động Redis, PostgreSQL, PgBouncer, Analyst API,
Scoring Worker, LLM Worker và Nginx, sau đó chạy file pytest này với
`FRAUD_E2E_URL=http://localhost:8080`.
