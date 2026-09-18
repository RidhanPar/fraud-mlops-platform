import numpy as np
import pandas as pd
import pytest

from fraud_mlops.api.store import PredictionStore
from fraud_mlops.features import RAW_COLUMNS
from fraud_mlops.monitoring.drift import MONITORED_FEATURES, feature_drift, psi
from fraud_mlops.monitoring.monitor import MonitorConfig, run_cycle
from tests.conftest import make_transactions


def test_psi_near_zero_for_same_distribution_and_large_for_shift():
    rng = np.random.default_rng(0)
    ref = rng.normal(size=20_000)
    assert psi(ref, rng.normal(size=5_000)) < 0.02
    assert psi(ref, rng.normal(loc=1.0, size=5_000)) > 0.25


def test_zero_filled_feature_is_the_top_drifter():
    ref = make_transactions(8000, seed=1)
    live = make_transactions(3000, seed=2)
    live["V14"] = 0.0
    top = feature_drift(ref, live).iloc[0]
    assert top["feature"] == "V14" and top["psi"] > 1


@pytest.fixture
def store(tmp_path):
    return PredictionStore(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")


def _log(store, df, scores, prefix):
    ids = [f"{prefix}-{i}" for i in range(len(df))]
    store.record_predictions("r", "1", 0.5, df[RAW_COLUMNS].to_dict("records"), ids, scores)
    return ids


def test_run_cycle_flags_drift_and_recall(store):
    ref = make_transactions(8000, seed=1).assign(score=0.01)
    thresholds = {f: 0.25 for f in MONITORED_FEATURES}
    cfg = MonitorConfig(drift_window=2000, rate_window=2000, label_window=2000,
                        min_rows=500, min_labelled_frauds=5)

    live = make_transactions(2000, seed=3)
    live["V14"] = 0.0
    # The model "misses" every fraud: all scores below the 0.5 threshold.
    ids = _log(store, live, np.full(len(live), 0.01), "tx")
    store.record_labels([(i, bool(y)) for i, y in zip(ids, live["Class"])])

    report = run_cycle(store, ref, thresholds, 0.5, cfg)
    assert "V14" in {d["feature"] for d in report["drifting"]}
    assert report["recall"] == 0.0


def test_run_cycle_publishes_nothing_on_too_few_labels(store):
    ref = make_transactions(4000, seed=1).assign(score=0.01)
    live = make_transactions(600, seed=4)
    _log(store, live, np.full(len(live), 0.01), "tx")
    report = run_cycle(store, ref, {f: 0.25 for f in MONITORED_FEATURES}, 0.5,
                       MonitorConfig(min_rows=500))
    assert "recall" not in report


def test_drift_gauges_reset_when_window_too_small(store):
    from fraud_mlops.monitoring import monitor

    ref = make_transactions(4000, seed=1).assign(score=0.01)
    live = make_transactions(1200, seed=5)
    live["V14"] = 0.0
    _log(store, live, np.full(len(live), 0.01), "a")
    cfg = MonitorConfig(min_rows=1000, drift_window=1000, rate_window=1000)
    run_cycle(store, ref, {f: 0.25 for f in MONITORED_FEATURES}, 0.5, cfg)
    assert monitor.FEATURE_PSI.labels("V14")._value.get() > 1

    small = PredictionStore(f"sqlite:///{store.engine.url.database}.empty")
    run_cycle(small, ref, {f: 0.25 for f in MONITORED_FEATURES}, 0.5, cfg)
    assert np.isnan(monitor.FEATURE_PSI.labels("V14")._value.get())
