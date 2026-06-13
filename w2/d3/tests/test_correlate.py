"""Unit tests — pure correlation functions (no network, no LLM)."""
import sys
from pathlib import Path

# Make the project root importable when pytest is run from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from correlate import build_graph_from_json, correlate, fingerprint, session_groups  # noqa: E402

_DATASET = Path(__file__).resolve().parent.parent / "lab" / "dataset"


def _alert(id_, ts, service, severity="warn", metric="m", value=1.0, threshold=0.0):
    return {"id": id_, "ts": ts, "service": service, "metric": metric,
            "severity": severity, "value": value, "threshold": threshold}


def test_fingerprint_excludes_timestamp_and_value():
    a = _alert("1", "2026-06-12T09:42:01Z", "payment-svc", "crit", "latency", 1840)
    b = _alert("2", "2026-06-12T09:42:30Z", "payment-svc", "crit", "latency", 1900)
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_on_service():
    a = _alert("1", "2026-06-12T09:42:01Z", "payment-svc")
    b = _alert("2", "2026-06-12T09:42:01Z", "order-svc")
    assert fingerprint(a) != fingerprint(b)


def test_session_split_on_gap():
    alerts = [
        _alert("1", "2026-06-12T09:42:00Z", "a"),
        _alert("2", "2026-06-12T09:42:30Z", "a"),
        _alert("3", "2026-06-12T09:50:00Z", "a"),  # 7.5 min gap → new session
    ]
    groups = session_groups(alerts, gap_sec=120)
    assert len(groups) == 2
    assert [len(g) for g in groups] == [2, 1]


def test_session_groups_empty():
    assert session_groups([], gap_sec=120) == []


def test_correlate_merges_topologically_close_services():
    graph = build_graph_from_json(_DATASET / "services.json")
    # payment-svc, order-svc, auth-svc all depend on db-primary → one cluster.
    alerts = [
        _alert("1", "2026-06-12T09:42:00Z", "payment-svc", "crit"),
        _alert("2", "2026-06-12T09:42:10Z", "order-svc", "error"),
        _alert("3", "2026-06-12T09:42:20Z", "db-primary", "crit"),
    ]
    clusters = correlate(alerts, graph, gap_sec=120, max_hop=2)
    assert len(clusters) == 1
    c = clusters[0]
    assert c["alert_count"] == 3
    assert set(c["services"]) == {"payment-svc", "order-svc", "db-primary"}
    assert len(c["time_range"]) == 2


def test_correlate_splits_unrelated_sessions():
    graph = build_graph_from_json(_DATASET / "services.json")
    alerts = [
        _alert("1", "2026-06-12T09:42:00Z", "payment-svc"),
        _alert("2", "2026-06-12T10:30:00Z", "payment-svc"),  # far later → split
    ]
    clusters = correlate(alerts, graph, gap_sec=120, max_hop=2)
    assert len(clusters) == 2
