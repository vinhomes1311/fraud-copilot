# Playbook cho Kaggle/Sparkov

Nguồn dataset: [Credit Card Transactions Fraud Detection Dataset — kartik2112](https://www.kaggle.com/datasets/kartik2112/fraud-detection).
Đối chiếu ngày 2026-11-04 bằng [metadata Kaggle](https://www.kaggle.com/api/v1/datasets/view/kartik2112/fraud-detection),
[danh sách file](https://www.kaggle.com/api/v1/datasets/list/kartik2112/fraud-detection)
và header CSV đọc qua HTTP Range 64 KiB đầu của `fraudTrain.csv`; không tải toàn bộ dataset.
Dataset có `fraudTrain.csv` và `fraudTest.csv`, là giao dịch mô phỏng từ
[Sparkov](https://github.com/namebrandon/Sparkov_Data_Generation).

`common_playbook.json` phiên bản `1.1-kaggle-sparkov` giữ năm nguyên tắc PB-01…PB-05
và mã citation cũ. Phần hướng dẫn áp dụng và checklist là **đề xuất của nhóm**,
không phải quy định điều tra do Kaggle công bố. Không rút ngưỡng nghiệp vụ từ nhãn dữ liệu.
Brief mới lưu `prompt_version=extractive-kaggle-sparkov-v2` để phân biệt lần cập nhật này.

## Ánh xạ vào context LLM

| Cột / đặc trưng | Cách dùng |
|---|---|
| `amt` | Số tiền được cung cấp; không tự gán loại tiền |
| `category` | Chỉ đưa giá trị thuộc vocabulary Sparkov được cho phép vào evidence |
| `trans_date_trans_time` | Worker hiện cung cấp giờ dẫn xuất qua `hour_of_day`; không gửi toàn bộ timestamp, không suy đoán múi giờ |
| `lat`, `long`, `merch_lat`, `merch_long` | Không gửi tọa độ gốc; chỉ dùng `dist_home_merchant_km` do scoring cung cấp |
| `cc_num` | Backend dùng định danh tài khoản để tính lịch sử; không đưa số thẻ, last-four hoặc hash tài khoản vào prompt |
| `txn_count_24h`, `avg_amt_24h`, `amt_vs_avg_ratio` | Đặc trưng của project, không phải cột gốc CSV; chỉ dùng giao dịch trước thời điểm hiện tại trong 24 giờ |
| `is_fraud` | Nhãn đánh giá offline, loại khỏi cả evidence và explanations đưa vào prompt |
| `merchant`, `trans_num`, tên, địa chỉ, DOB, giới tính, nghề nghiệp, `city_pop`, `unix_time`, index CSV | Không đưa vào prompt của adapter; không suy luận rủi ro từ nhân khẩu học hoặc tên merchant |

Header đã xác minh có các cột: `trans_date_trans_time`, `cc_num`, `merchant`,
`category`, `amt`, `first`, `last`, `gender`, `street`, `city`, `state`, `zip`,
`lat`, `long`, `city_pop`, `job`, `dob`, `trans_num`, `unix_time`, `merch_lat`,
`merch_long`, `is_fraud`, cùng cột index CSV không có tên.
Vocabulary category lấy từ [danh sách merchant của Sparkov](https://github.com/namebrandon/Sparkov_Data_Generation/blob/master/data/merchants.csv),
là tập cho phép của adapter; không khẳng định mọi category trong nguồn Sparkov đều xuất hiện ở bản Kaggle.

## Giới hạn cần giữ trong brief

- Khoảng cách nhà–merchant không phải khoảng cách giữa hai giao dịch liên tiếp,
  vị trí thiết bị, tốc độ di chuyển hay bằng chứng sao chép thẻ.
- `avg_amt_24h` là trung bình. Khi count thiếu/không dương hoặc average thiếu/không
  dương, không đưa average/ratio vào evidence hay giải thích model. Ratio mặc định
  `1` từ scoring không chứng minh số tiền bình thường. Ghi rõ phần baseline còn thiếu.
- Một count lớn chỉ là số giao dịch đã cung cấp, chưa đủ để gọi là velocity attack;
  giờ khuya, số tiền lớn hay category riêng lẻ cũng không xác nhận gian lận.
- Không sử dụng nhãn hoặc giao dịch tương lai để bổ sung lịch sử. Không tự tạo
  bằng chứng về OTP, IP, thiết bị, khách hàng xác nhận hoặc chargeback.
- Giữ nguyên ML/routing. Chuyên viên quyết định; playbook không đặt ngưỡng tự động
  chuyển cấp hay phong tỏa thẻ. Thống kê từ dữ liệu mô phỏng không tự chứng minh
  hiệu quả trên giao dịch thật.

## Kiểm tra sau cập nhật

Chạy test LLM/RAG không dùng API trả phí:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tests/test_kaggle_playbook.py tests/test_local_integration.py tests/test_api_client.py tests/test_llm_benchmark.py -q
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --mock --scope rag-llm --repeats 1 --warmup 0
```

Đánh giá local với các tình huống tổng hợp theo schema dataset (không phải mẫu
được trích từ CSV Kaggle và không phải đánh giá fraud accuracy):

```powershell
.\.venv\Scripts\python.exe -m benchmarks.compare_llm --live --scope rag-llm --providers ollama --cases benchmarks/kaggle_cases.json --completion-cap 512 --repeats 1 --warmup 0
```

Retriever trong `llm_worker` đọc KB mới khi khởi tạo; khởi động lại worker nếu đã
chạy. Retriever này embed lại KB mỗi lượt nên không cần build index thủ công.
Nếu dùng CLI `copilot.v2` với index lưu trên đĩa, phải build lại index sau thay đổi
KB; bộ kiểm tra hash sẽ từ chối index cũ. Brief lưu trước đó giữ nguyên, muốn áp
dụng nội dung mới cần regenerate. Chưa thay đổi scoring, DB, UI hoặc mô hình ML.
