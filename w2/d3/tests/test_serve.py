"""Integration tests — HTTP endpoints via FastAPI TestClient.

The LLM path is disabled (AIOPS_USE_LLM=false) so these run offline and fast;
RCA falls back to graph-only, which is deterministic.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("AIOPS_USE_LLM", "false")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from serve import app  # noqa: E402

client = TestClient(app)


def _alert(id_, ts, service, severity="crit", metric="latency_p99_ms",
           value=1840.0, threshold=800.0):
    return {"id": id_, "ts": ts, "service": service, "metric": metric,
            "severity": severity, "value": value, "threshold": threshold}


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_ok():
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["graph"] is True
    assert body["checks"]["history"] is True


def test_version():
    r = client.get("/version")
    assert r.status_code == 200
    assert r.json()["app"]


def test_incident_empty_alerts_is_400():
    r = client.post("/incident", json={"alerts": []})
    assert r.status_code == 400


def test_incident_malformed_is_422_not_500():
    # Missing required fields (ts, service, ...) → Pydantic validation error.
    r = client.post("/incident", json={"alerts": [{"id": "a-1"}]})
    assert r.status_code == 422


def test_incident_happy_path():
    payload = {"alerts": [
        _alert("a-1", "2026-06-12T09:42:01Z", "payment-svc"),
        _alert("a-2", "2026-06-12T09:42:08Z", "order-svc", "error"),
        _alert("a-3", "2026-06-12T09:42:15Z", "db-primary"),
    ]}
    r = client.post("/incident", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert "clusters" in body and len(body["clusters"]) >= 1
    assert "root_cause" in body
    # db-primary is depended on by both app services → expected root cause.
    assert body["root_cause"]["service"] == "db-primary"
    assert "recommended_actions" in body
    assert "similar_incidents" in body
    assert r.headers.get("X-Response-Time-Ms") is not None


def test_incident_single_alert_does_not_500():
    payload = {"alerts": [_alert("a-1", "2026-06-12T09:42:01Z", "payment-svc")]}
    r = client.post("/incident", json=payload)
    assert r.status_code == 200
