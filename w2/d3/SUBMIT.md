# SUBMIT.md — W2-D3 Model Serving

## What I built

A FastAPI service (`serve.py`) that exposes the full AIOps pipeline over HTTP:
`POST /incident` runs correlate (L1) → RCA (L2) → optional LLM enrich (L3) and
returns an incident report; `GET /healthz`, `/readyz`, `/version`, and `/metrics`
support operations. L1/L2/L3 are wired-in functional stubs (`correlate.py`,
`rca.py`, `enrich.py`) since the canonical D1/D2 code is not yet on disk — they
run end-to-end against a real service graph and incident history in
`lab/dataset/`.

### Acceptance (verified locally)

- `uvicorn serve:app --port 8000` boots.
- `GET /healthz` → `{"status":"ok"}`.
- `POST /incident` with valid input → 200 with `clusters`, `root_cause`,
  `recommended_actions`, `similar_incidents`.
- Malformed input (`{"alerts":[{"id":"a-1"}]}`) → **422, not 500**.
- Empty list → **400**.
- `pytest tests/` → all green (LLM mocked off via `AIOPS_USE_LLM=false`).

## EOD checkpoint

**1. Latency budget (p99)? Which phase dominates?**
With the LLM enabled, p99 is ~8.5 s and the L3 LLM network call is ~95–97% of it.
With `AIOPS_USE_LLM=false` (graph-only), p99 collapses to ~50–150 ms because only
the in-process L1 correlate + L2 graph RCA remain. The lesson: the only phase
worth optimizing is the LLM (cache, skip-on-high-confidence, smaller model);
shaving the Python layers is rounding error.

**2. 5 alerts vs 500 alerts — linear scale or fixed cost?**
There is a large **fixed cost** (one LLM call, ~3–8 s) that 5 and 500 alerts both
pay roughly equally, since we enrich only the primary cluster. The variable part
is L1 correlation: clustering does pairwise shortest-path checks within each
session, so it grows worse-than-linear (≈ O(S²) in distinct services per
session) but stays in the tens-of-ms range at lab scale. Net: 5 vs 500 alerts
barely differ in wall-clock when the LLM is on (fixed cost dominates); with the
LLM off, you can see the L1 growth.

**3. LLM provider down mid-demo — behavior + fallback?**
The endpoint keeps serving. `enrich.llm_enrich()` catches every provider error
(timeout/auth/parse) and returns `None`; `run_rca()` then returns the
deterministic graph-only result with `method: "graph"`. The 10 s timeout + 2
retries bound how long a hang can last. If the provider is known-down, set
`AIOPS_USE_LLM=false` and restart to skip the call entirely. `/readyz` does *not*
depend on the LLM, so the pod stays in rotation. (Asked again Friday — Q3.)

**4. `/healthz` vs `/readyz`?**
`/healthz` = **liveness**: is the process alive? No external dependencies — if it
fails, the orchestrator *restarts* the pod. `/readyz` = **readiness**: is it safe
to send traffic? It checks that the graph and history loaded; if those fail it
returns 503 and the orchestrator *removes the pod from the load balancer* (no
restart). The LLM is reported in `/readyz` but not required, because we degrade
gracefully rather than refuse traffic.

**5. 4 concurrent requests from 4 groups — handles it? First bottleneck?**
Yes. The sync `/incident` handler is dispatched to FastAPI's anyio threadpool
(default 40 threads), so 4 requests run concurrently on separate threads; one
blocking LLM call does not block the others. The **first bottleneck** is the LLM
provider — rate limits and per-call latency — not our code. Next would be the
threadpool size, then the GIL for the CPU-bound L1 loops. For real scale: run
`--workers N` (the pipeline is stateless, so no shared-state races) and/or move
to an async LLM client with `asyncio.gather` for multi-cluster enrichment.
(Asked again Friday — Q5.)

## Trade-offs I accepted (and why)

- **Single-worker / in-process cache** for the lab. The LLM cache is per-process,
  so it would not be shared across `--workers`. Acceptable here because the lab
  is not a scale exercise; the production fix is a shared cache (Redis) or a
  stateless design that reloads from a store. I kept the pipeline stateless apart
  from that read-only graph/history, so multi-worker is otherwise safe.
- **Largest cluster as the single primary incident** for RCA, to keep the demo
  response focused. All clusters are still returned; only the RCA target is one.
- **Functional D1/D2 stubs** rather than the real deliverables, because those
  files don't exist on disk yet. The interfaces (`correlate`, `run_rca`,
  `build_graph_from_json`) match the spec so the real modules can drop in.

## Concepts I can speak to (Layer 4)

- **Shadow deployment**: run v2 alongside v1 receiving the same input but not
  serving its output — log and compare against v1 on live traffic for ~a week,
  then promote. Out of scope to implement; relevant for safely rolling a new
  correlate/RCA version.
- **SLOs**: availability 99.5%, p99 < 10 s, LLM failure rate < 1%, offline
  top-3 root-cause precision > 70% — exported via `/metrics` for Prometheus.

## How to run

```bash
make install            # uv venv + deps
make test               # AIOPS_USE_LLM=false pytest -v tests/
make run                # uvicorn serve:app --port 8000 --reload

curl http://localhost:8000/healthz
curl -X POST http://localhost:8000/incident \
  -H "Content-Type: application/json" -d @sample_alerts.json
```
