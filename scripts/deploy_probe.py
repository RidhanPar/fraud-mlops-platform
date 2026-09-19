"""Send steady traffic during an ECS rolling deployment and record every response.

Stops when the API service's newest deployment reports COMPLETED (or FAILED),
then writes a summary: requests, errors by status, latency, and which image
tag answered over time (from the X-Model-Version header and /model).

    AWS_PROFILE=default python scripts/deploy_probe.py --out docs/aws/rolling_deploy.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

import boto3
import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clients", type=int, default=4)
    ap.add_argument("--timeout-min", type=float, default=15)
    ap.add_argument("--seconds", type=float, help="probe for a fixed time instead of waiting for an ECS rollout")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    url = subprocess.check_output(["terraform", "output", "-raw", "api_url"], cwd=ROOT / "infra/terraform",
                                  text=True).strip()
    ecs = boto3.Session(profile_name=os.environ.get("AWS_PROFILE", "default"),
                        region_name="eu-north-1").client("ecs")
    payload = json.loads((ROOT / "loadtest/payloads.json").read_text())[:200]
    records: list[tuple[float, int, float]] = []
    lock = threading.Lock()
    stop = threading.Event()

    def client(i: int) -> None:
        with httpx.Client(base_url=url, timeout=10) as c:
            n = i
            while not stop.is_set():
                t0 = time.time()
                try:
                    status = c.post("/predict", json=payload[n % len(payload)]).status_code
                except httpx.HTTPError:
                    status = 0  # connection error
                with lock:
                    records.append((t0, status, (time.time() - t0) * 1000))
                n += args.clients

    threads = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(args.clients)]
    for t in threads:
        t.start()

    started = time.time()
    rollout, events = None, []
    print("probing; waiting for a new deployment to appear and finish", flush=True)
    seen_new = False
    while args.seconds is None and time.time() - started < args.timeout_min * 60:
        svc = ecs.describe_services(cluster="fraud-mlops", services=["api"])["services"][0]
        deps = svc["deployments"]
        primary = next(d for d in deps if d["status"] == "PRIMARY")
        if len(deps) > 1:
            seen_new = True
        state = primary.get("rolloutState")
        if state != rollout:
            events.append({"t": round(time.time() - started), "rollout": state, "deployments": len(deps),
                           "task_definition": primary["taskDefinition"].rsplit("/", 1)[-1]})
            print(events[-1], flush=True)
            rollout = state
        if seen_new and len(deps) == 1 and state in ("COMPLETED", "FAILED"):
            break
        time.sleep(5)
    if args.seconds is not None:
        time.sleep(args.seconds)
    else:
        time.sleep(15)  # keep probing briefly after the old tasks are gone
    stop.set()
    for t in threads:
        t.join(timeout=15)

    lat = np.array([r[2] for r in records])
    codes = Counter(r[1] for r in records)
    # Outage windows: consecutive seconds with any non 200 response.
    bad = sorted({int(t) for t, status, _ in records if status != 200})
    windows = []
    for sec in bad:
        if windows and sec - windows[-1][1] <= 2:
            windows[-1][1] = sec
        else:
            windows.append([sec, sec])
    summary = {
        "duration_s": round(records[-1][0] - records[0][0]),
        "requests": len(records),
        "status_counts": {str(k): v for k, v in sorted(codes.items())},
        "non_200": sum(v for k, v in codes.items() if k != 200),
        "p50_ms": float(np.percentile(lat, 50)), "p99_ms": float(np.percentile(lat, 99)),
        "max_ms": float(lat.max()),
        "rollout_events": events,
        "non_200_windows_utc": [[time.strftime("%H:%M:%S", time.gmtime(a)), time.strftime("%H:%M:%S", time.gmtime(b)),
                                 b - a + 1] for a, b in windows],
        "model_after": httpx.get(url + "/model", timeout=10).json().get("model_version"),
        "note": "client on the operator's laptop through the internet; latency includes that path",
    }
    Path(args.out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "rollout_events"}, indent=2))


if __name__ == "__main__":
    main()
