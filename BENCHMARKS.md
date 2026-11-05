# So sánh hiệu năng Ollama local và LLM API

`python -m benchmarks.compare_llm` so sánh hai adapter của dự án trên cùng bộ case
tổng hợp, evidence và playbook được phê duyệt. Cả hai tạo cùng hợp đồng brief và
đi qua kiểm tra ID/citation hiện có. Không cần PostgreSQL, Redis hoặc dashboard.

Đối chiếu với tài liệu kiến trúc của nhóm, chỉ trong phạm vi LLM/RAG:
xem [LLM_BENCHMARK_ARCHITECTURE.md](LLM_BENCHMARK_ARCHITECTURE.md).
`--scope generation` giữ context chung để so sánh hai adapter;
`--scope rag-llm --retriever ollama` đo thêm semantic retrieval trước generation.

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

Một provider gặp lỗi 4xx/quota/rate limit sẽ dừng các job chưa bắt đầu; provider
còn lại tiếp tục. Khi concurrency > 1, request đang gửi có thể vẫn hoàn tất.
`skipped_measured`, `stopped_after`, tỷ lệ thành công và usage chưa biết vẫn được
ghi vào report. Không tự retry để tránh trộn chi phí/độ trễ retry vào một lần đo.

## So sánh công bằng và giới hạn

- Ollama dùng hợp đồng `checklist-only-v1`: model chỉ chọn 1–3 checklist ID;
  backend đưa toàn bộ evidence đã kiểm duyệt vào brief theo thứ tự cố định.
  Checklist lạ, trùng, rỗng, quá 3 mục hoặc field ngoài hợp đồng vẫn bị từ chối.
  Đây là cải thiện prompt/adapter cho `qwen2.5:3b`, không phải train lại hoặc đổi
  trọng số model. API giữ hợp đồng `evidence-and-checklist-v1` hiện có.
  `provider_options` ghi rõ hợp đồng và nguồn evidence; report cũng lưu hash prompt
  Ollama. Vì nhiệm vụ đầu ra khác nhau, không dùng kết quả để khẳng định model
  nào có năng lực tốt hơn chỉ từ latency hoặc số output token.
- `--completion-cap` mặc định 1024 được áp dụng cho cả hai provider. Ollama worker
  thường mặc định 128, cấu hình bằng `OLLAMA_MAX_COMPLETION_TOKENS`;
  benchmark truyền cap riêng qua constructor. Để đo đúng cấu hình demo, thêm
  `--completion-cap 128`.
- Dùng chung playbook và seed để xáo thứ tự case giống nhau. Hai provider chạy từng
  batch nối tiếp để tránh tranh tài nguyên. Thử đảo `--providers api ollama` và lặp
  nhiều lần khi cần giảm ảnh hưởng thứ tự, cache và nhiệt độ máy.
- Warm-up không tính vào độ trễ/thông lượng chính, nhưng có trong usage/ước tính
  phí. Warm-up không đảm bảo model local đã được unload trước đó; benchmark không
  ép cold start hoặc tắt cache của nhà cung cấp.
- Đây là phép đo adapter đang dùng trong dự án: prompt có reminder riêng của API,
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
[`benchmarks/playbook_v2_cases.json`](benchmarks/playbook_v2_cases.json),
[`benchmarks/playbook_v2_boundary_cases.json`](benchmarks/playbook_v2_boundary_cases.json)
và [`benchmarks/PLAYBOOK_V2_REFERENCES.md`](benchmarks/PLAYBOOK_V2_REFERENCES.md).
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
