# Tối ưu playbook và retrieval — 2026-11-05

Máy đo GTX 1650 4 GB VRAM, khoảng 16 GB RAM. Ollama 0.40.14, qwen2.5:3b và embeddinggemma:300m; concurrency 1, output cap 128, top 5 rule. Không đổi weights.

| Chỉ số | Playbook 29 mục trước sửa | Context-aware thăm dò | Bản cuối | Case biên mới |
|---|---:|---:|---:|---:|
| Lượt đo | 40 | 40 | 40 | 8 |
| Brief hợp lệ | 100.0% | 100.0% | 100.0% | 100.0% |
| Retrieval đủ nhóm tham chiếu | 70.0% | 90.0% | 95.0% | 100.0% |
| Strict success | 50.0% | 62.5% | 80.0% | 87.5% |
| Lượt chọn rule trái điều kiện | 6 | 0 | 0 | 0 |
| p50 (giây) | 8.86 | 9.52 | 9.39 | 10.05 |
| p95 (giây) | 11.37 | 11.75 | 12.67 | 13.71 |

Ba cột đầu dùng đúng 20 context và đáp án tham chiếu cũ, mỗi case lặp 2 lần. Cột biên dùng 8 case mới, mỗi case 1 lần. Mỗi batch thêm 1 warm-up; warm-up không tính vào latency. Các batch chạy nối tiếp, cache và nhiệt độ GPU có thể ảnh hưởng thời gian.
Lượt thăm dò được đo trước khi thêm guard cuối và bổ sung khoảng trống maximum. Bản cuối đo sau tất cả thay đổi; không trộn kết quả hai phiên bản.

## Thay đổi

- Worker và benchmark dùng cùng retrieval có context. Query chỉ dùng evidence allowlist, mô tả feature và khoảng trống baseline.
- Lọc rule theo điều kiện lịch sử 24h/7d, mean/max/std, rồi kết hợp ưu tiên feature chính với thứ tự semantic.
- Đánh dấu main_explanation/history_gap/supporting trong các chunk để model hiểu vai trò từng đoạn. LLM vẫn phải trả lựa chọn JSON; không thay output bằng đáp án benchmark.
- Guard từ chối rule không áp dụng trước khi lưu READY. Ghi hash policy và adapter trong benchmark.
- Thêm 8 case biên chốt trước inference và regression test về điều kiện, identifier, query, alias và guard. Case biên là test phát triển, chưa phải held-out expert evaluation.

## Kiểm thử và môi trường

**118 passed, 4 skipped, 79 subtests passed** khi chạy toàn bộ pytest, không bỏ qua module nào bằng --ignore.
Các test ML preprocessing đã chạy và pass sau khi cài pandas. Bốn skip còn lại: brief pipeline integration (1), Redis queue (1), workflow PostgreSQL/Redis (2). Chúng cần dịch vụ kiểm thử riêng, không còn bị chặn do pandas.
Có cảnh báo deprecation pandas về dayofweek và cấu hình pytest-asyncio; không có test fail.
```json
{
  "pandas": "3.1.1",
  "numpy": "2.5.4",
  "scikit-learn": "1.9.1",
  "xgboost": "2.0.3",
  "httpx": "0.28.1",
  "pytest": "9.2.0",
  "python": "3.14.2 (tags/v3.14.2:df79316, Dec  5 2025, 17:18:21) [MSC v.1944 64 bit (AMD64)]"
}
```

## Chạy lại

```powershell
python -B -m pytest -p no:cacheprovider -q -rs
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
python -B -m benchmarks.compare_llm --live --providers ollama --scope rag-llm --cases benchmarks/playbook_v2_cases.json --repeats 2 --warmup 1 --completion-cap 128 --max-requests 100 --output-dir benchmark_results/new_final_run
python -B -m benchmarks.playbook_eval benchmark_results/new_final_run/results.json
python -B -m benchmarks.compare_llm --live --providers ollama --scope rag-llm --cases benchmarks/playbook_v2_boundary_cases.json --repeats 1 --warmup 1 --completion-cap 128 --output-dir benchmark_results/new_boundary_run
python -B -m benchmarks.playbook_eval benchmark_results/new_boundary_run/results.json --cases benchmarks/playbook_v2_boundary_cases.json
```

Strict success yêu cầu đủ mọi nhóm tham chiếu và không chọn rule bị loại. Đáp án là đề xuất của nhóm từ điều kiện playbook, không phải accuracy phát hiện gian lận hay chất lượng đã được chuyên gia độc lập duyệt. Mẫu số tính cả lượt fail/skip dự kiến. Không đổi đáp án cũ sau khi xem output.

Chi tiết lượt cuối chưa đạt:

| Case | Lượt | Retrieved | Selected | Nhóm thiếu | Rule bị loại |
|---|---:|---|---|---|---|
| v2-no-calibrated-prob | 1 | ['PB-10', 'PB-11', 'PB-13', 'PB-20', 'PB-21'] | ['PB-11', 'PB-13'] | [['PB-21', 'PB-02']] | [] |
| kaggle-baseline-low | 1 | ['PB-01', 'PB-08', 'PB-10', 'PB-19', 'PB-20'] | ['PB-08', 'PB-10'] | [['PB-26', 'PB-02']] | [] |
| kaggle-baseline-low | 0 | ['PB-01', 'PB-08', 'PB-10', 'PB-19', 'PB-20'] | ['PB-08', 'PB-10'] | [['PB-26', 'PB-02']] | [] |
| v2-valid-category | 0 | ['PB-01', 'PB-10', 'PB-11', 'PB-13', 'PB-20'] | ['PB-10', 'PB-13'] | [['PB-20', 'PB-02']] | [] |
| v2-single-history-std | 1 | ['PB-10', 'PB-11', 'PB-13', 'PB-20', 'PB-28'] | ['PB-11', 'PB-13'] | [['PB-28', 'PB-05', 'PB-03']] | [] |
| v2-no-calibrated-prob | 0 | ['PB-10', 'PB-11', 'PB-13', 'PB-20', 'PB-21'] | ['PB-11', 'PB-13'] | [['PB-21', 'PB-02']] | [] |
| v2-valid-category | 1 | ['PB-01', 'PB-10', 'PB-11', 'PB-13', 'PB-20'] | ['PB-10', 'PB-13'] | [['PB-20', 'PB-02']] | [] |
| v2-single-history-std | 0 | ['PB-10', 'PB-11', 'PB-13', 'PB-20', 'PB-28'] | ['PB-11', 'PB-13'] | [['PB-28', 'PB-05', 'PB-03']] | [] |

Raw reports trong benchmark_results/playbook_v5_final_20261105 và playbook_v5_boundary_20261105; giữ cả lượt thăm dò và baseline để truy vết.
