# Quality

Job tương ứng: `quality` trong `.github/workflows/ci.yml`.

Job này không có hàm pytest riêng. GitHub Actions cài dependency và chạy:

```bash
python -m compileall -q .
```

Job cũng dùng Node.js để kiểm tra JavaScript và chạy test render dashboard:

```bash
node --check static/app.js
node --check static/risk-review.js
node --test tests/ui/risk-review.test.cjs
```

Mục tiêu là phát hiện lỗi cú pháp, dữ liệu hiển thị sai, mất thông tin gốc và
nội dung chưa được escape trong panel review. Không gọi LLM hoặc dịch vụ ngoài.
