"""ML service fault injection (2.2 / FR-07) and data/playbook versions (FR-08)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

import fraud_common.faults as faults
import scoring_worker.processor as scoring
from analyst_api.auth import simulator_or_admin
from analyst_api.routes import admin
from analyst_api.state import state
from fraud_common.config import get_settings
from fraud_common.data_version import file_data_version, normalize_data_version
from fraud_common.schemas import JobEnvelope, RiskLevel, ScoringOutput, ScoringStatus, ThresholdConfig
from llm_worker.rag import STUB_PLAYBOOK_VERSION, ApprovedPlaybookRetriever, StubRetriever, playbook_version
from simulator import replay


class FakeRedis:
    """Just the hash commands the fault store uses."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.fail = False

    async def hgetall(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    async def delete(self, key):
        self.hashes.pop(key, None)


def _settings(monkeypatch, **changes):
    patched = replace(get_settings(), **changes)
    monkeypatch.setattr(faults, "get_settings", lambda: patched)
    monkeypatch.setattr(admin, "get_settings", lambda: patched)
    return patched


# ---------------------------------------------------------------- fault store
def test_config_is_clamped_and_defaults_to_environment(monkeypatch):
    _settings(monkeypatch, ml_fault_rate=0.25, ml_fault_latency_ms=50.0)
    assert faults.parse_config({}) == faults.MLFaultConfig(0.25, 50.0, "env")
    assert faults.parse_config(MagicMock()).source == "env"  # never trust a non-dict reply
    config = faults.parse_config({"failure_rate": "7", "latency_ms": "-3", "set_by": "simulator"})
    assert (config.failure_rate, config.latency_ms, config.source) == (1.0, 0.0, "simulator")
    assert faults.parse_config({"failure_rate": "abc", "latency_ms": "99999"}).latency_ms == faults.MAX_LATENCY_MS


def test_disabled_flag_wins_over_runtime_override(monkeypatch):
    _settings(monkeypatch, fault_injection_enabled=False, ml_fault_rate=1.0)
    config = faults.parse_config({"failure_rate": "1"})
    assert config.source == "disabled" and not config.active


@pytest.mark.asyncio
async def test_provider_injects_failure_and_latency(monkeypatch):
    _settings(monkeypatch)
    redis = FakeRedis()
    await faults.write_config(redis, 1.0, 120, "admin")
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(faults.asyncio, "sleep", fake_sleep)
    provider = faults.FaultProvider(redis, ttl_s=0)
    with pytest.raises(faults.InjectedFault):
        await provider.inject()
    assert slept == [0.12]

    await faults.write_config(redis, 0.0, 0, "admin")
    assert (await provider.inject()).active is False


@pytest.mark.asyncio
async def test_provider_caches_and_survives_redis_errors(monkeypatch):
    _settings(monkeypatch, ml_fault_rate=0.0)
    redis = FakeRedis()
    await faults.write_config(redis, 0.5, 0, "simulator")
    provider = faults.FaultProvider(redis, ttl_s=60)
    assert (await provider.get()).failure_rate == 0.5
    await faults.write_config(redis, 0.9, 0, "simulator")
    assert (await provider.get()).failure_rate == 0.5  # cached
    redis.fail = True
    provider._expires = 0
    assert (await provider.get()).failure_rate == 0.5  # keeps last known config
    assert (await faults.FaultProvider(redis, ttl_s=0).get()).source == "env"


# ------------------------------------------------------------ scoring worker
def _processor(monkeypatch, provider):
    conn = AsyncMock()
    conn.fetchrow.return_value = {"txn_id": "t1"}
    conn.fetchval.return_value = 7

    @asynccontextmanager
    async def write_tx():
        yield conn

    processor = scoring.ScoringProcessor(SimpleNamespace(write_tx=write_tx), AsyncMock(), faults=provider)
    processor.thresholds.get = AsyncMock(return_value=ThresholdConfig(version=3, low_max=0.0072, high_min=0.3))
    monkeypatch.setattr(scoring, "compute_features", AsyncMock(return_value={"amt": 10}))
    monkeypatch.setattr(scoring, "predict", lambda features: ScoringOutput(
        raw_model_score=0.001, calibrated_prob=0.001, model_version="m1", explanations=[]))
    return processor, conn


def _envelope(data_version="fraudTest.csv@sha256:abc"):
    return JobEnvelope(job_type="score_transaction", trace_id="tr-1", payload={
        "txn_id": "t1", "data_version": data_version, "raw": {
            "trans_num": "t1", "trans_date_trans_time": "2026-11-04T12:00:00Z",
            "cc_num": "4111111111111111", "merchant": "m", "category": "grocery_pos",
            "amt": 10, "lat": 10, "long": 20, "merch_lat": 10, "merch_long": 20}})


@pytest.mark.asyncio
async def test_injected_failure_is_retried_then_unscored_never_low(monkeypatch):
    _settings(monkeypatch)
    redis = FakeRedis()
    await faults.write_config(redis, 1.0, 0, "simulator")
    processor, conn = _processor(monkeypatch, faults.FaultProvider(redis, ttl_s=0))
    env = _envelope()

    with pytest.raises(faults.InjectedFault):  # propagates -> queue retries the job
        await processor.handle(env)
    assert not any("INSERT INTO cases" in str(c.args[0]) for c in conn.fetchval.call_args_list)

    await processor.handle_dead(env, "InjectedFault('injected ML service failure')")
    statements = [c.args for c in conn.execute.call_args_list]
    scoring_insert = next(a for a in statements if "INSERT INTO scoring_results" in a[0])
    assert scoring_insert[2] == ScoringStatus.MANUAL_FALLBACK_UNSCORED.value
    assert "risk_level" not in scoring_insert[0]
    assert scoring_insert[3][0]["field"] == "_system"
    txn_insert = next(a for a in statements if "INSERT INTO transactions" in a[0])
    assert txn_insert[-1] == "fraudTest.csv@sha256:abc"
    assert any("unscored_queue" in a[0] for a in statements)


@pytest.mark.asyncio
async def test_injected_latency_counts_in_scoring_latency_and_data_version_is_stored(monkeypatch):
    _settings(monkeypatch)
    redis = FakeRedis()
    await faults.write_config(redis, 0.0, 30, "simulator")
    processor, conn = _processor(monkeypatch, faults.FaultProvider(redis, ttl_s=0))
    await processor.handle(_envelope())
    txn_insert = next(c.args for c in conn.execute.call_args_list if "INSERT INTO transactions" in c.args[0])
    assert "data_version" in txn_insert[0] and txn_insert[6] == "fraudTest.csv@sha256:abc"
    scoring_insert = conn.fetchrow.call_args.args
    assert scoring_insert[5] == RiskLevel.LOW.value
    assert scoring_insert[10] >= 30  # latency_ms includes the injected delay
    audit = next(c.args for c in conn.execute.call_args_list if "audit_log" in c.args[0])
    assert audit[5]["data_version"] == "fraudTest.csv@sha256:abc"


@pytest.mark.asyncio
async def test_job_timeout_from_injected_latency_propagates(monkeypatch):
    _settings(monkeypatch)
    redis = FakeRedis()
    await faults.write_config(redis, 0.0, 400, "simulator")
    processor, _ = _processor(monkeypatch, faults.FaultProvider(redis, ttl_s=0))
    processor.s = replace(processor.s, ml_job_timeout_s=0.05)
    with pytest.raises(asyncio.TimeoutError):
        await processor.handle(_envelope())


# --------------------------------------------------------------- admin API
@pytest.mark.asyncio
async def test_fault_endpoints_store_audit_and_clear(monkeypatch):
    _settings(monkeypatch)
    conn = AsyncMock()

    @asynccontextmanager
    async def write_tx():
        yield conn

    previous = state.redis, state.db
    state.redis, state.db = FakeRedis(), SimpleNamespace(write_tx=write_tx)
    try:
        body = admin.FaultInjectionUpdate(ml_failure_rate=0.4, ml_latency_ms=250)
        saved = await admin.put_fault_injection(body, actor="simulator")
        assert saved["failure_rate"] == 0.4 and saved["latency_ms"] == 250 and saved["active"]
        assert saved["source"] == "simulator" and "ml_job_timeout_ms" in saved
        assert (await admin.get_fault_injection("admin"))["failure_rate"] == 0.4
        cleared = await admin.delete_fault_injection(actor="admin")
        assert cleared["active"] is False
        events = [c.kwargs.get("event") or c.args for c in conn.execute.call_args_list]
        assert len(conn.execute.call_args_list) == 2 and events
    finally:
        state.redis, state.db = previous


@pytest.mark.asyncio
async def test_fault_update_rejected_when_disabled(monkeypatch):
    _settings(monkeypatch, fault_injection_enabled=False)
    with pytest.raises(HTTPException) as exc:
        await admin.put_fault_injection(admin.FaultInjectionUpdate(ml_failure_rate=1), actor="admin")
    assert exc.value.status_code == 403


def test_fault_update_validates_ranges():
    with pytest.raises(ValueError):
        admin.FaultInjectionUpdate(ml_failure_rate=1.5)
    with pytest.raises(ValueError):
        admin.FaultInjectionUpdate(ml_latency_ms=-1)


@pytest.mark.asyncio
async def test_only_simulator_key_or_admin_may_change_faults(monkeypatch):
    import analyst_api.auth as auth
    from fraud_common.schemas import Role

    monkeypatch.setattr(auth, "require_simulator", AsyncMock(return_value="simulator"))
    assert await simulator_or_admin("key", None) == "simulator"
    monkeypatch.setattr(auth, "current_user", AsyncMock(return_value=auth.User("lead1", Role.RISK_LEAD)))
    with pytest.raises(HTTPException) as exc:
        await simulator_or_admin(None, object())
    assert exc.value.status_code == 403
    monkeypatch.setattr(auth, "current_user", AsyncMock(return_value=auth.User("admin", Role.ADMIN)))
    assert await simulator_or_admin(None, object()) == "admin"


# ---------------------------------------------------------------- versions
def test_data_version_labels(tmp_path):
    data = tmp_path / "fraudTest.csv"
    data.write_bytes(b"a,b\n1,2\n")
    expected = hashlib.sha256(b"a,b\n1,2\n").hexdigest()[:12]
    assert file_data_version(data) == f"fraudTest.csv@sha256:{expected}"
    data.write_bytes(b"a,b\n1,3\n0\n")
    assert file_data_version(data) != f"fraudTest.csv@sha256:{expected}"  # cache follows the file
    assert normalize_data_version("  my set<script>\n ") == "my_set_script_"
    assert normalize_data_version("") is None and normalize_data_version(None) is None
    assert len(normalize_data_version("x" * 500)) == 200


@pytest.mark.asyncio
async def test_ingest_forwards_data_version_header():
    from analyst_api.routes.ingest import ingest_transaction
    from tests.unit_integration.test_ingest_and_admin import make_request

    redis = AsyncMock()
    redis.zcard = AsyncMock(return_value=0)
    redis.set = AsyncMock(return_value=True)
    queue = MagicMock(ready="ml_jobs:ready", publish=AsyncMock())
    previous = state.redis, state.ml_queue, state.settings
    state.redis, state.ml_queue, state.settings = redis, queue, MagicMock(max_ml_queue_depth=10)
    try:
        await ingest_transaction(make_request(json_data={"trans_num": "t9", "amt": 5},
                                              headers={"X-Data-Version": "fraudTest.csv@sha256:abc"}))
    finally:
        state.redis, state.ml_queue, state.settings = previous
    payload = queue.publish.call_args.args[0].payload
    assert payload["data_version"] == "fraudTest.csv@sha256:abc"


def test_playbook_version_names_the_knowledge_base():
    from llm_worker.local_integration import KB_PATH

    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    version = playbook_version(ApprovedPlaybookRetriever())
    assert version.startswith(f"{kb['document_id']}@{kb['version']}#sha256:")
    assert playbook_version(StubRetriever()) == STUB_PLAYBOOK_VERSION


# -------------------------------------------------------------- simulator
def test_simulator_data_version_choice(tmp_path):
    data = tmp_path / "sample.csv"
    data.write_text("trans_num\n1\n", encoding="utf-8")
    args = SimpleNamespace(data_version=None, csv=str(data))
    assert replay.data_version_for(args).startswith("sample.csv@sha256:")
    assert replay.data_version_for(SimpleNamespace(data_version=None, csv=None)) == replay.SYNTHETIC_DATA_VERSION
    assert replay.data_version_for(SimpleNamespace(data_version="run 7", csv=None)) == "run_7"


@pytest.mark.asyncio
async def test_simulator_sets_and_clears_ml_faults():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.content))
        return httpx.Response(200, json={"failure_rate": 0.5, "latency_ms": 100})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x") as client:
        assert (await replay.set_ml_faults(client, 0.5, 100))["failure_rate"] == 0.5
        await replay.clear_ml_faults(client)
    assert seen[0][:2] == ("PUT", replay.FAULT_PATH)
    assert json.loads(seen[0][2]) == {"ml_failure_rate": 0.5, "ml_latency_ms": 100}
    assert seen[1][:2] == ("DELETE", replay.FAULT_PATH)
