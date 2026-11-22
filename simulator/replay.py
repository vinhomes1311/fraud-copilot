"""Client Simulator: phát lại giao dịch theo thứ tự thời gian với tốc độ TPS chỉnh được.

  - Tách nhãn is_fraud ra file riêng (data/labels.csv), KHÔNG gửi trong payload.
  - Có tiêm lỗi đầu vào (--fault-rate) để thử luồng MANUAL_FALLBACK_UNSCORED.
  - Có tiêm lỗi dịch vụ ML (--ml-fault-rate, --ml-latency-ms): scoring worker
    chậm hoặc lỗi thật sự; job lỗi quá số lần thử -> MANUAL_FALLBACK_UNSCORED.
  - Gửi header X-Data-Version (tên file + sha256) để truy vết nguồn dữ liệu (FR-08).
  - Không có --csv thì tự sinh dữ liệu tổng hợp, chạy được ngay.

Ví dụ:
  python -m simulator.replay --url http://localhost:8080 --tps 10 --limit 500
  python -m simulator.replay --csv data/fraudTrain.csv --tps 10 --fault-rate 0.02
  python -m simulator.replay --csv data/fraudTest.csv --limit 300 --ml-fault-rate 0.5 --ml-latency-ms 300
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import random
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from fraud_common.data_version import file_data_version, normalize_data_version

NUM_FIELDS = {"amt": float, "lat": float, "long": float, "merch_lat": float, "merch_long": float,
              "city_pop": int, "unix_time": int}


def load_csv(path: str, limit: int) -> list[dict]:
    rows: list[dict] = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            r.pop("", None)
            r.pop("Unnamed: 0", None)
            for k, cast in NUM_FIELDS.items():
                if k in r and r[k] != "":
                    r[k] = cast(float(r[k])) if cast is int else cast(r[k])
            rows.append(r)
    rows.sort(key=lambda r: r["trans_date_trans_time"])      # phát lại đúng trình tự thời gian
    return rows[:limit] if limit else rows


def synth_rows(n: int) -> list[dict]:
    rnd = random.Random(42)
    accounts = [{"cc_num": str(4000000000000000 + rnd.randrange(10**8)),
                 "lat": rnd.uniform(25, 48), "long": rnd.uniform(-120, -75)} for _ in range(30)]
    t = datetime(2024, 1, 1, 8, 0, 0)
    rows = []
    for i in range(n):
        a = rnd.choice(accounts)
        fraud = rnd.random() < 0.05
        far = fraud or rnd.random() < 0.05
        t += timedelta(seconds=rnd.randint(1, 20))
        rows.append({
            "trans_num": f"synth-{i:07d}",
            "trans_date_trans_time": t.strftime("%Y-%m-%d %H:%M:%S"),
            "cc_num": a["cc_num"],
            "first": "Nguyen", "last": "Van A", "street": "1 Example St", "dob": "1990-01-01",
            "merchant": rnd.choice(["fraud_Acme", "fraud_Globex", "fraud_Initech"]),
            "category": rnd.choice(["grocery_pos", "gas_transport", "shopping_net", "travel"]),
            "amt": round(rnd.uniform(400, 1500) if fraud else rnd.lognormvariate(3.2, 0.9), 2),
            "city": "Springfield", "state": "IL", "city_pop": 50000,
            "lat": a["lat"], "long": a["long"],
            "merch_lat": a["lat"] + (rnd.uniform(3, 8) if far else rnd.uniform(-0.2, 0.2)),
            "merch_long": a["long"] + (rnd.uniform(3, 8) if far else rnd.uniform(-0.2, 0.2)),
            "unix_time": int(t.timestamp()),
            "is_fraud": int(fraud),
        })
    return rows


SYNTHETIC_DATA_VERSION = "synthetic@seed42-v1"
FAULT_PATH = "/api/v1/admin/fault-injection"


# Options added after run() was first called programmatically (e.g. the E2E
# smoke test builds its own Namespace); missing ones fall back to these.
OPTIONAL_DEFAULTS = {
    "data_version": None,
    "ml_fault_rate": 0.0,
    "ml_latency_ms": 0.0,
    "ml_fault_drain_s": 5.0,
    "keep_ml_faults": False,
}


def with_defaults(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(**{**OPTIONAL_DEFAULTS, **vars(args)})


def data_version_for(args: argparse.Namespace) -> str:
    args = with_defaults(args)
    if args.data_version:
        return normalize_data_version(args.data_version) or SYNTHETIC_DATA_VERSION
    return file_data_version(args.csv) if args.csv else SYNTHETIC_DATA_VERSION


async def set_ml_faults(client: httpx.AsyncClient, failure_rate: float, latency_ms: float) -> dict:
    resp = await client.put(FAULT_PATH, json={"ml_failure_rate": failure_rate, "ml_latency_ms": latency_ms})
    resp.raise_for_status()
    return resp.json()


async def clear_ml_faults(client: httpx.AsyncClient) -> None:
    try:
        (await client.delete(FAULT_PATH)).raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Cảnh báo: không tắt được tiêm lỗi ML ({exc}); gọi DELETE {FAULT_PATH} thủ công.")


def inject_fault(row: dict) -> dict:
    r = dict(row)
    kind = random.choice(["missing", "bad_type", "negative", "bad_geo"])
    if kind == "missing":
        r.pop(random.choice(["cc_num", "amt", "merchant", "trans_date_trans_time"]), None)
    elif kind == "bad_type":
        r["amt"] = "abc"
    elif kind == "negative":
        r["amt"] = -abs(float(r.get("amt", 1)))
    else:
        r["lat"] = 999
    return r


async def run(args: argparse.Namespace) -> None:
    args = with_defaults(args)
    rows = load_csv(args.csv, args.limit) if args.csv else synth_rows(args.limit or 500)
    labels_path = Path(args.labels)
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    stats: Counter = Counter()
    interval = 1.0 / args.tps
    data_version = data_version_for(args)
    headers = {"X-API-Key": os.getenv("SIMULATOR_API_KEY", "dev-sim-key"), "X-Data-Version": data_version}
    ml_faults = args.ml_fault_rate > 0 or args.ml_latency_ms > 0
    sem = asyncio.Semaphore(args.concurrency)
    pending: set[asyncio.Task] = set()

    lf = open(labels_path, "w", newline="", encoding="utf-8")
    async with httpx.AsyncClient(base_url=args.url, headers=headers, timeout=5.0) as client:
        writer = csv.writer(lf)
        writer.writerow(["txn_id", "is_fraud"])
        if ml_faults:
            fault = await set_ml_faults(client, args.ml_fault_rate, args.ml_latency_ms)
            print(f"Tiêm lỗi dịch vụ ML: {fault}")

        async def send(payload: dict) -> None:
            async with sem:
                try:
                    resp = await client.post("/api/v1/transactions", json=payload)
                    stats[resp.status_code] += 1
                    if resp.status_code == 429:
                        await asyncio.sleep(float(resp.headers.get("Retry-After", "1")))
                except httpx.HTTPError:
                    stats["conn_error"] += 1

        next_t = time.perf_counter()
        for row in rows:
            row = dict(row)
            label = row.pop("is_fraud", None)              # CHỐNG LỘ NHÃN: tách trước khi gửi
            writer.writerow([row.get("trans_num"), label])
            payload = inject_fault(row) if random.random() < args.fault_rate else row
            task = asyncio.create_task(send(payload))
            pending.add(task)
            task.add_done_callback(pending.discard)
            next_t += interval
            delay = next_t - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        await asyncio.gather(*pending)
        if ml_faults and not args.keep_ml_faults:
            # Jobs already queued still see the fault until the worker drains them.
            await asyncio.sleep(args.ml_fault_drain_s)
            await clear_ml_faults(client)
    lf.close()
    print(f"Xong {len(rows)} giao dịch ({data_version}). Kết quả HTTP: {dict(stats)}. "
          f"Nhãn lưu tại {labels_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--csv", default=None, help="đường dẫn dataset (cột như Sparkov/fraudTrain.csv)")
    ap.add_argument("--tps", type=float, default=10)
    ap.add_argument("--limit", type=int, default=500, help="0 = toàn bộ")
    ap.add_argument("--fault-rate", type=float, default=0.0, help="tỉ lệ giao dịch bị tiêm lỗi (0..1)")
    ap.add_argument("--concurrency", type=int, default=100)
    ap.add_argument("--labels", default="data/labels.csv")
    ap.add_argument("--data-version", default=None,
                    help="nhãn nguồn dữ liệu; mặc định <tên file>@sha256:<12 ký tự>")
    ap.add_argument("--ml-fault-rate", type=float, default=0.0,
                    help="xác suất mỗi lần scoring bị lỗi giả lập (0..1)")
    ap.add_argument("--ml-latency-ms", type=float, default=0.0,
                    help="độ trễ giả lập thêm vào bước ML inference (ms)")
    ap.add_argument("--ml-fault-drain-s", type=float, default=5.0,
                    help="chờ bao lâu sau khi gửi xong rồi mới tắt tiêm lỗi ML")
    ap.add_argument("--keep-ml-faults", action="store_true",
                    help="không tự tắt tiêm lỗi ML khi chạy xong")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
