#!/usr/bin/env python3
# =============================================================================
# W3-D2 Chaos Engineering — chaos_runner.py
# =============================================================================
# Self-contained chaos runner built from the lab spec before w3-d2-pack.zip was
# available. It corresponds to chaos_runner_skeleton.py with the two TODO
# functions implemented:
#
#     build_inject_cmd(exp)      -> dispatcher over the 10 fault types (§3, §8.5)
#     print_scoreboard(results)  -> confusion-matrix scoreboard (§6.1, §8.6)
#
# Everything else (orchestration loop, pipeline client, scoring) is provided so
# the file runs end-to-end. Experiments are read from experiments.yaml — nothing
# about them is hard-coded (§8.9).
#
# When the official pack arrives: keep these two functions, and either drop them
# into the pack's skeleton, or point this runner at the pack's stack via flags.
#
# Usage:
#   python chaos_runner.py                 # run all 10 against the live stack
#   python chaos_runner.py --dry-run       # print planned commands, no stack needed
#   python chaos_runner.py --only 1,10     # run a subset by 1-based index
#   python chaos_runner.py --pipeline-url http://localhost:8000
#
# Deps: PyYAML (pip install pyyaml). HTTP uses the stdlib (urllib) — no requests.
# =============================================================================

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover
    sys.stderr.write("ERROR: PyYAML is required. Install with: pip install pyyaml\n")
    sys.exit(2)


# --------------------------------------------------------------------------- #
# Configuration (overridable by env / CLI)                                    #
# --------------------------------------------------------------------------- #
PUMBA = os.environ.get("CHAOS_PUMBA", "pumba")
TOXIPROXY_CLI = os.environ.get("CHAOS_TOXIPROXY_CLI", "toxiproxy-cli")
TC_IMAGE = os.environ.get("CHAOS_TC_IMAGE", "gaiadocker/iproute2")
DEFAULT_PIPELINE_URL = os.environ.get("CHAOS_PIPELINE_URL", "http://localhost:8000")
DEFAULT_COOLDOWN_S = int(os.environ.get("CHAOS_COOLDOWN_S", "120"))   # §8.3 cooldown
DEFAULT_BASELINE_S = int(os.environ.get("CHAOS_BASELINE_S", "60"))    # pre-inject observe window
DEFAULT_POLL_S = int(os.environ.get("CHAOS_POLL_S", "3"))
DEFAULT_DETECT_GRACE_S = int(os.environ.get("CHAOS_DETECT_GRACE_S", "60"))

# Acceptance thresholds (§8.6) — used only for the printed verdict, never to tune.
ACC_MIN_DETECTED = 7
ACC_MIN_RCA_FRACTION = 0.70
ACC_MAX_FALSE_ALARMS = 1


# --------------------------------------------------------------------------- #
# Small helpers                                                               #
# --------------------------------------------------------------------------- #
def now_ts() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def show(cmd) -> str:
    """Shell-quoted, copy-pasteable rendering of a command list."""
    return shlex.join(cmd)


def percentile(values, p: float):
    """Linear-interpolation percentile. p in [0,1]. None if empty."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = (len(vals) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vals[int(k)]
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def run_cmd(cmd, timeout=None, dry_run=False):
    """Execute a command list; return (rc, stdout, stderr). No shell."""
    if dry_run:
        print(f"    [dry-run] {show(cmd)}")
        return 0, "", ""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        # Expected for recurring faults (e.g. pumba kill loop) bounded by duration.
        return 0, "", "(timed out — expected for duration-bounded faults)"
    except FileNotFoundError as e:
        return 127, "", f"(binary not found: {e})"


# --------------------------------------------------------------------------- #
# Docker resolution helpers                                                   #
# --------------------------------------------------------------------------- #
_container_cache: dict = {}


def resolve_container(service: str, dry_run: bool = False) -> str:
    """Map a compose service name to its running container name.

    Handles compose prefixes (project_service_1 / project-service-1). Falls back
    to the service name itself (dry-run, or no docker) so command-building never
    crashes.
    """
    if service in _container_cache:
        return _container_cache[service]
    if dry_run:
        _container_cache[service] = service
        return service
    rc, out, _ = run_cmd(["docker", "ps", "--format", "{{.Names}}"])
    name = service
    if rc == 0 and out:
        candidates = [n for n in out.splitlines() if service in n]
        # Prefer the closest match (exact, then shortest containing name).
        exact = [n for n in candidates if n == service]
        if exact:
            name = exact[0]
        elif candidates:
            name = sorted(candidates, key=len)[0]
    _container_cache[service] = name
    return name


def resolve_ip(container: str, dry_run: bool = False) -> str:
    if dry_run:
        return f"<{container}-ip>"
    rc, out, _ = run_cmd([
        "docker", "inspect", "-f",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container,
    ])
    return out.strip() if rc == 0 and out.strip() else f"<{container}-ip>"


# --------------------------------------------------------------------------- #
# TODO #1 (from skeleton): build_inject_cmd                                   #
# --------------------------------------------------------------------------- #
def build_inject_cmd(exp: dict, dry_run: bool = False):
    """Dispatch on exp['fault_type'] and return a command list for run_cmd().

    Covers all 10 fault categories from §3. Tool choices follow the §3 table:
      - tc/netem faults (latency, loss, dns)  -> pumba netem
      - resource faults (cpu, memory)          -> pumba stress (stress-ng sidecar)
      - container kill                         -> pumba kill
      - disk fill / clock skew / partition     -> docker exec (dd / date / iptables)
      - HTTP-level retry storm                 -> toxiproxy-cli toxic
    Container/proxy names are resolved here so the result is exec-ready (no shell
    metacharacters except inside an explicit `sh -c` argument).
    """
    ft = exp["fault_type"]
    p = exp.get("params", {})
    dur = int(exp["blast_radius"]["duration_s"])
    target = exp["target_service"]
    c = resolve_container(target, dry_run)

    if ft == "latency":
        return [
            PUMBA, "netem", "--duration", f"{dur}s", "--tc-image", TC_IMAGE,
            "delay",
            "--time", str(p.get("delay_ms", 500)),
            "--jitter", str(p.get("jitter_ms", 0)),
            "--correlation", str(p.get("correlation_pct", 0)),
            c,
        ]

    if ft == "network_loss":
        return [
            PUMBA, "netem", "--duration", f"{dur}s", "--tc-image", TC_IMAGE,
            "loss", "--percent", str(p.get("loss_percent", 30)),
            c,
        ]

    if ft == "dns_latency":
        # No per-container DNS API in Docker; approximate "slow lookup" by adding
        # latency to the resolver container's traffic (§3.1 DNS slow/fail row).
        return [
            PUMBA, "netem", "--duration", f"{dur}s", "--tc-image", TC_IMAGE,
            "delay", "--time", str(p.get("dns_delay_ms", 2000)),
            c,
        ]

    if ft == "availability":
        # Recurring kill bounded by run_experiment's timeout=duration.
        return [
            PUMBA, "--interval", f"{p.get('kill_interval_s', 60)}s",
            "kill", "--signal", str(p.get("signal", "SIGKILL")),
            c,
        ]

    if ft == "cpu_saturation":
        stressors = f"--cpu {p.get('cpu_workers', 4)} --cpu-load {p.get('cpu_load', 90)}"
        return [
            PUMBA, "stress", "--duration", f"{dur}s",
            "--stressors", stressors, c,
        ]

    if ft == "memory":
        stressors = f"--vm {p.get('vm_workers', 1)} --vm-bytes {p.get('vm_bytes', '80%')}"
        return [
            PUMBA, "stress", "--duration", f"{dur}s",
            "--stressors", stressors, c,
        ]

    if ft == "disk_fill":
        path = p.get("fill_path", "/tmp")
        pct = int(p.get("fill_percent", 95))
        # Fill to `pct`% of currently-free space on `path`, then keep the file
        # until rollback removes it.
        script = (
            f"AVAIL=$(df -m {path} | awk 'NR==2{{print $4}}'); "
            f"CNT=$(( AVAIL * {pct} / 100 )); "
            f"dd if=/dev/zero of={path}/chaos_fill bs=1M count=$CNT"
        )
        return ["docker", "exec", c, "sh", "-c", script]

    if ft == "time_skew":
        skew = int(p.get("skew_seconds", 60))
        # Best-effort: GNU `date -s` relative skew. Robust path is libfaketime
        # LD_PRELOAD set at container start (cannot be applied at runtime).
        return ["docker", "exec", c, "date", "-s", f"+{skew} seconds"]

    if ft == "network_partition":
        peer = p["peer_service"]
        peer_ip = resolve_ip(resolve_container(peer, dry_run), dry_run)
        script = (
            f"iptables -A INPUT -s {peer_ip} -j DROP; "
            f"iptables -A OUTPUT -d {peer_ip} -j DROP"
        )
        return ["docker", "exec", c, "sh", "-c", script]

    if ft == "cascade_retry":
        # Inject failures at the dependency (inject_at) so the target retries and
        # storms. toxiproxy reset_peer at toxicity=error_percent/100 approximates
        # 5xx; true HTTP 500 injection would use Chaos Mesh HTTPChaos or an app
        # fault flag. Toxic is named chaos_retry for clean rollback.
        proxy = p.get("proxy") or p.get("inject_at", "payment")
        toxicity = p.get("error_percent", 20) / 100.0
        return [
            TOXIPROXY_CLI, "toxic", "add", proxy,
            "-t", "reset_peer", "-a", "timeout=0",
            "--toxicity", str(toxicity), "-n", "chaos_retry",
        ]

    raise ValueError(f"Unknown fault_type: {ft!r} (experiment {exp.get('name')})")


def build_rollback_cmd(exp: dict, dry_run: bool = False):
    """Best-effort explicit rollback per fault type (most also auto-expire)."""
    ft = exp["fault_type"]
    p = exp.get("params", {})
    target = exp["target_service"]
    c = resolve_container(target, dry_run)

    if ft in ("latency", "network_loss", "dns_latency"):
        return ["docker", "exec", c, "tc", "qdisc", "del", "dev", "eth0", "root"]
    if ft == "availability":
        return ["docker", "compose", "up", "-d", target]
    if ft in ("cpu_saturation", "memory"):
        return ["docker", "exec", c, "pkill", "-f", "stress-ng"]
    if ft == "disk_fill":
        path = p.get("fill_path", "/tmp")
        return ["docker", "exec", c, "rm", "-f", f"{path}/chaos_fill"]
    if ft == "time_skew":
        # Re-sync from host clock (best effort).
        return ["docker", "exec", c, "sh", "-c", "date -s @$(date +%s)"]
    if ft == "network_partition":
        return ["docker", "exec", c, "iptables", "-F"]
    if ft == "cascade_retry":
        proxy = p.get("proxy") or p.get("inject_at", "payment")
        return [TOXIPROXY_CLI, "toxic", "remove", "-n", "chaos_retry", proxy]
    return ["true"]


# --------------------------------------------------------------------------- #
# Pipeline client (documented contract: §8.1)                                 #
# --------------------------------------------------------------------------- #
class PipelineClient:
    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            # Non-fatal: a differing/unavailable endpoint is recorded, not crashed.
            print(f"    [pipeline] {method} {path} failed: {e}")
            return None

    def get_alerts(self, since: float):
        out = self._req("GET", f"/alerts?since={int(since)}")
        if isinstance(out, dict):
            out = out.get("alerts", [])
        return out or []

    def correlate(self, window: dict):
        return self._req("POST", "/correlate", {"window": window})

    def rca(self, cluster):
        return self._req("POST", "/rca", {"cluster": cluster})


def alert_ts(alert) -> float:
    for k in ("timestamp", "ts", "fired_at", "time", "start"):
        v = alert.get(k) if isinstance(alert, dict) else None
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                try:
                    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    pass
    return now_ts()


def alert_service(alert) -> str:
    if isinstance(alert, dict):
        for k in ("service", "svc", "resource", "target"):
            if alert.get(k):
                return str(alert[k])
    return ""


# --------------------------------------------------------------------------- #
# Experiment orchestration                                                    #
# --------------------------------------------------------------------------- #
def observe_baseline(client: PipelineClient, seconds: int, dry_run: bool):
    """Watch for spurious alerts in a no-fault window -> false alarms (§6.1)."""
    if dry_run or seconds <= 0:
        return 0
    start = now_ts()
    print(f"  baseline observe {seconds}s (counting false alarms)...")
    seen = set()
    end = start + seconds
    while now_ts() < end:
        for a in client.get_alerts(since=start):
            seen.add(json.dumps(a, sort_keys=True))
        time.sleep(DEFAULT_POLL_S)
    return len(seen)


def wait_for_detection(client, t_inject, detect_timeout, dry_run):
    """Poll /alerts until the first new alert; return (detected, mttd, alerts)."""
    if dry_run:
        return None, None, []
    deadline = t_inject + detect_timeout
    first_ts = None
    collected = {}
    while now_ts() < deadline:
        for a in client.get_alerts(since=t_inject):
            key = json.dumps(a, sort_keys=True)
            if key not in collected:
                collected[key] = a
                ats = alert_ts(a)
                if first_ts is None or ats < first_ts:
                    first_ts = max(ats, t_inject)
        if first_ts is not None:
            break
        time.sleep(DEFAULT_POLL_S)
    if first_ts is None:
        return False, None, []
    return True, round(first_ts - t_inject, 1), list(collected.values())


def do_rca(client, t_inject, alerts, dry_run):
    """Correlate then RCA; return (root_service, confidence, evidence)."""
    if dry_run or not alerts:
        return None, None, None
    window = {"start": int(t_inject), "end": int(now_ts())}
    clusters = client.correlate(window)
    if isinstance(clusters, dict):
        clusters = clusters.get("clusters", clusters.get("cluster"))
    # Pick the largest cluster if a list came back.
    cluster = clusters
    if isinstance(clusters, list) and clusters:
        cluster = max(clusters, key=lambda c: len(c.get("alerts", [])) if isinstance(c, dict) else 0)
    res = client.rca(cluster if cluster is not None else {"alerts": alerts})
    if not isinstance(res, dict):
        return None, None, None
    return res.get("root_service") or res.get("root"), res.get("confidence"), res.get("evidence")


def classify(detected: bool, gt: dict):
    """Map detection vs ground-truth to a confusion-matrix cell (§6.1)."""
    fault = gt.get("fault_injected", True)
    if fault and detected:
        return "TP"
    if fault and not detected:
        return "FN"
    if not fault and detected:
        return "FP"
    return "TN"


def rca_is_correct(root_service, gt: dict):
    if not root_service:
        return False
    accept = gt.get("accept_rca") or [gt.get("expected_rca_service")]
    reject = gt.get("reject_rca") or []
    return (root_service in accept) and (root_service not in reject)


def run_experiment(exp, client, args, idx):
    gt = exp.get("ground_truth", {})
    name = exp.get("name", f"exp{idx}")
    dur = int(exp["blast_radius"]["duration_s"])
    print(f"\n=== [{idx}] {name}  ({exp['fault_type']} -> {exp['target_service']}) ===")

    inject_cmd = build_inject_cmd(exp, dry_run=args.dry_run)
    rollback_cmd = build_rollback_cmd(exp, dry_run=args.dry_run)

    # 1) baseline observe (no fault) -> false alarms
    baseline_fa = observe_baseline(client, args.baseline_window, args.dry_run)

    # 2) inject
    print(f"  inject: {show(inject_cmd)}")
    t_inject = now_ts()
    # Recurring/duration-bounded faults: cap subprocess at duration so the loop ends.
    inj_timeout = dur + 10 if exp["fault_type"] in ("availability",) else None
    rc, _, err = run_cmd(inject_cmd, timeout=inj_timeout, dry_run=args.dry_run)
    if rc not in (0,) and not args.dry_run:
        print(f"  WARN inject rc={rc}: {err.strip()}")

    # 3) detection + MTTD
    detect_timeout = dur + args.detect_grace
    detected, mttd, alerts = wait_for_detection(client, t_inject, detect_timeout, args.dry_run)

    # 4) RCA (only if something fired)
    rca_service, rca_conf, rca_ev = (None, None, None)
    if detected:
        rca_service, rca_conf, rca_ev = do_rca(client, t_inject, alerts, args.dry_run)

    # 5) rollback (always)
    print(f"  rollback: {show(rollback_cmd)}")
    run_cmd(rollback_cmd, dry_run=args.dry_run)

    cell = classify(bool(detected), gt) if detected is not None else "DRY"
    rca_ok = rca_is_correct(rca_service, gt) if detected else False

    result = {
        "idx": idx,
        "name": name,
        "fault_type": exp["fault_type"],
        "target_service": exp["target_service"],
        "inject_cmd": inject_cmd,
        "rollback_cmd": rollback_cmd,
        "t_inject": None if args.dry_run else iso(t_inject),
        "detected": None if detected is None else bool(detected),
        "mttd_s": mttd,
        "rca_service": rca_service,
        "rca_confidence": rca_conf,
        "rca_correct": None if detected is None else bool(rca_ok),
        "confusion_cell": cell,
        "baseline_false_alarms": baseline_fa,
        "ground_truth": gt,
        "dry_run": args.dry_run,
    }

    if not args.dry_run:
        d = "Y" if detected else "N"
        print(f"  -> detected={d}  mttd={mttd}  rca={rca_service}  rca_correct={rca_ok}  cell={cell}")
        # 6) cooldown to baseline (§8.3) — skip after the final experiment.
        if idx < args.total and args.cooldown > 0:
            print(f"  cooldown {args.cooldown}s...")
            time.sleep(args.cooldown)
    return result


# --------------------------------------------------------------------------- #
# Scoring + TODO #2 (from skeleton): print_scoreboard                         #
# --------------------------------------------------------------------------- #
def score(results):
    real = [r for r in results if not r.get("dry_run")]
    tp = sum(1 for r in real if r["confusion_cell"] == "TP")
    fn = sum(1 for r in real if r["confusion_cell"] == "FN")
    fp = sum(1 for r in real if r["confusion_cell"] == "FP")
    detected = sum(1 for r in real if r["detected"])
    rca_correct = sum(1 for r in real if r.get("rca_correct"))
    baseline_fa = sum(r.get("baseline_false_alarms", 0) for r in real)
    mttds = [r["mttd_s"] for r in real if r.get("mttd_s") is not None]

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {
        "total": len(results),
        "detected": detected,
        "rca_correct": rca_correct,
        "false_alarms_baseline": baseline_fa,
        "precision": round(precision, 2),
        "recall": round(recall, 2),
        "rca_accuracy": round(rca_correct / detected, 2) if detected else 0.0,
        "mttd_p50": percentile(mttds, 0.50),
        "mttd_p95": percentile(mttds, 0.95),
        "tp": tp, "fn": fn, "fp": fp,
    }


def _gaps(results):
    """Heuristic gap lines mapping symptoms to §7 failure modes."""
    out = []
    for r in results:
        if r.get("dry_run"):
            continue
        if r["detected"] is False:
            out.append(
                f"- {r['idx']} ({r['name']}): injected {r['fault_type']} not detected "
                f"-> detector miss; suspect noise-floor / mean-based threshold (§7.1) "
                f"or meta-monitoring blind spot (§7.5)."
            )
        elif r["detected"] and not r.get("rca_correct"):
            want = r["ground_truth"].get("accept_rca") or [r["ground_truth"].get("expected_rca_service")]
            out.append(
                f"- {r['idx']} ({r['name']}): RCA picked {r['rca_service']} not {want} "
                f"-> RCA root error; suspect topology-blind ranking / retry-storm trap (§7.2, §7.3)."
            )
        if r.get("baseline_false_alarms", 0) > 0:
            out.append(
                f"- {r['idx']} ({r['name']}): {r['baseline_false_alarms']} false alarm(s) in "
                f"baseline window -> detector over-sensitive / correlator merging noise (§7.2)."
            )
    return out


def print_scoreboard(results):
    """Confusion-matrix scoreboard in the exact §8.6 format."""
    s = score(results)
    dry = all(r.get("dry_run") for r in results)

    def f(x):
        return "-" if x is None else (f"{x:.2f}" if isinstance(x, float) else str(x))

    print("\n==== Chaos Run ====")
    print(f"Total: {s['total']}")
    if dry:
        print("(dry-run: commands built only — no detection/RCA/scoring performed)")
    print(f"Detected: {s['detected']}/{s['total']}")
    print(f"RCA correct: {s['rca_correct']}/{s['detected']}")
    print(f"False alarms in baseline windows: {s['false_alarms_baseline']}")
    print(f"Precision: {f(s['precision'])}")
    print(f"Recall: {f(s['recall'])}")
    print(f"MTTD p50: {f(s['mttd_p50'])}s, p95: {f(s['mttd_p95'])}s")

    # Per-experiment table
    print("\nPer-experiment:")
    cols = [("#", 2), ("name", 17), ("detected", 8), ("mttd", 5), ("rca_service", 12), ("rca_correct", 11)]
    header = "| " + " | ".join(c.ljust(w) for c, w in cols) + " |"
    sep = "|" + "|".join("-" * (w + 2) for _, w in cols) + "|"
    print(header)
    print(sep)
    for r in results:
        det = "-" if r["detected"] is None else ("Y" if r["detected"] else "N")
        rc = "-" if r["rca_correct"] is None else ("Y" if r["rca_correct"] else "N")
        mttd = "-" if r.get("mttd_s") is None else f"{r['mttd_s']:g}s"
        row = [
            str(r["idx"]).ljust(2),
            str(r["name"])[:17].ljust(17),
            det.ljust(8),
            mttd.ljust(5),
            str(r.get("rca_service") or "-")[:12].ljust(12),
            rc.ljust(11),
        ]
        print("| " + " | ".join(row) + " |")

    # Gaps + verdict
    if not dry:
        gaps = _gaps(results)
        print("\nGaps identified:")
        if gaps:
            for g in gaps:
                print(g)
        else:
            print("- none (all detected, RCA correct, no baseline false alarms)")

        passed = (
            s["detected"] >= ACC_MIN_DETECTED
            and (s["rca_correct"] >= math.ceil(ACC_MIN_RCA_FRACTION * s["detected"]) if s["detected"] else False)
            and s["false_alarms_baseline"] <= ACC_MAX_FALSE_ALARMS
        )
        print(
            f"\nAcceptance (§8.6): detected>={ACC_MIN_DETECTED}, "
            f"rca>={ACC_MIN_RCA_FRACTION:.0%} of detected, FA<={ACC_MAX_FALSE_ALARMS}  "
            f"=> {'PASS' if passed else 'FAIL'}"
        )
        if not passed:
            print("  (Do NOT tune the pipeline to force a pass — log the gap in chaos_report.md §4.)")


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def load_experiments(path):
    with open(path, "r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    exps = doc.get("experiments") if isinstance(doc, dict) else doc
    if not exps:
        raise SystemExit(f"No experiments found in {path}")
    return exps


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="W3-D2 chaos runner")
    ap.add_argument("--experiments", default="experiments.yaml")
    ap.add_argument("--results", default="chaos_results.json")
    ap.add_argument("--pipeline-url", default=DEFAULT_PIPELINE_URL)
    ap.add_argument("--dry-run", action="store_true",
                    help="build & print inject/rollback commands; no stack, no scoring")
    ap.add_argument("--only", default="",
                    help="comma-separated 1-based indices to run (e.g. 1,10)")
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_S)
    ap.add_argument("--baseline-window", type=int, default=DEFAULT_BASELINE_S)
    ap.add_argument("--detect-grace", type=int, default=DEFAULT_DETECT_GRACE_S)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    exps = load_experiments(args.experiments)

    only = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None
    selected = [(i + 1, e) for i, e in enumerate(exps) if (only is None or (i + 1) in only)]
    args.total = len(exps)

    print(f"Loaded {len(exps)} experiments from {args.experiments}"
          f"{' (DRY-RUN)' if args.dry_run else ''}")
    if not args.dry_run:
        print(f"Pipeline: {args.pipeline_url}  cooldown={args.cooldown}s "
              f"baseline={args.baseline_window}s")

    client = PipelineClient(args.pipeline_url)
    results = [run_experiment(e, client, args, idx) for idx, e in selected]

    with open(args.results, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    print(f"\nWrote {len(results)} results -> {args.results}")

    print_scoreboard(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
