from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

# Windows console mặc định cp1252 không in được tiếng Việt -> ép stdout sang UTF-8.
import sys
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --------------------------------- config ---------------------------------
SEED = 42
INTERVAL_S = 15            # scrape interval (giống Prometheus mặc định 15s)
DURATION_MIN = 120         # 2 giờ dữ liệu
WINDOW_POINTS = 20         # rolling window = 20 * 15s = 5 phút
EWMA_ALPHA = 0.2
Z_THRESHOLD = 3.0
HOSTS = ["pay-1", "pay-2", "pay-3"]
OUT_DIR = Path(__file__).parent

# cửa sổ sự cố: phút 70 -> 85 trên 1 host (latency + error + cpu spike, throughput tụt)
INCIDENT_START_MIN = 70
INCIDENT_END_MIN = 85
INCIDENT_HOST = "pay-2"

random.seed(SEED)


@dataclass(frozen=True)
class MetricSpec:
    name: str
    baseline: float
    noise: float
    unit: str


METRICS = [
    MetricSpec("latency_p99_ms", 180.0, 12.0, "ms"),
    MetricSpec("error_rate_pct", 0.4, 0.15, "pct"),
    MetricSpec("cpu_usage_pct", 55.0, 6.0, "pct"),
    MetricSpec("throughput_rps", 320.0, 25.0, "rps"),
]


@dataclass
class MetricEvent:
    ts: str            # ISO-8601 timestamp
    service: str
    host: str
    metric: str
    value: float
    unit: str


# --------------------------- 1+2. Service + Collection ---------------------
def producer():
    """payment-service emit metric (mô phỏng OTel SDK -> Collector)."""
    start = datetime(2026, 6, 3, 9, 0, 0, tzinfo=timezone.utc)
    n_steps = DURATION_MIN * 60 // INTERVAL_S
    for step in range(n_steps):
        ts = start + timedelta(seconds=step * INTERVAL_S)
        minute = step * INTERVAL_S / 60.0
        diurnal = math.sin(step / n_steps * math.pi)        # 0..1..0, "đời sống" nhẹ
        for host in HOSTS:
            for m in METRICS:
                base = m.baseline * (1 + 0.06 * diurnal)
                val = random.gauss(base, m.noise)
                in_incident = INCIDENT_START_MIN <= minute <= INCIDENT_END_MIN and host == INCIDENT_HOST
                if in_incident:
                    if m.name == "latency_p99_ms":
                        val *= 3.2
                    elif m.name == "error_rate_pct":
                        val *= 14.0
                    elif m.name == "cpu_usage_pct":
                        val = min(99.0, val * 1.7)
                    elif m.name == "throughput_rps":
                        val *= 0.55          # quá tải -> throughput tụt
                yield MetricEvent(ts.isoformat(), "payment", host, m.name, round(max(0.0, val), 3), m.unit)


# ------------------------------ 3. Transport ------------------------------
class Transport:
    """Kafka topic mô phỏng: buffer giữa producer & consumer (decouple, replay)."""

    def __init__(self, maxlen: int = 1_000_000):
        self._q: deque[MetricEvent] = deque(maxlen=maxlen)
        self.produced = 0
        self.consumed = 0

    def produce(self, ev: MetricEvent) -> None:
        self._q.append(ev)
        self.produced += 1

    def __iter__(self):
        while self._q:
            self.consumed += 1
            yield self._q.popleft()


# ------------------------------ 4. Processing -----------------------------
@dataclass
class SeriesState:
    values: deque
    ewma: float | None = None
    last_value: float | None = None
    last_ts: datetime | None = None


class FeatureProcessor:
    """Flink-style stateful streaming: 1 state machine cho mỗi series key (host, metric)."""

    def __init__(self, window: int = WINDOW_POINTS, alpha: float = EWMA_ALPHA):
        self.window = window
        self.alpha = alpha
        self.state: dict[tuple[str, str], SeriesState] = {}

    def process(self, ev: MetricEvent) -> dict:
        key = (ev.host, ev.metric)
        st = self.state.get(key)
        if st is None:
            st = SeriesState(values=deque(maxlen=self.window))
            self.state[key] = st

        ts = datetime.fromisoformat(ev.ts)
        window_vals = list(st.values)            # CHỈ dùng quá khứ -> không leak label
        n = len(window_vals)
        mean = sum(window_vals) / n if n else ev.value
        if n >= 2:
            var = sum((x - mean) ** 2 for x in window_vals) / (n - 1)
            std = math.sqrt(var)
        else:
            std = 0.0
        if st.last_value is not None and st.last_ts is not None:
            dt = (ts - st.last_ts).total_seconds() or INTERVAL_S
            roc = (ev.value - st.last_value) / dt
        else:
            roc = 0.0
        z = (ev.value - mean) / std if std > 1e-9 else 0.0
        st.ewma = ev.value if st.ewma is None else self.alpha * ev.value + (1 - self.alpha) * st.ewma

        # cập nhật state SAU khi tính feature
        st.values.append(ev.value)
        st.last_value = ev.value
        st.last_ts = ts

        return {
            "ts": ev.ts,
            "service": ev.service,
            "host": ev.host,
            "metric": ev.metric,
            "unit": ev.unit,
            "value": ev.value,
            "rolling_mean": round(mean, 4),
            "rolling_std": round(std, 4),
            "rate_of_change": round(roc, 5),
            "z_score": round(z, 4),
            "ewma": round(st.ewma, 4),
            "window_n": n,
            "is_anomaly": bool(abs(z) > Z_THRESHOLD and n >= self.window // 2),
        }


# --------------------------------- main -----------------------------------
def main() -> None:
    print("=" * 68)
    print("W1-D3 · streaming feature pipeline")
    print("[Service]->[OTel]->[Kafka(queue)]->[Flink(features)]->[Parquet]->[ML]")
    print("=" * 68)

    # 1+2+3 — collection -> transport
    transport = Transport()
    for ev in producer():
        transport.produce(ev)
    steps = DURATION_MIN * 60 // INTERVAL_S
    print(f"Collection : produced {transport.produced:,} events "
          f"({len(HOSTS)} hosts x {len(METRICS)} metrics x {steps} steps @ {INTERVAL_S}s)")

    # 4 — processing (stream consume từ queue)
    proc = FeatureProcessor()
    rows = [proc.process(ev) for ev in transport]
    print(f"Processing : consumed {transport.consumed:,} events -> "
          f"{len(rows):,} feature rows trên {len(proc.state)} series")

    df = pd.DataFrame(rows)

    # 5 — storage (offline feature store)
    parquet_ok = True
    try:
        df.to_parquet(OUT_DIR / "features.parquet", index=False)
    except Exception as exc:                       # pyarrow thiếu -> fallback JSON
        parquet_ok = False
        print(f"  ! parquet bị bỏ qua ({exc.__class__.__name__}); JSON vẫn được ghi")
    df.to_json(OUT_DIR / "features.json", orient="records", indent=2)

    if parquet_ok:
        kb = (OUT_DIR / "features.parquet").stat().st_size / 1024
        print(f"Storage    : features.parquet ({kb:.1f} KB) + features.json")
    else:
        print("Storage    : features.json")

    # 6 — query / ML (consumer của feature store)
    anom = df[df["is_anomaly"]]
    print("-" * 68)
    print(f"Query/ML   : {len(anom)} anomaly rows (|z|>{Z_THRESHOLD:g}) trên "
          f"{anom['host'].nunique() if len(anom) else 0} host / "
          f"{anom['metric'].nunique() if len(anom) else 0} metric")
    if len(anom):
        order = anom["z_score"].abs().sort_values(ascending=False).index
        top = anom.reindex(order).head(5)
        cols = ["ts", "host", "metric", "value", "rolling_mean", "z_score", "rate_of_change"]
        print("\nTop-5 anomaly theo |z_score|:")
        print(top[cols].to_string(index=False))
        print("\nAnomaly theo metric:")
        print(anom.groupby("metric").size().sort_values(ascending=False).to_string())
        print(f"\nWindow sự cố mô phỏng: phút {INCIDENT_START_MIN}-{INCIDENT_END_MIN} "
              f"trên host '{INCIDENT_HOST}' (onset + recovery đều bị flag).")
    print("=" * 68)


if __name__ == "__main__":
    main()
