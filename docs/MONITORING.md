# Monitoring, drift and alerting

## Architecture

```
client ──> API (2 uvicorn workers) ──> Postgres: predictions, labels
             │ /metrics (multiprocess)          │
             ▼                                  ▼
         Prometheus  <── /metrics ──  drift monitor (every 15 s)
             │  alert rules
             ▼
        Alertmanager ──> alert sink (stand in for PagerDuty / Slack)
             │
          Grafana (dashboard provisioned from monitoring/grafana)
```

- **The API** exposes latency, throughput, errors, input rejections by field, the score
  histogram and the model version. Each worker writes its own metric files and
  `/metrics` merges them, so a scrape sees the whole service.
- **Every prediction is written to Postgres** before the response is returned, with its
  inputs, score, decision and model version. If the write fails the API returns 503
  (fail closed). A decision nobody can trace later is not returned.
- **The drift monitor is a separate process.** It keeps drift maths off the request path
  and sees traffic from every worker. Each cycle compares the latest 5,000 predictions
  with the training reference and joins the latest 20,000 labels to their predictions.

## Why feature drift thresholds are calibrated, not 0.25

The textbook rule reads PSI above 0.25 as major drift. On this data that rule would page
every day. Comparing unseen days with the training reference:

| Feature | PSI, day 2 vs training |
|---|---|
| V1 | ~1.5 |
| V3 | ~1.1 |
| V28 | ~0.74 |

Over the same period, score PSI stayed at 0.01 to 0.02 and holdout PR AUC was 0.776. The
inputs genuinely shift from day to day (V3's median moves from +0.73 to −0.72 between the
two evenings), but the model copes. Time of day was tested and ruled out: drift was *larger*
when compared against the same hours of day 1.

So each feature gets its own threshold: `max(0.25, 1.5 × worst PSI seen over 10 windows of
known good validation traffic)`. For V1 that is 2.3; for V14 and Amount it is 0.25. With
these, the holdout period raises zero false alarms. The thresholds ship with the model in
`serving_model/drift_thresholds.json`, because they belong to that model's training
reference.

`hour_of_day` is not monitored: a live window covers a few hours while the reference covers
whole days, so it would always look drifted.

## Which signal pages, and why

| Alert | Severity | Catches |
|---|---|---|
| FraudApiDown | critical | service not answering |
| PredictLatencyP99High | critical | /predict p99 over the 100 ms authorisation budget |
| HighServerErrorRate | critical | over 1% 5xx |
| PredictionLogFailing | critical | audit writes failing, so requests are being refused |
| FraudFlagRateCollapsed | critical | model flags under 20% of its normal rate: blind model |
| ModelRecallDegraded | critical | recall on labelled traffic under 0.5 (release: 0.70) |
| HighInputRejectRate | warning | over 5% of requests rejected: upstream contract broken |
| FeatureDrift | warning | a feature's PSI above its calibrated threshold |
| PredictionDrift | warning | score PSI over 0.1 (normal: 0.01 to 0.02) |
| FraudFlagRateSpike | warning | flag rate over 5x normal: attack, or review queue overflow |
| DriftMonitorStale | warning | the monitor itself has stopped |

Recall is only published when the label window holds at least 10 confirmed frauds.
Below that the number is noise, and a noisy recall alert trains people to ignore it.

Alert `for` durations are 30 s to 2 min so the demo fires within minutes. Production would
use 5 to 15 minutes.

## Cost of the prediction log

Measured with the same load test as Phase 2 (2 CPUs, 2 workers), before and after logging:

| Scenario | Phase 2, no log | With Postgres log |
|---|---|---|
| single, 1 client, p50 | 6.5 ms | 10.7 ms |
| single, 8 clients, p99 | 55 ms | 90 ms |
| single, peak throughput | ~275 req/s | ~210 req/s |
| batch of 1000, rows/s | 29,510 | 5,951 |

Inside the container, one insert takes 3.8 ms (mostly the commit) and 1,000 rows take
58 ms. The remaining batch slowdown was not profiled further. Postgres, the API and the
load generator share one laptop, so contention is likely part of it.

The single request cost stays well inside the 100 ms budget, so the log stays synchronous
and fail closed. At higher volume the usual step is writing to a durable queue such as Kafka
(acknowledged by replicas) and loading into the warehouse asynchronously. That keeps the
guarantee without a database commit on the hot path.
