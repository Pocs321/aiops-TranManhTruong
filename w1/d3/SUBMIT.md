# SUBMIT — W1-D3: Data Layer Architecture + Observability Pipeline

---

## Phase 1 — Streaming pipeline + Architecture

### Architecture diagram (screenshot)

![AIOps data layer](architecture.png)

E2E: **Service → Collection → Transport → Processing → Storage → Query/ML**, tool cụ thể
mỗi stage + lý do chọn ở [`architecture.md`](architecture.md). Cốt lõi là debug flow
**metric → trace → log**: metric nói *cái gì sai*, trace nói *ở đâu*, log nói *tại sao*.

### `pipeline.py` — mô phỏng 5 stage data layer

Pipeline đọc event từ một **queue (Kafka mô phỏng bằng `deque`)** rồi chạy một
**Flink-style stateful operator** tính feature theo từng series — đúng bộ feature
(`rolling_mean`, `rolling_std`, `rate_of_change`, `z_score`, `ewma`) đã dùng ở W1-D1 —
và ghi ra **offline feature store** (`features.parquet`).

```
Collection : produced 5,760 events (3 hosts x 4 metrics x 480 steps @ 15s)
Processing : consumed 5,760 events -> 5,760 feature rows trên 12 series
Storage    : features.parquet (248.6 KB) + features.json
--------------------------------------------------------------------
Query/ML   : 67 anomaly rows (|z|>3) trên 3 host / 4 metric

Top-5 anomaly theo |z_score|:
                       ts  host         metric   value  rolling_mean  z_score  rate_of_change
2026-06-03T10:10:00+00:00 pay-2 latency_p99_ms 603.183      194.7909  39.3820        27.84320
2026-06-03T10:10:00+00:00 pay-2 error_rate_pct   5.455        0.4332  31.2098         0.33047
2026-06-03T10:25:15+00:00 pay-2 throughput_rps 341.082      184.7587  13.3208        11.01500
2026-06-03T10:25:15+00:00 pay-2 latency_p99_ms 179.856      601.8225 -13.1700       -27.41240
2026-06-03T10:25:15+00:00 pay-2  cpu_usage_pct  58.393       95.5794  -6.8298        -2.70713
```

**Đọc kết quả:** inject 1 sự cố ở phút 70–85 trên host `pay-2`. Pipeline bắt cả **onset**
(10:10 latency `z=39.4`, error `z=31.2`) lẫn **recovery** (10:25 latency rơi về baseline
`z=−13.2`) — đúng hành vi feature streaming kỳ vọng. Feature dùng **chỉ cửa sổ quá khứ**
(cập nhật state *sau* khi tính) nên không leak label.

---

## Phase 2 — Cost estimation (self-host *build* vs Datadog *buy*)

Bảng tổng hợp (output `cost_model.py`; đơn giá ballpark AWS + Datadog list 2024-2025,
khai báo ở dict `PRICES`/`DD`):

| Tier | Build infra | + SRE | **Build total** | **Buy (Datadog)** | Rẻ hơn |
|---|---:|---:|---:|---:|:--:|
| **Small** (10 svc · 50 GB/d · 100K eps) | $1,376 | $3,900 | **$5,276** | **$3,248** | 🟢 BUY |
| **Medium** (100 svc · 500 GB/d · 1M eps) | $8,346 | $13,000 | **$21,346** | **$32,480** | 🔵 BUILD |
| **Large** (1000 svc · 5 TB/d · 10M eps) | $81,587 | $39,000 | **$120,587** | **$324,800** | 🔵 BUILD |

Breakdown per component (storage / compute / network):

```
### Small: 10 svc · 50 GB log/day · 100,000 metric eps
Component                           Storage    Compute    Network   Subtotal
Metrics (VictoriaMetrics)               $21       $144         $0       $165
Logs (Loki + S3 tiering)                $48       $192         $0       $240
Traces (Jaeger, 1% sample)               $0        $84         $0        $84
Transport (Kafka, 3 brokers)            $55       $432         $0       $487
Processing (Flink)                       $0       $192         $0       $192
Network (cross-AZ egress)                $0         $0       $208       $208
BUILD infra total                      $124     $1,044       $208     $1,376
  Datadog TOTAL [40 hosts]                                            $3,248

### Medium: 100 svc · 500 GB log/day · 1,000,000 metric eps
Component                           Storage    Compute    Network   Subtotal
Metrics (VictoriaMetrics)              $207       $720         $0       $927
Logs (Loki + S3 tiering)               $476     $1,632         $0     $2,108
Traces (Jaeger, 1% sample)               $0        $84         $0        $84
Transport (Kafka, 5 brokers)           $554       $720         $0     $1,274
Processing (Flink)                       $0     $1,872         $0     $1,872
Network (cross-AZ egress)                $0         $0     $2,080     $2,080
BUILD infra total                    $1,238     $5,028     $2,080     $8,346
  Datadog TOTAL [400 hosts]                                         $32,480

### Large: 1000 svc · 5000 GB log/day · 10,000,000 metric eps
Component                           Storage    Compute    Network   Subtotal
Metrics (VictoriaMetrics)            $2,074     $7,200         $0     $9,274
Logs (Loki + S3 tiering)             $4,755    $16,032         $0    $20,787
Traces (Jaeger, 1% sample)               $3       $840         $0       $843
Transport (Kafka, 41 brokers)        $5,544     $5,904         $0    $11,448
Processing (Flink)                       $0    $18,432         $0    $18,432
Network (cross-AZ egress)                $0         $0    $20,804    $20,804
BUILD infra total                   $12,375    $48,408    $20,804    $81,587
  Datadog TOTAL [4,000 hosts]                                      $324,800
```

**3 điều bảng này dạy:**
1. **Crossover ở giữa Small↔Medium.** Scale nhỏ → BUY thắng vì 1 phần SRE ($3.9K) đè bẹp
   chi phí infra ($1.4K); scale lớn → BUILD thắng vì hạ tầng rẻ dần theo đầu việc còn
   Datadog tính tuyến tính theo host/GB → $325K vs $121K ở tier Large.
2. **Compute, không phải storage, là cost driver.** Storage chỉ ~15% (VM/Loki nén tốt +
   S3 rẻ); Flink + Loki ingester + Kafka broker mới ăn tiền.
3. **Network cross-AZ là cost ẩn lớn** ($20.8K/tháng ở Large) — replication Kafka RF3 +
   consumer đọc chéo AZ. Đây là khoản hay bị quên khi vẽ kiến trúc.

---

## Phase 3 — ADR-001 (tóm tắt)

**Quyết định:** dùng **Grafana Loki (không Elasticsearch)** làm primary log store, kèm
**ClickHouse** cho analytics nặng. Loki index *chỉ labels*, lưu chunk nén trên S3.

**Vì sao:** 85% truy vấn của on-call là *lọc theo label rồi grep* (không cần full-text),
nên năng lực "full-text mọi field" của ES là tiền trả cho thứ không dùng.

**Trade-off định lượng:**
- Loki + S3 ≈ **$2.1K/tháng** vs ES tự host ≈ **$18K/tháng** → **rẻ ~8×, tiết kiệm ~$190K/năm**.
- Đổi lại: query Loki phải lọc label trước; nếu label không chọn lọc → quét chunk
  **10–60s** (ES < 1s). Cấm nhét field high-cardinality (user_id/order_id) vào label.

Chi tiết Status/Context/Decision/Consequences/Alternatives: [`ADR-001.md`](ADR-001.md).

---

## Phase 4 — Reflection: Platform Engineer cho startup 50-service vừa raise Series A → **BUILD hay BUY?**

### TL;DR — **BUY (Datadog) ngay, nhưng theo kiểu hybrid có kỷ luật cost, và cắm sẵn tripwire để chuyển sang build sau.**

### Lý do (gắn với số trong `cost_model.py`)

50 service nằm **giữa tier Small (10) và Medium (100)**. Nội suy: build infra ~$4–5K/tháng
nhưng **không thể vận hành Kafka + Loki + VM + Flink + Jaeger với < 1 SRE** → +1 FTE
(~$13K) ⇒ **build total ≈ $17–18K/tháng**. Datadog cùng quy mô (≈200 host, ~250 GB log/ngày)
cũng rơi vào **~$16–18K/tháng**. Tức **về tiền gần như HOÀ** — nên tiền *không* phải yếu tố
quyết định. Ba yếu tố khác mới quyết:

1. **Headcount & opportunity cost.** Series A thường chỉ 10–20 engineer. Đốt 1 SRE (5–10%
   đội kỹ thuật) để chỉnh JVM heap của ES và vá Kafka rebalance = 1 người **không** làm
   product. Với startup, đó là chi phí đắt nhất, không nằm trên hoá đơn cloud.
2. **Time-to-value.** Datadog **1–2 tuần** là có dashboard + alert + Watchdog (AIOps
   out-of-box). Tự build hot path mất **3–6 tháng** mới ổn định — runway không cho phép.
3. **Rủi ro vận hành.** 2h sáng Kafka kẹt partition, ai on-call? Ở 50 service, bạn chưa có
   đội đủ dày để cõng cả nghĩa vụ vận hành observability stack lẫn sản phẩm.

### Nhưng "buy" phải có kỷ luật — nếu không Datadog sẽ bill-shock

- **Metric để self-host Prometheus + Grafana** (~$200/tháng) — chặn đúng khoản *custom-metric*
  khét tiếng đắt của Datadog. Đây là pillar rẻ nhất khi tự host.
- **Log: filter + sample tại nguồn** (drop health-check/debug), chỉ **index ~20%**, retention
  15 ngày + đẩy phần còn lại xuống **S3 archive**. Log index là khoản phình nhanh nhất ($14K
  ở tier Medium phần lớn là index).
- **APM: chỉ instrument critical path** (checkout, payment), sample trace — không bật APM toàn bộ host.

### Insurance: chuẩn hoá OpenTelemetry SDK từ ngày 1

Vì service emit qua **OTel SDK** (vendor-neutral), "đổi từ Datadog sang self-host" sau này
là **đổi config endpoint, không phải viết lại code**. Đó là điều khiến chiến lược
*buy-now-build-later* gần như miễn phí về lock-in.

### Tripwire — khi nào lật sang BUILD

Đặt sẵn ngưỡng review: **hoá đơn Datadog > ~$30–40K/tháng** (≈ 2 lương SRE) **HOẶC**
service > ~150–200 **HOẶC** cần data-residency/customization sâu. Khi chạm ngưỡng, migrate
hot path theo thứ tự "rẻ trước": **metric → VictoriaMetrics**, rồi **log → Loki** (đúng
[ADR-001](ADR-001.md)), cuối cùng trace. Theo `cost_model.py`, đúng quanh tier Medium là lúc
build bắt đầu thắng ($21K vs $32K) — đó là điểm để bấm nút.

> **Một câu:** ở Series A, mua thời gian và sự tập trung quan trọng hơn tiết kiệm vài nghìn
> đô hạ tầng — *buy* Datadog, nhưng giữ metric tự host, siết log index, và chuẩn hoá OTel
> để build lại rẻ khi đủ lớn.

---

