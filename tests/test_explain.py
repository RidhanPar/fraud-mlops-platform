import math

import numpy as np
import pandas as pd
import pytest

from fraud_mlops.config import load_params
from fraud_mlops.explain import explain
from fraud_mlops.features import RAW_COLUMNS
from fraud_mlops.train import build_pipeline
from tests.conftest import make_transactions


@pytest.fixture(scope="module")
def model():
    df = make_transactions()
    cfg = load_params()["model"] | {"n_estimators": 30, "max_depth": 3}
    return build_pipeline(cfg, 42).fit(df[RAW_COLUMNS], df["Class"])


@pytest.fixture
def row():
    r = make_transactions(1, seed=9).iloc[0]
    return {c: float(r[c]) for c in RAW_COLUMNS}


def test_contributions_add_up_to_the_model_score(model, row):
    e = explain(model, row, top=31)
    p = model.predict_proba(pd.DataFrame([row], columns=RAW_COLUMNS))[0, 1]
    margin = e["base_value"] + sum(c["contribution"] for c in e["top_contributions"])
    assert 1 / (1 + math.exp(-margin)) == pytest.approx(p, rel=1e-5)
    assert e["score_from_contributions"] == pytest.approx(p, rel=1e-5)


def test_matches_the_shap_library_exactly(model, row):
    shap = pytest.importorskip("shap")
    X = model.named_steps["features"].transform(pd.DataFrame([row], columns=RAW_COLUMNS))
    reference = shap.TreeExplainer(model.named_steps["xgb"]).shap_values(X)[0]
    ours = {c["feature"]: c["contribution"] for c in explain(model, row, top=31)["top_contributions"]}
    assert np.allclose([ours[f] for f in X.columns], reference, atol=1e-5)


def test_ranked_by_absolute_contribution(model, row):
    top = explain(model, row, top=5)["top_contributions"]
    mags = [abs(c["contribution"]) for c in top]
    assert mags == sorted(mags, reverse=True)
