# LLM online qua API — không cần train lại

LLM worker gọi endpoint tương thích OpenAI Chat Completions. Model được nhà cung
cấp vận hành; dự án không tải weights, chạy GPU hoặc fine-tune LLM. Playbook và
evidence của case được đưa vào mỗi request, vì vậy có thể cập nhật kiến thức nghiệp
vụ hoặc đổi model bằng cấu hình. Bộ chấm điểm giao dịch là thành phần riêng.

## Chạy bằng Docker

```powershell
Copy-Item .env.example .env
# Điền LLM_API_KEY trong .env bằng editor trên máy của bạn.
docker compose -f docker-compose.yml -f docker-compose.api.yml up --build -d
docker compose logs -f llm-worker
```

Không ghi đè `.env` nếu file đã tồn tại. Cấu hình tối thiểu trong file đó:

```dotenv
LLM_PROVIDER=api
LLM_API_BASE_URL=https://api.openai.com/v1
LLM_API_MODEL=gpt-5.4-mini
LLM_API_KEY=<API key của bạn>
LLM_API_TOKEN_PARAMETER=max_completion_tokens
LLM_API_STORE=false
LLM_RETRIEVER=playbook
LLM_JOB_TIMEOUT_S=120
LLM_VISIBILITY_TIMEOUT_S=300
```

File override `docker-compose.api.yml` bắt buộc có key ngay khi Compose đọc cấu
hình. Với `.env` đã điền đầy đủ, `docker compose up --build -d` cũng chuyển worker
sang API. Không có cấu hình `LLM_PROVIDER`, demo vẫn dùng `stub` để chạy offline.

## Chạy Python / VS Code

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m llm_worker.smoke_test
# Một request dữ liệu giả, có thể phát sinh phí API:
.\.venv\Scripts\python.exe -m llm_worker.smoke_test --live
.\.venv\Scripts\python.exe -m llm_worker.main
```

Python tự đọc `.env` ở thư mục dự án trước khi khởi tạo settings. Biến môi trường
đã đặt trong shell/container được ưu tiên. Khởi động lại worker sau khi đổi cấu
hình. Lệnh smoke mặc định chỉ kiểm tra cấu hình; `--live` mới gửi request. Smoke
không cần PostgreSQL, Redis hay Ollama và không đọc case thật. Kết quả gồm model,
token, thời gian phản hồi và brief có citation.

Queue worker cần DB/Redis và schema theo [hướng dẫn tích hợp local](local-integration.md).
Worker kiểm tra cấu hình LLM trước khi kết nối DB.

## Chọn model và tối ưu

Khi không đặt URL/model trong môi trường, client mặc định dùng OpenAI
`gpt-5.4-mini`. File `.env.example` có sẵn ví dụ Gemini; hướng dẫn giữ cả hai
provider và so sánh nằm trong [benchmark guide](../benchmarks/README.md#giữ-cả-ollama-và-gemini-trong-env).

`gpt-5.4-mini` là điểm bắt đầu cho tác vụ chọn evidence và checklist có
cấu trúc. Model này hỗ trợ Chat Completions và Structured Outputs theo
[OpenAI Docs](https://developers.openai.com/api/docs/models/gpt-5.4-mini).
Đây không phải kết luận về model tốt nhất cho dữ liệu của dự án: cần đo citation,
tỷ lệ thành công, độ trễ và token trên bộ case đại diện trước khi triển khai.

- Đổi `LLM_API_MODEL` để dùng model khác đã được cấp quyền trong tài khoản.
- Đổi `LLM_API_BASE_URL`, model và key để dùng dịch vụ tương thích khác. URL là
  HTTPS base URL, ví dụ có `/v1`, không thêm `/chat/completions`.
- `LLM_API_MAX_COMPLETION_TOKENS=1024` giới hạn output và cả reasoning token của
  model reasoning. Khi cần reasoning sâu hơn, tăng cap và kiểm tra lại token budget.
- `LLM_API_REASONING_EFFORT` để trống sẽ không gửi tham số này. Với `gpt-5.4-mini`,
  có thể dùng `none` hoặc `low` để thử cân bằng độ trễ/chất lượng. Các model khác
  có tập giá trị hỗ trợ riêng; không tự động suy đoán từ tên model.
- Mặc định dùng `json_schema`. Provider chỉ hỗ trợ JSON mode có thể đặt
  `LLM_API_RESPONSE_FORMAT=json_object`; việc kiểm tra ID tại ứng dụng vẫn bắt buộc.
- Nếu provider yêu cầu tham số cũ, đặt `LLM_API_TOKEN_PARAMETER=max_tokens`.
- `LLM_API_STORE=false` (mặc định) gửi `store=false`; `omit` bỏ tham số cho
  endpoint tương thích không hỗ trợ nó. Đây không phải chính sách lưu giữ dữ liệu.
- `LLM_CONCURRENCY=1` là điểm bắt đầu để hạn chế request đồng thời.

`LLM_RETRIEVER=playbook` đưa toàn bộ KB nhỏ hiện có vào context, không gọi
embedding/Ollama. Đây chưa phải semantic retrieval cho KB lớn. Có thể dùng
`LLM_RETRIEVER=ollama` cho semantic retrieval hiện có khi đã cài embedding model.

## Hợp đồng dữ liệu và xử lý lỗi

Client gửi evidence đã lọc theo allowlist và các đoạn playbook được phê duyệt.
API và Ollama dùng chung prompt và hợp đồng `explained-brief-v2`: LLM chọn
1–3 `checklist_ids` duy nhất và sinh `rationales`, mỗi rule có một đoạn giải thích
tiếng Anh khoảng 40–75 từ, kèm `rule_code`, `evidence_ids` và `text`.
Backend dựng số liệu; model giải thích ý nghĩa và liên hệ giữa các tín hiệu của case.
Các so sánh lớn hơn/nhỏ hơn/bằng giữa số tiền và mean/max hợp lệ được backend tính
trước. Không dùng baseline mặc định làm căn cứ so sánh.
Quy tắc không áp dụng được loại khỏi prompt và schema trước inference. Trường hợp
bắt buộc kiểm tra thiếu baseline maximum chỉ cho phép chọn quy tắc data-gap tương ứng.
Ứng dụng xác minh ID, checklist không trùng và render brief từ nguồn đã kiểm tra.
Nội dung hiển thị được dựng bằng tiếng Anh từ số liệu case đã projection:
giờ giao dịch, số giao dịch và mức tiền tham chiếu thực có. Không hiển thị chú
thích thiếu múi giờ, loại tiền, baseline hoặc xác suất; không thay dữ liệu thiếu bằng 0.
Các hướng dẫn như “Ghi rõ”, “Bỏ qua”, “Không tự suy luận” trong playbook chỉ phục
vụ model; không chép `implementation_action` hoặc prompt vào summary/checklist.
Ví dụ PB-19 hiển thị “Transaction hour: 13.”
Phía dưới số liệu là phần giải thích do LLM sinh; mục “Supporting case facts”
cho phép xem các tín hiệu còn lại. Brief cũ không có rationale vẫn đọc được.
Rationale chỉ được tham chiếu fact có thể hiển thị, không dùng gap/prompt làm
bằng chứng. Giá trị số trong prose phải dùng placeholder như `{{EV-AMOUNT}}`;
backend thay bằng giá trị nguồn và từ chối số tự viết, note thiếu dữ liệu, text
quá ngắn và tham chiếu sai. Đây là kiểm tra cấu trúc/nguồn số liệu, không phải
bảo đảm tính đúng đắn ngữ nghĩa của mọi nhận định tự do do model sinh.
Playbook v2.1 là bản dịch tiếng Anh, giữ rule ID và điều kiện áp dụng của v2.0.
Citation giữ quote nguyên văn của nguồn tiếng Anh, tách khỏi phần trình bày.
Kết quả benchmark cũ vẫn thuộc phiên bản nguồn/prompt cũ.
Brief đã lưu trước cập nhật cần được tạo lại để có nội dung mới.
Citation phải có quote không rỗng khớp nguyên văn nguồn được truy xuất; mỗi rule
trong checklist cần citation riêng. Model không tự thay đổi điểm rủi ro hay routing.
Sau khi cập nhật, rebuild/restart `analyst-api` và `llm-worker`. Output chỉ chứa
checklist ID mà thiếu rationale bị từ chối thay vì âm thầm sinh brief ngắn.
Mặc định trần output là 1024 token. Nếu `.env` cũ còn
`OLLAMA_MAX_COMPLETION_TOKENS=128`, đổi thành 1024 khi dùng Ollama.
Các tham số vận hành chung nằm trong `.env.example`: `LLM_MAX_ATTEMPTS`,
`BRIEF_TOKEN_BUDGET`, `REGEN_LIMIT_PER_HOUR`, `BREAKER_FAILURE_THRESHOLD`,
`BREAKER_RESET_S` và timeout/concurrency. Compose truyền cùng cấu hình queue cho
API và worker; hai override chỉ đổi phần riêng của provider. Giới hạn nội dung
và điều kiện playbook là hợp đồng trong code, không phải biến môi trường.
Request mặc định đặt `store=false` (có thể bỏ bằng `LLM_API_STORE=omit`), không theo redirect và không đưa key vào prompt.
Tham số `store` không thay thế chính sách lưu giữ dữ liệu của nhà cung cấp.

Endpoint phải trả `finish_reason=stop`, nội dung JSON và usage nguyên không âm
gồm `prompt_tokens`, `completion_tokens`, `total_tokens`. Refusal, output bị cắt,
ID sai, usage sai hoặc thiếu sẽ làm attempt thất bại. Lỗi HTTP/network được làm
sạch trước khi lưu `last_error`; không lưu body lỗi từ provider hay key.

Client gửi một lần mỗi attempt. Queue giữ retry có giới hạn, circuit breaker,
timeout và brief rule-based khi hết retry. 401/429/5xx vẫn theo retry chung, chưa
lập lịch theo `Retry-After`. Token budget kiểm tra sau inference nên không hoàn
lại phí; usage lưu cho attempt thành công, chưa tổng hợp phí mọi lần retry.

Để chạy local Ollama hoặc giả lập, dùng `LLM_PROVIDER=ollama` hoặc `stub` theo
hướng dẫn tương ứng. Các chế độ này vẫn được giữ lại.

## Kiểm thử

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Kiểm thử API dùng `httpx.MockTransport`: không gọi nhà cung cấp, không cần key
thật, không tính phí. Bao gồm kiểm tra dữ liệu gửi đi, citation, usage, đổi model,
reasoning effort, lỗi HTTP/timeout, `.env`, smoke test và API → worker → READY với
DB giả lập. Kiểm thử tích hợp PostgreSQL/Redis chỉ chạy khi có URL test riêng.

Tham khảo giao thức:
[Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
