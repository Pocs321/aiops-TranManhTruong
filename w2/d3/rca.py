"""Layer 2 — root-cause analysis (D2).

Graph-based RCA with optional LLM enrichment. The heuristic: within a cluster of
alerting services, the most likely root cause is the service that the *most other
alerting services depend on* (reachable via ``depends_on`` edges within
``max_hop``), weighted by alert severity. Infrastructure/leaf dependencies that
many app services lean on therefore bubble to the top.

The real D2 deliverable lives in ``w2/d2/``; this is the wired-in stub the
platform layer (D3) depends on. Public surface: ``rca_combined`` (pure graph
ranking) and ``run_rca`` (full result incl. LLM enrichment + history retrieval).
"""
from __future__ import annotations

from typing import Any

import networkx as nx

from enrich import llm_enrich

# Severity → weight. Unknown severities fall back to a small positive weight.
_SEVERITY_WEIGHT = {"crit": 4.0, "critical": 4.0, "error": 3.0, "warn": 2.0,
                    "warning": 2.0, "info": 1.0}


def _max_sev_weight(alerts: list[dict]) -> float:
    if not alerts:
        return 0.0
    return max(_SEVERITY_WEIGHT.get(str(a.get("severity", "")).lower(), 1.0)
               for a in alerts)


def rca_combined(cluster: dict, graph: nx.DiGraph, max_hop: int = 3) -> list[tuple[str, float]]:
    """Rank candidate root-cause services for a cluster.

    Returns ``[(service, normalized_score), ...]`` sorted descending. Score for
    service *s* = its own severity weight + 1.5 × (severity-weighted count of
    other alerting services that depend on *s* within ``max_hop``).
    """
    alerts_by_svc: dict[str, list[dict]] = {}
    for a in cluster.get("_alerts", []):
        alerts_by_svc.setdefault(a["service"], []).append(a)
    services = cluster.get("services", list(alerts_by_svc))

    scores: dict[str, float] = {}
    for s in services:
        dependents = 0.0
        for t in services:
            if t == s or t not in graph or s not in graph:
                continue
            try:
                dist = nx.shortest_path_length(graph, t, s)  # t depends_on ... s
            except nx.NetworkXNoPath:
                continue
            if dist <= max_hop:
                dependents += _max_sev_weight(alerts_by_svc.get(t, []))
        own = _max_sev_weight(alerts_by_svc.get(s, []))
        scores[s] = own + 1.5 * dependents

    total = sum(scores.values()) or 1.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [(s, round(sc / total, 3)) for s, sc in ranked]


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_similar_incidents(cluster: dict, history: list[dict], top_k: int = 3) -> list[dict]:
    """Retrieve past incidents similar to this cluster.

    Similarity = Jaccard over service sets, plus a +0.2 bonus when the historical
    root cause is one of the cluster's services. Clamped to [0, 1].
    """
    cluster_services = set(cluster.get("services", []))
    scored: list[dict] = []
    for inc in history:
        sim = _jaccard(cluster_services, set(inc.get("services", [])))
        if inc.get("root_cause") in cluster_services:
            sim = min(1.0, sim + 0.2)
        if sim <= 0:
            continue
        scored.append({
            "id": inc["id"],
            "similarity": round(sim, 3),
            "summary": inc.get("summary", ""),
            "actions": inc.get("actions", []),
        })
    scored.sort(key=lambda x: x["similarity"], reverse=True)
    return scored[:top_k]


def _default_actions(root_cause: str, similar: list[dict]) -> list[str]:
    """Suggest remediation actions: reuse the closest past incident's playbook
    when available, then generic graph-driven steps."""
    actions: list[str] = []
    if similar and similar[0]["actions"]:
        actions.extend(similar[0]["actions"])
    actions.extend([
        f"Inspect {root_cause} dashboards and recent deploys",
        f"Check {root_cause} downstream dependencies for saturation",
    ])
    # De-dupe while preserving order, cap at 4.
    seen: set[str] = set()
    deduped = [a for a in actions if not (a in seen or seen.add(a))]
    return deduped[:4]


def run_rca(cluster: dict, graph: nx.DiGraph, history: list[dict]) -> dict:
    """Full RCA for one cluster: graph ranking → history retrieval → optional
    LLM enrichment. Always returns a populated dict; never raises on LLM error.
    """
    candidates = rca_combined(cluster, graph)
    top_svc, top_conf = candidates[0] if candidates else ("unknown", 0.0)
    similar = find_similar_incidents(cluster, history)

    dependents = [s for s in cluster.get("services", []) if s != top_svc]
    reasoning = (
        f"{top_svc} is the most depended-on service among the "
        f"{len(cluster.get('services', []))} alerting services "
        f"({', '.join(dependents) or 'none'}); graph topology + severity rank it highest."
    )

    result = {
        "root_cause": top_svc,
        "confidence": top_conf,
        "reasoning": reasoning,
        "method": "graph",
        "candidates": candidates[:5],
        "actions": _default_actions(top_svc, similar),
        "similar_incidents": similar,
    }

    # LLM enrichment is best-effort: skip when graph is already very confident,
    # and fall back silently if the provider is down.
    if top_conf < 0.9:
        enriched = llm_enrich(cluster, result)
        if enriched is not None:
            result.update({
                "root_cause": enriched["root_cause"],
                "confidence": enriched["confidence"],
                "reasoning": enriched["reasoning"] or result["reasoning"],
                "actions": enriched["actions"] or result["actions"],
                "method": "graph+llm",
            })

    return result
