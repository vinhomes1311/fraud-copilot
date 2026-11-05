# Sửa bỏ sót bước kiểm tra maximum baseline

Áp dụng khi explanation dựa trên amt_to_max_7d_ratio nhưng không có maximum 7d dương cùng lịch sử hợp lệ. Giá trị mean dương không thay thế maximum thiếu.

PB-28 được đưa lên đầu retrieval và đánh dấu mandatory_gap. Trong tình huống này JSON schema chỉ cho checklist gồm PB-28: ưu tiên sửa dữ liệu trước các so sánh bổ trợ. Validator kiểm tra độc lập sau generation; thiếu hoặc thêm bước khác thì raise lỗi, đi qua retry/fallback hiện có, không ghi READY và không tự chèn đáp án vào output. Đây là ràng buộc chính sách ứng dụng, không phải model tự suy luận tốt hơn.

Điều kiện không đọc ID case hoặc đáp án benchmark. Nếu maximum hợp lệ hoặc không có explanation tương ứng, yêu cầu bắt buộc này không được kích hoạt.

## Kiểm chứng Ollama thật

Giữ nguyên 8 case biên và đáp án, tăng từ một lên hai lượt/case. Qwen2.5:3b, embeddinggemma:300m, concurrency 1, cap 128.
- Brief hợp lệ: 100.0% (16 lượt đo).
- Strict success: 100.0%.
- Retrieval đủ rule: 100.0%.
- Lượt chọn rule trái điều kiện: 0.
- p50: 1.53 giây.

| Case lỗi cũ | Lượt | Selected | Strict pass |
|---|---:|---|---|
| boundary-4 | 1 | ['PB-28'] | True |
| boundary-4 | 0 | ['PB-28'] | True |

122 test pass, 4 skip do chưa có PostgreSQL/Redis kiểm thử riêng. Regression test kiểm tra thứ tự retrieval, điều kiện kích hoạt/tắt, prompt mandatory_gap, từ chối output thiếu bước bắt buộc và giữ nguyên output hợp lệ.
Mẫu nhỏ trên dữ liệu giả, chưa chứng minh chất lượng trên mọi giao dịch. Chưa chạy lại toàn bộ cohort 20 case cho thay đổi prompt này; kết quả 80% trước đó thuộc phiên bản v5.

Lượt v6 chỉ dùng prompt/guard vẫn bỏ sót case lỗi và bị validator từ chối. Giữ raw report v6 để đối chiếu, không loại các lượt fail khỏi mẫu số.

Raw report cuối: benchmark_results/playbook_v7_gap_schema_20261105/results.json và quality.json.
