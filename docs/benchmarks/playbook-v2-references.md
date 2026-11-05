# Đáp án tham chiếu playbook v2

Version: playbook-v2-pre-run-1. Chốt trước khi chạy Ollama. Các rationale được viết từ điều kiện playbook và context; không dựa vào output model.

Các ID tham chiếu không được gửi vào prompt. Đây là test phát triển trên dữ liệu giả, chưa phải held-out evaluation được chuyên gia độc lập duyệt.

| Case | Các nhóm rule bắt buộc (OR trong nhóm, AND giữa nhóm) | Rule bị loại | Lý do |
|---|---|---|---|
| kaggle-amount-ratio | [['PB-08', 'PB-01']] | ['PB-06', 'PB-07'] | Positive 24h count and mean support amount comparison. |
| kaggle-count-24h | [['PB-10']] | ['PB-06'] | Positive 24h count is the focal explanation; no invented frequency baseline. |
| kaggle-home-merchant-distance | [['PB-18']] | [] | Home-to-merchant distance requires a geographic interpretation limit. |
| kaggle-no-history-default-ratio | [['PB-06', 'PB-03']] | ['PB-08'] | Zero count and default ratio require an explicit history-gap action. |
| kaggle-baseline-low | [['PB-26', 'PB-02']] | ['PB-27'] | LOW routing and below-mean amount do not justify HIGH escalation. |
| kaggle-recorded-hour | [['PB-19']] | [] | Recorded hour without timezone needs a time-context check. |
| kaggle-missing-mean | [['PB-07', 'PB-03']] | ['PB-08'] | Positive count with zero mean cannot support amount comparisons. |
| kaggle-unknown-category | [['PB-06', 'PB-28', 'PB-05']] | ['PB-20'] | Unknown category is filtered and no history remains. |
| ml-current-history | [['PB-14', 'PB-13'], ['PB-07', 'PB-24']] | ['PB-06', 'PB-12'] | Valid 7d mean ratio coexists with missing 24h mean; both windows matter. |
| ml-current-no-history | [['PB-12', 'PB-06', 'PB-03']] | ['PB-14', 'PB-15'] | Zero 7d history invalidates smoothed default ratios. |
| ml-current-7d-only | [['PB-18'], ['PB-24', 'PB-06']] | ['PB-12'] | Distance explanation plus 7d history despite empty 24h window. |
| v2-frequency-hour | [['PB-09']] | [] | 1h count needs its own time window; no fraud threshold supplied. |
| v2-max-ratio | [['PB-15']] | [] | The max ratio has denominator maximum + 1, not a spending limit. |
| v2-dispersion | [['PB-16']] | [] | Sample standard deviation with sufficient history needs distribution caveat. |
| v2-total-amount | [['PB-17']] | [] | A 24h total must not be treated as a mean. |
| v2-seven-day-count | [['PB-11', 'PB-24']] | ['PB-12'] | Positive 7d count survives a zero 24h count. |
| v2-no-calibrated-prob | [['PB-21', 'PB-02']] | ['PB-22'] | Raw score must not be relabeled as calibrated probability. |
| v2-single-history-std | [['PB-28', 'PB-05', 'PB-03']] | ['PB-16'] | One observation cannot establish sample standard deviation. |
| v2-valid-category | [['PB-20', 'PB-02']] | [] | Known category is context only; baseline does not itself imply fraud. |
| v2-missing-seven-day-mean | [['PB-13', 'PB-28', 'PB-03']] | ['PB-14', 'PB-12'] | Positive 7d count with missing mean does not permit ratio-based conclusions. |
