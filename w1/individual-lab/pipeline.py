#!/usr/bin/env python3
"""AIOps W1 Individual Lab — Streaming Anomaly Detection Pipeline.

Receives metrics + logs POSTed by stream_generator.py at /ingest, runs a
streaming anomaly detector, classifies the fault type, and appends one JSON
line per confirmed alert to alerts.jsonl.

Design goals (mapped to the grading rubric):
  * PASS      -> a working HTTP endpoint + detection that fires on the fault.
  * +1 type   -> classify memory_leak / traffic_spike / dependency_timeout.
  * +1 no FP  -> warmup baseline + robust z-scores + debounce kill false alerts.
  * +1 TTD    -> short debounce + sensitive thresholds detect within seconds.

Zero external dependencies — uses only the Python standard library so it runs
anywhere with `python pipeline.py` (or `uv run python pipeline.py`).

Usage:
    python pipeline.py                 # listens on 0.0.0.0:8000, /ingest
    python pipeline.py --port 8000 --alerts-file alerts.jsonl
"""

import argparse
import json
import math
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Tunable parameters ----------------------------------------------------

# Metrics that get an adaptive (EWMA) baseline + z-score.
TRACKED_METRICS = [
    "memory_usage_bytes",
    "cpu_usage_percent",
    "http_requests_per_sec",
    "http_p99_latency_ms",
    "http_5xx_rate",
    "jvm_gc_pause_ms_avg",
    "queue_depth",
    "upstream_timeout_rate",
]

WARMUP_TICKS = 20      # build a clean baseline before any alert can fire
EWMA_ALPHA = 0.08      # baseline adaptivity (~25-tick effective window) -> tracks diurnal drift
FREEZE_Z = 4.0         # while a metric's |z| exceeds this, stop folding it into the baseline
DEBOUNCE_K = 3         # consecutive confirmations required before firing (kills transient noise)
EPS = 1e-9


# --- Adaptive per-metric baseline -----------------------------------------

class EwmaBaseline:
    """Incremental EWMA mean + variance, giving a streaming robust z-score.

    EWMA adapts to slow diurnal drift automatically, so the http_requests_per_sec
    baseline follows the day/night cycle instead of raising false alarms on it.
    Updates are *frozen* while a value looks anomalous, which keeps the baseline
    from being poisoned by the fault itself (so a slow memory leak keeps growing
    its z-score instead of being absorbed).
    """

    def __init__(self, alpha: float):
        self.alpha = alpha
        self.mean = None
        self.var = 0.0

    def zscore(self, x: float) -> float:
        if self.mean is None:
            return 0.0
        return (x - self.mean) / (math.sqrt(self.var) + EPS)

    def update(self, x: float) -> None:
        if self.mean is None:
            self.mean = x
            self.var = 0.0
            return
        delta = x - self.mean
        self.mean += self.alpha * delta
        # EWMA of squared deviation -> variance estimate
        self.var = (1 - self.alpha) * (self.var + self.alpha * delta * delta)


# --- The detector ----------------------------------------------------------

class AnomalyDetector:
    """Streaming detector + fault classifier.

    Classification relies on the fact that each fault has one *orthogonal*
    primary signal that the other two leave at baseline:

        dependency_timeout -> upstream_timeout_rate   (normal max ~0.4%)
        memory_leak        -> memory utilisation + GC  (normal ~40%, ~12ms)
        traffic_spike      -> requests/sec vs baseline (normal 80-160 rps)

    Shared symptoms (5xx rate, p99 latency, CPU, queue) only adjust severity,
    they never decide the type. Checks run most-specific first.
    """

    def __init__(self, alerts_file: str):
        self.alerts_file = alerts_file
        self.baselines = {m: EwmaBaseline(EWMA_ALPHA) for m in TRACKED_METRICS}
        self.ticks = 0
        self.fault_active = False           # latched once a fault is confirmed
        self.candidate = None               # fault type currently accumulating evidence
        self.streak = 0                     # consecutive ticks supporting `candidate`
        self.fired = {}                     # fault_type -> highest severity level emitted (1=warn, 2=crit)

    # -- baseline maintenance ------------------------------------------------

    def _update_baselines(self, metrics: dict) -> None:
        in_warmup = self.ticks <= WARMUP_TICKS
        for name, bl in self.baselines.items():
            if name not in metrics:
                continue
            x = float(metrics[name])
            # During warmup learn unconditionally. Afterwards skip values that
            # look anomalous (and everything once a fault is latched) so the
            # baseline stays a clean picture of "normal".
            if in_warmup or (not self.fault_active and abs(bl.zscore(x)) < FREEZE_Z):
                bl.update(x)

    # -- classification ------------------------------------------------------

    def _classify(self, metrics: dict):
        """Return (fault_type, severity, message) or (None, None, None)."""
        mem = float(metrics.get("memory_usage_bytes", 0))
        mem_limit = float(metrics.get("memory_limit_bytes", 1)) or 1.0
        mem_util = mem / mem_limit

        rps = float(metrics.get("http_requests_per_sec", 0))
        rps_base = self.baselines["http_requests_per_sec"].mean or rps or 1.0
        rps_ratio = rps / max(rps_base, 1.0)

        upstream = float(metrics.get("upstream_timeout_rate", 0))
        gc = float(metrics.get("jvm_gc_pause_ms_avg", 0))
        err5xx = float(metrics.get("http_5xx_rate", 0))
        p99 = float(metrics.get("http_p99_latency_ms", 0))
        queue = float(metrics.get("queue_depth", 0))

        z = {m: self.baselines[m].zscore(float(metrics.get(m, 0))) for m in TRACKED_METRICS}

        # 1) dependency_timeout — upstream timeout rate is the unmistakable signal
        #    (normal range 0-0.4%; fault drives it to 5-80%).
        if upstream > 2.0 or z["upstream_timeout_rate"] > 6:
            sev = "critical" if (upstream > 20 or err5xx > 10) else "warning"
            msg = (f"Upstream timeout rate at {upstream:.1f}% (5xx={err5xx:.1f}%, "
                   f"p99={p99:.0f}ms) — dependency failing")
            return "dependency_timeout", sev, msg

        # 2) memory_leak — utilisation climbing toward the limit + GC pressure.
        #    A leak grows ~slowly, so an adaptive baseline would absorb the drift
        #    and never spike its z-score. We instead use ABSOLUTE thresholds
        #    anchored to the fixed normal ranges (util ~40%, GC 8-18ms); anything
        #    well past them is unambiguous and is reached early in the leak.
        if mem_util > 0.55 or gc > 40:
            sev = "critical" if (mem_util > 0.80 or gc > 120) else "warning"
            msg = (f"Memory utilisation at {mem_util*100:.0f}% "
                   f"(GC pause {gc:.0f}ms) — memory growing abnormally")
            return "memory_leak", sev, msg

        # 3) traffic_spike — requests/sec far above the adaptive baseline.
        if rps_ratio > 2.5 or z["http_requests_per_sec"] > 6:
            sev = "critical" if (rps_ratio > 4 or queue > 100) else "warning"
            msg = (f"Traffic at {rps:.0f} req/s ({rps_ratio:.1f}x baseline, "
                   f"queue={queue:.0f}, p99={p99:.0f}ms) — load surge")
            return "traffic_spike", sev, msg

        return None, None, None

    # -- main entry ----------------------------------------------------------

    def process(self, payload: dict) -> None:
        self.ticks += 1
        metrics = payload.get("metrics", {})
        timestamp = payload.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="milliseconds")

        # Classify against the *current* baseline, then fold this tick in.
        fault_type = severity = message = None
        if self.ticks > WARMUP_TICKS:
            fault_type, severity, message = self._classify(metrics)
        self._update_baselines(metrics)

        if self.ticks % 20 == 0:
            print(f"[PIPELINE] {self.ticks} datapoints processed | "
                  f"fault_active={self.fault_active}")

        # Debounce: require DEBOUNCE_K consecutive ticks agreeing on the type.
        if fault_type is None:
            if not self.fault_active:
                self.candidate, self.streak = None, 0
            return

        if fault_type == self.candidate:
            self.streak += 1
        else:
            self.candidate, self.streak = fault_type, 1

        if self.streak < DEBOUNCE_K:
            return

        self.fault_active = True
        self._maybe_fire(fault_type, severity, message, timestamp)

    # -- alerting ------------------------------------------------------------

    def _maybe_fire(self, fault_type, severity, message, timestamp) -> None:
        level = 2 if severity == "critical" else 1
        # Emit only on first detection or on warning -> critical escalation,
        # so alerts.jsonl stays clean (at most one warning + one critical per fault).
        if level <= self.fired.get(fault_type, 0):
            return
        self.fired[fault_type] = level

        alert = {
            "timestamp": timestamp,
            "type": fault_type,
            "severity": severity,
            "message": message,
        }
        with open(self.alerts_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(alert) + "\n")
        print(f"\n[ALERT] {severity.upper()} {fault_type} @ tick {self.ticks}: {message}\n")


# --- HTTP server -----------------------------------------------------------

def make_handler(detector: AnomalyDetector, lock: threading.Lock):

    class IngestHandler(BaseHTTPRequestHandler):
        def _respond(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802 (stdlib naming)
            if self.path.rstrip("/") not in ("/ingest", ""):
                self._respond(404, {"status": "not found"})
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw)
            except (ValueError, json.JSONDecodeError):
                self._respond(400, {"status": "bad json"})
                return
            with lock:                 # detector state is shared across threads
                detector.process(payload)
            self._respond(200, {"status": "ok"})

        def do_GET(self):  # noqa: N802 — simple health check
            self._respond(200, {"status": "ok", "ticks": detector.ticks})

        def log_message(self, *args):  # silence default per-request stderr spam
            return

    return IngestHandler


def main():
    parser = argparse.ArgumentParser(description="AIOps W1 streaming anomaly pipeline")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--alerts-file", default=None,
                        help="Output path (default: alerts.jsonl next to this script)")
    args = parser.parse_args()

    alerts_file = args.alerts_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "alerts.jsonl")
    detector = AnomalyDetector(alerts_file)
    lock = threading.Lock()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(detector, lock))
    print(f"[PIPELINE] listening on http://{args.host}:{args.port}/ingest")
    print(f"[PIPELINE] alerts -> {alerts_file}")
    print(f"[PIPELINE] warmup={WARMUP_TICKS} ticks, debounce={DEBOUNCE_K}, alpha={EWMA_ALPHA}")
    print("---")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[PIPELINE] shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
