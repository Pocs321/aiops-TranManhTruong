"""Glue layer — assemble L1 (correlate) → L2 (RCA) → L3 (LLM) into one call.

State (the dependency graph and incident history) is loaded **once** at import
time and treated as read-only, so the pipeline itself is stateless per-request:
any worker / thread can serve any request without coordination. See DESIGN.md
for the concurrency trade-off this buys.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from correlate import build_graph_from_json, correlate
from rca import run_rca

_DATASET = Path(__file__).parent / "lab" / "dataset"

# --- Load-once module state ------------------------------------------------
GRAPH = build_graph_from_json(_DATASET / "services.json")
HISTORY: list[dict] = json.loads(
    (_DATASET / "incidents_history.json").read_text(encoding="utf-8")
)["incidents"]

# Pipeline configuration (surfaced by /version).
CONFIG: dict[str, Any] = {
    "correlate_gap_sec": 120,
    "correlate_max_hop": 2,
    "rca_method": "graph+llm",
}


def _empty_response(reason: str) -> dict:
    return {
        "clusters": [],
        "root_cause": {"service": "unknown", "confidence": 0.0, "reasoning": reason},
        "recommended_actions": [],
        "similar_incidents": [],
    }


def process_batch(alerts: list[dict]) -> dict:
    """Run the full pipeline over a batch of alerts.

    Returns a dict matching the ``IncidentResponse`` schema in ``serve.py``.
    """
    clusters = correlate(
        alerts,
        GRAPH,
        gap_sec=CONFIG["correlate_gap_sec"],
        max_hop=CONFIG["correlate_max_hop"],
    )
    if not clusters:
        return _empty_response("No clusters formed from input alerts")

    # The largest cluster is treated as the primary incident for RCA.
    primary = max(clusters, key=lambda c: c["alert_count"])
    rca = run_rca(primary, GRAPH, HISTORY)

    return {
        "clusters": [
            {
                "cluster_id": c["cluster_id"],
                "alert_count": c["alert_count"],
                "services": c["services"],
                "time_range": c["time_range"],
            }
            for c in clusters
        ],
        "root_cause": {
            "service": rca["root_cause"],
            "confidence": rca["confidence"],
            "reasoning": rca["reasoning"],
        },
        "recommended_actions": rca["actions"],
        "similar_incidents": [
            {"id": s["id"], "similarity": s["similarity"], "summary": s["summary"]}
            for s in rca["similar_incidents"]
        ],
    }
