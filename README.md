# Fraud Copilot

Fraud Copilot là một pipeline xử lý giao dịch gian lận bất đồng bộ, gồm Analyst API,
Scoring Worker, LLM Worker, Redis, PostgreSQL, PgBouncer và dashboard web cho analyst.
Toàn bộ stack có thể chạy bằng Docker Compose.

> **Trạng thái hiện tại:** hạ tầng giao tiếp, hợp đồng dữ liệu, retry/timeout,
> circuit breaker, RBAC, audit log, metrics và dashboard đã chạy được. Phần ML/RAG/LLM
> hiện là implementation mẫu (stub), được đánh dấu trong source bằng `>>> HÀM GIẢ <<<`.

## Mục lục

- [Kiến trúc](#kiến-trúc)
- [Yêu cầu](#yêu-cầu)
- [Chạy nhanh bằng Docker](#chạy-nhanh-bằng-docker)
- [Chạy và debug trong VS Code](#chạy-và-debug-trong-vs-code)
- [Dashboard](#dashboard)
- [API](#api)
- [Simulator](#simulator)
- [Cấu hình](#cấu-hình)
- [Observability](#observability)
- [Test](#test)
- [Cơ chế xử lý lỗi](#cơ-chế-xử-lý-lỗi)
- [Các phần cần thay thế](#các-phần-cần-thay-thế)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)

## Kiến trúc

```mermaid
flowchart LR
  SIM[Simulator] -->|POST /transactions + X-API-Key| NGX[Nginx]
  UI[Dashboard] -->|JWT| NGX
  NGX --> API[Analyst API]
  API -->|ml_jobs| SW[Scoring Worker]
  SW -->|case_events| API
  API -->|llm_jobs| LW[LLM Worker]
  LW --> RAG[RAG + LLM stub]
  API & SW & LW --> PGW[PgBouncer write] --> PG[(PostgreSQL)]
  API & SW & LW --> PGR[PgBouncer read] --> PG
  API & SW & LW -. /metrics .-> PROM[Prometheus]
```

Luồng chính:

1. Simulator gửi giao dịch vào Nginx. API loại bỏ `is_fraud`, chống trùng theo
   `trans_num`, gắn `trace_id` và đưa job vào Redis `ml_jobs`.
2. Scoring Worker validate dữ liệu, che PII, tính điểm và tạo case.
3. API nhận event từ `case_events`, sau đó đưa yêu cầu tạo brief vào `llm_jobs`.
4. LLM Worker tạo brief có citation; nếu LLM lỗi hoặc timeout thì dùng brief
   rule-based.
5. Analyst xem review queue trên dashboard và thực hiện các thao tác xử lý case.

Các queue dùng at-least-once delivery. Mỗi queue có vùng `ready`, `processing` và
`dead`; job hết visibility timeout sẽ được reaper đưa lại vào queue.

## Yêu cầu

- Docker Desktop có hỗ trợ Docker Compose v2
- Hoặc Python 3.12+ và Redis/PostgreSQL khi chạy service ngoài Docker
- PowerShell, bash hoặc terminal tương đương để chạy các lệnh ví dụ

## Chạy nhanh bằng Docker

Từ thư mục gốc repository:

```bash
docker compose up --build -d
docker compose ps
curl http://localhost:8080/healthz
```

Dashboard mở tại <http://localhost:8080/>.

Trong dashboard, simulator tự bắt đầu phát lại dữ liệu test sau khi đăng nhập. Có thể chỉnh tốc độ
từ 1 đến 1.000 giao dịch/giây ở ô bên cạnh nút chạy; lựa chọn được lưu trên trình duyệt và có hiệu lực
ở lượt gửi tiếp theo. Đây là tốc độ phát yêu cầu mục tiêu, tốc độ xử lý thực tế còn phụ thuộc tài nguyên
hệ thống. Video chó truy vết nằm cạnh nút simulator và chỉ phát khi simulator hoạt động.
Nút **Dừng simulator**
dùng để dừng hoặc chạy lại. Review queue hiển thị cả LOW,
nhưng chỉ MEDIUM/HIGH được tạo brief. Mục Analytics chỉ hiển thị MEDIUM/HIGH,
được sắp xếp theo xác suất giảm dần; chọn một dòng để xem chi tiết và brief nếu
có.

Chạy simulator với dữ liệu tổng hợp:

```bash
docker compose --profile sim run --rm simulator --tps 10 --limit 300 --fault-rate 0.05
```

Chạy simulator với dataset có sẵn:

```bash
docker compose --profile sim run --rm simulator \
  --csv /data/fraudTrain.csv --tps 10 --limit 300 --labels /data/labels.csv
```

Xem log:

```bash
docker compose logs -f analyst-api scoring-worker llm-worker
```

Dừng stack:

```bash
docker compose down
```

Xóa cả dữ liệu PostgreSQL local (không thể khôi phục):

```bash
docker compose down -v
```

### Cổng mặc định

| Thành phần | Cổng | Ghi chú |
|---|---:|---|
| Nginx / dashboard / API gateway | `8080` | Cổng public duy nhất của stack |
| Analyst API trực tiếp | `8000` | Chỉ mở trong compose dev |
| PostgreSQL | `5432` | Chỉ mở trong compose dev |
| Redis | `6379` | Chỉ mở trong compose dev |
| PgBouncer write/read | `6432` / `6433` | Chỉ mở trong compose dev |
| Prometheus | `9090` | Chỉ mở trong compose dev |

## Chạy và debug trong VS Code

Compose dev chỉ chạy hạ tầng và mở các cổng nội bộ ra máy host:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml \
  up -d redis postgres pgbouncer-write pgbouncer-read
```

Sau đó chạy các process Python trong terminal hoặc VS Code:

```bash
python -m uvicorn analyst_api.main:app --host 0.0.0.0 --port 8000
python -m scoring_worker.main
python -m llm_worker.main
```

Nếu chạy ngoài Docker, các giá trị mặc định trong `fraud_common/config.py` sẽ
trỏ tới Redis `localhost:6379` và PgBouncer `localhost:6432/6433`. Có thể đặt
biến môi trường trước khi khởi động process nếu dùng endpoint khác.

## Dashboard

Dashboard được serve bởi Analyst API tại `/`. Sau khi đăng nhập, màn hình review
queue hỗ trợ:

- lọc theo risk level và trạng thái;
- xem confidence, trạng thái brief và chi tiết transaction đã che PII;
- thêm note, yêu cầu thêm thông tin, xác nhận simulated hoặc escalate;
- phân loại fraud/an toàn/chưa đủ bằng chứng và chỉnh sửa quyết định đã lưu; mỗi lần chỉnh sửa được thêm vào lịch sử/audit;
- risk lead xử lý case escalated;
- analyst/risk lead yêu cầu tạo lại brief trong giới hạn cấu hình.

Một số ô giao diện hiện là thông tin mẫu hoặc chưa nối backend (ví dụ analytics
và queue latency).

## API

API JSON có prefix `/api/v1`. Tài liệu OpenAPI tự sinh ở
<http://localhost:8000/docs> khi dùng compose dev; qua Nginx có thể dùng
<http://localhost:8080/docs>.

### Đăng nhập

```bash
curl -s http://localhost:8080/api/v1/auth/login \
  -H "content-type: application/json" \
  -d "{\"username\":\"analyst1\",\"password\":\"analyst123\"}"
```

Tài khoản demo:

| Username | Password | Role |
|---|---|---|
| `analyst1` | `analyst123` | Analyst |
| `analyst2` | `analyst123` | Analyst |
| `lead1` | `lead123` | Risk Lead |
| `admin` | `admin123` | Admin |

Người dùng mới có thể đăng ký trực tiếp trên màn hình đăng nhập hoặc gọi
`POST /api/v1/auth/register` với `username`, `display_name` và `password`.
Tài khoản đăng ký mới cần thêm email, được tạo ở vai trò Analyst; mật khẩu được
lưu dưới dạng hash scrypt và phải xác thực email qua SMTP trước khi đăng nhập.
Màn hình cũng hỗ trợ gửi email đặt lại mật khẩu và nút Google OAuth. Để bật hai
luồng email/Google, cấu hình `SMTP_*` và `GOOGLE_*` trong `.env`; secret không
được đưa vào source code.

Lưu `access_token` từ response rồi dùng header `Authorization: Bearer <token>`.
Các mật khẩu trên chỉ dành cho demo; production cần thay bằng user store và
mật khẩu đã hash.

### Các endpoint chính

| Method | Path | Quyền | Mô tả |
|---|---|---|---|
| `POST` | `/api/v1/auth/login` | Public | Đăng nhập, nhận JWT |
| `POST` | `/api/v1/auth/register` | Public | Tạo tài khoản Analyst và nhận JWT |
| `GET` | `/api/v1/auth/verify` | Public | Xác thực email |
| `POST` | `/api/v1/auth/forgot-password` | Public | Gửi email đặt lại mật khẩu |
| `POST` | `/api/v1/auth/reset-password` | Public | Đặt mật khẩu mới bằng token |
| `GET` | `/api/v1/auth/google` | Public | Bắt đầu Google OAuth |
| `POST` | `/api/v1/transactions` | `X-API-Key` | Queue giao dịch để chấm điểm |
| `GET` | `/api/v1/transactions/{txn_id}/result` | API key hoặc JWT | Lấy kết quả bất đồng bộ |
| `GET` | `/api/v1/cases` | Analyst, Risk Lead | Review queue, High trước |
| `GET` | `/api/v1/cases/classifications` | Analyst, Risk Lead | Giao dịch đã được analyst phân loại/chuyển tiếp; hỗ trợ `decision`, `search`, `limit`, `offset` |
| `PUT` | `/api/v1/cases/{case_id}/outcome` | Analyst, Risk Lead | Cập nhật hoặc phân loại lại kết quả; mọi lần chỉnh sửa được lưu trong lịch sử và audit log |
| `GET` | `/api/v1/cases/{case_id}` | Analyst, Risk Lead | Chi tiết case và lịch sử thao tác |
| `POST` | `/api/v1/cases/manual` | Analyst, Risk Lead | Tạo case thủ công cho giao dịch LOW/unscored |
| `POST` | `/api/v1/cases/{case_id}/actions` | Analyst | `NOTE`, `NEEDS_MORE_INFO`, `CONFIRM_SIMULATED`, `ESCALATE` |
| `POST` | `/api/v1/cases/{case_id}/notes` | Analyst, Risk Lead | Thêm note append-only |
| `PUT` | `/api/v1/cases/{case_id}/assign` | Analyst, Risk Lead | Gán hoặc bỏ gán case |
| `PUT` | `/api/v1/cases/{case_id}/outcome` | Analyst, Risk Lead | `CONFIRMED_FRAUD`, `DISMISSED`, `INSUFFICIENT_EVIDENCE`; ghi lịch sử và audit |
| `POST` | `/api/v1/cases/{case_id}/resolve` | Risk Lead | `CLOSE` hoặc `RETURN` case escalated |
| `POST` | `/api/v1/cases/{case_id}/brief:regenerate` | Analyst, Risk Lead | Queue tạo lại brief |
| `GET` / `PUT` | `/api/v1/admin/threshold-config` | Admin | Đọc/cập nhật ngưỡng risk có version |
| `GET` | `/api/v1/admin/queues` | Admin | Độ sâu các queue |
| `GET` | `/healthz` | Public | Health check |
| `GET` | `/metrics` | Internal | Prometheus metrics |

Analyst decisions are stored in the case status, action history, and audit log.
The dashboard's **Manual decisions** section reads those persisted action records,
showing the latest classification or handoff per case with reviewer, timestamp,
and masked transaction details.
Confirming fraud or dismissing a case does not itself block a payment, blacklist
a card, or suspend an account; those effects require integration with the
payment/account systems.

Ví dụ lấy queue:

```bash
curl -s http://localhost:8080/api/v1/cases \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Ví dụ chuyển case lên risk lead:

```bash
curl -s -X POST http://localhost:8080/api/v1/cases/1/actions \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "content-type: application/json" \
  -d "{\"action\":\"ESCALATE\",\"note\":\"Cần risk lead kiểm tra\"}"
```

## Simulator

Simulator không gửi nhãn `is_fraud` vào API; nhãn được ghi riêng vào file CSV
`data/labels.csv`. Nếu không truyền `--csv`, simulator tự sinh dữ liệu deterministic.

```bash
python -m simulator.replay --url http://localhost:8080 --tps 10 --limit 500
python -m simulator.replay --csv data/fraudTrain.csv --tps 10 \
  --limit 1000 --fault-rate 0.02 --concurrency 100
```

| Option | Mặc định | Ý nghĩa |
|---|---:|---|
| `--url` | `http://localhost:8080` | Base URL của gateway |
| `--csv` | không có | Dataset Sparkov/fraudTrain tương thích |
| `--tps` | `10` | Tốc độ phát giao dịch |
| `--limit` | `500` | Số dòng; `0` là toàn bộ |
| `--fault-rate` | `0` | Tỉ lệ payload bị tiêm lỗi |
| `--concurrency` | `100` | Số request đồng thời tối đa |
| `--labels` | `data/labels.csv` | File lưu nhãn thật |

## Cấu hình

Compose đã cung cấp các giá trị mặc định phù hợp cho local development. Production
cần thay tối thiểu `JWT_SECRET`, `SIMULATOR_API_KEY`, `PII_SALT` và thông tin
PostgreSQL bằng secret manager hoặc environment injection.

| Biến | Mặc định | Mục đích |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `DB_WRITE_URL` | PgBouncer write local | Kết nối ghi DB |
| `DB_READ_URL` | PgBouncer read local | Kết nối đọc DB |
| `JWT_SECRET` | `dev-only-change-me` | Ký JWT |
| `JWT_TTL_S` | `28800` | Thời hạn JWT (giây) |
| `SIMULATOR_API_KEY` | `dev-sim-key` | Xác thực simulator |
| `PII_SALT` | `dev-salt` | Hash định danh PII |
| `MAX_ML_QUEUE_DEPTH` | `5000` | Ngưỡng trả HTTP 429 |
| `ML_CONCURRENCY` | `8` | Worker ML đồng thời |
| `ML_JOB_TIMEOUT_S` | `2` | Timeout job ML |
| `ML_MAX_ATTEMPTS` | `3` | Số lần thử job ML |
| `LLM_CONCURRENCY` | `2` | Worker LLM đồng thời |
| `LLM_JOB_TIMEOUT_S` | `30` | Timeout job LLM |
| `LLM_MAX_ATTEMPTS` | `3` | Số lần thử job LLM |
| `BRIEF_TOKEN_BUDGET` | `5000` | Ngân sách brief |
| `REGEN_LIMIT_PER_HOUR` | `3` | Số lần regenerate/case/giờ |
| `LLM_PROVIDER` | `stub` | Provider LLM hiện tại |
| `LLM_STUB_LATENCY_S` | `1.0` | Độ trễ giả lập |
| `LLM_STUB_FAIL_RATE` | `0.0` | Tỉ lệ lỗi giả lập |

Để thử circuit breaker và fallback:

```bash
LLM_STUB_FAIL_RATE=0.5 docker compose up -d --build
```

## Observability

- `GET /healthz`: kiểm tra API còn kết nối Redis.
- Prometheus scrape Analyst API tại `/metrics/`.
- Worker expose metrics ở `METRICS_PORT` (`9101` cho scoring, `9102` cho LLM).
- `X-Request-Id` được truyền thành `trace_id` qua queue, log và audit record.
- Redis queue có thống kê `ready`, `processing`, `dead` qua endpoint admin.

Prometheus mở tại <http://localhost:9090> khi chạy compose dev.

## Test

Test logic không cần hạ tầng:

```bash
python -m pytest tests/test_logic.py -q
```

Test toàn bộ:

```bash
python -m pytest -q
```

`tests/test_queue_integration.py` sẽ tự skip nếu Redis chưa chạy. Để chạy test
integration, mở Redis bằng:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis
python -m pytest tests/test_queue_integration.py -q
```

### Kiểm thử tự động trên CI/CD

Workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) tự chạy khi có
Pull Request, push vào `main`/`master`, hoặc được kích hoạt thủ công trong
GitHub Actions. Workflow gồm bốn job:

1. **Code quality**: cài dependency và kiểm tra toàn bộ Python source compile được.
2. **Unit and integration tests**: tạo Redis và PostgreSQL tạm thời rồi chạy
   toàn bộ pytest, bao gồm queue và brief pipeline integration tests.
3. **Docker build**: kiểm tra Compose và build các image chính.
4. **ML quality gate**: kiểm tra PR-AUC/precision/recall/F1 của model baseline
   đã commit và upload report làm artifact.
5. **End-to-end smoke test**: khởi động API stack và gọi `GET /healthz`; nếu
   endpoint không trả HTTP 2xx, workflow thất bại và in log để tìm lỗi.

Đánh giá lại model đã có mà không train lại:

```bash
python -m ml_pipeline.evaluate_artifact \
  --artifact data/models/fraud_xgboost.joblib \
  --test-data data/fraudTest.csv \
  --output data/models/fraud_xgboost.evaluation.json
```

Lệnh trên dùng `.joblib` để dự đoán lại test set, sau đó ghi metric thực tế vào
file JSON. Nếu PR-AUC, recall, precision hoặc F1 thấp hơn ngưỡng, lệnh trả
exit code khác 0. CI dùng [`reports/ml-model-baseline.json`](reports/ml-model-baseline.json)
để gate ổn định vì dataset và model binary đang nằm trong `.gitignore`; khi
đã có artifact storage, có thể thay bước baseline bằng lệnh evaluator ở trên.

Để trình diễn với giảng viên:

1. Push code lên GitHub hoặc tạo Pull Request.
2. Mở tab **Actions** → chọn **Fraud Copilot CI**.
3. Mở một workflow run để xem từng bước, số test pass/fail, Docker build và
   kết quả health check.

Kết quả local tương đương có thể chạy bằng:

```bash
python -m pytest -q
docker compose config --quiet
docker compose up -d --build redis postgres pgbouncer-write pgbouncer-read analyst-api nginx
curl http://localhost:8080/healthz
docker compose down -v
```

## Cơ chế xử lý lỗi

- Giao dịch trùng trong một giờ trả `200 DUPLICATE`.
- ML queue đầy trả `429` kèm `Retry-After`.
- Job ML lỗi quá số lần thử được đưa vào manual fallback.
- Job LLM timeout được đánh dấu `FAILED_TIMEOUT` và tạo brief rule-based.
- Job đang xử lý nhưng worker chết sẽ được requeue bởi visibility-timeout reaper.
- Các handler được thiết kế idempotent để an toàn với at-least-once delivery.
- Giao dịch và brief không đưa nhãn `is_fraud` vào luồng suy luận; transaction
  hiển thị cho analyst đã được che PII.

## Các phần cần thay thế

| File | Hiện tại | Cần thay bằng |
|---|---|---|
| `scoring_worker/pipeline/inference.py` | Điểm giả theo công thức | Model và calibrator từ Model Store |
| `scoring_worker/pipeline/features.py` | Một số feature đơn giản | Feature engineering production |
| `llm_worker/llm_client.py` | `StubLLMClient` | Client LLM thật/local |
| `llm_worker/rag.py` | Bốn đoạn playbook mẫu | Vector index/RAG thật |
| `llm_worker/grounding.py` | Kiểm tra citation cơ bản | Kiểm chứng số liệu và grounding sâu hơn |
| `analyst_api/auth.py` | User table | Password hash scrypt và JWT |

Khi thay implementation, giữ nguyên interface và schema trong
`fraud_common/schemas.py` để không phá vỡ contract giữa các service.

## Cấu trúc thư mục

```text
analyst_api/       API, auth, case actions, admin và event consumer
fraud_common/      config, DB, schema, queue, metrics, resilience
scoring_worker/    validation, PII masking, feature và scoring pipeline
llm_worker/        RAG, grounding, LLM client và brief worker
simulator/         replay dataset hoặc dữ liệu tổng hợp
db/                PostgreSQL schema khởi tạo
nginx/             gateway và routing
prometheus/        Prometheus scrape config
static/            dashboard frontend
tests/             unit test và Redis queue integration test
```
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
