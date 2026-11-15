"""Test các hàm thuần (không cần Redis/Postgres): pytest tests -q"""
import asyncio
import json
import re
from pathlib import Path

import pytest

from fraud_common.resilience import CircuitBreaker, CircuitOpenError
from fraud_common.schemas import RiskLevel, ThresholdConfig
from scoring_worker.pipeline.pii import mask_pii
from scoring_worker.pipeline.routing import DEFAULT_THRESHOLDS, route
from scoring_worker.pipeline.validation import validate

GOOD = {
    "trans_num": "t1", "trans_date_trans_time": "2024-01-01 10:00:00", "cc_num": "4111111111111111",
    "first": "A", "last": "B", "street": "x", "merchant": "m", "category": "c", "amt": 12.5,
    "lat": 10.0, "long": 20.0, "merch_lat": 10.1, "merch_long": 20.1, "is_fraud": 1,
}
CFG = ThresholdConfig(version=1, low_max=0.006, high_min=0.25)
ROOT = Path(__file__).resolve().parents[2]


def test_routing_thresholds():
    """#sym:test_routing_thresholds
    Kiem tra: route theo nguong LOW/MEDIUM/HIGH cua model hien tai.
    @param Khong co tham so.
    Dung khi: 0.006 la MEDIUM, 0.25 la HIGH va cac moc bien dung.
    Sai khi: risk level sai tai moc bien.
    """
    assert route(0.0, CFG) == RiskLevel.LOW
    assert route(0.001, CFG) == RiskLevel.LOW
    assert route(0.0059, CFG) == RiskLevel.LOW
    assert route(0.006, CFG) == RiskLevel.MEDIUM
    assert route(0.10, CFG) == RiskLevel.MEDIUM
    assert route(0.2499, CFG) == RiskLevel.MEDIUM
    assert route(0.25, CFG) == RiskLevel.HIGH
    assert route(0.70, CFG) == RiskLevel.HIGH
    assert route(1.00, CFG) == RiskLevel.HIGH


def test_default_thresholds_match_db_seed_and_model_recommendation():
    """#sym:test_default_thresholds_match_db_seed_and_model_recommendation
    Kiem tra: nguong fallback, seed DB, migration va recommended_triage cua model khop nhau.
    @param Khong co tham so.
    Dung khi: ca bon nguon cung low_max/high_min.
    Sai khi: train lai model hoac doi nguong ma quen cap nhat mot noi.
    """
    expected = (DEFAULT_THRESHOLDS.low_max, DEFAULT_THRESHOLDS.high_min)
    assert (CFG.low_max, CFG.high_min) == expected
    insert = re.compile(
        r"INSERT INTO threshold_configs \(low_max, high_min, active, created_by\)\s*"
        r"VALUES \(([\d.]+),\s*([\d.]+),\s*true")
    for sql_file in ("db/init.sql", "db/migrations/002_model_triage_thresholds.sql"):
        match = insert.search((ROOT / sql_file).read_text(encoding="utf-8"))
        assert match, sql_file
        assert tuple(float(value) for value in match.groups()) == pytest.approx(expected), sql_file
    lineage = json.loads((ROOT / "data/models/fraud_xgboost.json").read_text(encoding="utf-8"))
    triage = lineage["recommended_triage"]
    assert (triage["low_max"], triage["high_min"]) == pytest.approx(expected)


def test_validation_ok():
    """#sym:test_validation_ok
    Kiem tra: giao dich hop le.
    @param Khong co tham so.
    Dung khi: transaction GOOD co ok=True.
    Sai khi: giao dich hop le bi tu choi.
    """
    assert validate(GOOD).ok


@pytest.mark.parametrize("field,value", [
    ("amt", 0),
    ("amt", 1_000_000),
    ("lat", -90.01),
    ("lat", 90.01),
    ("long", -180.01),
    ("long", 180.01),
])
def test_validation_rejects_numeric_boundary_values(field, value):
    """#sym:test_validation_rejects_numeric_boundary_values
    Kiem tra: gia tri so nam ngoai bien.
    @param field, value: truong va gia tri can kiem tra.
    Dung khi: amt/lat/long vuot gioi han bi tu choi.
    Sai khi: du lieu ngoai bien van duoc chap nhan.
    """
    assert not validate({**GOOD, field: value}).ok


@pytest.mark.parametrize("field,value", [
    ("amt", 0.01),
    ("amt", 999_999.99),
    ("lat", -90.0),
    ("lat", 90.0),
    ("long", -180.0),
    ("long", 180.0),
])
def test_validation_accepts_numeric_boundary_values(field, value):
    """#sym:test_validation_accepts_numeric_boundary_values
    Kiem tra: gia tri so tai bien hop le.
    @param field, value: truong va gia tri can kiem tra.
    Dung khi: amt/lat/long tai min/max duoc chap nhan.
    Sai khi: gia tri bien hop le bi tu choi.
    """
    assert validate({**GOOD, field: value}).ok


@pytest.mark.parametrize("patch", [
    {"amt": -5}, {"amt": "abc"}, {"lat": 999}, {"merchant": None},
])
def test_validation_rejects_bad_data(patch):
    """#sym:test_validation_rejects_bad_data
    Kiem tra: du lieu sai kieu hoac sai mien.
    @param patch: cac truong va gia tri sai.
    Dung khi: validator tra ok=False va co errors.
    Sai khi: du lieu xau duoc chap nhan.
    """
    r = validate({**GOOD, **patch})
    assert not r.ok and r.errors


def test_validation_missing_field():
    """#sym:test_validation_missing_field
    Kiem tra: thieu truong bat buoc.
    @param Khong co tham so.
    Dung khi: thieu cc_num bi tu choi.
    Sai khi: giao dich thieu truong van duoc chap nhan.
    """
    bad = {k: v for k, v in GOOD.items() if k != "cc_num"}
    assert not validate(bad).ok


def test_pii_masking():
    """#sym:test_pii_masking
    Kiem tra: che PII trong transaction.
    @param Khong co tham so.
    Dung khi: PII bi loai bo, mask giu 4 so cuoi va hash dai 32.
    Sai khi: PII con nguyen hoac mask/hash sai.
    """
    out = mask_pii(GOOD)
    assert "cc_num" not in out and "first" not in out and "is_fraud" not in out
    assert out["masked_cc_num"].endswith("1111") and out["masked_cc_num"].startswith("*")
    assert len(out["account_hash_id"]) == 32


def test_circuit_breaker_opens_then_blocks():
    """#sym:test_circuit_breaker_opens_then_blocks
    Kiem tra: circuit breaker mo sau du so loi.
    @param Khong co tham so.
    Dung khi: sau 2 loi request tiep theo nhan CircuitOpenError.
    Sai khi: breaker khong mo hoac van cho request di qua.
    """
    async def boom():
        raise RuntimeError("x")

    async def run():
        cb = CircuitBreaker(failure_threshold=2, reset_after_s=60)
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(boom)
        with pytest.raises(CircuitOpenError):
            await cb.call(boom)

    asyncio.run(run())
