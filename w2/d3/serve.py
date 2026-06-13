"""W2-D3 Layer 4 — Model serving.

FastAPI service that exposes the AIOps incident pipeline over HTTP:

    POST /incident   correlate (L1) → RCA (L2) → LLM enrich (L3) → report
    GET  /healthz    liveness  (process alive; no external deps)
    GET  /readyz     readiness (downstream deps loaded / reachable)
    GET  /version    deployed version + pipeline config
    GET  /metrics    Prometheus metrics (if prometheus_client installed)

Run:  uvicorn serve:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from pipeline import CONFIG, GRAPH, HISTORY, process_batch
from enrich import LLM_MODEL, USE_LLM, llm_available

APP_VERSION = "1.0.0"

# --- Structured (JSON) logging --------------------------------------------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "extra"):
            log_obj.update(record.extra)  # type: ignore[arg-type]
        return json.dumps(log_obj)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)
logger = logging.getLogger("aiops")

# --- Optional Prometheus metrics ------------------------------------------
try:
    from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Histogram,
                                   generate_latest)

    REQUEST_COUNT = Counter("aiops_incident_requests_total",
                            "Total incident requests", ["status"])
    REQUEST_LATENCY = Histogram("aiops_incident_latency_seconds",
                                "Incident pipeline latency")
    CLUSTER_COUNT = Histogram("aiops_clusters_per_request",
                              "Clusters produced per request")
    _PROM = True
except ImportError:  # metrics are optional — service runs without them
    _PROM = False

# --- Schemas ---------------------------------------------------------------
class Alert(BaseModel):
    id: str
    ts: str
    service: str
    metric: str
    severity: str
    value: float
    threshold: float
    labels: Optional[dict] = Field(default_factory=dict)


class IncidentRequest(BaseModel):
    alerts: list[Alert]


class Cluster(BaseModel):
    cluster_id: str
    alert_count: int
    services: list[str]
    time_range: list[str]


class RootCause(BaseModel):
    service: str
    confidence: float
    reasoning: str


class SimilarIncident(BaseModel):
    id: str
    similarity: float
    summary: str


class IncidentResponse(BaseModel):
    clusters: list[Cluster]
    root_cause: RootCause
    recommended_actions: list[str]
    similar_incidents: list[SimilarIncident]


# --- App -------------------------------------------------------------------
app = FastAPI(
    title="AIOps Incident Pipeline",
    version=APP_VERSION,
    description="W2 lab — correlate alerts → RCA → suggest action",
)


@app.middleware("http")
async def add_timing(request: Request, call_next):
    """Stamp every response with its server-side latency and log the request."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    response.headers["X-Response-Time-Ms"] = f"{duration_ms:.1f}"
    logger.info(
        f"{request.method} {request.url.path} {response.status_code} {duration_ms:.0f}ms",
        extra={"extra": {"path": request.url.path,
                         "status": response.status_code,
                         "duration_ms": round(duration_ms, 1)}},
    )
    return response


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: the process is up. Must NOT depend on external services."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    """Readiness: downstream deps are loaded / reachable. 503 if not ready.

    The LLM is reported but NOT required for readiness — the pipeline degrades
    to graph-only RCA when the provider is down, so we stay in the load balancer.
    """
    checks = {
        "graph": GRAPH.number_of_nodes() > 0,
        "history": len(HISTORY) > 0,
        "llm": llm_available(),  # informational only
    }
    required_ok = checks["graph"] and checks["history"]
    if not required_ok:
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ready", "checks": checks}


@app.get("/version")
def version() -> dict:
    return {
        "app": APP_VERSION,
        "pipeline_config": {**CONFIG, "llm_model": LLM_MODEL, "use_llm": USE_LLM},
    }


@app.post("/incident", response_model=IncidentResponse)
def post_incident(req: IncidentRequest) -> IncidentResponse:
    """Process a batch of alerts → incident report.

    Sync handler on purpose: FastAPI runs sync path operations in a threadpool,
    giving concurrency for the blocking LLM/CPU work without an event loop a
    single hung call could starve. See DESIGN.md §concurrency.
    """
    logger.info(f"Received {len(req.alerts)} alerts")
    if not req.alerts:
        raise HTTPException(status_code=400, detail="Empty alert list")

    alerts = [a.model_dump() for a in req.alerts]

    def _run() -> dict:
        return process_batch(alerts)

    try:
        if _PROM:
            with REQUEST_LATENCY.time():
                result = _run()
            CLUSTER_COUNT.observe(len(result["clusters"]))
            REQUEST_COUNT.labels(status="success").inc()
        else:
            result = _run()
    except Exception as exc:  # noqa: BLE001
        if _PROM:
            REQUEST_COUNT.labels(status="error").inc()
        logger.error(f"Pipeline failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}")

    return IncidentResponse(**result)


if _PROM:
    @app.get("/metrics")
    def metrics() -> Response:
        """Prometheus exposition endpoint (returns 200 at exactly /metrics)."""
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
