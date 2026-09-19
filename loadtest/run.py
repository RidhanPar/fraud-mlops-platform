"""Closed loop load test: N concurrent clients, each sends its next request as
soon as the previous one returns. Payloads are real holdout transactions.

Usage:
    python -m loadtest.run --url http://localhost:8000 --duration 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import time
from pathlib import Path
from multiprocessing import Pool

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent

SCENARIOS = [
    # (name, endpoint, rows per request, concurrency)
    ("single c=1", "/predict", 1, 1),
    ("single c=8", "/predict", 1, 8),
    ("single c=32", "/predict", 1, 32),
    ("single c=64", "/predict", 1, 64),
    ("batch100 c=4", "/predict/batch", 100, 4),
    ("batch1000 c=2", "/predict/batch", 1000, 2),
]


def load_payloads(path: str | None, n: int = 5000) -> list[dict]:
    """Real holdout rows. A pre exported JSON file lets the generator run in a
    bare container without the dataset or the training code."""
    if path:
        return json.loads(Path(path).read_text())
    from fraud_mlops import data
    from fraud_mlops.features import RAW_COLUMNS

    holdout = data.time_split(data.load(ROOT / "data/raw/creditcard.csv"), 0.6, 0.2).holdout
    return holdout[RAW_COLUMNS].sample(n=n, random_state=0).to_dict("records")


async def worker(client, url, rows, batch, deadline, latencies, errors):
    rng = random.Random()
    while time.perf_counter() < deadline:
        if batch == 1:
            body = rng.choice(rows)
        else:
            start = rng.randrange(0, len(rows) - batch)
            body = {"transactions": rows[start : start + batch]}
        t0 = time.perf_counter()
        try:
            r = await client.post(url, json=body)
            ok = r.status_code == 200
        except httpx.HTTPError:
            ok = False
        latencies.append((time.perf_counter() - t0) * 1000)
        if not ok:
            errors.append(1)


async def _scenario(base, endpoint, batch, concurrency, duration, rows, headers=None):
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base, timeout=30, limits=limits, headers=headers) as client:
        # Warm up connections so the first handshakes do not pollute the numbers.
        await asyncio.gather(*(client.get("/health") for _ in range(concurrency)))
        latencies: list[float] = []
        errors: list[int] = []
        start = time.perf_counter()
        deadline = start + duration
        await asyncio.gather(
            *(worker(client, endpoint, rows, batch, deadline, latencies, errors) for _ in range(concurrency))
        )
        elapsed = time.perf_counter() - start
    return latencies, len(errors), elapsed


def _run_proc(args):
    return asyncio.run(_scenario(*args))


def scenario(base, endpoint, batch, concurrency, duration, rows, procs=1, headers=None) -> dict:
    """Spread the clients over several processes so the generator is not the bottleneck."""
    procs = max(1, min(procs, concurrency))
    shares = [concurrency // procs + (i < concurrency % procs) for i in range(procs)]
    jobs = [(base, endpoint, batch, c, duration, rows, headers) for c in shares]
    if procs == 1:
        parts = [_run_proc(jobs[0])]
    else:
        with Pool(procs) as pool:
            parts = pool.map(_run_proc, jobs)
    lat = np.array([x for p in parts for x in p[0]])
    errors = sum(p[1] for p in parts)
    elapsed = max(p[2] for p in parts)
    return {
        "requests": len(lat),
        "errors": errors,
        "error_rate": errors / max(len(lat), 1),
        "req_per_s": len(lat) / elapsed,
        "rows_per_s": len(lat) * batch / elapsed,
        "p50_ms": float(np.percentile(lat, 50)),
        "p95_ms": float(np.percentile(lat, 95)),
        "p99_ms": float(np.percentile(lat, 99)),
        "max_ms": float(lat.max()),
    }


def run_all(args) -> None:
    rows = load_payloads(args.payloads)
    if args.export_payloads:
        Path(args.export_payloads).write_text(json.dumps(rows))
        print(f"wrote {len(rows)} payloads to {args.export_payloads}")
        return
    results = {}
    wanted = set(args.only) if args.only else None
    for name, endpoint, batch, conc in SCENARIOS:
        if wanted and name not in wanted:
            continue
        headers = dict(h.split(":", 1) for h in args.header) if args.header else None
        r = scenario(args.url, endpoint, batch, conc, args.duration, rows, args.procs, headers)
        results[name] = r
        print(
            f"{name:<15} {r['req_per_s']:>8.1f} req/s {r['rows_per_s']:>9.0f} rows/s  "
            f"p50 {r['p50_ms']:>7.1f}  p95 {r['p95_ms']:>7.1f}  p99 {r['p99_ms']:>7.1f} ms  "
            f"errors {r['errors']}",
            flush=True,
        )
    out = {
        "target": args.url,
        "duration_s_per_scenario": args.duration,
        "note": args.note,
        "generator_processes": args.procs,
        "client_host": platform.platform(),
        "results": results,
    }
    if args.print_json:
        # On Fargate there is no disk to collect from: the result goes to the log stream.
        print("RESULT_JSON " + json.dumps(out), flush=True)
        return
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")


def main() -> None:
    p = argparse.ArgumentParser()
    # Not "localhost": on Docker Desktop for Windows it adds ~40ms per request.
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--duration", type=float, default=30)
    p.add_argument("--note", default="")
    p.add_argument("--procs", type=int, default=1, help="load generator processes")
    p.add_argument("--header", action="append", default=[], help="extra header, Name:value")
    p.add_argument("--print-json", action="store_true", help="print results to stdout instead of a file")
    p.add_argument("--only", action="append", help='run only this scenario, e.g. "batch1000 c=2"')
    p.add_argument("--payloads", help="JSON list of transactions (skip reading the CSV)")
    p.add_argument("--export-payloads", help="write sampled holdout payloads to this file and exit")
    p.add_argument("--out", default=str(ROOT / "loadtest/results/latest.json"))
    run_all(p.parse_args())


if __name__ == "__main__":
    main()
