"""
make_dataset.py — generate the synthetic W2-D2 RCA lab dataset.

Produces (deterministically):
  lab/dataset/services.json          — service dependency graph + metadata
  lab/dataset/alerts_sample.jsonl    — raw alert stream (one JSON per line)
  lab/dataset/incidents_history.json — 30 historical incidents (for RAG retrieval)
  results/cluster_summary.json       — the D1 correlator output this lab consumes

Scenario (cluster c-001-000): payment-svc is the culprit (connection-pool
exhaustion). checkout-svc / edge-lb / notification-svc / cart-svc / inventory-svc
are victims of the cascade. payment-svc alerts first and is the most-depended-on
node, so both the temporal and the graph signals point at it.

Two secondary clusters are included so the pipeline analyses 3 clusters:
  c-002-000: cart-redis (cache) is the real culprit — a "terminal store" edge case.
  c-003-000: kafka-broker rebalance storm; notification-svc is the victim.

Run:  python make_dataset.py
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATASET_DIR = os.path.join(HERE, "lab", "dataset")
RESULTS_DIR = os.path.join(HERE, "results")
os.makedirs(DATASET_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 1. Service graph.  Edge A -> B means "A calls B" (A depends on B).
#    Failure propagates *against* the edge: if B breaks, its callers break too.
# ---------------------------------------------------------------------------
SERVICES = {
    "edge-lb":          {"type": "lb",      "criticality": "high",     "team": "platform"},
    "checkout-svc":     {"type": "service", "criticality": "critical", "team": "checkout"},
    "payment-svc":      {"type": "service", "criticality": "critical", "team": "payments"},
    "cart-svc":         {"type": "service", "criticality": "high",     "team": "checkout"},
    "inventory-svc":    {"type": "service", "criticality": "high",     "team": "fulfillment"},
    "notification-svc": {"type": "service", "criticality": "medium",   "team": "growth"},
    "catalog-svc":      {"type": "service", "criticality": "medium",   "team": "catalog"},
    "payments-db":      {"type": "store",   "criticality": "critical", "team": "payments"},
    "cart-redis":       {"type": "cache",   "criticality": "high",     "team": "checkout"},
    "kafka-broker":     {"type": "queue",   "criticality": "high",     "team": "platform"},
    "inventory-db":     {"type": "store",   "criticality": "high",     "team": "fulfillment"},
}

# depends_on: caller -> [callees]
DEPENDS_ON = {
    "edge-lb":          ["checkout-svc", "catalog-svc", "cart-svc"],
    "checkout-svc":     ["payment-svc", "inventory-svc", "cart-svc", "notification-svc"],
    "cart-svc":         ["cart-redis", "payment-svc"],
    "payment-svc":      ["payments-db"],
    "notification-svc": ["payment-svc", "kafka-broker"],
    "catalog-svc":      ["cart-redis"],
    "inventory-svc":    ["inventory-db"],
}

services_doc = {
    "platform": "GeekShop",
    "generated_at": iso(datetime(2026, 6, 8, 3, 0, 0, tzinfo=timezone.utc)),
    "services": [
        {"name": name, **meta, "depends_on": DEPENDS_ON.get(name, [])}
        for name, meta in SERVICES.items()
    ],
}
with open(os.path.join(DATASET_DIR, "services.json"), "w", encoding="utf-8") as f:
    json.dump(services_doc, f, indent=2)


# ---------------------------------------------------------------------------
# 2. Alerts.  Each cluster has a culprit that alerts first, then victims.
# ---------------------------------------------------------------------------
BASE = datetime(2026, 6, 8, 3, 0, 0, tzinfo=timezone.utc)

METRIC_BY_SVC = {
    "payment-svc":      ("db_pool_utilization", "critical", "payment-svc DB connection pool {pct}% utilized; acquire timeouts"),
    "checkout-svc":     ("latency_p99_ms", "critical", "checkout-svc p99 latency {pct}ms; downstream payment calls timing out"),
    "edge-lb":          ("5xx_rate", "high", "edge-lb 5xx rate {pct}% on /checkout route"),
    "notification-svc": ("kafka_consumer_lag", "high", "notification-svc consumer lag {pct} msgs; order-confirm emails delayed"),
    "cart-svc":         ("latency_p99_ms", "high", "cart-svc p99 latency {pct}ms"),
    "inventory-svc":    ("error_rate", "high", "inventory-svc error rate {pct}%"),
    "catalog-svc":      ("latency_p99_ms", "medium", "catalog-svc p99 latency {pct}ms; redis reads slow"),
    "cart-redis":       ("connected_clients", "critical", "cart-redis connected_clients {pct}; maxclients reached, refusing connections"),
    "kafka-broker":     ("under_replicated_partitions", "critical", "kafka-broker rebalance storm; {pct} under-replicated partitions"),
}


def make_alerts(cluster_id, plan):
    """plan: list of (service, count, first_offset_seconds). Returns alert dicts."""
    out = []
    seq = 0
    for service, count, first_off in plan:
        metric, sev, tmpl = METRIC_BY_SVC[service]
        for i in range(count):
            # first alert at first_off, the rest fan out afterwards
            ts = BASE + timedelta(seconds=first_off + i * 23)
            pct = 70 + (i * 7) % 30
            out.append({
                "alert_id": f"{cluster_id}-{service}-{i:02d}",
                "service": service,
                "ts": iso(ts),
                "severity": sev,
                "metric": metric,
                "value": round(0.7 + ((i * 7) % 30) / 100.0, 3),
                "summary": tmpl.format(pct=pct),
                "cluster_id": cluster_id,
            })
            seq += 1
    return out


# (service, count, first-alert offset in seconds from BASE)
CLUSTER_PLANS = {
    "c-001-000": [
        ("payment-svc",      14, 14 * 60 + 5),   # 03:14:05  earliest -> culprit
        ("checkout-svc",     12, 14 * 60 + 40),  # 03:14:40
        ("cart-svc",          4, 15 * 60 + 10),  # 03:15:10
        ("inventory-svc",     3, 15 * 60 + 30),  # 03:15:30
        ("notification-svc",  6, 16 * 60 + 0),   # 03:16:00
        ("edge-lb",           9, 16 * 60 + 30),  # 03:16:30  latest -> top-of-stack victim
    ],
    "c-002-000": [
        ("cart-redis",   8, 62 * 60 + 0),   # 04:02:00  earliest -> cache culprit
        ("catalog-svc",  5, 62 * 60 + 30),  # 04:02:30
        ("cart-svc",     6, 62 * 60 + 50),  # 04:02:50
    ],
    "c-003-000": [
        ("kafka-broker",     7, 130 * 60 + 0),   # 05:10:00  earliest -> culprit
        ("notification-svc", 6, 130 * 60 + 40),  # 05:10:40
    ],
}

CLUSTER_SERVICES = {
    "c-001-000": ["edge-lb", "checkout-svc", "payment-svc", "notification-svc", "cart-svc", "inventory-svc"],
    "c-002-000": ["catalog-svc", "cart-svc", "cart-redis"],
    "c-003-000": ["notification-svc", "kafka-broker"],
}

all_alerts = []
clusters = []
for cid, plan in CLUSTER_PLANS.items():
    alerts = make_alerts(cid, plan)
    all_alerts.extend(alerts)
    ts_sorted = sorted(a["ts"] for a in alerts)
    sev_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}
    max_sev = max((a["severity"] for a in alerts), key=lambda s: sev_rank[s])
    clusters.append({
        "cluster_id": cid,
        "services": CLUSTER_SERVICES[cid],
        "time_range": [ts_sorted[0], ts_sorted[-1]],
        "max_severity": max_sev,
        "alert_count": len(alerts),
        "alert_ids": [a["alert_id"] for a in alerts],
    })

# stable order by timestamp
all_alerts.sort(key=lambda a: a["ts"])
with open(os.path.join(DATASET_DIR, "alerts_sample.jsonl"), "w", encoding="utf-8") as f:
    for a in all_alerts:
        f.write(json.dumps(a) + "\n")

cluster_summary = {
    "generated_at": iso(BASE),
    "source": "w2-d1-correlator",
    "n_clusters": len(clusters),
    "clusters": clusters,
}
with open(os.path.join(RESULTS_DIR, "cluster_summary.json"), "w", encoding="utf-8") as f:
    json.dump(cluster_summary, f, indent=2)


# ---------------------------------------------------------------------------
# 3. Incident history (30 incidents) for RAG retrieval.
# ---------------------------------------------------------------------------
SEED_INCIDENTS = [
    # --- payment-svc connection-pool family (the main scenario's neighbours) ---
    {"root_cause_service": "payment-svc", "root_cause_class": "connection_pool_exhaustion",
     "services_involved": ["payment-svc", "checkout-svc", "edge-lb"], "severity": "critical",
     "summary": "payment-svc v3.2 shipped with pool size 50; Black-Friday traffic exhausted it, checkout cascaded.",
     "remediation": "Rolled back payment-svc to v3.1 and raised HikariCP pool 50 -> 120."},
    {"root_cause_service": "payment-svc", "root_cause_class": "connection_pool_exhaustion",
     "services_involved": ["payment-svc", "checkout-svc"], "severity": "critical",
     "summary": "payment-svc DB pool acquire timeouts after a slow downstream provider held connections open.",
     "remediation": "Set acquisition timeout to 2s, added circuit breaker on provider calls, bumped pool to 100."},
    {"root_cause_service": "payment-svc", "root_cause_class": "slow_query",
     "services_involved": ["payment-svc", "payments-db", "checkout-svc"], "severity": "high",
     "summary": "Missing index on payments.txn(merchant_id, created_at) made settlement query table-scan.",
     "remediation": "Added composite index; query dropped 4s -> 30ms."},
    # --- cart-redis / cache family ---
    {"root_cause_service": "cart-redis", "root_cause_class": "connection_pool_exhaustion",
     "services_involved": ["cart-redis", "cart-svc", "catalog-svc"], "severity": "critical",
     "summary": "cart-redis hit maxclients=10000; a client leak in cart-svc held idle connections, catalog reads failed.",
     "remediation": "Raised maxclients, fixed cart-svc connection leak, added idle-timeout."},
    {"root_cause_service": "cart-redis", "root_cause_class": "memory_leak",
     "services_involved": ["cart-redis", "cart-svc"], "severity": "high",
     "summary": "cart-redis evicting keys under maxmemory; cart sessions dropped during sale.",
     "remediation": "Increased maxmemory, switched eviction to allkeys-lru, sharded cart keyspace."},
    # --- kafka / rebalance family ---
    {"root_cause_service": "kafka-broker", "root_cause_class": "rebalance_storm",
     "services_involved": ["kafka-broker", "notification-svc"], "severity": "critical",
     "summary": "Consumer group rebalance storm after a notification-svc deploy flapped liveness; lag spiked.",
     "remediation": "Raised session.timeout.ms and max.poll.interval.ms, fixed flapping health check."},
    {"root_cause_service": "kafka-broker", "root_cause_class": "rebalance_storm",
     "services_involved": ["kafka-broker", "notification-svc", "checkout-svc"], "severity": "high",
     "summary": "Broker under-replicated partitions during rolling restart; consumers rebalanced repeatedly.",
     "remediation": "Throttled restart, increased replica.lag.time.max.ms, paused non-critical consumers."},
    # --- other realistic incidents (filler that should NOT match strongly) ---
    {"root_cause_service": "checkout-svc", "root_cause_class": "bad_deploy",
     "services_involved": ["checkout-svc", "edge-lb"], "severity": "high",
     "summary": "checkout-svc v8 NPE on guest-checkout path; 5xx on edge-lb.",
     "remediation": "Rolled back checkout-svc v8."},
    {"root_cause_service": "inventory-svc", "root_cause_class": "deadlock",
     "services_involved": ["inventory-svc", "inventory-db"], "severity": "high",
     "summary": "Row-lock deadlock on stock decrement under concurrent orders.",
     "remediation": "Reordered lock acquisition, added retry-on-deadlock."},
    {"root_cause_service": "edge-lb", "root_cause_class": "tls_expiry",
     "services_involved": ["edge-lb"], "severity": "critical",
     "summary": "Wildcard TLS cert expired; all HTTPS terminations failed.",
     "remediation": "Rotated cert, added 30-day expiry alert."},
    {"root_cause_service": "edge-lb", "root_cause_class": "ddos",
     "services_involved": ["edge-lb", "catalog-svc"], "severity": "critical",
     "summary": "L7 flood on /search drove edge-lb saturation.",
     "remediation": "Enabled rate limiting + WAF rule, scaled lb."},
    {"root_cause_service": "catalog-svc", "root_cause_class": "config_push",
     "services_involved": ["catalog-svc", "cart-redis"], "severity": "medium",
     "summary": "Bad feature-flag push disabled catalog cache, hammering redis.",
     "remediation": "Reverted flag, added staged rollout."},
    {"root_cause_service": "payments-db", "root_cause_class": "network_partition",
     "services_involved": ["payments-db", "payment-svc"], "severity": "critical",
     "summary": "AZ network partition isolated payments-db primary; failover delayed.",
     "remediation": "Forced failover, tuned failover detector."},
    {"root_cause_service": "notification-svc", "root_cause_class": "memory_leak",
     "services_involved": ["notification-svc"], "severity": "medium",
     "summary": "Template cache leak OOM-killed notification pods.",
     "remediation": "Bounded cache, raised memory limit."},
    {"root_cause_service": "cart-svc", "root_cause_class": "slow_query",
     "services_involved": ["cart-svc", "cart-redis"], "severity": "medium",
     "summary": "N+1 redis MGET pattern on cart render.",
     "remediation": "Batched into pipeline."},
]

# expand to 30 with plausible filler variants
classes = ["bad_deploy", "config_push", "slow_query", "deadlock", "memory_leak",
           "network_partition", "tls_expiry", "ddos", "other"]
svcs = list(SERVICES.keys())
incidents = []
for idx, base in enumerate(SEED_INCIDENTS):
    incidents.append(base)
i = len(SEED_INCIDENTS)
day = 1
while len(incidents) < 30:
    svc = svcs[i % len(svcs)]
    cls = classes[i % len(classes)]
    incidents.append({
        "root_cause_service": svc,
        "root_cause_class": cls,
        "services_involved": [svc],
        "severity": ["high", "medium", "low"][i % 3],
        "summary": f"{svc} {cls.replace('_', ' ')} incident during routine operations.",
        "remediation": f"Standard {cls.replace('_', ' ')} remediation applied to {svc}.",
    })
    i += 1

# stamp ids + dates (descending from 2026-05 back)
history = []
d = datetime(2026, 5, 30, tzinfo=timezone.utc)
for n, inc in enumerate(incidents):
    date = d - timedelta(days=n * 6)
    history.append({"id": f"INC-{date.strftime('%Y-%m-%d')}", "date": date.strftime("%Y-%m-%d"), **inc})

with open(os.path.join(DATASET_DIR, "incidents_history.json"), "w", encoding="utf-8") as f:
    json.dump(history, f, indent=2)

print("Wrote:")
print("  lab/dataset/services.json          ", len(SERVICES), "services")
print("  lab/dataset/alerts_sample.jsonl    ", len(all_alerts), "alerts")
print("  lab/dataset/incidents_history.json ", len(history), "incidents")
print("  results/cluster_summary.json       ", len(clusters), "clusters")
