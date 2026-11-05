# Docker build

Job tương ứng: `docker-build` trong `.github/workflows/ci.yml`.

Job này không có file pytest riêng. Workflow kiểm tra trực tiếp:

```bash
docker compose config --quiet
docker compose build analyst-api scoring-worker llm-worker model-service
```

Mục tiêu là xác nhận Compose hợp lệ và các image ứng dụng build được.
