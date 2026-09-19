import os
from unittest import mock

from fraud_mlops.audit.anchor import SealAnchor
from fraud_mlops.dburl import admin_database_url, app_database_url
from fraud_mlops.monitoring.cloudwatch import metrics_from_report


def names(data):
    return {d["MetricName"]: d["Value"] for d in data}


def test_incident_report_maps_to_alarm_metrics():
    m = names(metrics_from_report({
        "window_rows": 5000, "flag_rate_rows": 20000, "drifting": [{"feature": "V14"}],
        "stuck_features": ["V14"], "score_psi": 0.01, "flag_rate": 0.0001,
        "reference_flag_rate": 0.002, "recall": 0.05, "precision": 1.0, "labelled_frauds": 26,
    }))
    assert m["StuckFeatures"] == 1 and m["FeaturesDrifting"] == 1
    assert abs(m["FlagRateRatio"] - 0.05) < 1e-9 and m["LabelledRecall"] == 0.05


def test_unknown_values_are_not_published():
    m = names(metrics_from_report({"reference_flag_rate": 0.002}))
    assert set(m) == {"MonitorHeartbeat"}


def test_flag_ratio_needs_a_full_window():
    m = names(metrics_from_report({"window_rows": 1000, "flag_rate_rows": 1000, "flag_rate": 0.0,
                                   "reference_flag_rate": 0.002, "score_psi": 0.01}))
    assert "FlagRateRatio" not in m


def test_db_url_escapes_generated_passwords():
    env = {"DB_HOST": "db.example", "DB_USER": "fraud_app", "DB_PASSWORD": "p@ss:/w#rd",
           "DB_ADMIN_USER": "admin", "DB_ADMIN_PASSWORD": "x"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert app_database_url() == ("postgresql+psycopg://fraud_app:p%40ss%3A%2Fw%23rd@db.example:5432/"
                                      "fraud?sslmode=require")
        assert admin_database_url().startswith("postgresql+psycopg://admin:x@")


def test_anchor_detects_rewritten_chain():
    stored = {}

    class FakeS3:
        def put_object(self, Bucket, Key, Body, **kw):
            stored[Key] = Body

        def get_paginator(self, _):
            class P:
                def paginate(self, **kw):
                    return [{"Contents": [{"Key": k} for k in stored]}]
            return P()

        def get_object(self, Bucket, Key):
            import io
            return {"Body": io.BytesIO(stored[Key])}

    anchor = SealAnchor("b", client=FakeS3())
    seals = [{"id": 1, "first_id": 1, "last_id": 10, "seal_hash": "aaa"}]
    anchor.anchor_missing(seals, set())
    assert anchor.compare(seals) == []
    assert "differs from its write once copy" in anchor.compare([dict(seals[0], seal_hash="bbb")])[0]
