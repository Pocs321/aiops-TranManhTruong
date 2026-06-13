"""Layer 1 — alert correlation (D1).

Self-contained, functional implementation for the W2-D3 serving lab. It groups
raw alerts into incident *clusters* using two independent signals:

  1. Temporal sessionization — alerts are split into sessions whenever the
     inter-arrival gap exceeds ``gap_sec``.
  2. Topological proximity — within a session, services whose dependency-graph
     distance is ``<= max_hop`` are merged into one cluster (union-find).

The real D1 deliverable lives in ``w2/d1/``; this module is the wired-in stub
the platform layer (D3) depends on. The public surface — ``fingerprint``,
``session_groups``, ``build_graph_from_json``, ``correlate`` — matches the names
referenced by the lab spec.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import networkx as nx


def parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z`` (UTC)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fingerprint(alert: dict[str, Any]) -> str:
    """Stable dedup key for an alert.

    Deliberately excludes the timestamp and the instantaneous value so that two
    firings of the *same* condition collapse to one fingerprint.
    """
    return "|".join(str(alert.get(k, "")) for k in ("service", "metric", "severity"))


def session_groups(alerts: list[dict], gap_sec: int = 120) -> list[list[dict]]:
    """Split alerts into time-contiguous sessions.

    A new session starts whenever the gap to the previous (chronologically
    sorted) alert exceeds ``gap_sec`` seconds.
    """
    if not alerts:
        return []
    ordered = sorted(alerts, key=lambda a: parse_ts(a["ts"]))
    sessions: list[list[dict]] = [[ordered[0]]]
    for prev, cur in zip(ordered, ordered[1:]):
        gap = (parse_ts(cur["ts"]) - parse_ts(prev["ts"])).total_seconds()
        if gap > gap_sec:
            sessions.append([cur])
        else:
            sessions[-1].append(cur)
    return sessions


def build_graph_from_json(path: str | Path) -> nx.DiGraph:
    """Load the service dependency graph.

    Edge ``A -> B`` means *A depends on B* (A calls B), so a fault in B can
    surface as alerts on A.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    g = nx.DiGraph()
    for svc in data.get("services", []):
        name = svc["name"]
        g.add_node(name, tier=svc.get("tier", "unknown"))
        for dep in svc.get("depends_on", []):
            g.add_edge(name, dep)
    return g


class _UnionFind:
    """Minimal union-find over a fixed set of service names."""

    def __init__(self, items: list[str]) -> None:
        self.parent = {x: x for x in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path compression
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def correlate(
    alerts: list[dict],
    graph: nx.DiGraph,
    gap_sec: int = 120,
    max_hop: int = 2,
) -> list[dict]:
    """Correlate raw alerts into incident clusters.

    Returns a list of cluster dicts shaped for the API response, plus two
    internal keys (``alert_ids`` and ``_alerts``) the RCA layer consumes:

        {
          "cluster_id": "clu-001",
          "alert_count": 5,
          "services": ["auth-svc", "db-primary", "payment-svc"],
          "time_range": ["2026-06-12T09:42:01+00:00", "2026-06-12T09:43:10+00:00"],
          "alert_ids": ["a-1", ...],
          "_alerts": [ {raw alert}, ... ],
        }
    """
    undirected = graph.to_undirected()
    clusters: list[dict] = []
    counter = 0

    for session in session_groups(alerts, gap_sec=gap_sec):
        # Bucket the session's alerts by service.
        svc_alerts: dict[str, list[dict]] = {}
        for a in session:
            svc_alerts.setdefault(a["service"], []).append(a)
        services = list(svc_alerts)

        # Union services that sit within max_hop of each other on the graph.
        uf = _UnionFind(services)
        for i, a in enumerate(services):
            for b in services[i + 1:]:
                if a in undirected and b in undirected:
                    try:
                        dist = nx.shortest_path_length(undirected, a, b)
                    except nx.NetworkXNoPath:
                        continue
                    if dist <= max_hop:
                        uf.union(a, b)

        # Collect connected components → clusters.
        components: dict[str, list[str]] = {}
        for s in services:
            components.setdefault(uf.find(s), []).append(s)

        for comp_services in components.values():
            comp_alerts = [a for s in comp_services for a in svc_alerts[s]]
            ts_sorted = sorted(parse_ts(a["ts"]) for a in comp_alerts)
            counter += 1
            clusters.append(
                {
                    "cluster_id": f"clu-{counter:03d}",
                    "alert_count": len(comp_alerts),
                    "services": sorted(set(comp_services)),
                    "time_range": [ts_sorted[0].isoformat(), ts_sorted[-1].isoformat()],
                    "alert_ids": [a["id"] for a in comp_alerts],
                    "_alerts": comp_alerts,
                }
            )

    return clusters
