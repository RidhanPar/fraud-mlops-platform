"""Publish monitor signals to CloudWatch, where AWS alarms play the role that
Prometheus and Alertmanager play locally.

Only values the monitor could actually compute are sent. A missing value stays
missing, and the alarms treat missing data as "not breaching" (except the
heartbeat), mirroring the NaN handling of the Prometheus gauges.
"""

from __future__ import annotations

import math
from typing import Any


def metrics_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = [{"MetricName": "MonitorHeartbeat", "Value": 1.0, "Unit": "Count"}]

    def add(name: str, value: float | None, unit: str = "None") -> None:
        if value is not None and not math.isnan(value):
            data.append({"MetricName": name, "Value": float(value), "Unit": unit})

    if "window_rows" in report:
        add("WindowRows", report["window_rows"], "Count")
        add("FeaturesDrifting", len(report.get("drifting", [])), "Count")
        add("StuckFeatures", len(report.get("stuck_features", [])), "Count")
        add("ScorePSI", report.get("score_psi"))
        add("FlagRate", report.get("flag_rate"))
        ref = report.get("reference_flag_rate")
        if ref and report.get("flag_rate_rows", 0) >= 20_000:
            add("FlagRateRatio", report["flag_rate"] / ref)
    if "recall" in report:
        add("LabelledRecall", report["recall"])
        add("LabelledPrecision", report.get("precision"))
        add("LabelledFrauds", report.get("labelled_frauds"), "Count")
    return data


def publish(client, namespace: str, report: dict[str, Any]) -> int:
    data = metrics_from_report(report)
    for i in range(0, len(data), 20):
        client.put_metric_data(Namespace=namespace, MetricData=data[i:i + 20])
    return len(data)
