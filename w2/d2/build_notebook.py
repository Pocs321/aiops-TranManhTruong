"""Assemble and execute assignment.ipynb from a list of cells."""
import os
import nbformat as nbf
from nbformat.v4 import new_notebook, new_markdown_cell, new_code_cell
from nbconvert.preprocessors import ExecutePreprocessor

HERE = os.path.dirname(os.path.abspath(__file__))

cells = []
def md(t): cells.append(new_markdown_cell(t))
def code(t): cells.append(new_code_cell(t))

md("""# W2-D2 — Root Cause Analysis (Graph + Causal + LLM-augmented)

We take the **D1 correlator output** (`results/cluster_summary.json`) — clusters of
related alerts — and, for each cluster, decide **which service is the culprit** vs which
are victims of the cascade. Three signals are combined:

1. **Graph traversal** — PageRank over the service *dependency* subgraph (the
   most-depended-on service is the likely root).
2. **Temporal** — the service that alerts *earliest* gets a boost.
3. **LLM-augmented** — Claude classifies the failure and proposes actions, grounded in
   retrieved (RAG) similar past incidents.

All logic lives in `rca.py`; this notebook drives it and writes `results/rca_output.json`.""")

code("""import json, inspect
import networkx as nx
import rca

cluster_summary = rca.load_json("results/cluster_summary.json")
alerts          = rca.load_jsonl("lab/dataset/alerts_sample.jsonl")
services_doc    = rca.load_json("lab/dataset/services.json")
history         = rca.load_json("lab/dataset/incidents_history.json")

print("clusters :", cluster_summary["n_clusters"])
print("alerts   :", len(alerts))
print("services :", len(services_doc["services"]))
print("incidents:", len(history))
main = cluster_summary["clusters"][0]
print("\\nMain cluster", main["cluster_id"], "services:", main["services"])""")

md("""## 1. Build the service dependency graph

Edge `A -> B` means **A calls / depends on B**. Failure propagates *against* the edge:
if `B` breaks, every caller of `B` breaks too. So the root cause tends to sit at the
**bottom** of the dependency graph (the most-depended-on sink).""")

code("""graph = rca.build_graph(services_doc)
print("nodes:", graph.number_of_nodes(), " edges:", graph.number_of_edges())

sub = graph.subgraph(main["services"])
print("\\nDependencies inside the main cluster (caller -> callee):")
for u, v in sub.edges():
    print(f"  {u} -> {v}")
print("\\npayment-svc is called by:", sorted(graph.predecessors("payment-svc")))""")

md("""## 2. Graph + temporal RCA

`rca_pagerank` runs PageRank on the caller→callee subgraph, so rank accumulates at the
most-depended-on service. `rca_combined` blends normalised PageRank (0.6) with an
earliest-alert temporal score (0.4).""")

code("""pr = rca.rca_pagerank(main["services"], graph)
print("Raw PageRank (main cluster):")
for k, v in sorted(pr.items(), key=lambda x: -x[1]):
    print(f"  {k:18s} {v:.4f}")
print("confidence (top/sum): %.3f" % (max(pr.values()) / sum(pr.values())))

scoped = rca.cluster_alerts(main, alerts)
print("\\nEarliest alert per service:")
for s, t in sorted(rca._earliest_per_service(main["services"], scoped).items(), key=lambda x: x[1]):
    print(f"  {s:18s} {t}")

print("\\nCombined graph+temporal ranking (top candidates):")
for svc, sc in rca.rca_combined(main, alerts, graph):
    print(f"  {svc:18s} {sc:.4f}")""")

md("""## 3. Retrieval — similar past incidents (RAG)

`incidents_history.json` holds 30 past incidents. `top_k_similar` ranks them against the
current cluster by shared root-cause service, service overlap, and matching severity —
the retrieved cases are fed to the LLM as grounding context.""")

code("""for it in rca.top_k_similar(main, history, k=3):
    print(f"{it['id']}  sim={it['_similarity']}  "
          f"root_cause={it['root_cause_service']} ({it['root_cause_class']})")
    print("   ", it["summary"])""")

md("""## 4. LLM-augmented RCA (Claude, JSON-schema output)

`rca.call_llm_rca` calls the **Anthropic Messages API** with a JSON-schema
(`output_config.format`) so the reply is guaranteed parseable. Model: `claude-opus-4-8`
(no `temperature` — it is removed on Opus 4.8). When no `ANTHROPIC_API_KEY` is present,
the pipeline falls back to a deterministic, retrieval-grounded heuristic so it still
produces valid output offline.""")

code('''print("LLM available (ANTHROPIC_API_KEY + sdk):", rca.llm_available())
print("model:", rca.LLM_MODEL)
print("\\nThe Claude call (from rca.call_llm_rca):")
src = inspect.getsource(rca.call_llm_rca)
print("\\n".join(l for l in src.splitlines() if "client.messages.create" in l
                  or "output_config" in l or "model=" in l or "anthropic.Anthropic" in l))

result_main = rca.run_rca(main, alerts, graph, history)
print("\\nRCA result for", main["cluster_id"], ":")
print(json.dumps(result_main, indent=2))''')

md("""## 5. Run the full pipeline and write `results/rca_output.json`""")

code('''doc = rca.run_all(
    cluster_summary_path="results/cluster_summary.json",
    alerts_path="lab/dataset/alerts_sample.jsonl",
    services_path="lab/dataset/services.json",
    history_path="lab/dataset/incidents_history.json",
    out_path="results/rca_output.json",
)
print("clusters_analyzed:", doc["clusters_analyzed"], " llm_used:", doc["llm_used"])
for r in doc["results"]:
    print(f"  {r['cluster_id']}: {r['root_cause']:16s} ({r['class']}) "
          f"conf={r['confidence']}  method={r['method']}")''')

md("""## 6. Acceptance checks""")

code('''doc = rca.load_json("results/rca_output.json")
assert doc["clusters_analyzed"] == 3
for r in doc["results"]:
    assert r["graph_top3"] and r["root_cause"] and r["class"]
    ok, errs = rca.validate_llm_output(r, next(c for c in cluster_summary["clusters"]
                                               if c["cluster_id"] == r["cluster_id"]))
    assert ok, (r["cluster_id"], errs)
assert doc["results"][0]["root_cause"] == "payment-svc"
assert doc["results"][1]["store_diagnostic"]["verdict"] == "store_is_culprit"
print("ALL CHECKS PASSED — rca_output.json is valid with graph_top3 + root_cause + class.")''')

nb = new_notebook(cells=cells, metadata={
    "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
    "language_info": {"name": "python"},
})

ep = ExecutePreprocessor(timeout=300, kernel_name="python3")
ep.preprocess(nb, {"metadata": {"path": HERE}})

with open(os.path.join(HERE, "assignment.ipynb"), "w", encoding="utf-8") as f:
    nbf.write(nb, f)
print("Wrote executed assignment.ipynb with", len(nb.cells), "cells.")
