# Fraud Copilot – Khung giao tiếp giữa các thành phần

Khung chạy được bằng Docker Compose. Phần **giao tiếp** (API, hàng đợi Redis, hợp đồng dữ liệu, retry/timeout/circuit breaker, ghi DB qua PgBouncer, metrics) đã làm thật.
Phần **nghiệp vụ AI** là hàm giả, có đánh dấu `>>> HÀM GIẢ <<<`, chỉ cần thay thân hàm, giữ nguyên chữ ký.

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
Chạy file tasks.json trong phần .vscode
## 3. Thử nhanh bằng curl
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

## 4. Hợp đồng giao tiếp

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

## 5. Những phần cần thay thế
| File | Hiện tại | Thay bằng |
|---|---|---|
| `scoring_worker/pipeline/inference.py` | xác suất giả theo công thức | nạp model + calibrator từ Model Store |
| `scoring_worker/pipeline/features.py` | vài đặc trưng đơn giản (đã query lịch sử đúng `t_history < t_current`) | đặc trưng thật cho mô hình |
| `llm_worker/llm_client.py` | `StubLLMClient` | client gọi API/local LLM (giữ `LLMClient.generate`) |
| `llm_worker/rag.py` | 4 đoạn playbook giả | truy vấn Vector Index |
| `llm_worker/grounding.py` | kiểm tra citation ∈ ngữ cảnh RAG | kiểm tra số liệu, chống ảo giác sâu hơn |
| `analyst_api/auth.py` | user cứng trong code | bảng `users` + băm mật khẩu |

Để thử các đường lỗi: `LLM_STUB_FAIL_RATE=0.5` (circuit breaker, FAILED_TIMEOUT) · `--fault-rate 0.1` ở simulator (MANUAL_FALLBACK_UNSCORED) · tăng `--tps` để thấy 429 backpressure.

## 6. ML pipeline thật: preprocessing, training và serving

Notebook đã được chuyển thành package `ml_pipeline` với các hàm độc lập:

- `read_csv_safely` và `validate_schema`: kiểm tra file, cột bắt buộc và timestamp.
- `extract_features` và `compute_historical_features`: tạo feature theo thứ tự thời gian, chỉ dùng lịch sử quá khứ.
- `chronological_split`: chia train/validation/test không làm rò rỉ tương lai.
- `train_model`, `evaluate_model`, `save_artifact`: huấn luyện XGBoost, calibrate xác suất bằng Isotonic Regression, chọn threshold trên validation và lưu lineage.

Cấu hình nằm tại [ml_pipeline/config.json](ml_pipeline/config.json), không hard-code trong code. Có thể override bằng `ML_CONFIG_PATH`.

Chạy toàn bộ DAG preprocessing -> training -> evaluation -> artifact:

```bash
python -m ml_pipeline --config ml_pipeline/config.json
```

Hoặc chạy profile Docker:

```bash
docker compose --profile training run --rm ml-pipeline
```

Để demo toàn bộ luồng bằng dữ liệu Kaggle, phát lại test set (nhãn chỉ được
ghi vào `data/labels.csv`, không gửi vào hệ thống):

```bash
python -m simulator.replay --csv data/fraudTest.csv --tps 10 --limit 500
```

Các giao dịch đi qua scoring worker bằng artifact đã train; case Medium/High
sẽ tiếp tục được đưa sang LLM worker để tạo brief.

Artifact được ghi vào `data/models/fraud_xgboost.joblib`, metadata lineage vào file `.json` cạnh artifact. Scoring worker và `model-service` đều nạp cùng artifact qua `MODEL_ARTIFACT_PATH`; nếu artifact thiếu hoặc hỏng, request sẽ lỗi rõ ràng và worker chuyển sang retry/manual fallback, không âm thầm dùng heuristic.

File [dvc.yaml](dvc.yaml) cung cấp stage `train` để version hóa đồng bộ input data, config, processed splits và model outputs khi team dùng DVC remote:

```bash
dvc repro
dvc push
```

Inference service nội bộ:

```bash
curl -X POST http://localhost:8010/v1/predict \
  -H "content-type: application/json" \
  -d '{"features":{"amt":120.5,"transaction_hour":23,"trans_count_24h":2}}'
```

`model-service:8010/metrics` và các metric `fraud_inference_*`, `fraud_stage_seconds` được Prometheus scrape. Có thể kích hoạt workflow theo cron/Task Scheduler bằng lệnh `python -m ml_pipeline`; trigger sự kiện có dữ liệu mới chỉ cần gọi cùng lệnh sau khi file input được cập nhật. Nên lưu `data/models/*.joblib` và file metadata trong DVC/object storage ở môi trường production thay vì commit binary vào Git.
