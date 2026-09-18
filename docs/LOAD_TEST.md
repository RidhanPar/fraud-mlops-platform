# Load test results

Measured 2026-09-18 against image `fraud-detector:v1` (model v1). Raw numbers are in
[`loadtest/results/`](../loadtest/results). Each scenario ran for 20 seconds, closed loop,
with real holdout transactions as payloads. Zero errors in every scenario.

## Setup

- Service: `--cpus 2`, 2 uvicorn workers, XGBoost pinned to 1 thread per request.
- Generator: separate container on the same Docker network, `--cpus 4`, 4 processes.
- Host: Windows 11 laptop, 8 cores, Docker Desktop (WSL2).

## One replica (2 CPUs)

| Scenario | Throughput | p50 | p95 | p99 |
|---|---|---|---|---|
| single, 1 client | 147 req/s | 6.5 ms | 8.5 ms | 11.0 ms |
| single, 8 clients | 277 req/s | 26.7 ms | 42.1 ms | 55.3 ms |
| single, 32 clients | 274 req/s | 113 ms | 168 ms | 199 ms |
| single, 64 clients | 269 req/s | 209 ms | 405 ms | 470 ms |
| batch of 100, 4 clients | 16,471 rows/s | 22.8 ms | 32.1 ms | 49.1 ms |
| batch of 1000, 2 clients | 29,510 rows/s | 62.6 ms | 109 ms | 123 ms |

## What the numbers say

- **Capacity is about 275 single predictions per second per 2 CPU replica.** Past 8
  concurrent clients, throughput stays flat and only latency grows. That is queueing:
  at 32 clients, 32 / 274 req/s = 117 ms, which matches the measured p50 of 113 ms.
- **Latency budget.** A card authorisation budget of around 100 ms holds up to about
  8 concurrent requests per replica at p99. Beyond that you add replicas, not threads.
- **Batching is roughly 100x cheaper per row end to end** (29,510 vs 277 rows/s).
  Model time inside the container is 3.6 ms for 1 row and 8.3 ms for 1000 rows. Most of a single prediction is fixed
  overhead (HTTP, validation, DataFrame construction), not the trees.

## Measurement pitfalls found and fixed

1. **`localhost` on Docker Desktop for Windows added about 40 ms per request.** The first
   run showed a 48 ms p50 for one client. `127.0.0.1` gave 6.7 ms. The load test now
   defaults to `127.0.0.1`.
2. **The load generator was the bottleneck, not the service.** Running one Python
   process on the host through the Docker port proxy, throughput *fell* from 250 to
   112 req/s as clients increased, while the container sat at 144% of a 400% CPU
   budget. Moving the generator into its own multi process container on the Docker
   network removed the collapse.

## Scaling caveat

At `--cpus 4` with 4 workers, single request capacity rose to about 350 req/s (+27%),
not the 2x you might expect. The service and generator share the laptop's 8 cores,
so this run cannot show linear scaling. A real capacity test would put the generator
on separate hardware and scale replicas behind a load balancer.
