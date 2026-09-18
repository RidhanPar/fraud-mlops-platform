"""Prometheus metrics for the inference service.

uvicorn runs several worker processes, each with its own counters. With
PROMETHEUS_MULTIPROC_DIR set, prometheus_client writes per process files and
/metrics merges them, so a scrape sees the whole service, not one worker.
"""

from __future__ import annotations

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

REQUESTS = Counter("fraud_http_requests_total", "HTTP requests", ["route", "status"])
LATENCY = Histogram(
    "fraud_http_request_duration_seconds",
    "End to end request latency inside the service",
    ["route"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
ROWS_SCORED = Counter("fraud_rows_scored_total", "Transactions scored", ["decision"])
SCORES = Histogram(
    "fraud_score",
    "Distribution of fraud probabilities returned",
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 0.995, 0.999),
)
VALIDATION_ERRORS = Counter(
    "fraud_validation_errors_total", "Rejected input by field", ["field"]
)
LOG_FAILURES = Counter(
    "fraud_prediction_log_failures_total", "Predictions refused because logging failed"
)
MODEL_INFO = Gauge(
    "fraud_model_info", "Model version being served", ["model_version"], multiprocess_mode="max"
)


def render() -> tuple[bytes, str]:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
