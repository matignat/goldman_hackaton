#!/usr/bin/env python3
"""Real HTTP ingress load test against a running SignalDesk deployment.

Tests durable webhook ingress. Requires the seeded demo source and D persona for
optional queue-drain observation. Results are client ACK latency, not UI receipt.
"""

import argparse
import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

p = argparse.ArgumentParser()
p.add_argument("--base", default="http://localhost:8000")
p.add_argument("--count", type=int, default=500)
p.add_argument("--workers", type=int, default=16)
p.add_argument("--mode", choices=["burst", "mixed", "unknown"], default="burst")
p.add_argument("--source", default="webhook")
p.add_argument(
    "--rate", type=float, default=0, help="Target submissions per second; zero means burst"
)
p.add_argument("--timeout", type=float, default=60)
p.add_argument("--out", default="benchmark-http.json")
a = p.parse_args()
if a.count < 1 or a.workers < 1 or a.rate < 0:
    p.error("Invalid count, workers or rate")
root = Path(__file__).resolve().parents[1]
PAYMENTS = json.loads((root / "fixtures/payments.json").read_text())
WORKFLOW = json.loads((root / "fixtures/workflow.json").read_text())
batch = uuid.uuid4().hex[:10]


def payload(i):
    if a.mode == "unknown":
        return {"neverSeen": {"benchmarkBatch": batch, "index": i, "nested": {"x": 42}}}
    if a.mode == "mixed" and i % 3 == 1:
        r = copy.deepcopy(WORKFLOW)
        r["workflowId"] = f"WF-BENCH-{batch}-{i}"
        return r
    if a.mode == "mixed" and i % 3 == 2:
        return {"neverSeen": {"benchmarkBatch": batch, "index": i}}
    r = copy.deepcopy(PAYMENTS)
    r["labels"]["region"] = "benchmark-" + batch
    return r


accepted = []
failures = []
client = httpx.Client(timeout=15)
with httpx.Client(timeout=5) as debug_client:
    debug_client.post(a.base + "/api/session", json={"user_id": 6})
    r = debug_client.get(a.base + "/api/debug")
    baseline = r.json()["events"] if r.status_code == 200 else None

    def send(i):
        body = payload(i)
        start = time.perf_counter()
        try:
            r = client.post(
                a.base + "/api/ingest/" + a.source,
                json=body,
                headers={"X-Event-ID": batch + "-" + str(i)},
            )
            return {
                "index": i,
                "status": r.status_code,
                "ms": (time.perf_counter() - start) * 1000,
            }
        except Exception as exc:
            return {
                "index": i,
                "status": 0,
                "error": str(exc),
                "ms": (time.perf_counter() - start) * 1000,
            }

    begin = time.perf_counter()
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futures = []
        for i in range(a.count):
            if a.rate:
                planned = begin + i / a.rate
                wait = planned - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
            futures.append(pool.submit(send, i))
        for f in as_completed(futures):
            record = f.result()
            (accepted if record["status"] == 200 else failures).append(record)
    seconds = time.perf_counter() - begin
    drained = None
    pending = None
    if baseline is not None:
        deadline = time.monotonic() + a.timeout
        while time.monotonic() < deadline:
            d = debug_client.get(a.base + "/api/debug").json()
            pending = d["pending"]
            if pending == 0 and d["events"] >= baseline + len(accepted):
                drained = round(time.perf_counter() - begin, 3)
                break
            time.sleep(0.15)
client.close()
latencies = sorted(x["ms"] for x in accepted)


def percent(p):
    return round(latencies[int((len(latencies) - 1) * p)], 2) if latencies else None


result = {
    "test": "HTTP webhook load",
    "mode": a.mode,
    "batch": batch,
    "requested": a.count,
    "accepted": len(accepted),
    "failed": len(failures),
    "client_ingress_seconds": round(seconds, 3),
    "accepted_events_per_second": round(len(accepted) / seconds, 2) if seconds else None,
    "HTTP_ack_p50_ms": percent(0.5),
    "HTTP_ack_p99_ms": percent(0.99),
    "queue_drained_elapsed_seconds": drained,
    "pending_after": pending,
    "errors": failures[:10],
    "note": "HTTP ACK latency ends when raw event is persisted. For processing and captured-channel latencies use benchmark_local.py; UI SSE receipt requires browser instrumentation.",
}
Path(a.out).write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
