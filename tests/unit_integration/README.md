# Unit và integration tests

Job tương ứng: `tests` trong `.github/workflows/ci.yml`.

Các file trong thư mục này được chạy bằng:

```bash
python -m pytest -q tests/unit_integration
```

Job CI khởi động Redis và PostgreSQL service containers, đồng thời đặt các
biến `FRAUD_TEST_DB_URL`, `FRAUD_TEST_REDIS_URL` và `REDIS_URL`.

Các test bổ sung từ PR #5 nằm cùng thư mục: API adapter, cấu hình khởi động,
logging, cache embedding Ollama, evidence từ ML, retrieval, playbook và benchmark.
Các transport API/Ollama được mock; không cần API key thật và không phát sinh phí LLM.
