# aiops w2/d3 — Model Serving (Layer 4)

FastAPI service that serves the AIOps incident pipeline:
`correlate (L1) → RCA (L2) → LLM enrich (L3)` over a single HTTP endpoint.

## Layout

```
serve.py                  FastAPI app: /incident /healthz /readyz /version /metrics
pipeline.py               glue — process_batch(): L1 → L2 → L3, loads state once
correlate.py              L1 — alert correlation (sessionize + topological clustering)
rca.py                    L2 — graph root-cause ranking + similar-incident retrieval
enrich.py                 L3 — LLM enrichment (TTL cache, timeout, graceful fallback)
lab/dataset/              services.json (dependency graph) + incidents_history.json
tests/                    unit (correlate) + integration (endpoints, LLM off)
DESIGN.md  SUBMIT.md       lab deliverables
requirements.txt  Makefile  Dockerfile  sample_alerts.json
```

## Run

```bash
# Windows PowerShell
uv venv
uv pip install -r requirements.txt
.\.venv\Scripts\uvicorn.exe serve:app --host 0.0.0.0 --port 8000 --reload
```

```bash
# make (uv) — Linux/macOS or Git Bash
make install && make run
```

## Try it

```bash
curl http://localhost:8000/healthz                 # {"status":"ok"}
curl http://localhost:8000/readyz
curl http://localhost:8000/version
curl -X POST http://localhost:8000/incident \
  -H "Content-Type: application/json" -d @sample_alerts.json
```

Interactive docs: http://localhost:8000/docs

## LLM (optional)

The LLM path is **off by default unless** `OPENAI_API_KEY` is set and
`AIOPS_USE_LLM` is not `false`. Without it, RCA returns deterministic graph-only
results — the service is fully functional offline. To enable:

```bash
export OPENAI_API_KEY=sk-...
export AIOPS_USE_LLM=true          # default
export AIOPS_LLM_MODEL=gpt-4o-mini # default
```

## Test

```bash
AIOPS_USE_LLM=false pytest -v tests/
```
