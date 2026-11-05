# Benchmark playbook v2 bằng Ollama thật

Ngày 2026-11-05. Model qwen2.5:3b, embeddinggemma:300m, GTX 1650 4 GB VRAM, khoảng 16 GB RAM.
20 case giả, mỗi case lặp 2 lần, thêm 1 warm-up. Concurrency 1; output cap 128; semantic retrieval top 5 trong 29 rule.

| Chỉ số | Kết quả |
|---|---:|
| Lượt đo thực hiện / dự kiến | 40 / 40 |
| Brief hợp lệ | 100.0% |
| Retrieval tìm đủ nhóm rule tham chiếu | 70.0% |
| Chọn đủ nhóm rule và không chọn rule bị loại | 50.0% |
| Độ phủ nhóm rule trung bình | 60.0% |
| Lượt chọn rule bị loại | 6 |
| p50 brief hợp lệ | 8.86 giây |
| p95 brief hợp lệ | 11.37 giây |

## Cách chấm

Đáp án tham chiếu được viết và chốt trước inference từ điều kiện của rule và context. Hash case trong report được kiểm tra lại khi chấm; thay đáp án sau chạy sẽ bị từ chối.
Mỗi reference group chứa các rule thay thế chấp nhận được; một case có nhiều group phải chọn ít nhất một rule ở mỗi group. Chọn rule trong forbidden_rules làm strict pass thất bại. Brief lỗi và lượt bị bỏ qua không được loại khỏi mẫu số tỷ lệ thành công.
Đây là đáp án đề xuất của nhóm, chưa có chuyên gia độc lập duyệt. Rule phù hợp nhưng ngoài đáp án có thể bị chấm thiếu; cần xem chi tiết trước khi diễn giải lỗi. Không dùng tỷ lệ này làm accuracy phát hiện fraud.
Điểm reference-any cũ chỉ cần khớp một rule và dùng bộ case khác, nên không so trực tiếp với strict success mới.

## Kiểm thử code

110 passed, 2 skipped, 79 subtests passed. Loại hai module ML/E2E do môi trường thiếu pandas; không khẳng định đã kiểm thử toàn bộ sản phẩm.
Test mới kiểm tra citation tham chiếu tồn tại, nhóm đáp án không mâu thuẫn, thiếu một nhóm không được tính pass, rule bị loại làm fail, hash chống thay đáp án và mẫu số tính cả lượt bị bỏ qua.

## Chạy lại

```powershell
$env:OLLAMA_BASE_URL = 'http://127.0.0.1:11434'
python -B -m benchmarks.compare_llm --live --providers ollama --scope rag-llm --retriever ollama --cases benchmarks/playbook_v2_cases.json --repeats 2 --warmup 1 --completion-cap 128 --max-requests 100 --output-dir benchmark_results/playbook_v2_new_run
python -B -m benchmarks.playbook_eval benchmark_results/playbook_v2_new_run/results.json
```

Scope chỉ gồm retrieval → generation → grounding; không gồm queue/DB/UI. Warm-up không tính vào latency. Kết quả của batch nhỏ trên máy local không cam kết tốc độ máy host khác.

## Chi tiết các lượt chưa đạt tham chiếu

| Case | Lượt | Rule truy xuất | Rule chọn | Nhóm còn thiếu | Rule bị loại |
|---|---:|---|---|---|---|
| ml-current-no-history | 0 | ['PB-01', 'PB-04', 'PB-13', 'PB-14', 'PB-22'] | ['PB-01', 'PB-13'] | [['PB-12', 'PB-06', 'PB-03']] | [] |
| ml-current-7d-only | 0 | ['PB-01', 'PB-04', 'PB-05', 'PB-18', 'PB-25'] | ['PB-01', 'PB-18'] | [['PB-24', 'PB-06']] | [] |
| ml-current-7d-only | 1 | ['PB-01', 'PB-04', 'PB-05', 'PB-18', 'PB-25'] | ['PB-01', 'PB-18'] | [['PB-24', 'PB-06']] | [] |
| v2-max-ratio | 1 | ['PB-01', 'PB-04', 'PB-13', 'PB-14', 'PB-15'] | ['PB-13', 'PB-14'] | [['PB-15']] | [] |
| kaggle-count-24h | 1 | ['PB-01', 'PB-03', 'PB-04', 'PB-06', 'PB-11'] | ['PB-03', 'PB-06'] | [['PB-10']] | ['PB-06'] |
| ml-current-history | 1 | ['PB-01', 'PB-04', 'PB-11', 'PB-13', 'PB-14'] | ['PB-11', 'PB-13'] | [['PB-07', 'PB-24']] | [] |
| v2-valid-category | 0 | ['PB-01', 'PB-02', 'PB-04', 'PB-22', 'PB-26'] | ['PB-01', 'PB-26'] | [['PB-20', 'PB-02']] | [] |
| v2-max-ratio | 0 | ['PB-01', 'PB-04', 'PB-13', 'PB-14', 'PB-15'] | ['PB-13', 'PB-14'] | [['PB-15']] | [] |
| v2-total-amount | 1 | ['PB-01', 'PB-03', 'PB-04', 'PB-06', 'PB-17'] | ['PB-03', 'PB-06'] | [['PB-17']] | [] |
| kaggle-amount-ratio | 1 | ['PB-01', 'PB-03', 'PB-04', 'PB-06', 'PB-22'] | ['PB-01', 'PB-06'] | [] | ['PB-06'] |
| v2-single-history-std | 1 | ['PB-01', 'PB-04', 'PB-11', 'PB-13', 'PB-16'] | ['PB-11', 'PB-16'] | [['PB-28', 'PB-05', 'PB-03']] | ['PB-16'] |
| ml-current-no-history | 1 | ['PB-01', 'PB-04', 'PB-13', 'PB-14', 'PB-22'] | ['PB-01', 'PB-13'] | [['PB-12', 'PB-06', 'PB-03']] | [] |
| kaggle-amount-ratio | 0 | ['PB-01', 'PB-03', 'PB-04', 'PB-06', 'PB-22'] | ['PB-01', 'PB-06'] | [] | ['PB-06'] |
| v2-valid-category | 1 | ['PB-01', 'PB-02', 'PB-04', 'PB-22', 'PB-26'] | ['PB-01', 'PB-26'] | [['PB-20', 'PB-02']] | [] |
| kaggle-unknown-category | 1 | ['PB-01', 'PB-02', 'PB-04', 'PB-22', 'PB-27'] | ['PB-02', 'PB-27'] | [['PB-06', 'PB-28', 'PB-05']] | [] |
| ml-current-history | 0 | ['PB-01', 'PB-04', 'PB-11', 'PB-13', 'PB-14'] | ['PB-11', 'PB-13'] | [['PB-07', 'PB-24']] | [] |
| v2-total-amount | 0 | ['PB-01', 'PB-03', 'PB-04', 'PB-06', 'PB-17'] | ['PB-03', 'PB-06'] | [['PB-17']] | [] |
| v2-single-history-std | 0 | ['PB-01', 'PB-04', 'PB-11', 'PB-13', 'PB-16'] | ['PB-11', 'PB-16'] | [['PB-28', 'PB-05', 'PB-03']] | ['PB-16'] |
| kaggle-count-24h | 0 | ['PB-01', 'PB-03', 'PB-04', 'PB-06', 'PB-11'] | ['PB-03', 'PB-06'] | [['PB-10']] | ['PB-06'] |
| kaggle-unknown-category | 0 | ['PB-01', 'PB-02', 'PB-04', 'PB-22', 'PB-27'] | ['PB-02', 'PB-27'] | [['PB-06', 'PB-28', 'PB-05']] | [] |

Report đầy đủ: `benchmark_results/playbook_v2_20261105/results.json`, `quality.json`, `quality.md`, `samples.csv`.
Đáp án: [PLAYBOOK_V2_REFERENCES.md](benchmarks/PLAYBOOK_V2_REFERENCES.md).
