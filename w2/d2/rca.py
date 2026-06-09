"""
rca.py — W2-D2 Root Cause Analysis (graph + temporal + LLM-augmented).

Pipeline per cluster:
  1. Graph ranking      — PageRank on the dependency subgraph, blended with an
                          "earliest alert" temporal score (rca_combined).
  2. Retrieval (RAG)    — pull the top-K most similar past incidents.
  3. LLM enrichment     — ask Claude (Anthropic Messages API, JSON-schema output)
                          for {root_cause, class, confidence, actions, reasoning}.
                          Falls back to a deterministic, retrieval-grounded
                          heuristic when no ANTHROPIC_API_KEY / SDK is available,
                          so the pipeline always produces valid output.
  4. Validation         — guard against hallucinated services / classes.

A note on PageRank orientation
------------------------------
Edges are stored caller -> callee (A -> B means "A depends on B / A calls B").
Failure propagates *against* the edge, so the root cause is the most-depended-on
node — the sink of the dependency subgraph. PageRank rank accumulates at sinks,
so we run nx.pagerank on the subgraph as-is (caller -> callee); the
most-depended-on service ends up with the highest score. (Reversing the edges,
as some write-ups suggest, would instead rank the top-of-stack caller highest —
i.e. the victim. See SUBMIT.md Q2.)
"""
from __future__ import annotations

import json
import os
from typing import Optional

import networkx as nx

ALLOWED_CLASSES = {
    "connection_pool_exhaustion", "slow_query", "memory_leak", "rebalance_storm",
    "deadlock", "network_partition", "bad_deploy", "config_push", "tls_expiry",
    "ddos", "other",
}

# Default to the most capable model; RCA accuracy matters more than per-call cost
# here (one call per *cluster*, not per alert). Swap to "claude-haiku-4-5" or
# "claude-sonnet-4-6" for cheaper/faster classification if you batch many clusters.
LLM_MODEL = "claude-opus-4-8"

STORE_TYPES = {"store", "cache", "queue", "db"}


# ===========================================================================
# Loading helpers
# ===========================================================================
def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build_graph(services_doc: dict) -> nx.DiGraph:
    """Build a dependency DiGraph. Edge caller -> callee (caller depends on callee)."""
    g = nx.DiGraph()
    for svc in services_doc["services"]:
        g.add_node(svc["name"], type=svc.get("type"),
                   criticality=svc.get("criticality"), team=svc.get("team"))
    for svc in services_doc["services"]:
        for dep in svc.get("depends_on", []):
            g.add_edge(svc["name"], dep)
    return g


# ===========================================================================
# 1. Graph-based RCA
# ===========================================================================
def downstream_most(cluster_services: list[str], graph: nx.DiGraph) -> Optional[str]:
    """Pick the 'deepest' service: lowest out-degree (calls no-one), tie-break on
    highest in-degree (depended on by many) within the cluster subgraph."""
    sub = graph.subgraph([s for s in cluster_services if s in graph]).copy()
    if len(sub) == 0:
        return None
    scores = [(n, sub.in_degree(n), sub.out_degree(n)) for n in sub.nodes()]
    # smallest out_degree first, then largest in_degree
    scores.sort(key=lambda x: (x[2], -x[1]))
    return scores[0][0]


def rca_pagerank(cluster_services: list[str], graph: nx.DiGraph,
                 alpha: float = 0.85) -> dict[str, float]:
    """PageRank on the caller->callee subgraph. The most-depended-on service
    (the dependency sink) accumulates the most rank -> highest score."""
    sub = graph.subgraph([s for s in cluster_services if s in graph]).copy()
    if len(sub) == 0:
        return {}
    # Add self-loops weight 0 not needed; pagerank handles dangling nodes.
    return nx.pagerank(sub, alpha=alpha)


def cluster_alerts(cluster: dict, alerts: list[dict]) -> list[dict]:
    """Scope the alert stream to a single cluster. A service can appear in several
    clusters, so filtering by service name alone leaks alerts across clusters and
    corrupts the temporal signal — join on the cluster's own alert_ids instead."""
    ids = cluster.get("alert_ids")
    if ids:
        idset = set(ids)
        return [a for a in alerts if a.get("alert_id") in idset]
    cid = cluster.get("cluster_id")
    by_cid = [a for a in alerts if a.get("cluster_id") == cid]
    return by_cid if by_cid else alerts


def _timestamp_scores(cluster_services, alerts) -> dict[str, float]:
    """1.0 for the earliest-alerting service, 0.0 for the latest, linear between.
    `alerts` must already be scoped to this cluster (see cluster_alerts)."""
    earliest: dict[str, str] = {}
    for a in alerts:
        s = a.get("service")
        if s in cluster_services:
            ts = a.get("ts")
            if s not in earliest or ts < earliest[s]:
                earliest[s] = ts
    if not earliest:
        return {}
    ordered = sorted(earliest.items(), key=lambda kv: kv[1])
    n = len(ordered)
    return {svc: 1.0 - i / max(n - 1, 1) for i, (svc, _) in enumerate(ordered)}


def rca_combined(cluster: dict, alerts: list[dict], graph: nx.DiGraph,
                 w_graph: float = 0.6, w_time: float = 0.4) -> list[tuple[str, float]]:
    """Blend normalised PageRank (0.6) with the earliest-alert temporal score (0.4).
    Returns [(service, score), ...] sorted high -> low."""
    services = cluster["services"]
    scoped = cluster_alerts(cluster, alerts)
    pr = rca_pagerank(services, graph)
    ts = _timestamp_scores(services, scoped)

    pr_max = max(pr.values()) if pr else 0.0
    combined = []
    for svc in services:
        pr_n = (pr.get(svc, 0.0) / pr_max) if pr_max > 0 else 0.0
        t = ts.get(svc, 0.0)
        combined.append((svc, round(w_graph * pr_n + w_time * t, 4)))
    combined.sort(key=lambda x: -x[1])
    return combined


def store_culprit_check(top_service: str, cluster_services: list[str],
                        alerts: list[dict], graph: nx.DiGraph) -> dict:
    """Edge case (notes §2.5): if the top candidate is a store/cache/queue, decide
    whether it is the real culprit (alerted before the apps that depend on it) or a
    victim (alerted after). Returns a small diagnostic dict."""
    node_type = graph.nodes.get(top_service, {}).get("type")
    if node_type not in STORE_TYPES:
        return {"is_store": False}
    earliest = _earliest_per_service(cluster_services, alerts)
    store_ts = earliest.get(top_service)
    # callers within the cluster that depend on this store
    dependents = [u for u, v in graph.edges() if v == top_service and u in cluster_services]
    dep_ts = [earliest[d] for d in dependents if d in earliest]
    if store_ts is None or not dep_ts:
        return {"is_store": True, "verdict": "insufficient_data", "dependents": dependents}
    alerted_first = store_ts <= min(dep_ts)
    return {
        "is_store": True,
        "dependents": dependents,
        "store_alerted_first": alerted_first,
        "verdict": "store_is_culprit" if alerted_first else "store_is_victim",
    }


def _earliest_per_service(cluster_services, alerts) -> dict[str, str]:
    earliest: dict[str, str] = {}
    for a in alerts:
        s = a.get("service")
        if s in cluster_services:
            ts = a.get("ts")
            if s not in earliest or ts < earliest[s]:
                earliest[s] = ts
    return earliest


# ===========================================================================
# 2. Retrieval — similar past incidents (RAG, keyword similarity)
# ===========================================================================
def incident_similarity(cluster: dict, item: dict) -> float:
    score = 0.0
    if item.get("root_cause_service") in cluster["services"]:
        score += 0.4
    overlap = set(cluster["services"]) & set(item.get("services_involved", []))
    score += min(0.4, 0.2 * len(overlap))
    if cluster.get("max_severity") == item.get("severity"):
        score += 0.2
    return min(score, 1.0)


def top_k_similar(cluster: dict, history: list[dict], k: int = 3) -> list[dict]:
    scored = [(it, incident_similarity(cluster, it)) for it in history]
    scored.sort(key=lambda x: -x[1])
    return [{**it, "_similarity": round(s, 3)} for it, s in scored[:k] if s > 0.2]


# ===========================================================================
# 3. LLM-augmented RCA (Anthropic Claude, JSON-schema structured output)
# ===========================================================================
RCA_PROMPT_TEMPLATE = """\
You are a senior SRE diagnosing a production incident at GeekShop e-commerce.

# Cluster
Cluster ID: {cluster_id}
Time range: {time_range}
Services with alerts: {services}
Max severity: {max_severity}

Top root-cause candidates (graph RCA, ranked):
{ranked_candidates}

# Service dependencies inside this cluster (A -> B means A calls/depends-on B)
{topology_block}

# Similar past incidents (retrieved by similarity)
{similar_incidents_block}

# Task
Pick the single most likely root_cause (must be one of the cluster services),
classify it, give a confidence in [0,1], suggest 1-3 ordered actions, and explain
in 2-3 sentences why this candidate over the others. Cite the relevant past
incident IDs in similar_incidents.
"""

# JSON-schema for structured output. Structured outputs support enum but NOT
# numeric/length constraints, so we keep the schema flat and validate ranges
# ourselves in validate_llm_output().
RCA_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "class": {"type": "string", "enum": sorted(ALLOWED_CLASSES)},
        "confidence": {"type": "number"},
        "actions": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
        "similar_incidents": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["root_cause", "class", "confidence", "actions", "reasoning", "similar_incidents"],
    "additionalProperties": False,
}


def llm_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except Exception:
        return False


def _format_prompt(cluster, candidates, similar, graph) -> str:
    sub = graph.subgraph([s for s in cluster["services"] if s in graph])
    edges = "\n".join(f"  {u} -> {v}" for u, v in sub.edges()) or "  (no intra-cluster edges)"
    ranked = "\n".join(f"  {i+1}. {svc}  (score {score:.2f})"
                       for i, (svc, score) in enumerate(candidates[:5]))
    sim = "\n".join(
        f"- {it['id']}: root_cause={it['root_cause_service']} ({it['root_cause_class']}) "
        f"| {it['summary']} | remediation: {it['remediation']}"
        for it in similar
    ) or "(no similar incidents found)"
    return RCA_PROMPT_TEMPLATE.format(
        cluster_id=cluster["cluster_id"],
        time_range=" to ".join(cluster.get("time_range", ["?", "?"])),
        services=", ".join(cluster["services"]),
        max_severity=cluster.get("max_severity", "?"),
        ranked_candidates=ranked,
        topology_block=edges,
        similar_incidents_block=sim,
    )


def call_llm_rca(cluster, candidates, history, graph, trace_path: str = "rca_llm_trace.log") -> dict:
    """Call Claude with JSON-schema-constrained output. Raises on any failure so the
    caller can fall back to the heuristic."""
    import anthropic

    similar = top_k_similar(cluster, history, k=3)
    prompt = _format_prompt(cluster, candidates, similar, graph)

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    # NOTE: no `temperature` — it is removed on Opus 4.8 (would 400).
    # output_config.format guarantees the first text block is schema-valid JSON.
    resp = client.messages.create(
        model=LLM_MODEL,
        max_tokens=1024,
        system="You are a senior SRE. Respond ONLY with JSON matching the provided schema.",
        messages=[{"role": "user", "content": prompt}],
        output_config={"format": {"type": "json_schema", "schema": RCA_SCHEMA}},
    )
    raw = next(b.text for b in resp.content if b.type == "text")
    try:
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(f"\n--- {cluster['cluster_id']} ---\nPROMPT:\n{prompt}\nRESPONSE:\n{raw}\n")
    except OSError:
        pass
    return json.loads(raw)


def heuristic_rca(cluster, candidates, history, graph) -> dict:
    """Deterministic, retrieval-grounded fallback used when the LLM is unavailable.
    Infers class + actions from the best-matching past incident for the top
    graph candidate. This is what keeps the notebook runnable offline."""
    top_svc, top_score = candidates[0]
    similar = top_k_similar(cluster, history, k=3)

    # prefer an incident whose root_cause_service == our top candidate
    match = next((it for it in similar if it["root_cause_service"] == top_svc), None)
    if match is None:
        match = similar[0] if similar else None

    if match:
        cls = match["root_cause_class"]
        actions = [match["remediation"]]
        reasoning = (
            f"{top_svc} is the most-depended-on service in the cluster and alerts "
            f"earliest, so the cascade originates there. Past incident {match['id']} "
            f"({match['root_cause_class']}) on {match['root_cause_service']} is the "
            f"closest match (similarity {match['_similarity']})."
        )
        sims = [it["id"] for it in similar]
    else:
        cls = "other"
        actions = ["Investigate manually — no similar incident found."]
        reasoning = f"{top_svc} ranks first on graph+temporal score but has no historical analogue."
        sims = []

    return {
        "root_cause": top_svc,
        "class": cls,
        "confidence": round(min(0.9, 0.5 + top_score / 2), 3),
        "actions": actions,
        "reasoning": reasoning,
        "similar_incidents": sims,
    }


# ===========================================================================
# 4. Validation
# ===========================================================================
def validate_llm_output(parsed: dict, cluster: dict) -> tuple[bool, list[str]]:
    errors = []
    if parsed.get("root_cause") not in cluster["services"]:
        errors.append(f"root_cause '{parsed.get('root_cause')}' not in cluster services")
    if parsed.get("class") not in ALLOWED_CLASSES:
        errors.append(f"class '{parsed.get('class')}' not in allowed enum")
    conf = parsed.get("confidence", None)
    if not isinstance(conf, (int, float)) or not (0 <= conf <= 1):
        errors.append(f"confidence '{conf}' not in [0,1]")
    actions = parsed.get("actions")
    if not isinstance(actions, list) or len(actions) == 0:
        errors.append("actions empty or wrong type")
    return (len(errors) == 0, errors)


# ===========================================================================
# Pipeline
# ===========================================================================
def run_rca(cluster: dict, alerts: list[dict], graph: nx.DiGraph,
            history: list[dict]) -> dict:
    candidates = rca_combined(cluster, alerts, graph)
    if not candidates:
        return {"cluster_id": cluster["cluster_id"], "root_cause": None,
                "confidence": 0.0, "method": "no-candidates"}

    graph_top3 = [[s, sc] for s, sc in candidates[:3]]
    scoped = cluster_alerts(cluster, alerts)
    store_diag = store_culprit_check(candidates[0][0], cluster["services"], scoped, graph)

    if llm_available():
        try:
            out = call_llm_rca(cluster, candidates, history, graph)
            valid, errors = validate_llm_output(out, cluster)
            if valid:
                method = "graph+llm"
            else:
                out = heuristic_rca(cluster, candidates, history, graph)
                out["reasoning"] = f"LLM output invalid ({errors}); used graph+retrieval fallback. " + out["reasoning"]
                method = "graph+llm-invalid-fallback"
        except Exception as e:  # noqa: BLE001
            out = heuristic_rca(cluster, candidates, history, graph)
            out["reasoning"] = f"LLM call failed ({e}); used graph+retrieval fallback. " + out["reasoning"]
            out["confidence"] = round(out["confidence"] * 0.7, 3)
            method = "graph-only-llm-failed"
    else:
        out = heuristic_rca(cluster, candidates, history, graph)
        method = "graph+retrieval-heuristic"  # no ANTHROPIC_API_KEY in this env

    out.update({
        "cluster_id": cluster["cluster_id"],
        "graph_top3": graph_top3,
        "graph_score": candidates[0][1],
        "store_diagnostic": store_diag,
        "method": method,
        "llm_used": method == "graph+llm",
    })
    # stable key order
    ordered = {k: out[k] for k in [
        "cluster_id", "graph_top3", "root_cause", "class", "confidence",
        "actions", "reasoning", "similar_incidents", "graph_score",
        "store_diagnostic", "method", "llm_used"] if k in out}
    return ordered


def run_all(cluster_summary_path: str, alerts_path: str, services_path: str,
            history_path: str, out_path: str) -> dict:
    summary = load_json(cluster_summary_path)
    alerts = load_jsonl(alerts_path)
    services_doc = load_json(services_path)
    history = load_json(history_path)
    graph = build_graph(services_doc)

    results = [run_rca(c, alerts, graph, history) for c in summary["clusters"]]
    doc = {
        "generated_by": "w2-d2-rca",
        "clusters_analyzed": len(results),
        "llm_used": any(r.get("llm_used") for r in results),
        "results": results,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    return doc


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    doc = run_all(
        cluster_summary_path=os.path.join(here, "results", "cluster_summary.json"),
        alerts_path=os.path.join(here, "lab", "dataset", "alerts_sample.jsonl"),
        services_path=os.path.join(here, "lab", "dataset", "services.json"),
        history_path=os.path.join(here, "lab", "dataset", "incidents_history.json"),
        out_path=os.path.join(here, "results", "rca_output.json"),
    )
    for r in doc["results"]:
        print(r["cluster_id"], "->", r["root_cause"], f"({r.get('class')})",
              "conf", r.get("confidence"), "| top3", r.get("graph_top3"))
