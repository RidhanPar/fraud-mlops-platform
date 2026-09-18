"""Replay real holdout traffic through the running stack and inject an incident.

Incident: the upstream feature service fails and fills V14 with 0. Every value
is still a valid number, so request validation passes. V14 is the model's most
important feature, so the model quietly stops catching fraud.

Phases: baseline -> incident -> recovery. Ground truth labels are posted after
a delay, the way chargebacks arrive after the transaction.

    python scripts/simulate_drift.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fraud_mlops import data  # noqa: E402
from fraud_mlops.config import ROOT, load_params  # noqa: E402
from fraud_mlops.features import RAW_COLUMNS  # noqa: E402


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--baseline", type=float, default=180)
    ap.add_argument("--incident", type=float, default=300)
    ap.add_argument("--recovery", type=float, default=240)
    ap.add_argument("--rows-per-sec", type=float, default=250)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--label-lag", type=float, default=45, help="seconds before labels arrive")
    ap.add_argument("--out", default=str(ROOT / "docs/drift_event/timeline.json"))
    args = ap.parse_args()

    p = load_params()
    holdout = data.time_split(data.load(ROOT / p["data"]["path"]), p["data"]["train_frac"], p["data"]["valid_frac"]).holdout
    rng = np.random.default_rng(7)
    X = holdout[RAW_COLUMNS].to_numpy()
    y = holdout["Class"].to_numpy()

    phases = [("baseline", args.baseline), ("incident", args.incident), ("recovery", args.recovery)]
    timeline = {"incident": "V14 zero filled by upstream feature service", "phases": []}
    pending: deque = deque()  # (due_time, [(tx_id, label)])
    tick = args.batch / args.rows_per_sec
    run = datetime.now().strftime("%H%M%S")
    sent = 0
    order = rng.permutation(len(X))
    cursor = 0

    with httpx.Client(base_url=args.url, timeout=30) as client:
        for name, duration in phases:
            start = time.time()
            timeline["phases"].append({"phase": name, "start": now_iso()})
            print(f"{now_iso()} phase {name} for {duration:.0f}s", flush=True)
            while time.time() - start < duration:
                t0 = time.time()
                if cursor + args.batch > len(order):
                    order, cursor = rng.permutation(len(X)), 0
                idx = order[cursor : cursor + args.batch]
                cursor += args.batch

                rows = X[idx].copy()
                if name == "incident":
                    rows[:, RAW_COLUMNS.index("V14")] = 0.0
                ids = [f"sim-{run}-{sent + i}" for i in range(len(idx))]
                txs = [dict(zip(RAW_COLUMNS, map(float, r)), transaction_id=t) for r, t in zip(rows, ids)]
                # Most traffic is batched; a slice goes to /predict to keep its latency series live.
                client.post("/predict/batch", json={"transactions": txs[5:]}).raise_for_status()
                for tx in txs[:5]:
                    client.post("/predict", json=tx).raise_for_status()
                sent += len(idx)
                pending.append((t0 + args.label_lag, list(zip(ids, map(bool, y[idx])))))

                while pending and pending[0][0] <= time.time():
                    _, labels = pending.popleft()
                    client.post("/feedback", json={"labels": [
                        {"transaction_id": t, "is_fraud": lbl} for t, lbl in labels
                    ]}).raise_for_status()

                time.sleep(max(0.0, tick - (time.time() - t0)))
            timeline["phases"][-1]["end"] = now_iso()

    timeline["rows_sent"] = sent
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(timeline, indent=2), encoding="utf-8")
    print(f"done, {sent} transactions sent, timeline in {args.out}")


if __name__ == "__main__":
    main()
