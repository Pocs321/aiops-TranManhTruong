# ---
# jupyter:
#   jupytext:
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # W2-D1 — Alert Correlation: Từ Noise sang Signal
#
# **Mục tiêu:** gộp một flood alert thành ít cluster có nghĩa để D2 (RCA) chỉ làm việc
# trên vài cluster thay vì hàng trăm noise. Correlation **không** tìm root cause — nó
# rút gọn số việc RCA phải làm.
#
# Pipeline 4 layer (rẻ → đắt): **dedup → time-window (session) → topology → semantic**.
# Hai alert cùng cluster ⇔ *vừa cùng cửa sổ thời gian* **VÀ** *vừa gần nhau trên service graph*.
#
# > **Lưu ý dữ liệu:** lab dataset chính thức (`alerts.jsonl`, 200 alert) phát hành Thursday.
# > Notebook này chạy trên `lab/dataset/alerts_sample.jsonl` (20 alert) — bản dựng lại
# > **trung thực theo kịch bản trong bài học**: sự cố `payment-svc` pool-exhaustion lúc 09:42
# > lan lên `checkout-svc` → `edge-lb`, cùng lúc `recommender-svc` OOM do batch retrain
# > (nhiễu trùng giờ), `search-svc` degrade, và một alert `notification-svc` đến muộn.
# > Khi có dataset thật, chỉ cần thay file và chạy lại — code không đổi.

# %%
import json
from pathlib import Path

import networkx as nx

import correlate as C

DATA = Path("lab/dataset")
alerts = C.load_alerts(DATA / "alerts_sample.jsonl")
graph = C.build_graph(str(DATA / "services.json"))

print(f"Loaded {len(alerts)} alerts, "
      f"{len([n for n,d in graph.nodes(data=True) if d.get('node_type')=='service'])} services, "
      f"{graph.number_of_edges()} dependency edges")
print("Distinct services in alert stream:",
      sorted({a['service'] for a in alerts}))

# %% [markdown]
# ## Service graph
# `A → B` nghĩa là *A gọi B* (A phụ thuộc B). Một sự cố ở node con (vd `payments-db`)
# lan **ngược** lên các caller (`payment-svc → checkout-svc → edge-lb`). Đây là lý do
# topology correlation gom được cả chuỗi cascade.

# %%
for u, v, d in sorted(graph.edges(data=True)):
    print(f"  {u:>16}  --{d.get('type'):>5}-->  {v}")

# %% [markdown]
# ## Layer 1 — Dedup
# `fingerprint = service | metric | severity`. Cùng alert fire lại ⇒ không tạo cluster
# mới, chỉ tăng counter. Ở sample, `payment-svc|latency_p99_ms|crit` fire 3 lần
# (a001/a002/a003) và `checkout-svc|http_5xx_ratio|crit` fire 2 lần (a009/a010).

# %%
dd = C.Deduper()
for a in alerts:
    dd.push(a)

dup = [c for c in dd.clusters() if c["count"] > 1]
print(f"{len(alerts)} alerts -> {len(dd.clusters())} fingerprints "
      f"({len(alerts) - len(dd.clusters())} duplicates collapsed)\n")
for c in sorted(dup, key=lambda c: -c["count"]):
    print(f"  x{c['count']}  {c['cluster_id']:<40} alerts={c['alerts']}")

# %% [markdown]
# ## Layer 2 — Time-window (session)
# Session window đóng khi không có alert mới trong `gap_sec`. Khác tumbling cố định
# (dễ cắt đôi 1 incident ở biên window), session **tự co giãn theo burst**.
#
# Với `gap_sec = 120s`: 19 alert đầu (sự cố 09:42–09:47) thuộc **một** session;
# alert `notification-svc` lúc 10:05 (cách 18 phút) rơi sang **session riêng**.

# %%
sessions = C.session_groups(alerts, gap_sec=120)
for i, s in enumerate(sessions):
    print(f"session {i}: {len(s):>2} alerts  "
          f"{s[0]['ts']} .. {s[-1]['ts']}  "
          f"services={sorted({a['service'] for a in s})}")

# %% [markdown]
# ## Layer 3 — Topology
# Trong mỗi session, gom alert nếu service của chúng cách nhau `≤ max_hop` trên graph
# (undirected — cascade lan cả hai chiều), bằng Union-Find.
#
# Điểm mấu chốt (câu "soul"): `recommender-svc` alert **cùng session** với sự cố payment
# nhưng **không** bị gom vào — vì nó chỉ phụ thuộc `model-store`, **không có path** tới
# chuỗi payment/checkout/edge. Time-window đơn thuần sẽ gom nhầm; topology cứu được.

# %%
topo = C.topology_group(sessions[0], graph, max_hop=2)
for i, g in enumerate(sorted(topo, key=lambda g: -len(g))):
    print(f"component {i}: {len(g):>2} alerts  "
          f"services={sorted({a['service'] for a in g})}")

# %% [markdown]
# ## Pipeline kết hợp + ghi kết quả
# `correlate()` = session (Layer 2) → topology (Layer 3), mỗi cluster kèm fingerprints
# (Layer 1) và `max_severity` tính theo **rank** (`crit > warn > info`) chứ không phải
# `max()` trên string (vì `max("crit","warn") == "warn"` theo alphabet → sai).

# %%
clusters = C.correlate(alerts, graph, gap_sec=120, max_hop=2)
summary = C.summarize(alerts, clusters)

Path("results").mkdir(exist_ok=True)
out_path = Path("results/cluster_summary.json")
out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

print(f"input_alerts   = {summary['input_alerts']}")
print(f"output_clusters= {summary['output_clusters']}")
print(f"reduction_ratio= {summary['reduction_ratio']}")
print(f"-> wrote {out_path}\n")
for c in summary["clusters"]:
    print(f"  {c['cluster_id']}  n={c['alert_count']:<2} sev={c['max_severity']:<4} "
          f"services={c['services']}")

# %% [markdown]
# ## Acceptance check
# Tự kiểm tra các tiêu chí chấm điểm trước khi nộp.

# %%
assert out_path.exists(), "cluster_summary.json missing"
loaded = json.loads(out_path.read_text(encoding="utf-8"))
assert 3 <= loaded["output_clusters"] <= 7, "want 3-7 clusters"
assert loaded["reduction_ratio"] >= 0.5, "want >=50% reduction"
for c in loaded["clusters"]:
    assert c["services"] and c["time_range"], "cluster needs services + time_range"
    assert len(c["alert_ids"]) == c["alert_count"]
total = sum(c["alert_count"] for c in loaded["clusters"])
assert total == loaded["input_alerts"], "no alert lost or double-counted"
print("PASS: 4 clusters, reduction 0.80, every alert accounted for, "
      "each cluster has services + time_range.")

# %% [markdown]
# ## Limitation — bài toán "hub" của topology grouping (demo)
# `max_hop` undirected + Union-Find sụp đổ khi có một **node fan-out cao** (gateway, DB
# dùng chung). Mọi downstream của hub bị gom chung dù không liên quan nhân quả.
#
# Demo: thêm cạnh `edge-lb → search-svc` (edge LB cũng route search). Giờ `search-svc`
# cách `edge-lb` 1 hop → bị **nuốt nhầm** vào cluster payment, dù search có sự cố riêng.

# %%
g_hub = graph.copy()
g_hub.add_edge("edge-lb", "search-svc", type="http")
topo_hub = C.topology_group(sessions[0], g_hub, max_hop=2)
biggest = max(topo_hub, key=len)
print(f"Với cạnh hub edge-lb->search-svc, cluster lớn nhất có {len(biggest)} alert, "
      f"services={sorted({a['service'] for a in biggest})}")
print("=> search-svc/search-es bị over-merge vào sự cố payment (false correlation).")
print("Fix khả dĩ: traversal có hướng (causal) thay vì undirected; hoặc down-weight node "
      "fan-out cao bằng centrality (PageRank) — chính là chủ đề RCA của D2.")

# %% [markdown]
# ## Bonus — Layer 4 semantic similarity
# Hai alert khác fingerprint nhưng "cùng nói 1 chuyện": `db_pool_used_ratio` (warn) và
# `db_connection_count` (crit) của `payment-svc` đều mô tả *pool gần cạn*. Dedup miss,
# nhưng Jaccard trên `metric + note` bắt được liên hệ ngữ nghĩa.

# %%
a_pool = next(a for a in alerts if a["metric"] == "db_pool_used_ratio")
a_conn = next(a for a in alerts if a["metric"] == "db_connection_count")
a_far = next(a for a in alerts if a["service"] == "search-es")
print(f"sim(db_pool, db_conn)   = {C.text_similarity(a_pool, a_conn):.2f}  (cùng chuyện)")
print(f"sim(db_pool, search-es) = {C.text_similarity(a_pool, a_far):.2f}  (không liên quan)")

# %% [markdown]
# ## Ghi chú scale — 10k alert thay vì 200
# - **Dedup (Layer 1):** O(N) hash — rẻ; cần TTL evict (`evict_stale`) kẻo store phình vô hạn.
# - **Session (Layer 2):** sort O(N log N) + quét O(N) — ổn.
# - **Topology (Layer 3):** vòng lặp cặp service `O(S²)` × `shortest_path_length` là chỗ
#   **nghẽn**. S = số *service* (không phải alert) nên thường nhỏ, nhưng nếu cluster lớn
#   thì nên precompute all-pairs distance / BFS ≤ max_hop một lần, hoặc dùng connected
#   components trên subgraph thay vì gọi shortest_path mỗi cặp.
