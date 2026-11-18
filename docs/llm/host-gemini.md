# Chạy toàn pipeline trên host Windows với Gemini API

Chạy 3 process Python trên Windows: Analyst API, Scoring Worker và LLM Worker.
Docker chỉ chạy PostgreSQL, Redis và hai PgBouncer. Dashboard dùng cổng **8000**.
Scoring Worker nạp artifact ML trực tiếp; không cần chạy thêm model-service.

Luồng tự động: HTTP ingest → Redis ml_jobs → ML scoring → PostgreSQL case →
case_events → Analyst API → llm_jobs → Gemini + playbook → brief/citation → DB/UI.
Chỉ MEDIUM/HIGH tự phát case_events để tạo brief; LOW cần analyst yêu cầu thủ công.

## 1. Chuẩn bị và cấu hình

Mọi terminal đều đứng ở thư mục gốc repository:

```powershell
Set-Location C:\path\to\fraud-copilot
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Test-Path data\models\fraud_xgboost.joblib
```

Artifact đang có thì không cần train lại chỉ để chạy thử. Mở `.env`, sửa các dòng
đã có, không khai báo trùng tên. Giữ nguyên `LLM_API_KEY` Gemini đã điền:

```dotenv
LLM_PROVIDER=api
LLM_RETRIEVER=playbook
LLM_API_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_MODEL=gemini-3.5-flash-lite
LLM_API_RESPONSE_FORMAT=json_schema
LLM_API_TOKEN_PARAMETER=max_tokens
LLM_API_STORE=omit
LLM_API_MAX_COMPLETION_TOKENS=1024
LLM_API_REASONING_EFFORT=
LLM_CONCURRENCY=1
LLM_JOB_TIMEOUT_S=120
LLM_VISIBILITY_TIMEOUT_S=300

REDIS_URL=redis://127.0.0.1:6379/0
DB_WRITE_URL=postgresql://fraud:fraud@127.0.0.1:6432/fraud
DB_READ_URL=postgresql://fraud:fraud@127.0.0.1:6433/fraud
APP_BASE_URL=http://127.0.0.1:8000
MODEL_ARTIFACT_PATH=data/models/fraud_xgboost.joblib
ML_CONCURRENCY=1
ML_JOB_TIMEOUT_S=30
ML_VISIBILITY_TIMEOUT_S=90
```

DB URL trên dùng thông tin demo mặc định `fraud/fraud/fraud`. Nếu đã đổi
POSTGRES_USER/PASSWORD/DB thì sửa URL tương ứng. Các biến OLLAMA vẫn giữ được
trong `.env`; `LLM_RETRIEVER=playbook` khiến lần chạy API này không cần Ollama.
Biến shell có ưu tiên hơn `.env`; cần khởi động lại process sau khi đổi cấu hình.

## 2. Chạy hạ tầng

Mở Docker Desktop. Nếu đã chạy các application service trong Docker, dừng chúng
trước để tránh hai bộ worker cùng lấy job trên một queue:

```powershell
docker compose stop nginx analyst-api scoring-worker llm-worker model-service
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --wait redis postgres pgbouncer-write pgbouncer-read
docker compose -f docker-compose.yml -f docker-compose.dev.yml ps
```

Chỉ định 4 service ở cuối lệnh để không khởi chạy toàn bộ application trong
container. Không dùng `docker-compose.validation.yml`: đó là DB/Redis tạm cho
runner kiểm thử riêng, không phải hạ tầng ứng dụng trên cổng 5432/6379/6432/6433.

## 3. Mở ba terminal Python

Terminal A — API và dashboard:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m uvicorn analyst_api.main:app --host 127.0.0.1 --port 8000
```

Terminal B — Scoring Worker:

```powershell
$env:SERVICE_NAME = 'scoring-worker'
$env:METRICS_PORT = '9101'
.\.venv\Scripts\python.exe -X utf8 -m scoring_worker.main
```

Terminal C — LLM Worker gọi Gemini:

```powershell
$env:SERVICE_NAME = 'llm-worker'
$env:METRICS_PORT = '9102'
$env:LLM_PROVIDER = 'api'
$env:LLM_RETRIEVER = 'playbook'
.\.venv\Scripts\python.exe -X utf8 -m llm_worker.smoke_test
.\.venv\Scripts\python.exe -X utf8 -m llm_worker.main
```

Smoke không có `--live` chỉ kiểm tra cấu hình, không tiêu request. Log LLM cần có
`LLM configured` với provider `api` và `llm-worker sẵn sàng`.
Profile VS Code `LLM Worker (local)` hiện ép `LLM_PROVIDER=ollama`; các lệnh
terminal trên chọn Gemini rõ ràng, không dùng profile đó cho lượt thử API.

## 4. Gửi một giao dịch và theo dõi đến brief

Terminal D — health, đăng nhập và dừng simulator trước khi mở dashboard để
không vô tình phát nhiều giao dịch vào free API:

```powershell
$base = 'http://127.0.0.1:8000'
Invoke-RestMethod "$base/healthz"

$login = Invoke-RestMethod -Method Post -Uri "$base/api/v1/auth/login" -ContentType 'application/json' -Body '{"username":"analyst1","password":"analyst123"}'
$auth = @{ Authorization = "Bearer $($login.access_token)" }
Invoke-RestMethod -Method Post -Uri "$base/api/v1/demo/simulator" -Headers $auth -ContentType 'application/json' -Body '{"action":"stop"}'
```

Simulator key dưới đây là giá trị demo mặc định. Nếu `.env` đã đổi
SIMULATOR_API_KEY thì dùng giá trị tương ứng, không dùng Gemini key cho ingest.

```powershell
$ingestAuth = @{ 'X-API-Key' = 'dev-sim-key' }
$txnId = 'host-api-' + [guid]::NewGuid().ToString('N')
$body = @{
    trans_num = $txnId
    trans_date_trans_time = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    cc_num = '4000000000001234'
    merchant = 'host_test_merchant'
    category = 'grocery_pos'
    amt = 1750.50
    lat = 40.7128
    long = -74.0060
    merch_lat = 34.0522
    merch_long = -118.2437
    city_pop = 100000
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "$base/api/v1/transactions" -Headers $ingestAuth -ContentType 'application/json' -Body $body

# QUEUED/202 chỉ xác nhận đã nhận job; chờ ML ghi kết quả.
for ($attempt = 0; $attempt -lt 90; $attempt++) {
    $score = Invoke-RestMethod -Uri "$base/api/v1/transactions/$txnId/result" -Headers $ingestAuth
    if ($score.status -ne 'PENDING') { break }
    Start-Sleep -Seconds 1
}
$score | Format-List
if ($score.status -ne 'SCORED') { throw 'ML chưa SCORED; xem log Scoring Worker và validation_errors trong DB.' }
if (-not $score.case_id) { throw 'Scoring đã có nhưng chưa có case_id; xem log/persistence.' }

# LOW không tự tạo brief. Yêu cầu thủ công để thử nhánh Gemini cho chính case này.
# Với MEDIUM/HIGH, giữ luồng tự động qua case_events, không enqueue thêm.
if ($score.risk_level -eq 'LOW') {
    Invoke-RestMethod -Method Post -Uri "$base/api/v1/cases/$($score.case_id)/brief:regenerate" -Headers $auth
}

for ($attempt = 0; $attempt -lt 300; $attempt++) {
    $case = Invoke-RestMethod -Uri "$base/api/v1/cases/$($score.case_id)" -Headers $auth
    if ($case.brief_status -in @('READY','FAILED','FAILED_TIMEOUT')) { break }
    Start-Sleep -Seconds 1
}
$case | Select-Object case_id, risk_level, brief_status, brief_attempts, last_error
$case.brief | ConvertTo-Json -Depth 8
```

Kết quả mong đợi: `SCORED`, có `case_id`, `brief_status=READY`,
`brief.source=llm`, checklist/citation không rỗng. MEDIUM/HIGH tự đi qua toàn
luồng event; LOW giữ routing của ML và chỉ gọi Gemini khi analyst yêu cầu.
`FAILED`/`FAILED_TIMEOUT` cùng `source=rule_based` nghĩa là fallback, chưa phải
lượt gọi Gemini thành công.

Xác nhận model của brief trong DB (endpoint case.model_version là model ML,
không phải model Gemini):

```powershell
docker compose exec postgres psql -U fraud -d fraud -c "SELECT case_id,status,model_name,tokens_used,last_error FROM case_briefs ORDER BY updated_at DESC LIMIT 5;"
```

`model_name` của brief mới cần là `gemini-3.5-flash-lite` và có token usage.
Mở http://127.0.0.1:8000/, đăng nhập `analyst1` / `analyst123`, xem đúng case
vừa gửi. Swagger ở http://127.0.0.1:8000/docs.

## 5. Khi có lỗi và khi dừng

- Không kết nối DB/Redis: kiểm tra 4 container và port ở bước 2; dùng localhost
  trong host Python, không dùng hostname `redis`/`pgbouncer-write` của container.
- Đụng cổng metrics: Scoring dùng 9101, LLM dùng 9102; tắt worker cũ.
- `column generation_id does not exist`: DB cũ cần migration
  `db/migrations/001_brief_generation.sql`; dừng app worker trước khi áp dụng:

```powershell
Get-Content db\migrations\001_brief_generation.sql -Raw | docker compose exec -T postgres psql -U fraud -d fraud
```

- LLM vẫn dùng Ollama: kiểm tra biến shell hoặc profile VS Code đang ghi đè `.env`.
- API HTTP 429/503: xem `last_error` và log LLM; retry/fallback vẫn giữ kết quả ML.
  Giữ simulator dừng và gửi ít giao dịch trước; concurrency=1 không tự giới hạn RPM.

Dừng ba process bằng Ctrl+C ở từng terminal. Nếu muốn dừng cả hạ tầng, giữ dữ liệu:

```powershell
docker compose -f docker-compose.yml -f docker-compose.dev.yml stop redis postgres pgbouncer-write pgbouncer-read
```
