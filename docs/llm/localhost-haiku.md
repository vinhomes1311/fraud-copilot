# Chạy Haiku v29

Haiku chọn tối đa ba rule khác nhau từ các lựa chọn do ứng dụng giới hạn bằng JSON Schema. Ứng dụng dựng phần giải thích từ dữ liệu của ca, giữ liên kết đến nguồn và lưu `rationale_origin=renderer`. Phần chọn checklist vẫn do Haiku thực hiện.

Trong `.env`, thay các giá trị đang dùng bằng cấu hình dưới đây và điền API key Anthropic.

```dotenv
LLM_PROVIDER=api
LLM_RETRIEVER=playbook
LLM_API_BASE_URL=https://api.anthropic.com/v1
LLM_API_MODEL=claude-haiku-5-5
LLM_API_KEY=<key của bạn>
LLM_API_RESPONSE_FORMAT=json_schema
LLM_API_TOKEN_PARAMETER=max_tokens
LLM_API_STORE=omit
LLM_API_REASONING_EFFORT=
LLM_API_THINKING=disabled
LLM_API_BRIEF_MODE=grounded_selection
LLM_API_MAX_COMPLETION_TOKENS=3072
BRIEF_TOKEN_BUDGET=8000
LLM_CONCURRENCY=1
LLM_MAX_ATTEMPTS=3
LLM_JOB_TIMEOUT_S=120
LLM_VISIBILITY_TIMEOUT_S=300
```

Giữ `LLM_API_BRIEF_MODE=grounded_selection` khi triển khai bằng overlay API chung. Chế độ `explained` vẫn là mặc định của adapter chung và giữ hợp đồng diễn giải cũ. Cấu hình localhost dùng overlay riêng để dashboard chỉ lắng nghe tại `127.0.0.1`.

```powershell
docker compose -f docker-compose.yml -f docker-compose.api.yml -f docker-compose.localhost.yml up -d --build
Invoke-RestMethod http://localhost:8080/healthz
```

Mở [dashboard localhost](http://localhost:8080), chọn một ca demo và tạo lại brief để xem kết quả. Tạo brief gọi API trả phí. Khi đổi `.env`, tạo lại worker để nạp cấu hình mới.

Profile v29 đã tạo 213/213 brief hợp lệ ngay lần đầu trên benchmark tổng hợp với hai luồng đồng thời, với p95 tạo brief 2,81 giây. Hàng đợi thử nghiệm xử lý 30/30 job ở lượt đầu và stack localhost lưu được 3/3 brief ở lượt đầu. Đây là kết quả kiểm tra trước triển khai thử. Tỷ lệ nhận định có căn cứ cần được đánh giá riêng trên nội dung hiển thị.
