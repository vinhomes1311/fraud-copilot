# Fraud Copilot

Fraud Copilot là một pipeline xử lý giao dịch gian lận bất đồng bộ, gồm Analyst API,
Scoring Worker, LLM Worker, Redis, PostgreSQL, PgBouncer và dashboard web cho analyst.
Toàn bộ stack có thể chạy bằng Docker Compose.

> **Trạng thái hiện tại:** hạ tầng giao tiếp, hợp đồng dữ liệu, retry/timeout,
> circuit breaker, RBAC, audit log, metrics và dashboard đã có implementation. LLM hỗ trợ
> API online, Ollama và stub; phần chấm điểm ML vẫn là implementation demo.
> Xem [hướng dẫn API online](docs/llm/api-adapter.md) để chọn model mà không cần train lại LLM.

## Mục lục

- [Kiến trúc](#kiến-trúc)
- [Yêu cầu](#yêu-cầu)
- [Thiết lập cho thành viên mới và train lại model](#thiết-lập-cho-thành-viên-mới-và-train-lại-model)
- [Chạy nhanh bằng Docker](#chạy-nhanh-bằng-docker)
- [Dùng LLM online qua API](#dùng-llm-online-qua-api)
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
  LW --> RAG[Playbook + LLM: API / Ollama / stub]
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

## Thiết lập cho thành viên mới và train lại model

Mỗi thành viên clone repository cần tự tải dataset từ Kaggle về máy và train
model local. Dataset, processed data và artifact mới sinh ra được bỏ qua bởi
`.gitignore`. Model baseline đã được theo dõi trong Git vẫn được giữ để serving
và kiểm thử scoring thật trên CI; tránh commit đè bằng model của từng máy.

### 1. Clone repository và cài dependency

Từ PowerShell:

```powershell
git clone https://github.com/vinhomes1311/fraud-copilot.git
Set-Location fraud-copilot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Nếu PowerShell chặn activate script, có thể chạy trực tiếp bằng Python đã cài
dependency hoặc mở PowerShell với policy phù hợp trong phạm vi user.

### 2. Tải dataset Kaggle

Tải bộ dữ liệu fraud detection Sparkov từ Kaggle, sau đó đặt đúng hai file vào
folder `data/`:

```text
data/
├── fraudTrain.csv
└── fraudTest.csv
```

Tên file phải đúng như trên vì [ml_pipeline/config.json](ml_pipeline/config.json)
đang trỏ tới:

```json
{
  "data": {
    "train_file": "data/fraudTrain.csv",
    "test_file": "data/fraudTest.csv"
  }
}
```

Dataset cần có tối thiểu các cột mà pipeline yêu cầu, gồm `is_fraud`, `amt`,
`cc_num` và timestamp (`trans_date_trans_time` hoặc cột timestamp tương thích),
cùng các cột địa lý/merchant nếu muốn tạo đầy đủ feature như pipeline hiện tại.
Không đổi tên hoặc trộn train/test trước khi chạy; pipeline tự xử lý theo thứ
tự thời gian và loại bỏ PII khỏi feature.

Có thể kiểm tra nhanh file đã đặt đúng chỗ:

```powershell
Test-Path data\fraudTrain.csv
Test-Path data\fraudTest.csv
```

Cả hai lệnh phải trả về `True`.

### 3. Chọn GPU hoặc CPU

Mặc định hiện tại trong `ml_pipeline/config.json` là:

```json
"device": "cpu"
```

Mặc định CPU để cấu hình dùng chung chạy được trên máy không có CUDA. Nếu máy
có NVIDIA GPU, driver hoạt động và XGBoost hỗ trợ CUDA, tạo cấu hình riêng:

```powershell
Copy-Item ml_pipeline\config.json ml_pipeline\config.local.json
# Đổi model.device thành "cuda" trong config.local.json, rồi chạy:
$env:ML_CONFIG_PATH = 'ml_pipeline/config.local.json'
```

`config.local.json` được Git bỏ qua để mỗi thành viên tự chọn phần cứng. Kiểm tra GPU:

```powershell
nvidia-smi
```

Nếu CUDA lỗi, dùng config CPU mặc định hoặc đặt `model.device` thành `cpu` trong override:

```json
"device": "cpu"
```

Các bước đọc CSV, preprocessing bằng pandas, calibration và ghi file vẫn chạy
trên CPU; chỉ phần XGBoost training có thể dùng GPU.

### 4. Chạy lại preprocessing và train từ đầu

Chạy tại thư mục gốc repository:

```powershell
python -m ml_pipeline --config ml_pipeline/config.json
```

Nếu đang dùng GPU override, truyền đường dẫn cấu hình local thay vào lệnh trên:

```powershell
python -m ml_pipeline --config $env:ML_CONFIG_PATH
```

Lệnh này chạy toàn bộ workflow:

```text
fraudTrain.csv + fraudTest.csv
        ↓
feature engineering và chronological split
        ↓
data/processed/train.csv
data/processed/validation.csv
data/processed/test.csv
        ↓
train XGBoost + validation/calibration/evaluation
        ↓
data/models/fraud_xgboost.joblib
data/models/fraud_xgboost.json
```

Không chạy trực tiếp file `fraud_xgboost.json`; đó chỉ là metadata metric và
lineage được tạo sau khi train. Mỗi lần chạy workflow sẽ ghi đè processed data
và model artifact local.

Nếu muốn chạy training trong Docker:

```powershell
docker compose --profile training run --rm ml-pipeline
```

Profile Docker này thuận tiện cho môi trường không cần debug Python local,
nhưng GPU chỉ được dùng khi Docker đã được cấu hình NVIDIA Container Toolkit.
Để dùng GPU đơn giản trên Windows, chạy lệnh Python local ở trên.

### 5. Kiểm tra artifact sau khi train

```powershell
Get-ChildItem data\processed
Get-Item data\models\fraud_xgboost.joblib
Get-Content data\models\fraud_xgboost.json
```

Đánh giá lại artifact mà không train lại:

```powershell
python -m ml_pipeline.evaluate_artifact `
  --artifact data\models\fraud_xgboost.joblib `
  --test-data data\fraudTest.csv `
  --output reports\ml-model-evaluation.json
```

Sau khi có artifact, scoring worker sẽ dùng file
`data/models/fraud_xgboost.joblib`. Nếu xóa artifact hoặc chưa train, scoring
worker không thể chấm điểm các giao dịch mới.

## Chạy nhanh bằng Docker

Từ thư mục gốc repository:

```bash
docker compose up --build -d
docker compose ps
curl http://localhost:8080/healthz
```

Dashboard mở tại <http://localhost:8080/>.

Trong dashboard, simulator tự bắt đầu phát lại dữ liệu test ở lần đăng nhập đầu tiên nếu chưa từng được
điều khiển. Trạng thái,
tiến độ và tốc độ được lưu dùng chung trên server nên các tab dashboard đồng bộ với nhau; dừng ở một tab
sẽ dừng ở các tab còn lại. Có thể chỉnh tốc độ từ 1 đến 1.000 giao dịch/giây; tốc độ mới có hiệu lực từ
lượt gửi tiếp theo. Đây là tốc độ phát yêu cầu mục tiêu, tốc độ xử lý thực tế còn phụ thuộc tài nguyên
hệ thống. Đóng tab không tự dừng simulator; dùng nút **Dừng simulator** để dừng hoặc chạy lại.
Video chó truy vết nằm cạnh nút simulator và chỉ phát khi simulator hoạt động. Review queue hiển thị cả LOW,
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

## Dùng LLM online qua API

Copy `.env.example` thành `.env` nếu chưa có, điền `LLM_API_KEY` trong editor trên
máy của bạn và giữ `LLM_PROVIDER=api`. Cấu hình mẫu dùng `gpt-5.4-mini`; có thể đổi
model hoặc endpoint mà không train lại LLM. `.env` không được commit vào Git.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Điền API key vào .env trước khi chạy lệnh dưới.
docker compose -f docker-compose.yml -f docker-compose.api.yml up --build -d
```

Dashboard tiếp tục dùng tại <http://localhost:8080/>. Với `.env` đã điền đầy đủ,
`docker compose up --build -d` cũng chuyển LLM worker sang API. Không cấu hình
`LLM_PROVIDER` thì demo dùng stub. `LLM_RETRIEVER=playbook` dùng toàn bộ playbook
nhỏ hiện có, không cần Ollama/GPU hay embedding model.

Khi chạy Python/VS Code, cài requirements rồi kiểm tra:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m llm_worker.smoke_test
# Một request dữ liệu giả đến API thật, có thể phát sinh phí:
.\.venv\Scripts\python.exe -m llm_worker.smoke_test --live
```

Python tự nạp `.env` ở thư mục dự án; biến môi trường trong shell được ưu tiên.
Smoke mặc định chỉ kiểm tra cấu hình; `--live` mới gọi API. LLM chọn evidence và
checklist ID; ứng dụng kiểm tra nguồn và dựng brief có citation trước khi lưu DB.
Chi tiết model, reasoning effort, token budget và lỗi quota/credit xem
[docs/llm/api-adapter.md](docs/llm/api-adapter.md). Local Ollama được mô tả ở
[hướng dẫn Ollama local](docs/llm/local-integration.md).

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
biến môi trường hoặc `.env` trước khi khởi động process nếu dùng endpoint khác.

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
`POST /api/v1/auth/register` chỉ với `username` và `password`. Tài khoản được
lưu vào PostgreSQL ở vai trò Analyst, mật khẩu được lưu dưới dạng hash scrypt
và đăng nhập ngay sau khi tạo; không cần email hay Gmail.
Màn hình cũng hỗ trợ gửi email đặt lại mật khẩu cho tài khoản có email và nút
Google OAuth. Để bật các luồng email/Google, cấu hình `SMTP_*` và `GOOGLE_*`
trong `.env`; secret không được đưa vào source code.

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
| `GET` | `/api/v1/cases/analytics/ml-operations` | Analyst, Risk Lead | p95 inference latency và throughput từ Prometheus |
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
| `LLM_CONCURRENCY` | `2` Python / `1` Compose | Worker LLM đồng thời |
| `LLM_JOB_TIMEOUT_S` | `30` Python / `120` Compose | Timeout mỗi stage LLM (giây) |
| `LLM_MAX_ATTEMPTS` | `3` | Số lần thử job LLM |
| `BRIEF_TOKEN_BUDGET` | `5000` | Kiểm tra tổng token sau inference; chưa phải trần chi phí |
| `REGEN_LIMIT_PER_HOUR` | `3` | Số lần regenerate/case/giờ |
| `LLM_PROVIDER` | `stub`; `.env.example` bật `api` | `api`, `ollama` hoặc `stub` |
| `LLM_API_BASE_URL` | `https://api.openai.com/v1` | HTTPS endpoint tương thích Chat Completions |
| `LLM_API_MODEL` | `gpt-5.4-mini` | Model online được cấp quyền trong tài khoản API |
| `LLM_API_KEY` | trống | API key; chỉ cấu hình cục bộ |
| `LLM_API_MAX_COMPLETION_TOKENS` | `1024` | Trần output gồm cả reasoning token |
| `LLM_API_REASONING_EFFORT` | trống | Tùy chọn theo khả năng của model |
| `LLM_RETRIEVER` | `playbook` cho API / `ollama` cho Ollama | Nguồn context |
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
- Analytics hiển thị phân bổ quyết định theo risk level và xu hướng inference p95/RPS trong 60 phút; API truy vấn Prometheus qua `PROMETHEUS_URL`.
- `X-Request-Id` được truyền thành `trace_id` qua queue, log và audit record.
- Redis queue có thống kê `ready`, `processing`, `dead` qua endpoint admin.

Prometheus mở tại <http://localhost:9090> khi chạy compose dev.

## Test

So sánh hiệu năng Ollama local và API online bằng bộ case chung:
xem [docs/benchmarks/README.md](docs/benchmarks/README.md). Lệnh mặc định chỉ in kế hoạch;
`--mock` chạy offline, `--live` mới gọi model thật.
Phạm vi LLM/RAG theo tài liệu kiến trúc và cách đo retrieval + generation được
đối chiếu tại [docs/benchmarks/architecture.md](docs/benchmarks/architecture.md).

Test logic không cần hạ tầng:

```bash
python -m pytest tests/unit_integration/test_logic.py -q
```

Test toàn bộ:

```bash
python -m pytest -q
```

`tests/unit_integration/test_queue_integration.py` sẽ tự skip nếu Redis chưa chạy. Để chạy test
integration, mở Redis bằng:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis
python -m pytest tests/unit_integration/test_queue_integration.py -q
```

Các bài API dùng MockTransport, không dùng key thật và không tính phí LLM.
Để chạy đầy đủ workflow qua SQL/Redis thật, đặt URL của dịch vụ kiểm thử riêng:

```powershell
$env:LLM_PROVIDER = 'stub'
$env:LLM_STUB_LATENCY_S = '0'
$env:LLM_STUB_FAIL_RATE = '0'
$env:FRAUD_TEST_DB_URL = 'postgresql://fraud:fraud@127.0.0.1:5432/fraud'
$env:FRAUD_TEST_REDIS_URL = 'redis://127.0.0.1:6379/15'
python -m pytest -p no:cacheprovider -o asyncio_default_fixture_loop_scope=function -q
```

Điều chỉnh URL theo dịch vụ dành riêng cho kiểm thử. `test_workflow_e2e.py` kiểm tra
giao dịch LOW/MEDIUM/HIGH, dữ liệu sai/trùng, brief, phân quyền, chuyển cấp/đóng
case, cấu hình ngưỡng, regenerate và retry/fallback ở cả stub lẫn API giả lập.
Các bài cần DB/Redis sẽ tự skip khi chưa cấu hình dịch vụ tương ứng.

### Kiểm thử tự động trên CI/CD

Workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) tự chạy khi có
Pull Request, push vào `main`/`master`, hoặc được kích hoạt thủ công trong
GitHub Actions. Workflow gồm năm job:

1. **Code quality**: cài dependency và kiểm tra toàn bộ Python source compile được.
2. **Unit and integration tests**: tạo Redis và PostgreSQL tạm thời rồi chạy
   toàn bộ pytest, bao gồm queue và brief pipeline integration tests.
3. **Docker build**: kiểm tra Compose và build các image chính.
4. **ML quality gate**: kiểm tra PR-AUC/precision/recall/F1 của model baseline
   đã commit và upload report làm artifact.
5. **End-to-end smoke test**: khởi động API stack và gọi `GET /healthz`; nếu
   endpoint không trả HTTP 2xx, workflow thất bại và in log để tìm lỗi.

Test transaction thật qua simulator cần một stack đang chạy và được bật riêng
để tránh làm bộ test mặc định phụ thuộc Docker:

```powershell
$env:FRAUD_E2E_URL = "http://localhost:8080"
$env:SIMULATOR_API_KEY = "dev-sim-key"
python -m pytest tests/e2e_smoke/test_transaction_e2e.py -q
```

Test này dùng chính `simulator.replay`, gửi transaction qua Nginx/Analyst API,
đợi Redis/Scoring Worker xử lý và xác nhận kết quả đã được ghi vào PostgreSQL.
Nếu không đặt `FRAUD_E2E_URL`, test sẽ được skip; đây là chủ ý để CI unit test
không cần chạy toàn bộ service worker.

Đánh giá lại model đã có mà không train lại:

```bash
python -m ml_pipeline.evaluate_artifact \
  --artifact data/models/fraud_xgboost.joblib \
  --test-data data/fraudTest.csv \
  --output data/models/fraud_xgboost.evaluation.json
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

Playbook LLM/RAG đã được điều chỉnh cho dataset `kartik2112/fraud-detection`.
Xem [ánh xạ cột, nguồn và giới hạn suy luận](docs/playbooks/kaggle-dataset.md).

| File | Hiện tại | Cần thay bằng |
|---|---|---|
| `scoring_worker/pipeline/inference.py` | Điểm giả theo công thức | Model và calibrator từ Model Store |
| `scoring_worker/pipeline/features.py` | Một số feature đơn giản | Feature engineering production |
| `llm_worker/llm_client.py`, `api_client.py` | API online, Ollama hoặc stub | Đánh giá model trên case đại diện |
| `llm_worker/rag.py` | Playbook được phê duyệt; semantic retrieval cho Ollama | Mở rộng retrieval khi KB lớn hơn |
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
static/media/      media assets served by the dashboard
docs/llm/          hướng dẫn LLM local, demo và API tùy chọn
docs/benchmarks/   quy trình benchmark, kiến trúc và reference
docs/playbooks/    giải thích playbook, dataset và điều kiện áp dụng
benchmarks/        script benchmark; fixtures/ và config/ được version
knowledge/         playbook đang dùng khi chạy worker
reports/           baseline CI và report local được Git ignore
notebooks/         exploratory data preprocessing and ML notebooks
tests/             test được chia theo các job CI: unit_integration, ml_evaluation, e2e_smoke
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

## Tổ chức tài liệu và dữ liệu local

Xem [mục lục tài liệu](docs/README.md) và [cấu trúc benchmark](benchmarks/README.md).
Fixture/config benchmark được version để tái lập kiểm thử. Dataset, model mới,
kết quả benchmark, report sinh ra, cache và cấu hình VS Code cá nhân được bỏ qua
bởi `.gitignore`. File baseline đang được CI sử dụng vẫn được giữ trong Git.
