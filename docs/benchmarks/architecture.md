# Benchmark LLM và RAG theo tài liệu kiến trúc

Nguồn: [Tài liệu Đặc tả Kiến trúc Hệ thống của nhóm](https://docs.google.com/document/d/1-2mwhZHakWOM58tmmEZ3Djpv_ZTLCV_F8KpChOtvgYg/edit),
đọc ngày 2026-11-04. URL, ngày đọc và SHA-256 của bản text export được lưu trong
`benchmarks/config/llm_architecture.json` và mỗi report để truy vết phiên bản tham chiếu.

Phạm vi chỉ là LLM/RAG trong Cold Path. Không dùng mốc Hot Path p95 <= 500 ms để
chấm LLM. Tài liệu chưa đặt SLA latency bằng số cho LLM; report ghi p50/p95,
timeout và throughput nhưng không tự tuyên bố đạt SLA production.

## Hai phép đo

| Scope | Luồng được đo | Mục đích |
|---|---|---|
| `generation` | Evidence + cùng playbook -> LLM adapter -> kiểm tra/render brief | So sánh Ollama/API với context chung; không đo retrieval |
| `rag-llm`, retriever `ollama` | Semantic retrieval -> LLM adapter -> grounding/budget | Đo phần xử lý LLM/RAG theo Cold Path |
| `rag-llm`, retriever `playbook` | Đọc playbook -> LLM adapter -> grounding/budget | Đo phương án KB nhỏ; không xác nhận semantic retrieval |

Hai provider dùng chung lựa chọn retriever. Embedding có thể chạy local dù
generation dùng API. Benchmark không thay đổi cấu hình worker thật. `--timeout`
giới hạn riêng từng stage RAG và generation, tương tự worker; tổng thời gian có
thể gần hai lần timeout nếu cả hai stage đều chậm.

## Đối chiếu yêu cầu phần LLM

| Yêu cầu kiến trúc | Kiểm tra / bằng chứng | Giới hạn |
|---|---|---|
| FR-05: brief/checklist có citation | Selection ID được kiểm tra; renderer dùng nguồn phê duyệt; grounding check | Checklist cần analyst đánh giá mức phù hợp |
| RAG truy xuất ngữ nghĩa | `--scope rag-llm --retriever ollama`; đo `retrieval_s` | Vector KB cache RAM theo endpoint/model/text với TTL; mỗi query vẫn được embed; chưa xác minh persistent vector index |
| Mỗi brief tối đa 5.000 token | Budget là min(cấu hình worker, 5000); ghi lỗi `token_budget` | Theo cách dự án đếm input + output; embedding usage được tách khỏi budget brief |
| FR-09: tối thiểu hóa định danh | Projection allowlist và test privacy của API adapter | Không xem mọi trường `payload_masked` là an toàn để gửi đi |
| Timeout và lỗi từng bước | `failure_stage`, timeout retrieval/generation riêng, success rate và usage | Benchmark không retry để giữ phép đo minh bạch |
| FR-07: fallback và giữ nguyên scoring | `test_brief_pipeline_integration.py`, `test_brief_processor.py` | Cần PostgreSQL/Redis test để chứng minh persistence thật; không nằm trong timing report |
| Job ưu tiên, retry, generation cũ | Các bài queue/brief integration có sẵn | Bộ đo adapter không tự chứng minh toàn bộ kiến trúc queue |

Report đánh dấu `architecture_coverage.evaluation` là **partial Cold Path only**,
không đồng nghĩa toàn tài liệu đã được nghiệm thu. Transaction TPS, người dùng
đồng thời, ML, UI và Hot Path nằm ngoài phạm vi phần việc này.

Một điểm cần thống nhất trong hợp đồng: tài liệu ghi FAILED_TIMEOUT khi LLM lỗi
hoặc timeout; worker hiện dùng FAILED_TIMEOUT cho timeout/circuit-open và FAILED
cho lỗi khác. Benchmark giữ nguyên code worker và ghi nhận khác biệt này, không
gộp hai trạng thái rồi báo đạt yêu cầu.

## Chạy không tiêu token API

```powershell
# Xem kế hoạch, không gọi model.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --scope rag-llm

# Kiểm thử cả retrieval/generation bằng response giả.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --mock --scope rag-llm --repeats 1 --warmup 0

# Một case thật chỉ dùng model local đã cài.
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --live --scope rag-llm --providers ollama --ollama-model qwen2.5:3b --case-ids history-gap --repeats 1 --warmup 0 --max-requests 2
```

Đo API thật bằng `--live --scope rag-llm --providers ollama api` cần API có quota
và có thể tính phí generation. Mặc định có 32 generation request và 32 embedding
request local; trong đó 16 generation request gửi API. Warm-up có trong tổng
call/usage. `--max-requests` tính cả embedding; không phải trần tiền.

## Đọc kết quả

Mỗi sample có `retrieval_s`, `generation_s`, `validation_s`, `latency_s`,
`failure_stage`, `within_brief_budget` và các bộ đếm token. `generation_s` gồm
adapter parse/validate/render; không đồng nghĩa thời gian suy luận thuần của model.
JSON summary có thống kê retrieval/generation riêng và độ trễ toàn phần của scope.
`retrieval_options` ghi model embedding và top_k để so sánh các lần chạy cùng cấu hình.

Nếu retrieval lỗi thì generation không được gọi. Report không coi lượt đó là
generation có usage chưa biết. Nếu generation timeout thì usage có thể chưa được
thu thập và không thể suy ra phí thực tế bằng số 0.

Chỉ số `--mock` kiểm tra code. Để lựa chọn model deploy, cần kết quả `--live`,
tách warm-up, đủ mẫu và analyst đánh giá checklist trên case đại diện.
