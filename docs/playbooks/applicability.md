# Điều kiện áp dụng playbook

Playbook vẫn gồm 29 rule. Worker và benchmark cùng gọi retrieve_context để xét
context thực tế. Truy vấn embedding chỉ gồm mô tả feature allowlist và evidence
đã projection, không gồm identifier, nhãn fraud hoặc đáp án benchmark.

Trước khi giới hạn top 5, lọc rule theo điều kiện:

- PB-06 chỉ khi không có count 24h dương; PB-07 khi count dương nhưng thiếu mean 24h.
- PB-12 khi trường count 7d được cung cấp và không dương.
- PB-08/PB-14/PB-15 chỉ khi baseline tương ứng hợp lệ; không đổi mean thành maximum.
- PB-16 chỉ khi count 7d ít nhất 2 và std được cung cấp.
- PB-18/PB-19 nhận tên feature cũ và alias ML mới.
- PB-24 diễn giải riêng các cửa sổ; PB-28 được ưu tiên nếu explanation dựa vào
  baseline không hợp lệ. Không có maximum hợp lệ thì đưa khoảng trống vào evidence.
- Rule cụ thể cho explanation hợp lệ được ưu tiên, tiếp theo là khoảng trống
  baseline, cuối cùng là ngữ cảnh bổ trợ và nguyên tắc chung. Semantic similarity
  xếp thứ tự trong cùng mức ưu tiên; đây là retrieval kết hợp điều kiện và semantic.

Các điều kiện nằm trong llm_worker/retrieval_policy.py, không đọc fixture hoặc
case ID. LLM vẫn tự chọn checklist; renderer từ chối citation trái điều kiện trước
khi lưu READY. Code không tự sửa hoặc thay output không hợp lệ bằng đáp án chuẩn.

Nguồn điều kiện là ý nghĩa feature và điều kiện viết trong playbook nội bộ;
chưa phải chính sách nghiệp vụ được chuyên gia độc lập thẩm định.

Nếu explanation về maximum-ratio không có maximum baseline hợp lệ, PB-28 là mandatory_gap: phải truy xuất và phải được model chọn. Thiếu bước đó sẽ bị từ chối trước READY. Hợp đồng này có regression coverage trong [test_required_gap.py](../../tests/unit_integration/test_required_gap.py) và case tham chiếu trong [playbook_v2_boundary_cases.json](../../benchmarks/fixtures/playbook_v2_boundary_cases.json).
