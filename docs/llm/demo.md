# LLM local cho demo

Phạm vi: Ollama, semantic retrieval, tạo brief, grounding và cấu hình LLM worker.
Máy host ứng dụng vẫn chịu trách nhiệm Redis/PostgreSQL, artifact ML và frontend.

## Cấu hình bàn giao

Máy host cần Ollama và hai model đã tải trước:

```powershell
ollama pull qwen2.5:3b
ollama pull embeddinggemma:300m
```

Trong `.env` của ứng dụng, thay các giá trị LLM hiện có bằng:

```dotenv
LLM_PROVIDER=ollama
LLM_RETRIEVER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_EMBED_MODEL=embeddinggemma:300m
OLLAMA_MAX_COMPLETION_TOKENS=128
OLLAMA_KEEP_ALIVE=10m
OLLAMA_EMBED_KEEP_ALIVE=0
OLLAMA_EMBED_CACHE_TTL_S=300
LLM_CONCURRENCY=1
LLM_JOB_TIMEOUT_S=120
LLM_VISIBILITY_TIMEOUT_S=300
```

`host.docker.internal` dùng khi Ollama chạy trên host của Docker. Nếu Ollama ở máy
khác, dùng IP LAN máy đó. Khi chạy worker bằng Python trực tiếp trên cùng máy
Ollama, dùng `http://127.0.0.1:11434`.

Ollama cần lắng nghe trên interface mà container/máy ứng dụng truy cập được.
Trên Windows, thoát Ollama ở khay hệ thống rồi chạy trong một PowerShell riêng:

```powershell
$env:OLLAMA_HOST = '0.0.0.0:11434'
$env:OLLAMA_NUM_PARALLEL = '1'
ollama serve
```

Giữ cửa sổ đó mở. Chỉ cho phép truy cập cổng 11434 từ mạng/máy demo cần thiết.
Khởi động stack ở thư mục repo:

```powershell
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up --build -d
docker compose -f docker-compose.yml -f docker-compose.ollama.yml logs -f llm-worker
```

## Các tối ưu và giới hạn

- Worker cache vector của 29 mục playbook trong RAM 300 giây; mỗi case vẫn được
  embed query và xếp hạng cosine thật. Sau lần đầu, một request embed chỉ chứa
  query thay vì cả playbook. Không cache case hoặc kết quả sinh brief.
- Cache tự hết hạn, đổi khi endpoint/model/nội dung đổi; lock tránh nhiều job cùng
  xây lại. Đổi weights dưới cùng model tag thì restart worker hoặc chờ TTL hết.
  `OLLAMA_EMBED_CACHE_TTL_S=0` tắt cache để đo đối chứng.
- Model trả JSON chỉ gồm 1–3 checklist ID. Trần output mặc định 128; caller benchmark
  có thể ghi đè bằng `--completion-cap`. Truncated output vẫn bị từ chối.
- Request generation giữ model 10 phút. Embedding mặc định giải phóng model sau
  request để nhường VRAM trên máy nhỏ. Máy nhiều VRAM có thể thử
  `OLLAMA_EMBED_KEEP_ALIVE=5m` rồi đo lại; không mặc định rằng luôn nhanh hơn.
- Cùng một model trả lời có thể hưởng prompt/KV cache của Ollama. So sánh một lượt
  trước/sau không tách riêng tác động cache vector, model warm-up và prefix cache.
- LLM hiểu các tên ML hiện tại `trans_count_24h`, `transaction_hour`,
  `customer_merchant_distance_km`; vẫn nhận tên cũ. Thống kê 7 ngày không được
  biến thành thống kê 24 giờ. Tỷ lệ 7 ngày được mô tả đúng công thức mẫu số + 1.
- Các baseline mặc định 0 và ratio tương ứng không được dùng để bịa lịch sử.
  Giá trị std 7 ngày chỉ được dùng khi có ít nhất hai giao dịch trước đó.
- Evidence/citation vẫn được validate; LLM không thay score/risk và quyết định cuối
  cùng thuộc analyst. Đây là tối ưu inference/RAG, không phải fine-tune weights.

## Đo trên máy host trước buổi demo

Từ PowerShell trên máy có repo, Python requirements và Ollama:

```powershell
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
python -B -m benchmarks.compare_llm --live --providers ollama --scope rag-llm --retriever ollama --cases benchmarks/fixtures/kaggle_cases.json --repeats 3 --warmup 1 --completion-cap 128
python -B -m benchmarks.compare_llm --live --providers ollama --scope rag-llm --retriever ollama --cases benchmarks/fixtures/ml_contract_cases.json --repeats 2 --warmup 1 --completion-cap 128
```

Hai lệnh chỉ gọi Ollama với dữ liệu giả, không cần API key, database hay Redis.
Benchmark cố ý chỉ cho endpoint local; đo ở chính máy host Ollama.
Report nằm trong `benchmark_results/`, gồm valid briefs, p50/p95, token usage,
retrieval/generation timing và tỷ lệ khớp quy tắc tham chiếu. Reference match là
heuristic trên fixture, không phải độ chính xác phát hiện gian lận.

Sau đó host ứng dụng phải kiểm tra thêm một case MEDIUM/HIGH qua queue thật:
`READY`, `model_name=qwen2.5:3b`, có brief và citations. Brief fallback không chứng
minh LLM thành công. Benchmark adapter không kiểm tra queue, DB hay dashboard.

Nếu timeout, xem log và đo lại trước khi tăng concurrency. Nếu đổi timeout, giữ
`LLM_VISIBILITY_TIMEOUT_S > 2 * LLM_JOB_TIMEOUT_S + 10`.

Nguồn tham số Ollama: https://docs.ollama.com/api/chat,
https://docs.ollama.com/api/embed và https://docs.ollama.com/faq.

## Cập nhật retrieval

Xem [hướng dẫn benchmark](../benchmarks/README.md) và [điều kiện áp dụng rule](../playbooks/applicability.md). Query dùng context đã projection; các rule không đủ baseline được loại trước top 5. Model vẫn chọn 1–3 checklist, guard từ chối rule trái điều kiện.
