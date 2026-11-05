# ML evaluation

Job tương ứng: `ml-evaluation` trong `.github/workflows/ci.yml`.

File [test_ml_pipeline.py](./test_ml_pipeline.py) kiểm tra preprocessing,
feature lịch sử, chronological split và schema.

Ngoài file pytest này, CI còn kiểm tra trực tiếp
`reports/ml-model-baseline.json` và upload report làm artifact.
