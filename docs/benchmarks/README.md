# So sánh hiệu năng Ollama local và LLM API

`python -m benchmarks.compare_llm` so sánh hai adapter của dự án trên cùng bộ case
tổng hợp, evidence và playbook được phê duyệt. Cả hai tạo cùng hợp đồng brief và
đi qua kiểm tra ID/citation hiện có. Không cần PostgreSQL, Redis hoặc dashboard.

Đối chiếu với tài liệu kiến trúc của nhóm, chỉ trong phạm vi LLM/RAG:
xem [docs/benchmarks/architecture.md](architecture.md).
`--scope generation` chọn tối đa 5 rule phù hợp cho từng case từ toàn bộ playbook,
rồi giữ context của case đó giống nhau để so sánh hai adapter;
`--scope rag-llm --retriever ollama` đo thêm semantic retrieval trước generation.

Trong `generation`, benchmark dùng chính sách applicability/ranking của worker,
chuẩn bị context một lần cho mỗi case trước warm-up và các lần đo. Không dùng
case ID hoặc nhãn tham chiếu để chọn rule; không gọi embedding. Thời gian chọn
context không nằm trong latency/throughput generation. `results.json` ghi
`retrieval_options.kind=shared-case-playbook`, danh sách rule theo case trong
`retrieval_options.case_rules`, và `retrieved_rules` của từng attempt.
Các báo cáo cũ chỉ dùng chung `PB-01`–`PB-05` có context khác; cần chạy lại để
so hiệu năng với context mới.

Mặc định chỉ in kế hoạch, không gọi model. `--mock` chạy cả hai adapter với HTTP
giả lập. `--live` mới gọi model thật; khi chọn provider `api`, có thể phát sinh phí.
Không tự tải model Ollama, không retry request trong benchmark.

## Chạy nhanh

Từ thư mục gốc project, dùng Python đã cài requirements:

```powershell
# 1. Xem kế hoạch: không cần API key hay Ollama đang chạy.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm

# 2. Chạy thử bộ đo bằng response giả, không tiêu token API thật.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --mock --repeats 2 --warmup 0

# 3. Đo một case với Ollama local thật, không gọi API online.
ollama list
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --live --providers ollama --ollama-model qwen2.5:3b --case-ids history-gap --repeats 1 --warmup 0

# 4. So sánh hai model thật sau khi API đã có credit/quota.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --live --providers ollama api --ollama-model qwen2.5:3b --api-model gpt-5.4-mini --repeats 3 --warmup 1 --concurrency 1 --max-requests 32
```

## Giữ cả Ollama và Gemini trong .env

Một file `.env` giữ được cả hai nhóm; không khai báo `LLM_PROVIDER` hai lần.
`LLM_PROVIDER` chỉ chọn model cho worker của ứng dụng, còn benchmark chọn độc
lập bằng `--providers ollama api`. Không cần đổi provider trước mỗi lần đo.
Giữ các biến hạ tầng và local hiện có, rồi thêm nhóm API:

```dotenv
# Worker đang dùng local; đổi thành api khi muốn worker dùng Gemini.
LLM_PROVIDER=ollama
LLM_RETRIEVER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_EMBED_MODEL=embeddinggemma:300m

# Nhóm API luôn có thể giữ cùng nhóm local ở trên.
LLM_API_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_API_MODEL=gemini-3.5-flash-lite
LLM_API_KEY=<điền key Gemini trực tiếp trên máy>
LLM_API_RESPONSE_FORMAT=json_schema
LLM_API_TOKEN_PARAMETER=max_tokens
LLM_API_STORE=omit
LLM_API_MAX_COMPLETION_TOKENS=1024
LLM_API_REASONING_EFFORT=
```

Tên model là ví dụ; chọn model có quota trong tài khoản của bạn. Endpoint và JSON
schema dựa trên [Gemini OpenAI compatibility](https://aistudio.google.com/docs/openai).
Free tier có giới hạn theo model/project; xem [quota trong AI Studio](https://aistudio.google.com/docs/rate-limits).
`store=omit` chỉ bỏ tham số transport, không thay đổi chính sách dữ liệu Google.
`.env` đã được Git ignore; không đưa key vào lệnh hoặc report.

Chạy từ PowerShell trên máy host. Biến shell dưới đây dùng loopback cho lần chạy
Python này; `.env` vẫn giữ địa chỉ dành cho container:

```powershell
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# Xem kế hoạch nhỏ: 1 request local + 1 Gemini, không warm-up.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --providers ollama api --case-ids history-gap --repeats 1 --warmup 0 --max-requests 2

# Thêm --live để thực sự gọi cả hai, dùng cùng evidence/playbook.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --live --providers ollama api --case-ids history-gap --repeats 1 --warmup 0 --max-requests 2

# Sau khi lần nhỏ thành công: 5 case x 2 lần + 1 warm-up/provider.
# Tổng 22 request, gồm 11 Gemini; interval 15s chỉ là ví dụ, chỉnh theo quota.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --live --providers ollama api --repeats 2 --warmup 1 --concurrency 1 --api-min-interval 15 --max-requests 22
```

`--scope generation` mặc định dùng chung context đã chọn riêng cho từng case, không gọi embedding dù worker
đặt `LLM_RETRIEVER=ollama`. Khi muốn đo thêm RAG, dùng
`--scope rag-llm --retriever ollama` và tăng `--max-requests` để tính cả embedding.
Thời gian giãn cách API không nằm trong latency từng attempt; throughput toàn
batch có tính khoảng chờ đó. Vì vậy dùng p50/p95 để so độ trễ và ghi rõ interval
khi so thông lượng. Interval không đảm bảo tránh hết TPM/RPD/429; benchmark vẫn
dừng provider khi gặp 429. Dữ liệu mặc định là case giả.

Khi chạy ứng dụng bằng Docker, chọn một override: `docker-compose.ollama.yml`
cho local hoặc `docker-compose.api.yml` cho Gemini. Override ép `LLM_PROVIDER`
tương ứng, nên không ghép cả hai override cho cùng worker. Với API, đặt
`LLM_RETRIEVER=playbook` nếu muốn chạy không cần embedding local. Khởi động lại
worker sau khi đổi cấu hình.

Với 5 case mặc định, 3 lần lặp và 1 warm-up/provider, kế hoạch gồm **32 request**:
15 lần đo + 1 warm-up cho mỗi provider, trong đó 16 request API khi chạy live.
Warm-up API cũng có thể tính phí. `--max-requests` kiểm tra tổng số request trước
khi bắt đầu, không phải trần tiền hay tổng token. Request thất bại hoặc timeout
vẫn có thể đã được nhà cung cấp xử lý.

Với `--scope rag-llm --retriever ollama`, mặc định có thêm 32 request embedding
local, tổng 64 request. `--max-requests` tính cả embedding và generation.

API đọc `LLM_API_KEY`, `LLM_API_BASE_URL`, response format, token parameter và
reasoning effort từ môi trường hoặc `.env`. Đối số `--api-model` ghi đè model
cho benchmark. Ollama đọc `OLLAMA_BASE_URL` (mặc định `http://127.0.0.1:11434`)
và `OLLAMA_MODEL`; đối số `--ollama-model` được ưu tiên. Benchmark Ollama giới hạn
endpoint local/host Docker, không nhận credentials/query trong URL.

Chế độ `--mock` thay transport bằng `httpx.MockTransport` và dùng key tổng hợp.
Nó chứng minh bộ đo/report hoạt động, **không đánh giá tốc độ của model thật**.

## Chỉ số và báo cáo

Mỗi lần chạy tạo một thư mục mới dưới `benchmark_results/`, được Git/Docker ignore:

| File | Nội dung |
|---|---|
| `summary.md` | Bảng success rate, p50/p95 và số brief hợp lệ/giây |
| `results.json` | Từng request, lỗi, usage, cấu hình, model, case/playbook/prompt hash và thông tin CPU/Python |
| `samples.csv` | Dữ liệu từng lần đo/warm-up để phân tích bằng Excel/Python |

Có thể đặt `--output-dir benchmark_results/ten-lan-do`; thư mục đích phải chưa tồn
tại. Dữ liệu report không chứa API key, URL provider, request payload, raw response
hoặc nội dung exception từ provider. Case ID và checklist rule ID được ghi lại.

| Chỉ số | Cách tính |
|---|---|
| `latency_success_s` | Mean/p50/p95/min/max của các brief hợp lệ, không tính warm-up |
| `latency_all_attempts_s` | Độ trễ mọi lần đo, kể cả lỗi, để tránh che giấu request thất bại |
| `valid_briefs_per_s` | Số brief hợp lệ / thời gian cả batch đo, có tính ảnh hưởng concurrency |
| `reported_output_tokens_per_wall_s` | Tổng output token của brief hợp lệ / thời gian batch |
| `known_usage_all_phases` | Input/output/total token đã nhận được, gồm cả warm-up và lỗi có usage |
| `attempts_with_unknown_usage` | Số attempt không thu được usage, ví dụ timeout/HTTP error |
| `native_seconds` | Thời gian load/prompt eval/generation của Ollama, đổi từ nanosecond sang giây |
| `ollama_eval_tokens_per_s` | Output token / eval_duration của Ollama; không phải tốc độ toàn request |
| `reference_match_rate` | Tỷ lệ brief hợp lệ chọn ít nhất một rule tham chiếu của fixture |
| `retrieval_latency_s` / `generation_latency_s` | Thống kê từng stage; retrieval chỉ đo trong `rag-llm` |
| `failure_stage` | Phân biệt lỗi retrieval, generation, validation và token budget |

Các rule tham chiếu là heuristic để xem xét lựa chọn checklist, không phải nhãn
gian lận hoặc thước đo đầy đủ về chất lượng điều tra. Brief chỉ được tính thành
công sau kiểm tra selection, citation và `BRIEF_TOKEN_BUDGET` hiện có.
Các reference trong `cases.json` là heuristic cũ cho smoke benchmark; rule chi
tiết phù hợp hơn có thể không nằm trong tập reference đó. Dùng bộ case v2 và
evaluator bên dưới khi muốn đánh giá độ phủ các rule chi tiết.

Một provider gặp lỗi 4xx/quota/rate limit sẽ dừng các job chưa bắt đầu; provider
còn lại tiếp tục. Khi concurrency > 1, request đang gửi có thể vẫn hoàn tất.
`skipped_measured`, `stopped_after`, tỷ lệ thành công và usage chưa biết vẫn được
ghi vào report. Không tự retry để tránh trộn chi phí/độ trễ retry vào một lần đo.

## So sánh công bằng và giới hạn

- API và Ollama dùng chung prompt và hợp đồng `explained-brief-v2`: model chọn 1–3 checklist ID
  và viết đoạn giải thích ngắn cho từng rule, kèm evidence ID. Backend dựng số liệu
  và kiểm tra nguồn số trong prose; các nhận định ngữ nghĩa vẫn cần analyst đánh giá.
  Checklist lạ, trùng, rỗng, quá 3 mục hoặc field ngoài hợp đồng vẫn bị từ chối.
  Đây là cải thiện prompt/adapter, không phải train lại hoặc đổi trọng số model.
  `provider_options` ghi rõ hợp đồng và nguồn evidence; report cũng lưu hash prompt
  chung và hash validator cho phần giải thích. Kết quả cũ thuộc hợp đồng chỉ chọn ID
  không so sánh trực tiếp với tác vụ sinh giải thích dài hơn. Không dùng latency
  hoặc số output token để kết luận về chất lượng lựa chọn;
  cần kiểm tra rule tham chiếu trên bộ case đại diện.
- `--completion-cap` mặc định 1024 được áp dụng cho cả hai provider. Ollama worker
  cũng mặc định 1024, cấu hình bằng `OLLAMA_MAX_COMPLETION_TOKENS`;
  benchmark truyền cap riêng qua constructor. Cap cũ 128 có thể cắt mất phần giải thích.
- Dùng chung context theo từng case và seed để xáo thứ tự case giống nhau. Hai provider chạy từng
  batch nối tiếp để tránh tranh tài nguyên. Thử đảo `--providers api ollama` và lặp
  nhiều lần khi cần giảm ảnh hưởng thứ tự, cache và nhiệt độ máy.
- Warm-up không tính vào độ trễ/thông lượng chính, nhưng có trong usage/ước tính
  phí. Warm-up không đảm bảo model local đã được unload trước đó; benchmark không
  ép cold start hoặc tắt cache của nhà cung cấp.
- Đây là phép đo adapter đang dùng trong dự án: prompt chung nhưng
  cơ chế constrained JSON và tùy chọn temperature/reasoning khác nhau. Effective
  options được lưu trong `provider_options`; không coi đây là so sánh hai engine
  với mọi tham số nội bộ giống hệt nhau.
- Tokenizer và reasoning giữa model có thể khác. Output token API có thể gồm token
  không hiển thị, nên token/giây không đồng nghĩa với số từ/giây.
- Benchmark đo projection, gọi model, parse/validate và render brief. Không gồm
  DB, Redis, retry/circuit breaker, Nginx, streaming TTFT, độ chính xác scoring ML
  hay tải toàn workflow. Semantic embeddings được đo khi dùng `--scope rag-llm`
  với retriever `ollama`. Mốc p95 <= 500 ms trong kiến trúc là Hot Path, không phải LLM.
- Report không đo VRAM/GPU utilization, điện năng hoặc giá phần cứng local.
  Ghi chú GPU/CPU, quantization và tải nền khi dùng báo cáo để lựa chọn triển khai.
- Nhiều mẫu mới giúp p95 có ý nghĩa; bộ mặc định phù hợp smoke benchmark hơn là
  kết luận thống kê về model nào tốt nhất.

## Ước tính phí API (tùy chọn)

Truyền cả `--api-input-price-per-million` và `--api-output-price-per-million` theo
giá model/service tier bạn dùng. Không hard-code giá vì giá và model có thể đổi.
`estimated_api_usd_known_usage_uncached` dùng giá input không cache cho usage đã
biết, gồm warm-up và output reasoning. Không áp dụng chiết khấu cache, không tính
request có usage chưa biết, thuế hoặc phí khác; vì vậy không phải hóa đơn hoặc
trần chi phí. Xem billing của nhà cung cấp để biết phí thực tế.

## Kiểm thử bộ benchmark

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit_integration/test_llm_benchmark.py -q
```

Các bài này chỉ dùng mock: kiểm tra kế hoạch không gọi mạng, cùng input/cap giữa
hai adapter, percentile/throughput/usage, bounded concurrency, quota fail-fast,
timeout, output chưa tồn tại và việc không ghi credentials vào report.

## Đánh giá lựa chọn playbook

Các case tham chiếu và rationale được giữ trong
[`benchmarks/fixtures/playbook_v2_cases.json`](../../benchmarks/fixtures/playbook_v2_cases.json),
[`benchmarks/fixtures/playbook_v2_boundary_cases.json`](../../benchmarks/fixtures/playbook_v2_boundary_cases.json)
và [`docs/benchmarks/playbook-v2-references.md`](playbook-v2-references.md).
Đây là fixture phát triển tổng hợp, không phải bộ kiểm định độc lập.

Sau khi có `results.json` từ benchmark, chấm độ phủ nhóm rule, forbidden rules
và brief hợp lệ bằng:

```powershell
python -m benchmarks.playbook_eval benchmark_results\ten-lan-do\results.json
```

Evaluator ghi `quality.json` và `quality.md` cạnh report đầu vào. Toàn bộ output
benchmark nằm trong `benchmark_results/` và được Git ignore; giữ lại script,
fixtures và rationale để có thể tái chạy mà không commit report theo từng máy.

Tài liệu giao thức: [Ollama usage](https://docs.ollama.com/api/usage),
[Ollama chat](https://docs.ollama.com/api/chat),
[OpenAI token counting](https://developers.openai.com/api/docs/guides/token-counting).

## Case mới cố định trước inference và kiểm tra vận hành

`fixtures/playbook_holdout_20261118.json` gồm 36 context tổng hợp mới, tách khỏi
v2 và các case boundary đã dùng để chỉnh code. Các nhóm rule/forbidden/rationale
được viết từ playbook trước khi gọi model; file manifest cố định SHA-256 của
fixture, playbook và retrieval policy. Không đổi prompt/policy/đáp án theo output
holdout. Đây là case chưa chạy trước đó, **chưa phải dữ liệu thực hoặc bộ nhãn
được analyst độc lập thẩm định**. Generator `build_validation_holdout.py` ghi
lại cách xây dựng; mặc định từ chối ghi đè fixture đã tồn tại.

```powershell
# 36 case, 1 lần/case/model + 1 warm-up: 74 generation request.
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --live --providers api ollama --cases benchmarks/fixtures/playbook_holdout_20261118.json --repeats 1 --warmup 1 --concurrency 1 --api-min-interval 15 --max-requests 74
# Chấm đúng fixture đã chạy; không dùng mặc định v2 cho report holdout.
.\.venv\Scripts\python.exe -m benchmarks.playbook_eval benchmark_results\TEN_LAN_DO\results.json --cases benchmarks/fixtures/playbook_holdout_20261118.json
```

Benchmark không retry: lỗi HTTP thực tế vẫn tính vào denominator và báo cáo.
Worker dùng retry/circuit breaker/fallback nên phải kiểm tra luồng riêng:

```powershell
docker compose -f docker-compose.validation.yml up -d --wait
$env:FRAUD_TEST_DB_URL = 'postgresql://validation:validation-local-only@127.0.0.1:15432/validation'
$env:FRAUD_TEST_REDIS_URL = 'redis://127.0.0.1:16379/0'
# Mặc định chỉ in kế hoạch. --live mới gọi model và tạo schema/queue tạm.
.\.venv\Scripts\python.exe -m benchmarks.operational_validation
.\.venv\Scripts\python.exe -m benchmarks.operational_validation --live --api-min-interval 15
docker compose -f docker-compose.validation.yml down
```

Hai service validation là instance riêng, dùng tmpfs cho PostgreSQL và Redis
không persistence. Runner từ chối URL ngoài cổng localhost 15432/16379, tạo
schema/queue duy nhất và dọn chúng sau chạy. Dữ liệu ML được so sánh trước/sau.
Kiểm tra thực tế dùng production enqueue, Redis queue/consumer, BriefProcessor,
retrieval, live API/Ollama, grounding và PostgreSQL; gồm priority, concurrency=2,
semantic embedding thật, duplicate delivery và generation fencing. Các tình
huống 429/timeout/mất kết nối/breaker/lease được **tiêm lỗi có kiểm soát**, ghi
rõ trong `operations.json`, không giả là outage đã quan sát từ provider.

Khoảng nghỉ API của runner là cấu hình kiểm thử; worker production hiện không
có bộ điều tiết RPM/TPM hoặc Retry-After riêng. Kết quả chưa xác nhận tải dài hạn,
HA, replica lag, production traffic, giá API thực tế hoặc SLA vận hành.
