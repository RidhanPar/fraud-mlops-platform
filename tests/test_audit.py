"""Tamper evidence. SQLite has no triggers, so a direct UPDATE or DELETE here plays
the part of an admin who bypassed them; the verifier must still catch it."""

import numpy as np
import pytest
from sqlalchemy import text

from fraud_mlops.api.store import PredictionStore
from fraud_mlops.audit import seal, verify
from fraud_mlops.audit.hashing import record_hash
from fraud_mlops.features import RAW_COLUMNS
from tests.conftest import make_transactions


@pytest.fixture
def store(tmp_path):
    s = PredictionStore(f"sqlite:///{(tmp_path / 'audit.db').as_posix()}")
    s.create_schema()
    df = make_transactions(300)
    for i in range(3):
        chunk = df.iloc[i * 100:(i + 1) * 100]
        s.record_predictions(f"req-{i}", "t", "1", "a" * 64, 0.5, chunk[RAW_COLUMNS].to_dict("records"),
                             [f"tx-{i}-{j}" for j in range(100)], np.linspace(0, 1, 100))
    return s


def _seal_all(s):
    while seal.seal_once(s, max_rows=120):
        pass


def test_record_hash_round_trips_through_the_database(store):
    assert all(record_hash(r) == r["record_hash"] for r in store.iter_predictions())


def test_intact_trail_verifies(store):
    _seal_all(store)
    r = verify.verify(store)
    assert r.ok and r.rows_checked == 300 and r.seals_checked == 3 and r.unsealed_rows == 0


def test_seal_cuts_off_by_id_and_later_rows_stay_unsealed(store):
    first = seal.seal_once(store)
    assert (first["first_id"], first["last_id"], first["row_count"]) == (1, 300, 300)
    df = make_transactions(5)
    store.record_predictions("late", "t", "1", "a" * 64, 0.5, df[RAW_COLUMNS].to_dict("records"),
                             [None] * 5, np.zeros(5))
    r = verify.verify(store)
    assert r.ok and r.unsealed_rows == 5
    assert seal.seal_once(store)["first_id"] == 301


def test_edited_score_is_detected(store):
    _seal_all(store)
    with store.engine.begin() as c:
        c.execute(text("UPDATE predictions SET score = 0.0 WHERE id = 150"))
    problems = verify.verify(store).problems
    assert any("prediction 150" in p for p in problems)
    assert any("seal 2" in p for p in problems)


def test_edit_with_recomputed_row_hash_is_still_caught_by_the_seal(store):
    _seal_all(store)
    row = next(r for r in store.iter_predictions() if r["id"] == 42)
    row["score"] = 0.0
    with store.engine.begin() as c:
        c.execute(text("UPDATE predictions SET score = 0.0, record_hash = :h WHERE id = 42"),
                  {"h": record_hash(row)})
    assert verify.verify(store).problems == ["seal 1: rows in ids 1..120 changed since sealing"]


def test_deleted_row_is_detected(store):
    _seal_all(store)
    with store.engine.begin() as c:
        c.execute(text("DELETE FROM predictions WHERE id = 200"))
    assert any("expected 120 rows" in p for p in verify.verify(store).problems)


def test_rewritten_seal_breaks_the_chain(store):
    _seal_all(store)
    with store.engine.begin() as c:
        c.execute(text("UPDATE audit_seals SET rows_digest = 'x' WHERE id = 1"))
    assert any("seal 1: seal hash" in p for p in verify.verify(store).problems)
