# Fraud Copilot – Khung giao tiếp giữa các thành phần

Khung chạy được bằng Docker Compose. Phần **giao tiếp** (API, hàng đợi Redis, hợp đồng dữ liệu, retry/timeout/circuit breaker, ghi DB qua PgBouncer, metrics) đã làm thật.
LLM hỗ trợ **gọi API online** qua endpoint tương thích OpenAI, chọn model bằng cấu hình và dùng playbook làm context, không cần train lại LLM. Bộ chấm điểm ML và một số thành phần demo vẫn là hàm giả có đánh dấu `>>> HÀM GIẢ <<<`.

### Dùng LLM online

Copy `.env.example` thành `.env` (nếu chưa có), điền `LLM_API_KEY`, rồi chạy:

```powershell
docker compose -f docker-compose.yml -f docker-compose.api.yml up --build -d
```

Cấu hình mẫu dùng `LLM_PROVIDER=api`, `LLM_API_MODEL=gpt-5.4-mini` và playbook không cần Ollama/GPU. Có thể đổi model, endpoint, giới hạn token và reasoning effort trong `.env`. Python tự nạp file này; biến môi trường của shell được ưu tiên. Không có cấu hình thì demo vẫn dùng `stub`.

Xem [hướng dẫn API online](API_ADAPTER.md) để chạy Python, kiểm tra cấu hình và gọi thử bằng dữ liệu giả (`python -m llm_worker.smoke_test --live`).

## 1. Sơ đồ giao tiếp

```mermaid
flowchart LR
  SIM[Simulator] -->|POST /transactions + X-API-Key| NGX[Nginx]
  UI[Dashboard] -->|JWT| NGX
  NGX --> API[Analyst API]
  API -- "ml_jobs (Redis)" --> SW[Scoring Worker]
  SW -- "case_events (Redis)" --> API
  API -- "llm_jobs (Redis, High>Medium)" --> LW[LLM Worker]
  LW --> RAG[RAG + Vector Index]
  SW & API & LW --> PGW[PgBouncer-write] --> PG[(PostgreSQL Primary)]
  SW & API & LW --> PGR[PgBouncer-read] --> PGREP[(Replica)]
  API & SW & LW -. /metrics .-> PROM[Prometheus]
```

## 2. Chạy

**Cách A – toàn bộ trong Docker** (cần Docker Desktop):
```bash
docker compose up --build -d
docker compose --profile sim run --rm simulator --tps 10 --limit 300 --fault-rate 0.05
curl http://localhost:8080/healthz
```

**Cách B – chạy/debug Python trong VS Code** (hạ tầng trong Docker, code chạy trên máy):
```bash
python -m venv .venv && .venv\Scripts\activate      # Windows (macOS/Linux: source .venv/bin/activate)
pip install -r requirements-dev.txt                  # cần Python >= 3.11
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis postgres pgbouncer-write pgbouncer-read
```
Mở VS Code → Run and Debug → chọn **"Cả 3 service"** (file `.vscode/launch.json`). Gửi dữ liệu: `python -m simulator.replay --url http://localhost:8000 --tps 5 --limit 100`.

Nếu PgBouncer báo lỗi xác thực, tạm đổi `DB_WRITE_URL`/`DB_READ_URL` trỏ thẳng `postgres:5432`; code không phải sửa.

## 3. Thử nghiệm nhanh bằng cURL
```bash
# 1) đăng nhập analyst
TOKEN=$(curl -s localhost:8080/api/v1/auth/login -H 'content-type: application/json' \
  -d '{"username":"analyst1","password":"analyst123"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
# 2) hàng đợi ưu tiên
curl -s localhost:8080/api/v1/cases -H "Authorization: Bearer $TOKEN"
# 3) chi tiết case (giao dịch đã che PII, điểm, giải thích, brief)
curl -s localhost:8080/api/v1/cases/1 -H "Authorization: Bearer $TOKEN"
# 4) chuyển cấp
curl -s -X POST localhost:8080/api/v1/cases/1/actions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"action":"ESCALATE","note":"nghi ngờ"}'
```
Tài khoản demo (trong `analyst_api/auth.py`): `analyst1/analyst123`, `lead1/lead123`, `admin/admin123`.
Tài liệu API tự sinh: `http://localhost:8000/docs` (chỉ khi mở cổng bằng file dev).

## 4. Hợp đồng giao tiếp (Communication Contracts)

### REST (qua Nginx, tiền tố `/api/v1`)
| Method & đường dẫn | Ai gọi | Quyền | Ghi chú |
|---|---|---|---|
| `POST /transactions` | Simulator | `X-API-Key` | 202 QUEUED · 200 DUPLICATE · 429 hàng đợi đầy (có `Retry-After`) |
| `GET /transactions/{id}/result` | Simulator/UI | key hoặc JWT | 202 PENDING nếu chưa chấm xong |
| `POST /auth/login` | UI | – | trả JWT |
| `GET /cases` | Analyst, Risk Lead | JWT | sắp xếp High trước, xác suất giảm dần; Risk Lead mặc định chỉ thấy ESCALATED |
| `GET /cases/{id}` | Analyst, Risk Lead | JWT | giao dịch, điểm, SHAP-like, brief, lịch sử thao tác |
| `POST /cases/{id}/actions` | Analyst | JWT | NOTE · NEEDS_MORE_INFO · CONFIRM_SIMULATED · ESCALATE |
| `POST /cases/{id}/resolve` | Risk Lead | JWT | CLOSE · RETURN (chỉ case ESCALATED) |
| `POST /cases/{id}/brief:regenerate` | Analyst, Risk Lead | JWT | tối đa 3 lần/giờ/case |
| `GET/PUT /admin/threshold-config` | Admin | JWT | mỗi lần PUT tạo version mới |
| `GET /admin/queues` | Admin | JWT | độ sâu 3 hàng đợi |

### Hàng đợi Redis (phong bì `JobEnvelope`, xem `fraud_common/schemas.py`)
| Queue | Producer → Consumer | job_type | Ưu tiên | Timeout / thử lại |
|---|---|---|---|---|
| `ml_jobs` | Analyst API → Scoring Worker | `score_transaction` | FIFO | 2 s / 3 lần, quá thì ghi `MANUAL_FALLBACK_UNSCORED` |
| `case_events` | Scoring Worker → Analyst API | `case_created` | High trước | 30 s / 5 lần |
| `llm_jobs` | Analyst API → LLM Worker | `generate_brief` | High (0) trước Medium (1) | 30 s / 3 lần, quá thì `FAILED_TIMEOUT` + brief rule-based |

Mỗi queue gồm `:ready` (ZSET), `:processing` (HASH, có deadline) và `:dead` (LIST). Lấy job là thao tác nguyên tử bằng Lua script; worker chết thì reaper trả job về hàng đợi. Cơ chế là **at-least-once**, vì vậy mọi handler đều idempotent (`ON CONFLICT DO NOTHING`).

`trace_id` đi xuyên suốt: Nginx `X-Request-Id` → job → log → cột `trace_id` trong DB.

### Quyền ghi DB
| Tiến trình | Bảng được ghi |
|---|---|
| scoring-worker | `transactions`, `scoring_results`, `cases` (tạo), `unscored_queue` |
| analyst-api | `case_actions`, `cases` (đổi trạng thái), `case_briefs` (tạo PENDING), `threshold_configs` |
| llm-worker | `case_briefs` (cập nhật kết quả) |
| cả ba | `audit_log` (chỉ INSERT) |

## 5. Những chỗ cần thay bằng code thật
| File | Hiện tại | Thay bằng |
|---|---|---|
| `scoring_worker/pipeline/inference.py` | xác suất giả theo công thức | nạp model + calibrator từ Model Store |
| `scoring_worker/pipeline/features.py` | vài đặc trưng đơn giản (đã query lịch sử đúng `t_history < t_current`) | đặc trưng thật cho mô hình |
| `llm_worker/llm_client.py`, `api_client.py` | API online, Ollama hoặc stub qua `LLM_PROVIDER` | cấu hình model/endpoint; đánh giá trên case đại diện |
| `llm_worker/rag.py` | playbook được phê duyệt cho API; semantic retrieval cho Ollama; stub cho demo | mở rộng retrieval khi KB lớn hơn |
| `llm_worker/grounding.py` | kiểm tra citation ∈ ngữ cảnh RAG | kiểm tra số liệu, chống ảo giác sâu hơn |
| `analyst_api/auth.py` | user cứng trong code | bảng `users` + băm mật khẩu |

Để thử các đường lỗi: `LLM_STUB_FAIL_RATE=0.5` (circuit breaker, FAILED_TIMEOUT) · `--fault-rate 0.1` ở simulator (MANUAL_FALLBACK_UNSCORED) · tăng `--tps` để thấy 429 backpressure.

## 6. Hạn chế đã biết
- Khung này chưa được chạy thử trên Docker trong môi trường soạn (không có mạng); mới kiểm tra cú pháp. Nếu có lỗi khi `up`, xem `docker compose logs <service>`.
- Chưa có Replica PostgreSQL thật: cả hai PgBouncer cùng trỏ một Postgres.
- Sau khi commit case mà Redis lỗi trước lúc phát `case_created` thì case chưa có brief (cần outbox pattern); tạm thời analyst dùng `brief:regenerate`.
- Chưa có Grafana, Alert Service, Slack (Prometheus đã thu metrics), và chưa có UI.
- Nginx OSS không tự kiểm tra JWT, nên xác thực nằm ở Analyst API; Nginx lo rate limit và định tuyến.
