# End-to-end smoke

Job tương ứng: `e2e-smoke` trong `.github/workflows/ci.yml`.

File [test_transaction_e2e.py](./test_transaction_e2e.py) là test transaction
thật qua simulator, API, Redis, Scoring Worker và PostgreSQL. Test cần
`FRAUD_E2E_URL`.

Hiện workflow `e2e-smoke` mới health-check `/healthz`; nó chưa gọi file pytest
này và cũng chưa khởi động Scoring Worker/LLM Worker.
