import pytest

from fraud_mlops import data
from fraud_mlops.features import MODEL_COLUMNS, add_time_features


def test_time_split_is_ordered_and_disjoint(transactions):
    s = data.time_split(transactions.sample(frac=1, random_state=1), 0.6, 0.2)
    assert len(s.train) + len(s.valid) + len(s.holdout) == len(transactions)
    assert s.train["Time"].max() <= s.valid["Time"].min()
    assert s.valid["Time"].max() <= s.holdout["Time"].min()


def test_validate_rejects_missing_column(transactions):
    with pytest.raises(ValueError, match="Missing columns"):
        data.validate(transactions.drop(columns=["V7"]))


def test_validate_rejects_negative_amount(transactions):
    transactions.loc[0, "Amount"] = -1
    with pytest.raises(ValueError, match="Negative"):
        data.validate(transactions)


def test_hour_of_day_wraps_each_day(transactions):
    transactions.loc[0, "Time"] = 86_400 + 3_600 * 5
    out = add_time_features(transactions)
    assert list(out.columns) == MODEL_COLUMNS
    assert out.loc[0, "hour_of_day"] == pytest.approx(5.0)
    assert out["hour_of_day"].between(0, 24).all()
