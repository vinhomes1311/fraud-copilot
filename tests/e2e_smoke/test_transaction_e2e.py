"""Opt-in end-to-end test for the real simulator -> API -> scoring flow."""
from __future__ import annotations

import asyncio
import os
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import pytest

from simulator.replay import run


pytestmark = pytest.mark.skipif(
    not os.getenv("FRAUD_E2E_URL"),
    reason="Set FRAUD_E2E_URL to a running gateway for the real transaction flow",
)


def test_simulator_transaction_reaches_scoring_result():
    """#sym:test_simulator_transaction_reaches_scoring_result
    Kiem tra: transaction that di qua simulator/API/Redis/scoring/DB.
    @param Khong co tham so.
    Dung khi: scoring result duoc luu va case/brief co state hop le.
    Sai khi: transaction khong den DB hoac status sai.
    """
    async def scenario() -> None:
        """Chạy các bước bất đồng bộ của bài kiểm thử E2E.

        """
        base_url = os.environ["FRAUD_E2E_URL"].rstrip("/")
        txn_id = f"e2e-{uuid4().hex}"
        with TemporaryDirectory() as directory:
            csv_path = Path(directory) / "transaction.csv"
            labels_path = Path(directory) / "labels.csv"
            csv_path.write_text(
                "trans_num,trans_date_trans_time,cc_num,merchant,category,amt,"
                "lat,long,merch_lat,merch_long,city_pop,is_fraud\n"
                f"{txn_id},2024-01-01 10:00:00,4111111111111111,test_merchant,"
                "grocery_pos,25.5,10,20,10.01,20.01,1000,0\n",
                encoding="utf-8",
            )
            await run(Namespace(
                url=base_url,
                csv=str(csv_path),
                tps=1000,
                limit=1,
                fault_rate=0.0,
                concurrency=1,
                labels=str(labels_path),
            ))
            headers = {"X-API-Key": os.getenv("SIMULATOR_API_KEY", "dev-sim-key")}
            async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=5) as client:
                for _ in range(60):
                    response = await client.get(f"/api/v1/transactions/{txn_id}/result")
                    if response.status_code == 200:
                        result = response.json()
                        assert result["txn_id"] == txn_id
                        assert result["status"] == "SCORED"
                        if result.get("case_id") is not None:
                            login = await client.post(
                                "/api/v1/auth/login",
                                json={"username": "analyst1", "password": "analyst123"},
                            )
                            assert login.status_code == 200
                            detail_headers = {
                                "Authorization": f"Bearer {login.json()['access_token']}"
                            }
                            for _ in range(60):
                                detail = await client.get(
                                    f"/api/v1/cases/{result['case_id']}",
                                    headers=detail_headers,
                                )
                                assert detail.status_code == 200
                                case = detail.json()
                                if case.get("brief_status") in {"READY", "FAILED", "FAILED_TIMEOUT"}:
                                    break
                                if case.get("brief_status") is None:
                                    break
                                await asyncio.sleep(0.5)
                            assert case["txn_id"] == txn_id
                            if result["risk_level"] == "LOW":
                                assert case["brief_status"] is None
                            else:
                                assert case["brief_status"] in {"READY", "FAILED", "FAILED_TIMEOUT"}
                        return
                    assert response.status_code == 202
                    await asyncio.sleep(0.5)
        pytest.fail("Scoring result was not persisted within 30 seconds")

    asyncio.run(scenario())
