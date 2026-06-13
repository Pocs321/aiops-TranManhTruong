# DESIGN.md — W2-D3 Layer 4 (Model Serving)

## Pipeline architecture inside the endpoint

`POST /incident` accepts a batch of alerts and runs a three-stage chain, all
glued by `pipeline.process_batch()`:

1. **L1 — correlate** (`correlate.py`): alerts are sessionized by time gap
   (`gap_sec=120`), then services within `max_hop=2` on the dependency graph are
   merged via union-find into clusters.
2. **L2 — RCA** (`rca.py`): the largest cluster is the "primary incident". A
   graph heuristic ranks the root cause as the service the most other *alerting*
   services depend on (severity-weighted), then retrieves similar past incidents
   by Jaccard service overlap.
3. **L3 — enrich** (`enrich.py`): optional LLM refinement of the graph guess,
   behind the `AIOPS_USE_LLM` flag, with a TTL cache and a hard timeout.

The graph and incident history are loaded **once** at import time and treated as
read-only, so each request is stateless. The response is assembled and validated
against the `IncidentResponse` Pydantic model before it leaves the process.

## Concrete decisions

- **`gap_sec=120`** — alert storms from one root cause typically fan out within
  ~1–2 minutes (retry/backoff cascades), so a 2-minute session window keeps a
  single incident's alerts together without merging two unrelated outages.
- **`max_hop=2`** — a fault propagates to direct callers (1 hop) and their
  callers (2 hops); beyond that the topological link is too weak to assume one
  incident, and we risk gluing the whole graph into one cluster.
- **Largest cluster = primary** — keeps the RCA result single and actionable for
  the demo; all clusters are still returned so nothing is hidden.

## Latency budget (200-alert scenario)

| Phase | Time | % |
|---|---|---|
| Pydantic parse + validate | 5–10 ms | <1% |
| L1 correlate (Python + shortest-path) | 50–100 ms | ~1–2% |
| L2 graph RCA + history | 20–50 ms | ~1% |
| L3 LLM call (network) | 3–8 s | ~95–97% |
| Serialize | 5–10 ms | <1% |
| **Total** | **~3.5–8.5 s** | |

The LLM network call dominates. We therefore (a) cache identical prompts,
(b) skip the LLM when graph confidence ≥ 0.9, and (c) cap it with a 10 s timeout
and 2 retries. Optimizing L1/L2 by 100 ms is noise next to the LLM; the LLM is
the only phase worth tuning.

## Production concern handled: fault tolerance (LLM dependency)

The LLM provider is the riskiest dependency, so it is **never required**:

- `enrich.llm_enrich()` returns `None` (never raises) on any failure — missing
  API key, SDK not installed, timeout, malformed JSON — and `run_rca()` falls
  back to the deterministic graph-only result (`method: "graph"`).
- The feature flag `AIOPS_USE_LLM=false` lets an operator disable the LLM during
  a provider outage and restart, with the service still returning useful RCA.
- `/readyz` reports the LLM status but does **not** gate readiness on it, so a
  provider outage does not pull the pod out of the load balancer.

## Concurrency

`POST /incident` is a **sync** handler on purpose. FastAPI runs sync path
operations in an `anyio` threadpool (default 40 workers), so concurrent requests
run on separate threads — the blocking LLM/CPU work of one request cannot starve
an event loop shared by the others (which an `async def` calling blocking code
*would* do). For real horizontal scale we run multiple uvicorn workers
(`--workers 4`); because the pipeline is stateless, no cross-worker coordination
is needed. The first bottleneck under load is the LLM provider's rate limit /
latency, then the threadpool size, then the GIL for the CPU-bound L1 loops.

## Why FastAPI over Flask / BentoML

- vs **Flask**: the pipeline is IO-bound (LLM network call) and has a real input
  schema. FastAPI gives Pydantic validation (malformed input → 422 for free,
  never a 500), automatic OpenAPI docs for the trainer to curl against, and a
  first-class async/threadpool model. Flask would need extra libraries for each.
- vs **BentoML**: BentoML shines for model-centric serving (versioning, adaptive
  batching of a single model). Our unit of work is a *pipeline* (graph + LLM
  call), not one ML model, so BentoML's batching/runner machinery is overhead
  with little payoff for this lab. FastAPI is the right altitude.
