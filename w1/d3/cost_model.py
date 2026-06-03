from __future__ import annotations

import math
import sys
from dataclasses import dataclass

try:                                    # Windows cp1252 -> ép UTF-8 cho tiếng Việt
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SECONDS_PER_MONTH = 30 * 86400          # 2,592,000

# --------------------- Đơn giá (USD) — giả định, public-cloud ----------------
PRICES = {
    "block_gb_mo":   0.08,    # SSD gp3 $/GB-month (hot)
    "object_gb_mo":  0.023,   # S3 Standard $/GB-month (warm)
    "archive_gb_mo": 0.004,   # S3 Glacier $/GB-month (cold)
    "vcpu_mo":       24.0,    # $/vCPU-month (~$0.033/hr on-demand blended)
    "ram_gb_mo":     3.0,     # $/GB-RAM-month
    "egress_gb":     0.02,    # cross-AZ transfer $/GB
    "sre_mo":        13_000.0,# 1 platform/SRE engineer fully-loaded $/month
}

# ----------------------- Hệ số kỹ thuật (minh bạch) --------------------------
BYTES_PER_SAMPLE   = 1.0      # VictoriaMetrics nén ~1 byte/sample khi lưu
WIRE_BYTES_SAMPLE  = 25.0     # metric trên dây (đã batch/nén)
LOG_BYTES_LINE     = 600.0    # 1 dòng log ~600 bytes
LOG_COMPRESSION    = 0.30     # Loki/Parquet nén ~3.3:1
TRACE_BYTES        = 15_000   # 1 trace ~50 span x 300B
TRACE_SAMPLE       = 0.01     # tail-based 1%
TRACES_PER_SVC_DAY = 100_000  # 100 svc -> 10M traces/day (đúng ví dụ bài học)

# Datadog (buy) — public list pricing xấp xỉ, annual commit
DD = {
    "host_mo":        40.0,   # Infra Pro $15 + APM $31 ~ blended/host
    "log_ingest_gb":  0.10,   # $/GB ingest
    "log_index_m":    2.50,   # $/triệu event index (15d retain)
    "log_index_frac": 0.20,   # thực tế chỉ index ~20% volume
    "custom_metric":  0.05,   # $/100 custom timeseries/month
    "incl_metric_host": 100,  # custom metric included / host
    "apm_host":       5.0,    # span ingest phụ trội / host
}


@dataclass
class Tier:
    name: str
    services: int
    log_gb_day: float
    metric_eps: float         # metric samples/sec
    instances_per_svc: int    # cho Datadog host pricing
    sre_fte: float            # SRE quy riêng cho observability stack


TIERS = [
    Tier("Small",   10,    50,      100_000,  4, 0.3),
    Tier("Medium",  100,   500,   1_000_000,  4, 1.0),
    Tier("Large",  1000,  5000,  10_000_000,  4, 3.0),
]


# ------------------------------- helpers ----------------------------------
def _compute(vcpu: float, ram_gb: float) -> float:
    return vcpu * PRICES["vcpu_mo"] + ram_gb * PRICES["ram_gb_mo"]


def money(x: float) -> str:
    return f"${x:,.0f}"


# ----------------------- self-host components -----------------------------
def metrics_component(t: Tier) -> dict:
    vcpu = math.ceil(max(2, math.ceil(t.metric_eps / 100_000)) * 1.5)
    ram = vcpu * 8
    gb = t.metric_eps * SECONDS_PER_MONTH * BYTES_PER_SAMPLE / 1e9
    return {"name": "Metrics (VictoriaMetrics)",
            "storage": gb * PRICES["block_gb_mo"], "compute": _compute(vcpu, ram), "network": 0.0}


def logs_component(t: Tier) -> dict:
    vcpu = max(2, math.ceil(t.log_gb_day / 15))
    ram = vcpu * 8
    daily = t.log_gb_day * LOG_COMPRESSION
    hot = daily * 30 * PRICES["object_gb_mo"]       # 0-30d  (Loki chunks @ S3)
    warm = daily * 60 * PRICES["object_gb_mo"]      # 30-90d
    cold = daily * 275 * PRICES["archive_gb_mo"]    # 90-365d (Parquet/Glacier)
    return {"name": "Logs (Loki + S3 tiering)",
            "storage": hot + warm + cold, "compute": _compute(vcpu, ram), "network": 0.0}


def traces_component(t: Tier) -> dict:
    traces_day = t.services * TRACES_PER_SVC_DAY
    gb_day = traces_day * TRACE_SAMPLE * TRACE_BYTES / 1e9
    storage_gb = gb_day * 15 * 0.5                  # 15d retain, nén 0.5
    vcpu = max(2, math.ceil(traces_day / 5_000_000))
    ram = vcpu * 6
    return {"name": "Traces (Jaeger, 1% sample)",
            "storage": storage_gb * PRICES["object_gb_mo"], "compute": _compute(vcpu, ram), "network": 0.0}


def transport_component(t: Tier) -> dict:
    log_eps = t.log_gb_day * 1e9 / LOG_BYTES_LINE / 86400
    brokers = max(3, math.ceil((t.metric_eps + log_eps) / 250_000))
    vcpu, ram = brokers * 4, brokers * 16
    metric_bytes_day = t.metric_eps * 86400 * WIRE_BYTES_SAMPLE
    log_bytes_day = t.log_gb_day * 1e9 * LOG_COMPRESSION
    disk_gb = (metric_bytes_day + log_bytes_day) / 1e9 * 1 * 3   # 1d retention x RF3
    return {"name": f"Transport (Kafka, {brokers} brokers)",
            "storage": disk_gb * PRICES["block_gb_mo"], "compute": _compute(vcpu, ram), "network": 0.0}


def processing_component(t: Tier) -> dict:
    vcpu = max(2, math.ceil(t.metric_eps / 200_000 + t.log_gb_day / 15))
    ram = vcpu * 8
    return {"name": "Processing (Flink)",
            "storage": 0.0, "compute": _compute(vcpu, ram), "network": 0.0}


def network_component(t: Tier) -> dict:
    metric_bytes_day = t.metric_eps * 86400 * WIRE_BYTES_SAMPLE
    log_bytes_day = t.log_gb_day * 1e9 * LOG_COMPRESSION
    trace_bytes_day = t.services * TRACES_PER_SVC_DAY * TRACE_SAMPLE * TRACE_BYTES
    gb_mo = (metric_bytes_day + log_bytes_day + trace_bytes_day) / 1e9 * 30
    cross_az = gb_mo * 1.5      # replication + cross-AZ consumer reads
    return {"name": "Network (cross-AZ egress)",
            "storage": 0.0, "compute": 0.0, "network": cross_az * PRICES["egress_gb"]}


BUILD_COMPONENTS = [metrics_component, logs_component, traces_component,
                    transport_component, processing_component, network_component]


# ----------------------------- buy (Datadog) ------------------------------
def datadog_estimate(t: Tier) -> dict:
    hosts = t.services * t.instances_per_svc
    host_cost = hosts * DD["host_mo"]
    gb_mo = t.log_gb_day * 30
    events_m = t.log_gb_day * 1e9 / LOG_BYTES_LINE * 30 / 1e6
    log_cost = gb_mo * DD["log_ingest_gb"] + events_m * DD["log_index_frac"] * DD["log_index_m"]
    overage = max(0.0, t.metric_eps - hosts * DD["incl_metric_host"])
    metric_cost = overage / 100 * DD["custom_metric"]
    apm = hosts * DD["apm_host"]
    return {"Hosts (infra+APM)": host_cost, "Logs (ingest+index)": log_cost,
            "Custom metrics": metric_cost, "APM spans": apm,
            "_total": host_cost + log_cost + metric_cost + apm, "_hosts": hosts}


# -------------------------------- output ----------------------------------
def print_build_table(t: Tier) -> tuple[float, dict]:
    print(f"\n### {t.name}: {t.services} svc · {t.log_gb_day:g} GB log/day · "
          f"{t.metric_eps:,.0f} metric eps")
    hdr = f"{'Component':32}{'Storage':>11}{'Compute':>11}{'Network':>11}{'Subtotal':>11}"
    print(hdr); print("-" * len(hdr))
    tot = {"storage": 0.0, "compute": 0.0, "network": 0.0}
    for fn in BUILD_COMPONENTS:
        c = fn(t)
        sub = c["storage"] + c["compute"] + c["network"]
        for k in tot:
            tot[k] += c[k]
        print(f"{c['name']:32}{money(c['storage']):>11}{money(c['compute']):>11}"
              f"{money(c['network']):>11}{money(sub):>11}")
    grand = sum(tot.values())
    print("-" * len(hdr))
    print(f"{'BUILD infra total':32}{money(tot['storage']):>11}{money(tot['compute']):>11}"
          f"{money(tot['network']):>11}{money(grand):>11}")
    return grand, tot


def main() -> None:
    print("=" * 78)
    print("W1-D3 · Phase 2 — Monthly cost: self-host (build) vs Datadog (buy)")
    print("Đơn giá: xem dict PRICES / DD (ballpark AWS + Datadog list 2024-2025).")
    print("=" * 78)

    rows = []
    for t in TIERS:
        infra, _ = print_build_table(t)
        sre = t.sre_fte * PRICES["sre_mo"]
        build_total = infra + sre
        dd = datadog_estimate(t)
        buy = dd["_total"]

        print(f"\n  Datadog (buy) — {t.name}  [{dd['_hosts']:,} hosts]:")
        for k, v in dd.items():
            if k.startswith("_"):
                continue
            print(f"    {k:24}{money(v):>14}")
        print(f"    {'Datadog TOTAL':24}{money(buy):>14}")

        rec = "BUILD" if build_total < buy else "BUY"
        rows.append((t.name, infra, sre, build_total, buy, rec))

    print("\n" + "=" * 78)
    print("SUMMARY — build (infra + SRE) vs buy (Datadog)")
    h = (f"{'Tier':8}{'Build infra':>14}{'+ SRE':>12}{'Build total':>14}"
         f"{'Buy (Datadog)':>16}{'Cheaper':>10}")
    print(h); print("-" * len(h))
    for name, infra, sre, bt, buy, rec in rows:
        print(f"{name:8}{money(infra):>14}{money(sre):>12}{money(bt):>14}"
              f"{money(buy):>16}{rec:>10}")
    print("-" * len(h))
    fte = ", ".join(f"{t.name} {t.sre_fte} FTE" for t in TIERS)
    print(f"\nGhi chú: 'Build infra' chưa gồm người vận hành; 'Build total' = infra + SRE ({fte}).")
    print("Crossover: scale nhỏ -> BUY (đỡ headcount + time-to-value);")
    print("           scale lớn -> BUILD (economics of scale nuốt chi phí headcount).")


if __name__ == "__main__":
    main()
