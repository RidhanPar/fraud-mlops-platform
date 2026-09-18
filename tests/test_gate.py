from fraud_mlops.gate import decide

CFG = {
    "primary_metric": "pr_auc",
    "min_improvement": 0.005,
    "max_recall_drop": 0.02,
    "min_pr_auc_floor": 0.70,
}


def m(pr_auc, recall):
    return {"pr_auc": pr_auc, "recall": recall}


def test_first_model_promoted_when_above_floor():
    assert decide(m(0.78, 0.7), None, CFG).promote


def test_first_model_rejected_below_floor():
    assert not decide(m(0.50, 0.7), None, CFG).promote


def test_equal_model_is_not_promoted():
    assert not decide(m(0.78, 0.7), m(0.78, 0.7), CFG).promote


def test_worse_model_is_not_promoted():
    assert not decide(m(0.74, 0.73), m(0.78, 0.7), CFG).promote


def test_better_pr_auc_but_recall_collapse_is_blocked():
    d = decide(m(0.80, 0.60), m(0.78, 0.70), CFG)
    assert not d.promote and "recall" in d.reason


def test_clear_improvement_is_promoted():
    assert decide(m(0.80, 0.71), m(0.78, 0.70), CFG).promote
