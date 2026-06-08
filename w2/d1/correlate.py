"""
correlate.py — Alert correlation engine cho W2-D1.

Bốn layer, từ rẻ đến đắt:
  Layer 1  dedup            — gộp alert giống hệt (fingerprint), giữ 1 + count
  Layer 2  time-window      — gom alert cùng "session" (burst trong gap_sec)
  Layer 3  topology         — gom alert có service gần nhau (<= max_hop) trên service graph
  Layer 4  semantic (bonus) — Jaccard trên metric/note để bắt alert "cùng nói 1 chuyện"

Mục tiêu: 200 alert noise -> 3-7 cluster signal. Correlation KHÔNG tìm root cause,
nó chỉ rút gọn số việc RCA phải làm (D2 nhận output này làm input).
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import networkx as nx

# Severity càng cao càng nặng. Dùng rank thay vì max() trên string vì
# max("crit", "warn") == "warn" theo alphabet -> SAI. Phải so theo mức độ.
SEVERITY_RANK = {"info": 0, "warn": 1, "warning": 1, "error": 2, "crit": 3, "critical": 3}


def _parse_ts(ts: str) -> datetime:
    """ISO-8601 (kết thúc 'Z') -> datetime tz-aware (UTC)."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def max_severity(severities) -> str:
    """Trả về severity nặng nhất theo SEVERITY_RANK (không theo alphabet)."""
    return max(severities, key=lambda s: SEVERITY_RANK.get(s, -1))


# --------------------------------------------------------------------------- #
# Layer 1 — Dedup
# --------------------------------------------------------------------------- #
def fingerprint(alert: dict) -> str:
    """
    Khóa định danh "cùng 1 loại alert". KHÔNG include ts/value (đổi mỗi lần fire
    -> include thì không gì duplicate gì -> dedup vô dụng). KHÔNG include host
    (3 pod cùng service = 1 vấn đề). Có: service | metric | severity.
    """
    return f"{alert['service']}|{alert['metric']}|{alert['severity']}"


class Deduper:
    """Stateful dedup store: fingerprint -> cluster info. Production cần TTL evict."""

    def __init__(self):
        self.store: dict[str, dict] = {}

    def push(self, alert: dict) -> str:
        fp = fingerprint(alert)
        ts = _parse_ts(alert["ts"])
        if fp not in self.store:
            self.store[fp] = {
                "cluster_id": fp,
                "count": 1,
                "first_seen": ts,
                "last_seen": ts,
                "alerts": [alert["id"]],
                "max_value": alert["value"],
            }
        else:
            c = self.store[fp]
            c["count"] += 1
            c["last_seen"] = max(c["last_seen"], ts)
            c["first_seen"] = min(c["first_seen"], ts)
            c["alerts"].append(alert["id"])
            c["max_value"] = max(c["max_value"], alert["value"])
        return fp

    def clusters(self) -> list[dict]:
        return list(self.store.values())


def evict_stale(store: dict, ttl_sec: int = 3600, now: datetime | None = None) -> int:
    """Xoá entry cũ hơn ttl_sec (theo last_seen). Gọi định kỳ. Trả số entry đã xoá."""
    now = now or datetime.now(timezone.utc)
    stale = [k for k, v in store.items() if (now - v["last_seen"]).total_seconds() > ttl_sec]
    for k in stale:
        del store[k]
    return len(stale)


# --------------------------------------------------------------------------- #
# Layer 2 — Time-window (session)
# --------------------------------------------------------------------------- #
def session_groups(alerts: list[dict], gap_sec: int = 120) -> list[list[dict]]:
    """
    Session window: 1 group kết thúc khi không có alert mới trong gap_sec giây.
    Tự adapt theo burst (incident span 30s hay 4 phút đều được), khác tumbling
    cố định dễ cắt đôi 1 incident ở biên window.
    """
    if not alerts:
        return []
    s = sorted(alerts, key=lambda a: a["ts"])
    groups: list[list[dict]] = [[s[0]]]
    for alert in s[1:]:
        last_ts = _parse_ts(groups[-1][-1]["ts"])
        if (_parse_ts(alert["ts"]) - last_ts).total_seconds() <= gap_sec:
            groups[-1].append(alert)
        else:
            groups.append([alert])
    return groups


def sliding_window_buffer(alerts: list[dict], window_sec: int = 300) -> deque:
    """Tham khảo: buffer trượt 'alert nào còn trong window_sec gần đây' (live alerting)."""
    buf: deque = deque()
    for alert in sorted(alerts, key=lambda a: a["ts"]):
        ts = _parse_ts(alert["ts"])
        cutoff = ts - timedelta(seconds=window_sec)
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        buf.append((ts, alert))
    return buf


# --------------------------------------------------------------------------- #
# Layer 3 — Topology
# --------------------------------------------------------------------------- #
def build_graph(services) -> nx.DiGraph:
    """
    Build DiGraph: A -> B nghĩa là A GỌI B (A depends on B). RCA đi NGƯỢC edge.
    `services` có thể là path tới services.json hoặc dict đã load.
    """
    if isinstance(services, str):
        with open(services, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = services

    g = nx.DiGraph()
    for svc in data.get("services", []):
        g.add_node(svc["name"], node_type="service", **{k: v for k, v in svc.items() if k != "name"})
    for store in data.get("stores", []):
        g.add_node(store["name"], node_type="store", **{k: v for k, v in store.items() if k != "name"})
    for edge in data.get("edges", []):
        g.add_edge(edge["from"], edge["to"], type=edge.get("type"))
    return g


def topology_group(alerts: list[dict], graph: nx.DiGraph, max_hop: int = 2) -> list[list[dict]]:
    """
    Union-Find: 2 service cùng group nếu khoảng cách undirected <= max_hop.
    Dùng undirected vì cascade lan cả 2 chiều (effect lên upstream caller,
    propagation xuống downstream). Service không nằm trên graph -> đứng riêng.
    """
    if not alerts:
        return []
    undirected = graph.to_undirected()
    by_service: dict[str, list] = defaultdict(list)
    for a in alerts:
        by_service[a["service"]].append(a)
    svcs = list(by_service.keys())

    parent = {s: s for s in svcs}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i, s1 in enumerate(svcs):
        for s2 in svcs[i + 1:]:
            if s1 not in undirected or s2 not in undirected:
                continue
            try:
                if nx.shortest_path_length(undirected, s1, s2) <= max_hop:
                    union(s1, s2)
            except nx.NetworkXNoPath:
                continue

    out: dict[str, list] = defaultdict(list)
    for s in svcs:
        out[find(s)].extend(by_service[s])
    return list(out.values())


# --------------------------------------------------------------------------- #
# Layer 4 — Semantic (bonus)
# --------------------------------------------------------------------------- #
def text_similarity(alert_a: dict, alert_b: dict) -> float:
    """Jaccard trên token của metric + labels.note."""
    def tokens(a):
        text = f"{a.get('metric','')} {a.get('labels', {}).get('note', '')}"
        return set(text.lower().replace("_", " ").split())

    ta, tb = tokens(alert_a), tokens(alert_b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def _cluster_record(cid: str, group: list[dict]) -> dict:
    fps = sorted({fingerprint(a) for a in group})
    return {
        "cluster_id": cid,
        "alert_count": len(group),
        "unique_alert_types": len(fps),
        "services": sorted({a["service"] for a in group}),
        "alert_ids": [a["id"] for a in group],
        "time_range": [min(a["ts"] for a in group), max(a["ts"] for a in group)],
        "max_severity": max_severity(a["severity"] for a in group),
        "fingerprints": fps,
    }


def correlate(alerts: list[dict], graph: nx.DiGraph, gap_sec: int = 120,
              max_hop: int = 2) -> list[dict]:
    """
    Pipeline: sort -> session (Layer 2) -> trong mỗi session topology-group
    (Layer 3) -> cluster record (kèm dedup fingerprints của Layer 1).
    2 alert cùng cluster <=> vừa cùng time-window VỪA cùng topology component.
    """
    clusters: list[dict] = []
    for si, session in enumerate(session_groups(alerts, gap_sec=gap_sec)):
        for gi, group in enumerate(topology_group(session, graph, max_hop=max_hop)):
            clusters.append(_cluster_record(f"c-{si:03d}-{gi:03d}", group))
    return clusters


def summarize(alerts: list[dict], clusters: list[dict]) -> dict:
    n_in, n_out = len(alerts), len(clusters)
    return {
        "input_alerts": n_in,
        "output_clusters": n_out,
        "reduction_ratio": round(1 - n_out / n_in, 4) if n_in else 0.0,
        "clusters": clusters,
    }


def load_alerts(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


if __name__ == "__main__":
    import sys

    base = sys.argv[1] if len(sys.argv) > 1 else "lab/dataset"
    alerts = load_alerts(f"{base}/alerts_sample.jsonl")
    graph = build_graph(f"{base}/services.json")
    clusters = correlate(alerts, graph, gap_sec=120, max_hop=2)
    summary = summarize(alerts, clusters)
    print(json.dumps(summary, indent=2))
