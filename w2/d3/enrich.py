"""Layer 3 — LLM enrichment (D2's LLM call), with graceful degradation.

This module isolates every interaction with the LLM provider so the rest of the
pipeline never imports ``openai`` directly. Three production concerns are baked
in here:

  * **Timeout** — the client is constructed with a hard timeout so one hung call
    cannot freeze the endpoint forever.
  * **Cache** — identical prompts hit an in-memory TTL cache instead of paying
    the network round-trip again.
  * **Fallback** — if the provider is unconfigured, uninstalled, or erroring,
    ``llm_enrich`` returns ``None`` and the caller falls back to graph-only RCA.

Feature flag: set ``AIOPS_USE_LLM=false`` to skip the LLM path entirely (e.g.
during a provider outage) — the service keeps serving graph-only results.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger("aiops.enrich")

# --- Config (read once) ----------------------------------------------------
USE_LLM = os.environ.get("AIOPS_USE_LLM", "true").lower() == "true"
LLM_MODEL = os.environ.get("AIOPS_LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_S = float(os.environ.get("AIOPS_LLM_TIMEOUT_S", "10"))

# --- Cache (TTL, bounded) --------------------------------------------------
try:
    from cachetools import TTLCache

    _cache: Any = TTLCache(maxsize=1000, ttl=3600)
except ImportError:  # cachetools optional — degrade to a plain bounded dict
    _cache = {}


def _cache_get(key: str) -> Optional[dict]:
    return _cache.get(key)


def _cache_put(key: str, value: dict) -> None:
    if isinstance(_cache, dict) and len(_cache) > 1000:
        _cache.clear()  # crude bound when cachetools is unavailable
    _cache[key] = value


def llm_available() -> bool:
    """True only if the LLM path is enabled, the SDK is importable, and an API
    key is present. Used by the pipeline and by ``/readyz``."""
    if not USE_LLM:
        return False
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    try:
        import openai  # noqa: F401
    except ImportError:
        return False
    return True


def _build_prompt(cluster: dict, rca: dict) -> str:
    """Render a compact, deterministic prompt from the graph-RCA result."""
    payload = {
        "services": cluster.get("services", []),
        "alert_count": cluster.get("alert_count", 0),
        "graph_root_cause": rca.get("root_cause"),
        "graph_confidence": rca.get("confidence"),
        "candidates": rca.get("candidates", []),
    }
    return (
        "You are an SRE assistant. Given a correlated incident cluster and a "
        "graph-based root-cause guess, return strict JSON with keys "
        '"root_cause" (service), "confidence" (0-1), "reasoning" (one sentence), '
        'and "actions" (list of <=3 imperative remediation steps).\n'
        f"INCIDENT = {json.dumps(payload, sort_keys=True)}"
    )


def _do_llm_call(prompt: str) -> dict:
    """Single real provider call. Raises on any failure (caller handles)."""
    from openai import OpenAI

    client = OpenAI(timeout=LLM_TIMEOUT_S, max_retries=2)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    return json.loads(resp.choices[0].message.content)


def cached_llm_call(prompt: str) -> dict:
    """LLM call with content-addressed caching (sha256 of the prompt)."""
    key = hashlib.sha256(prompt.encode()).hexdigest()
    hit = _cache_get(key)
    if hit is not None:
        logger.info("llm cache hit")
        return hit
    result = _do_llm_call(prompt)
    _cache_put(key, result)
    return result


def llm_enrich(cluster: dict, rca: dict) -> Optional[dict]:
    """Enrich a graph-RCA result via the LLM. Returns ``None`` (never raises) if
    the LLM is unavailable or the call fails, so the pipeline degrades cleanly.
    """
    if not llm_available():
        logger.info("llm unavailable — skipping enrichment (graph-only)")
        return None
    try:
        out = cached_llm_call(_build_prompt(cluster, rca))
        # Keep only the fields we trust; ignore anything malformed.
        return {
            "root_cause": out.get("root_cause", rca.get("root_cause")),
            "confidence": float(out.get("confidence", rca.get("confidence", 0.0))),
            "reasoning": str(out.get("reasoning", "")),
            "actions": list(out.get("actions", []))[:3],
        }
    except Exception as exc:  # noqa: BLE001 — any provider error → graph fallback
        logger.warning("llm enrichment failed (%s) — falling back to graph", exc)
        return None
