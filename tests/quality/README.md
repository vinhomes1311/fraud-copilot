# Quality

Job tương ứng: `quality` trong `.github/workflows/ci.yml`.

Job này không có hàm pytest riêng. GitHub Actions cài dependency và chạy:

```bash
python -m compileall -q .
```

Mục tiêu là phát hiện lỗi cú pháp/compile trong toàn bộ source.
