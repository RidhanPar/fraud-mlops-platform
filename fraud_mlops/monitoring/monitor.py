"""Drift and performance monitor.

Runs beside the API, not inside it: the serving path stays fast, and the
monitor sees traffic from every API worker through the shared prediction log.

Every INTERVAL seconds it reads the latest predictions and labels, compares
them with the training reference, and publishes gauges on :9100/metrics that
Prometheus scrapes and alerts on.

    python -m fraud_mlops.monitoring.monitor
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from prometheus_client import Counter, Gauge, start_http_server
from sklearn.metrics import average_precision_score

from fraud_mlops.api.store import PredictionStore
from fraud_mlops.monitoring.drift import MONITORED_FEATURES, feature_drift, mode_share, score_drift

log = logging.getLogger("drift_monitor")

FEATURE_PSI = Gauge("fraud_feature_psi", "PSI of live window vs training reference", ["feature"])
FEATURE_PSI_THRESHOLD = Gauge("fraud_feature_psi_threshold", "Calibrated PSI alert threshold", ["feature"])
MODE_SHARE = Gauge("fraud_feature_mode_share", "Share of the window holding one identical value", ["feature"])
REF_MODE_SHARE = Gauge("fraud_feature_reference_mode_share", "Same share in the training reference", ["feature"])
FEATURES_DRIFTING = Gauge("fraud_features_drifting", "Features whose PSI is above their threshold")
SCORE_PSI = Gauge("fraud_score_psi", "PSI of the live score distribution vs reference")
WINDOW_ROWS = Gauge("fraud_drift_window_rows", "Rows in the drift window")
FLAG_RATE = Gauge("fraud_flag_rate", "Share of live transactions flagged as fraud")
REF_FLAG_RATE = Gauge("fraud_reference_flag_rate", "Share flagged on the training reference")
FLAG_RATE_ROWS = Gauge("fraud_flag_rate_window_rows", "Rows behind fraud_flag_rate")
LABELLED_ROWS = Gauge("fraud_labelled_rows", "Labelled predictions in the performance window")
LABELLED_FRAUDS = Gauge("fraud_labelled_frauds", "Confirmed frauds in the performance window")
LABELLED_RECALL = Gauge("fraud_labelled_recall", "Recall on labelled predictions")
LABELLED_PRECISION = Gauge("fraud_labelled_precision", "Precision on labelled predictions")
LABELLED_PR_AUC = Gauge("fraud_labelled_pr_auc", "PR AUC on labelled predictions")
LAST_RUN = Gauge("fraud_monitor_last_run_timestamp_seconds", "When the monitor last completed")
RUN_ERRORS = Counter("fraud_monitor_errors_total", "Monitor cycles that failed")


@dataclass
class MonitorConfig:
    drift_window: int = 5000        # matches the calibration window size
    rate_window: int = 20000        # flag rate needs more rows: ~0.1% of traffic is flagged
    label_window: int = 20000
    min_rows: int = 1000
    min_labelled_frauds: int = 10   # below this, recall is too noisy to publish


def load_reference(model_dir: Path) -> tuple[pd.DataFrame, dict, float]:
    reference = pd.read_csv(model_dir / "reference.csv.gz")
    thresholds = json.loads((model_dir / "drift_thresholds.json").read_text("utf-8"))["thresholds"]
    threshold = json.loads((model_dir / "metadata.json").read_text("utf-8"))["threshold"]
    return reference, thresholds, float(threshold)


def run_cycle(
    store: PredictionStore,
    reference: pd.DataFrame,
    psi_thresholds: dict[str, float],
    threshold: float,
    cfg: MonitorConfig,
) -> dict:
    """One monitoring pass. Returns a report and updates the gauges."""
    report: dict = {}
    REF_FLAG_RATE.set(float((reference["score"] >= threshold).mean()))

    live = store.recent_predictions(cfg.rate_window)
    if len(live) >= cfg.min_rows:
        FLAG_RATE.set(float(live["is_fraud"].mean()))
        FLAG_RATE_ROWS.set(len(live))
        window = live.head(cfg.drift_window)  # newest first
        WINDOW_ROWS.set(len(window))

        fd = feature_drift(reference, window)
        fd["threshold"] = fd["feature"].map(psi_thresholds)
        fd["drifting"] = fd["psi"] > fd["threshold"]
        for row in fd.itertuples():
            FEATURE_PSI.labels(row.feature).set(row.psi)
            FEATURE_PSI_THRESHOLD.labels(row.feature).set(row.threshold)
        FEATURES_DRIFTING.set(int(fd["drifting"].sum()))
        stuck = []
        for f in MONITORED_FEATURES:
            share = mode_share(window[f].to_numpy())
            ref_share = mode_share(reference[f].to_numpy())
            MODE_SHARE.labels(f).set(share)
            REF_MODE_SHARE.labels(f).set(ref_share)
            if share > 0.5 and ref_share < 0.1:
                stuck.append(f)

        sd = score_drift(reference["score"].to_numpy(), window["score"].to_numpy(), threshold)
        SCORE_PSI.set(sd["score_psi"])
        report.update(
            window_rows=len(window),
            stuck_features=stuck,
            flag_rate=float(live["is_fraud"].mean()),
            score_psi=sd["score_psi"],
            drifting=fd.loc[fd["drifting"], ["feature", "psi", "threshold", "ref_mean", "cur_mean"]]
            .round(4).to_dict("records"),
        )
    else:
        # Too little traffic to judge. Publish "unknown" rather than leave the last
        # values in place, which would keep an old alert firing (or hide a new one).
        for g in (FLAG_RATE, SCORE_PSI, FEATURES_DRIFTING):
            g.set(np.nan)
        for f in MONITORED_FEATURES:
            FEATURE_PSI.labels(f).set(np.nan)
            MODE_SHARE.labels(f).set(np.nan)
        WINDOW_ROWS.set(len(live))
        FLAG_RATE_ROWS.set(len(live))

    labelled = store.recent_labelled(cfg.label_window)
    frauds = int(labelled["label"].sum()) if len(labelled) else 0
    LABELLED_ROWS.set(len(labelled))
    LABELLED_FRAUDS.set(frauds)
    if frauds >= cfg.min_labelled_frauds:
        y = labelled["label"].astype(int).to_numpy()
        flagged = labelled["is_fraud"].astype(bool).to_numpy()
        recall = float(flagged[y == 1].mean())
        precision = float(y[flagged].mean()) if flagged.any() else 0.0
        LABELLED_RECALL.set(recall)
        LABELLED_PRECISION.set(precision)
        LABELLED_PR_AUC.set(float(average_precision_score(y, labelled["score"])))
        report.update(labelled_rows=len(labelled), labelled_frauds=frauds, recall=recall, precision=precision)
    else:
        # Publish "unknown", never a stale or noisy number.
        for g in (LABELLED_RECALL, LABELLED_PRECISION, LABELLED_PR_AUC):
            g.set(np.nan)

    LAST_RUN.set(time.time())
    return report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    model_dir = Path(os.environ.get("MODEL_DIR", "serving_model"))
    interval = float(os.environ.get("MONITOR_INTERVAL_SECONDS", "15"))
    store = PredictionStore(os.environ["DATABASE_URL"])
    reference, psi_thresholds, threshold = load_reference(model_dir)
    for f in MONITORED_FEATURES:
        FEATURE_PSI_THRESHOLD.labels(f).set(psi_thresholds[f])
    cfg = MonitorConfig()

    start_http_server(int(os.environ.get("MONITOR_PORT", "9100")))
    log.info("monitor started, interval %.0fs", interval)
    while True:
        try:
            report = run_cycle(store, reference, psi_thresholds, threshold, cfg)
            if report.get("drifting"):
                log.warning("feature drift: %s", json.dumps(report["drifting"]))
            log.info(
                "window=%s flag_rate=%s score_psi=%s recall=%s",
                report.get("window_rows"),
                report.get("flag_rate"),
                report.get("score_psi"),
                report.get("recall"),
            )
        except Exception:
            RUN_ERRORS.inc()
            log.exception("monitor cycle failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
