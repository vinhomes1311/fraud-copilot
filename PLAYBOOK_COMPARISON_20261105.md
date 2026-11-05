# So sánh playbook cũ và mới — 2026-11-05

Cùng 20 context giả, 2 lượt/case, 1 warm-up, concurrency 1, Qwen2.5:3b, embeddinggemma:300m, output cap 128 và cùng adapter đã tối ưu. Chỉ thay kho playbook 5 mục bằng 29 mục. Không thay weights hoặc phiên bản Ollama.

| Chỉ số | Playbook cũ | Playbook mới |
|---|---:|---:|
| Brief hợp lệ | 40.00 | 40.00 |
| Lượt đo | 40.00 | 40.00 |
| Khớp nhóm hành động tương đương | 60.0% | 80.0% |
| p50 (giây) | 4.93 | 8.86 |
| p95 (giây) | 10.03 | 11.37 |
| Mean (giây) | 5.61 | 7.29 |
| Warm-up (giây) | 6.87 | 21.28 |

Nhóm hành động quy các rule chi tiết về năm nguyên tắc gốc: bất thường/đối chiếu tín hiệu, giới hạn score, lịch sử, quyết định analyst, thiếu bằng chứng. Đây là chỉ số thô để hai bộ so sánh được; không chứng minh bộ cũ đáp ứng các điều kiện chi tiết của bộ mới.
Strict success mới được báo riêng trong PLAYBOOK_V2_BENCHMARK.md, không so với reference-any của các lần đo cũ.
Hai batch chạy nối tiếp, có warm-up nhưng không ép trạng thái cold-start/GPU giống hệt. Số mẫu nhỏ và prefix cache có thể ảnh hưởng latency. Không quy toàn bộ chênh lệch cho playbook hoặc coi đây là nâng cấp weights Ollama.

Crosswalk sử dụng trước khi chấm:
```json
{
  "PB-01": "PB-01",
  "PB-02": "PB-02",
  "PB-03": "PB-03",
  "PB-04": "PB-04",
  "PB-05": "PB-05",
  "PB-06": "PB-03",
  "PB-07": "PB-03",
  "PB-08": "PB-01",
  "PB-09": "PB-01",
  "PB-10": "PB-01",
  "PB-11": "PB-03",
  "PB-12": "PB-03",
  "PB-13": "PB-03",
  "PB-14": "PB-01",
  "PB-15": "PB-01",
  "PB-16": "PB-03",
  "PB-17": "PB-01",
  "PB-18": "PB-01",
  "PB-19": "PB-01",
  "PB-20": "PB-01",
  "PB-21": "PB-02",
  "PB-22": "PB-02",
  "PB-23": "PB-02",
  "PB-24": "PB-03",
  "PB-25": "PB-01",
  "PB-26": "PB-02",
  "PB-27": "PB-04",
  "PB-28": "PB-05",
  "PB-29": "PB-04"
}
```
