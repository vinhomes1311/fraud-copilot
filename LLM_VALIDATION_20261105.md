# Kết quả kiểm tra LLM — 2026-11-05

Máy đo: Windows, GTX 1650 4 GB VRAM, khoảng 16 GB RAM; Ollama 0.40.14,
generation `qwen2.5:3b`, embedding `embeddinggemma:300m`, concurrency 1.
Chỉ dùng dữ liệu giả. Không gọi API online. Scope: retrieval → generation →
grounding/validation; không gồm Redis queue, SQL persistence hoặc UI.

| Lượt đo | Số mẫu sau warm-up | Brief hợp lệ | p50 | p95 | Khớp rule tham chiếu |
|---|---:|---:|---:|---:|---:|
| Baseline trước chỉnh, Kaggle fixtures, cap 512 | 8 | 8/8 | 10,08 s | 15,44 s | 87,5% |
| Cache + cap 128, lượt thăm dò trước bổ sung ML aliases | 8 | 8/8 | 1,17 s | 3,23 s | 87,5% |
| Bản cuối, Kaggle fixtures, lặp 2 lần, cap 128 | 16 | 16/16 | 2,86 s | 6,67 s | 87,5% |
| Bản cuối, feature ML hiện tại, lặp 2 lần, cap 128 | 6 | 6/6 | 2,83 s | 11,30 s | 66,7% |

Mỗi lượt có thêm một warm-up, loại khỏi bảng latency. Đây là các batch nhỏ,
chạy nối tiếp trên cùng máy, không phải thí nghiệm A/B có kiểm soát cold start.
Model warm-up/prefix cache, thứ tự và trạng thái GPU ảnh hưởng kết quả. Không
quy toàn bộ chênh lệch latency cho document cache, không cam kết latency máy khác.
Con số 1,17 giây là lượt thăm dò; bản cuối có p50 2,86 giây trên bộ Kaggle.

"Hợp lệ" nghĩa là vượt parse/schema, citation/grounding, completeness và budget
kiểm tra trong adapter benchmark. Không đồng nghĩa checklist tối ưu về nghiệp vụ.
Reference match chỉ kiểm tra có ít nhất một rule thuộc danh sách gợi ý của fixture;
không phải accuracy phát hiện fraud. Các lượt chưa khớp rule cần analyst xem lại.

Kiểm thử offline trên bản cuối: **104 passed, 2 skipped, 79 subtests passed**:

```powershell
python -B -m pytest -p no:cacheprovider -q --ignore=tests/test_ml_pipeline.py --ignore=tests/test_workflow_e2e.py
```

Hai module trên được loại khỏi lượt chạy vì `.venv` hiện tại thiếu dependency ML
(`pandas`). Các integration test yêu cầu DB/Redis riêng không được tính là đã chạy
end-to-end. Phạm vi nhóm cần xác nhận tiếp: queue thật → READY → lưu brief → UI.

Report JSON/CSV đầy đủ được lưu local ở `benchmark_results/tuning_20261105/`,
không commit vào Git. Xem [LLM_DEMO.md](LLM_DEMO.md) để chạy lại trên máy host.
