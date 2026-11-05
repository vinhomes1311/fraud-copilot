# Playbook v2 chi tiết

29 mục: 5 nguyên tắc nền và 24 tình huống kiểm tra. Mỗi mục gồm điều kiện, giới hạn suy luận và hành động analyst.

Không đưa toàn bộ KB vào một prompt: retriever chọn tối đa 5 mục, LLM chọn 1–3 checklist. Cache embedding tự đổi theo nội dung. Restart worker sau cập nhật playbook.

Tỷ lệ JSON/grounding hợp lệ và chất lượng chọn checklist phải được báo cáo riêng. Rule tham chiếu cũ PB-01..05 không đủ để chấm các rule chi tiết mới; không tự sửa đáp án benchmark để báo tăng điểm. Cần analyst duyệt reference cho bộ case độc lập trước khi kết luận cải thiện nghiệp vụ.

## PB-01

Kiểm tra giao dịch bất thường. Với Kaggle/Sparkov, xem amt, category, hour_of_day và dist_home_merchant_km nếu được cung cấp. Khoảng cách nhà–merchant không phải vị trí thiết bị hoặc quãng đường di chuyển của chủ thẻ; số tiền, danh mục và giờ riêng lẻ không xác nhận gian lận.

**Checklist:** Đối chiếu số tiền và tín hiệu đã cung cấp. Chỉ so sánh amt với avg_amt_24h hoặc amt_vs_avg_ratio khi có lịch sử hợp lệ; không tự đặt ngưỡng tiền, khoảng cách hay giờ để kết luận gian lận.

## PB-02

Phân biệt tín hiệu nghi ngờ và bằng chứng xác nhận gian lận. Raw score và đóng góp đặc trưng không mặc nhiên là xác suất hoặc quan hệ nhân quả. is_fraud là nhãn đánh giá của dữ liệu mô phỏng, không phải đầu vào cho RAG/LLM và không được dùng để viết nhận định.

**Checklist:** Phân biệt raw score, xác suất hiệu chỉnh do ML cung cấp và giải thích model. Ghi nhận đây là tín hiệu cần xem xét; không suy ra gian lận, hành vi tấn công hoặc nguyên nhân chỉ từ score hay một đặc trưng.

## PB-03

Kiểm tra thông tin giao dịch và lịch sử liên quan. txn_count_24h và avg_amt_24h là đặc trưng dẫn xuất theo tài khoản, chỉ từ giao dịch trước thời điểm hiện tại trong cửa sổ 24 giờ. avg_amt_24h là trung bình, không phải trung vị. Không dùng giao dịch tương lai hoặc nhãn is_fraud làm lịch sử suy luận.

**Checklist:** Dùng lịch sử đã cung cấp để đối chiếu số lượng và trung bình số tiền. Nếu không có bản ghi trước đó hoặc không có trung bình dương, ghi nhận thiếu cơ sở so sánh; không diễn giải giá trị điền mặc định là hành vi bình thường.

## PB-04

Nguyên tắc chuyển cấp và ghi nhận quyết định. Với giao dịch mô phỏng Kaggle/Sparkov, LLM hỗ trợ chuyên viên và giữ nguyên kết quả ML/routing; HIGH không tự động chứng minh gian lận hay bắt buộc chuyển cấp. Dataset không cung cấp chính sách phong tỏa hoặc quy trình xác minh khách hàng.

**Checklist:** Ghi nhận tín hiệu, nguồn playbook và thông tin còn thiếu để chuyên viên quyết định theo chính sách nhóm. Không tự phong tỏa thẻ, liên hệ khách hàng, thay đổi routing hoặc đặt ngưỡng chuyển cấp mới.

## PB-05

Những trường hợp không đủ bằng chứng để kết luận. Dataset là dữ liệu mô phỏng; không cung cấp bằng chứng xác minh khách hàng, IP, thiết bị, OTP hay chargeback. Tọa độ nhà và merchant không đủ để kết luận impossible travel hoặc thẻ bị sao chép. Múi giờ và loại tiền không được tự giả định khi context chưa cung cấp.

**Checklist:** Nêu rõ lịch sử, mức giao dịch tham chiếu hoặc xác minh còn thiếu. Không tự tạo lịch sử merchant, vị trí người dùng, phương thức thanh toán hay bằng chứng xác nhận. Chưa kết luận khi chưa đủ bằng chứng; quyết định cuối cùng thuộc chuyên viên.

## PB-06

Không có giao dịch trước đó trong 24 giờ. Tín hiệu liên quan: txn_count_24h trans_count_24h. Điều kiện và ý nghĩa: Áp dụng khi count 24h bằng 0 hoặc chưa cung cấp. Không có lịch sử 24h không chứng minh khách hàng mới và không phủ định lịch sử 7 ngày. Hướng kiểm tra: Ghi rõ thiếu baseline 24h; bỏ qua avg_amt_24h và amt_vs_avg_ratio điền mặc định. Nếu có lịch sử 7 ngày, trình bày riêng với đúng cửa sổ.

**Checklist:** Ghi rõ thiếu baseline 24h; bỏ qua avg_amt_24h và amt_vs_avg_ratio điền mặc định. Nếu có lịch sử 7 ngày, trình bày riêng với đúng cửa sổ.

## PB-07

Có số lượng nhưng thiếu trung bình 24 giờ. Tín hiệu liên quan: txn_count_24h avg_amt_24h. Điều kiện và ý nghĩa: Áp dụng khi count 24h dương nhưng avg_amt_24h thiếu hoặc không dương. Không lấy avg_amt_7d thay thế. Hướng kiểm tra: Ghi nhận số giao dịch đã biết và trung bình 24h còn thiếu. Yêu cầu đối chiếu nguồn thống kê; chưa kết luận số tiền bất thường từ ratio mặc định.

**Checklist:** Ghi nhận số giao dịch đã biết và trung bình 24h còn thiếu. Yêu cầu đối chiếu nguồn thống kê; chưa kết luận số tiền bất thường từ ratio mặc định.

## PB-08

So sánh số tiền với lịch sử 24 giờ. Tín hiệu liên quan: amt avg_amt_24h amt_vs_avg_ratio. Điều kiện và ý nghĩa: Cần count 24h dương và mean 24h dương. avg_amt_24h là trung bình, không phải trung vị. Ratio không phải xác suất gian lận. Hướng kiểm tra: Đối chiếu amt, mean và ratio được cung cấp, ghi đúng cửa sổ. Kết hợp tín hiệu khác; không tự đặt ngưỡng cảnh báo hoặc xác nhận fraud.

**Checklist:** Đối chiếu amt, mean và ratio được cung cấp, ghi đúng cửa sổ. Kết hợp tín hiệu khác; không tự đặt ngưỡng cảnh báo hoặc xác nhận fraud.

## PB-09

Tần suất giao dịch trong một giờ. Tín hiệu liên quan: trans_count_1h. Điều kiện và ý nghĩa: Chỉ dùng khi có count 1h. Một số đếm riêng lẻ chưa chứng minh tăng đột biến nếu thiếu baseline tần suất và quy tắc nghiệp vụ. Hướng kiểm tra: Ghi số lượng trong 1h; đối chiếu count 24h và lịch sử được cung cấp. Đề nghị analyst xác minh các giao dịch liên quan, không gọi đây là velocity attack đã xác nhận.

**Checklist:** Ghi số lượng trong 1h; đối chiếu count 24h và lịch sử được cung cấp. Đề nghị analyst xác minh các giao dịch liên quan, không gọi đây là velocity attack đã xác nhận.

## PB-10

Tần suất giao dịch trong 24 giờ. Tín hiệu liên quan: txn_count_24h trans_count_24h. Điều kiện và ý nghĩa: Chỉ diễn giải số đếm là giao dịch trước thời điểm hiện tại. Không có baseline tần suất thì không tự gắn nhãn nhiều hoặc bất thường. Hướng kiểm tra: Đối chiếu count 24h với dữ liệu cửa sổ khác nếu có. Ghi rõ đây là count chứ không phải tỷ lệ tăng; không lấy count 7 ngày chia 7 làm baseline chính thức.

**Checklist:** Đối chiếu count 24h với dữ liệu cửa sổ khác nếu có. Ghi rõ đây là count chứ không phải tỷ lệ tăng; không lấy count 7 ngày chia 7 làm baseline chính thức.

## PB-11

Lịch sử giao dịch bảy ngày. Tín hiệu liên quan: trans_count_7d. Điều kiện và ý nghĩa: Count 7d dương chứng minh có lịch sử trong cửa sổ này, kể cả count 24h bằng 0. Không coi count nhỏ là bằng chứng fraud. Hướng kiểm tra: Ghi tách count 24h và 7d. Đánh dấu mẫu lịch sử còn hạn chế khi analyst đánh giá độ đại diện, không bịa các mốc thời gian giao dịch cụ thể.

**Checklist:** Ghi tách count 24h và 7d. Đánh dấu mẫu lịch sử còn hạn chế khi analyst đánh giá độ đại diện, không bịa các mốc thời gian giao dịch cụ thể.

## PB-12

Thiếu lịch sử bảy ngày. Tín hiệu liên quan: trans_count_7d avg_amt_7d. Điều kiện và ý nghĩa: Count 7d bằng 0 hoặc thiếu không cung cấp cơ sở đánh giá mean/max/std 7d. Giá trị 0 có thể là fallback tính toán. Hướng kiểm tra: Ghi thiếu baseline 7d. Bỏ qua các ratio từ mean/max mặc định; ưu tiên yêu cầu thông tin thay vì kết luận bất thường từ ratio lớn.

**Checklist:** Ghi thiếu baseline 7d. Bỏ qua các ratio từ mean/max mặc định; ưu tiên yêu cầu thông tin thay vì kết luận bất thường từ ratio lớn.

## PB-13

Trung bình số tiền bảy ngày. Tín hiệu liên quan: avg_amt_7d trans_count_7d. Điều kiện và ý nghĩa: Cần count 7d và mean 7d dương. Không diễn giải trung bình 7d thành 24h, trung vị hay hạn mức giao dịch. Hướng kiểm tra: Đối chiếu amt và mean 7d với tên cửa sổ rõ ràng. Ghi nhận thiếu mean nếu count dương nhưng mean không hợp lệ; không tự suy ra phân bố giao dịch.

**Checklist:** Đối chiếu amt và mean 7d với tên cửa sổ rõ ràng. Ghi nhận thiếu mean nếu count dương nhưng mean không hợp lệ; không tự suy ra phân bố giao dịch.

## PB-14

Tỷ lệ số tiền trên trung bình bảy ngày. Tín hiệu liên quan: amt_to_avg_7d_ratio avg_amt_7d. Điều kiện và ý nghĩa: Trong pipeline hiện tại ratio = amt / (avg_amt_7d + 1). Chỉ dùng với count và mean 7d dương; đây là tỷ lệ làm trơn, không phải amt / mean thuần túy. Hướng kiểm tra: Trình bày ratio theo đúng công thức mẫu số +1. So sánh cùng amt và mean; không gọi ratio là xác suất hoặc áp dụng ngưỡng tự đặt.

**Checklist:** Trình bày ratio theo đúng công thức mẫu số +1. So sánh cùng amt và mean; không gọi ratio là xác suất hoặc áp dụng ngưỡng tự đặt.

## PB-15

Tỷ lệ số tiền trên mức lớn nhất bảy ngày. Tín hiệu liên quan: amt_to_max_7d_ratio max_amt_7d. Điều kiện và ý nghĩa: Trong pipeline hiện tại ratio = amt / (max_amt_7d + 1). Cần count và max 7d dương. Maximum chỉ phản ánh cửa sổ quan sát. Hướng kiểm tra: Đối chiếu amt, max 7d và ratio làm trơn; không gọi maximum là hạn mức được phép. Không khẳng định giao dịch lớn nhất từ trước tới nay.

**Checklist:** Đối chiếu amt, max 7d và ratio làm trơn; không gọi maximum là hạn mức được phép. Không khẳng định giao dịch lớn nhất từ trước tới nay.

## PB-16

Độ phân tán lịch sử bảy ngày. Tín hiệu liên quan: std_amt_7d trans_count_7d. Điều kiện và ý nghĩa: Sample standard deviation cần ít nhất hai quan sát. Std bằng 0 có thể do các giá trị trùng nhau hoặc fallback, không chứng minh ổn định. Hướng kiểm tra: Ghi số mẫu và std nếu hợp lệ. Không tính z-score khi std bằng 0, không suy ra phân phối chuẩn hoặc ngưỡng fraud từ một thống kê.

**Checklist:** Ghi số mẫu và std nếu hợp lệ. Không tính z-score khi std bằng 0, không suy ra phân phối chuẩn hoặc ngưỡng fraud từ một thống kê.

## PB-17

Tổng số tiền trong 24 giờ. Tín hiệu liên quan: sum_amt_24h trans_count_24h. Điều kiện và ý nghĩa: sum_amt_24h là tổng tiền của các giao dịch trước đó, không phải số lượng hoặc trung bình. Không so tổng 24h trực tiếp với mean 7d để kết luận tăng đột biến. Hướng kiểm tra: Ghi tổng tiền và count 24h nếu được cung cấp; phân biệt tổng với giá trị giao dịch hiện tại. Không giả định loại tiền hoặc cộng dữ liệu từ các cửa sổ chồng lấn.

**Checklist:** Ghi tổng tiền và count 24h nếu được cung cấp; phân biệt tổng với giá trị giao dịch hiện tại. Không giả định loại tiền hoặc cộng dữ liệu từ các cửa sổ chồng lấn.

## PB-18

Khoảng cách nhà và merchant. Tín hiệu liên quan: customer_merchant_distance_km dist_home_merchant_km. Điều kiện và ý nghĩa: Khoảng cách được tính từ tọa độ nhà và merchant. Không có vị trí thiết bị, lộ trình, thời gian di chuyển hay sự hiện diện thực tế của chủ thẻ. Hướng kiểm tra: Đối chiếu khoảng cách đã cung cấp và ghi giới hạn nguồn. Không kết luận impossible travel, sao chép thẻ hay vị trí hiện tại của khách hàng.

**Checklist:** Đối chiếu khoảng cách đã cung cấp và ghi giới hạn nguồn. Không kết luận impossible travel, sao chép thẻ hay vị trí hiện tại của khách hàng.

## PB-19

Giờ giao dịch được ghi nhận. Tín hiệu liên quan: transaction_hour hour_of_day. Điều kiện và ý nghĩa: Giờ thuộc timestamp đầu vào; context không đảm bảo múi giờ hoặc thói quen sinh hoạt của khách hàng. Giờ muộn riêng lẻ không xác nhận fraud. Hướng kiểm tra: Ghi giờ được cung cấp và tình trạng thiếu múi giờ/baseline thói quen. Đề nghị analyst kiểm tra ngữ cảnh nếu cần, không tự xác định giờ làm việc.

**Checklist:** Ghi giờ được cung cấp và tình trạng thiếu múi giờ/baseline thói quen. Đề nghị analyst kiểm tra ngữ cảnh nếu cần, không tự xác định giờ làm việc.

## PB-20

Danh mục merchant. Tín hiệu liên quan: category. Điều kiện và ý nghĩa: Chỉ dùng danh mục thuộc allowlist. Không có danh mục không đồng nghĩa merchant đáng ngờ. Danh mục không chứng minh phương thức thanh toán hoặc hành vi mua hàng. Hướng kiểm tra: Mô tả danh mục được cung cấp như ngữ cảnh bổ trợ; không suy ra website, món hàng, merchant cụ thể hay mức rủi ro ngành tự đặt.

**Checklist:** Mô tả danh mục được cung cấp như ngữ cảnh bổ trợ; không suy ra website, món hàng, merchant cụ thể hay mức rủi ro ngành tự đặt.

## PB-21

Diễn giải raw score. Tín hiệu liên quan: raw_model_score. Điều kiện và ý nghĩa: Raw score phải được phân biệt với calibrated_prob. Không giả định raw score nằm trong [0,1] hoặc có đơn vị phần trăm. Hướng kiểm tra: Ghi tên score và giá trị đúng như ML cung cấp. Nếu thiếu calibrated_prob, ghi thiếu xác suất hiệu chỉnh thay vì đổi raw score thành phần trăm.

**Checklist:** Ghi tên score và giá trị đúng như ML cung cấp. Nếu thiếu calibrated_prob, ghi thiếu xác suất hiệu chỉnh thay vì đổi raw score thành phần trăm.

## PB-22

Xác suất hiệu chỉnh. Tín hiệu liên quan: calibrated_prob. Điều kiện và ý nghĩa: Xác suất do ML cung cấp là tín hiệu model, không phải bằng chứng kết luận. Giá trị hợp lệ nằm trong [0,1]; LLM không tự hiệu chỉnh lại. Hướng kiểm tra: Dẫn đúng xác suất và risk routing. Nếu score và cảm nhận của analyst khác nhau, ghi nhận để kiểm tra model/threshold; không tự thay score.

**Checklist:** Dẫn đúng xác suất và risk routing. Nếu score và cảm nhận của analyst khác nhau, ghi nhận để kiểm tra model/threshold; không tự thay score.

## PB-23

Giải thích đóng góp đặc trưng. Tín hiệu liên quan: contribution explanations. Điều kiện và ý nghĩa: Một contribution không chứng minh quan hệ nhân quả. Khi method và scale chưa có trong context, không gọi contribution là SHAP, phần trăm hay xác suất. Hướng kiểm tra: Ghi tên feature, value và contribution đã được cung cấp cùng giới hạn phương pháp. Không suy ra raw score bằng cách cộng các contribution thiếu bias hoặc thiếu feature.

**Checklist:** Ghi tên feature, value và contribution đã được cung cấp cùng giới hạn phương pháp. Không suy ra raw score bằng cách cộng các contribution thiếu bias hoặc thiếu feature.

## PB-24

Đối chiếu cửa sổ lịch sử. Tín hiệu liên quan: trans_count_24h trans_count_7d avg_amt_7d avg_amt_24h. Điều kiện và ý nghĩa: Các cửa sổ 1h, 24h và 7d chồng lấn. Không cộng các count để có tổng giao dịch duy nhất. Một cửa sổ trống không có nghĩa mọi lịch sử đều trống. Hướng kiểm tra: Ghi rõ cửa sổ cho từng thống kê. Giữ phần lịch sử đã biết và nêu riêng khoảng trống, không dùng baseline cửa sổ này thay baseline cửa sổ khác.

**Checklist:** Ghi rõ cửa sổ cho từng thống kê. Giữ phần lịch sử đã biết và nêu riêng khoảng trống, không dùng baseline cửa sổ này thay baseline cửa sổ khác.

## PB-25

Kết hợp nhiều tín hiệu. Tín hiệu liên quan: amt amt_to_avg_7d_ratio customer_merchant_distance_km. Điều kiện và ý nghĩa: Nhiều tín hiệu liên quan có thể cùng xuất phát từ một giao dịch hoặc một feature. Không coi chúng là bằng chứng độc lập và không cộng score/ratio tùy ý. Hướng kiểm tra: Chọn tối đa ba bước kiểm tra bổ sung nhau: xác minh baseline, đối chiếu tín hiệu chính và ghi thông tin thiếu. Tránh nhiều checklist lặp lại cùng một hành động.

**Checklist:** Chọn tối đa ba bước kiểm tra bổ sung nhau: xác minh baseline, đối chiếu tín hiệu chính và ghi thông tin thiếu. Tránh nhiều checklist lặp lại cùng một hành động.

## PB-26

Xử lý case có routing LOW. Tín hiệu liên quan: risk_level LOW. Điều kiện và ý nghĩa: LOW là routing của hệ thống, không phải đảm bảo giao dịch hợp lệ. Chỉ tạo brief khi workflow yêu cầu; không tự chuyển routing vì model ngôn ngữ đánh giá khác. Hướng kiểm tra: Ghi nhận dữ liệu đã có và thông tin cần kiểm tra nếu analyst mở case thủ công. Không đề xuất chuyển cấp chỉ vì có một giờ hoặc khoảng cách riêng lẻ.

**Checklist:** Ghi nhận dữ liệu đã có và thông tin cần kiểm tra nếu analyst mở case thủ công. Không đề xuất chuyển cấp chỉ vì có một giờ hoặc khoảng cách riêng lẻ.

## PB-27

Hỗ trợ analyst với routing HIGH. Tín hiệu liên quan: risk_level HIGH. Điều kiện và ý nghĩa: HIGH ưu tiên xem xét trong workflow nhưng không chứng minh fraud và không tự cho phép khóa thẻ. Không có chính sách chuyển cấp cụ thể trong dataset. Hướng kiểm tra: Tóm tắt các tín hiệu đã kiểm chứng và thông tin còn thiếu để analyst quyết định. Chỉ đề xuất xem xét theo quy trình nhóm, không phát lệnh phong tỏa hoặc kết luận có tội.

**Checklist:** Tóm tắt các tín hiệu đã kiểm chứng và thông tin còn thiếu để analyst quyết định. Chỉ đề xuất xem xét theo quy trình nhóm, không phát lệnh phong tỏa hoặc kết luận có tội.

## PB-28

Giới hạn dữ liệu và yêu cầu bổ sung. Tín hiệu liên quan: missing unknown unavailable. Điều kiện và ý nghĩa: Context có thể thiếu lịch sử, timezone, currency, thông tin xác minh hay phương pháp explanation. Không chuyển giá trị thiếu thành 0 hoặc bình thường. Hướng kiểm tra: Nêu cụ thể trường hoặc bằng chứng cần bổ sung và lý do nó ảnh hưởng đánh giá. Chỉ đề xuất yêu cầu thông tin; không viết rằng đã liên hệ khách hàng hay đã xác minh.

**Checklist:** Nêu cụ thể trường hoặc bằng chứng cần bổ sung và lý do nó ảnh hưởng đánh giá. Chỉ đề xuất yêu cầu thông tin; không viết rằng đã liên hệ khách hàng hay đã xác minh.

## PB-29

Ghi nhận nguồn và quyết định. Tín hiệu liên quan: citation checklist analyst. Điều kiện và ý nghĩa: Checklist là đề xuất của nhóm cho dataset mô phỏng. Citation phải trỏ tới đoạn playbook được truy xuất và không phải bằng chứng khách hàng gian lận. Hướng kiểm tra: Ghi nhận evidence đã dùng, khoảng trống dữ liệu và rule tham chiếu. Quyết định cuối cùng do analyst thực hiện và lưu theo workflow, LLM không tự tạo kết quả điều tra.

**Checklist:** Ghi nhận evidence đã dùng, khoảng trống dữ liệu và rule tham chiếu. Quyết định cuối cùng do analyst thực hiện và lưu theo workflow, LLM không tự tạo kết quả điều tra.
