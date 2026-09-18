import numpy as np
import pandas as pd
import pytest

from fraud_mlops.features import PCA_COLUMNS


def make_transactions(n: int = 4000, fraud_rate: float = 0.05, seed: int = 0) -> pd.DataFrame:
    """Synthetic rows with the Kaggle schema and a learnable fraud signal in V1/V2."""
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < fraud_rate).astype(int)
    df = pd.DataFrame(rng.normal(size=(n, len(PCA_COLUMNS))), columns=PCA_COLUMNS)
    df["V1"] -= 3 * y
    df["V2"] += 2 * y
    df["Time"] = np.sort(rng.uniform(0, 172_800, n))
    df["Amount"] = rng.gamma(2.0, 40.0, n)
    df["Class"] = y
    return df


@pytest.fixture
def transactions() -> pd.DataFrame:
    return make_transactions()
