from fraud_mlops.retrain_policy import Signals, decide


def s(**kw):
    base = dict(window="24h", recall_max=0.7, labelled_frauds_min=30, reject_rate=0.0,
                score_psi_min=0.01, flag_rate_ratio=0.5, model_age_days=3)
    return Signals(**(base | kw))


def test_healthy_means_no_action():
    assert decide(s()).action == "NO_ACTION"


def test_upstream_fault_blocks_retraining_even_when_recall_collapses():
    d = decide(s(stuck_features=["V14"], recall_max=0.03))
    assert d.action == "FIX_DATA"
    assert any("symptom" in r for r in d.reasons)


def test_broken_input_contract_is_a_data_fault():
    assert decide(s(reject_rate=0.2)).action == "FIX_DATA"


def test_decay_on_clean_inputs_means_retrain():
    assert decide(s(recall_max=0.35)).action == "RETRAIN_NOW"


def test_low_recall_on_too_few_labels_is_not_trusted():
    assert decide(s(recall_max=0.2, labelled_frauds_min=3)).action == "NO_ACTION"


def test_sustained_drift_prepares_a_candidate():
    assert decide(s(drifting_features=["V3"])).action == "RETRAIN_CANDIDATE"


def test_old_model_gets_scheduled_refresh():
    assert decide(s(model_age_days=45)).action == "SCHEDULED_RETRAIN"
