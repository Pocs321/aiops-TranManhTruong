# Chaos Engineering Report — <tên của bạn>

> SCAFFOLD. Cấu trúc + hypothesis đã pre-fill từ `experiments.yaml`.
> Mọi field đánh dấu `<ĐIỀN SAU KHI CHẠY>` phải điền từ `chaos_results.json` /
> scoreboard thật. KHÔNG bịa số (§8.6: ép pass là dishonest — log gap ở §4 thay vì vậy).

## 1. Setup
- Stack version + commit hash: `<ĐIỀN SAU KHI CHẠY>`
- Pipeline version + commit hash: `<ĐIỀN SAU KHI CHẠY>` (pipeline W1+W2 — xem `../../w2/d3`)
- Baseline window: `<start>` → `<end>` (`python scripts/capture_baseline.py --duration 300`)
- Chaos tools: pumba `<ver>`, toxiproxy `<ver>`, tc-image `gaiadocker/iproute2`
- Lệnh chạy runner: `python chaos_runner.py` (cooldown 120s, baseline-window 60s)
- Tổng số experiment chạy: 10

## 2. Results table
> Dán scoreboard stdout từ `chaos_runner.py` (format §8.6) vào đây.

```
<ĐIỀN SAU KHI CHẠY — dán block ==== Chaos Run ====>
```

## 3. Detailed per-experiment analysis
> 80–150 từ mỗi cái. Hypothesis copy từ `experiments.yaml`; điền Observed + Match
> từ `chaos_results.json`.

### 1. payment_latency_500ms — latency → payment-svc
- **Hypothesis:** steady-state payment_p99 ≤ 300ms / checkout_p99 ≤ 800ms; +500ms±100ms trong 60s đẩy payment_p99 >3× baseline; detect ≤ 60s; correlate payment+checkout; RCA → payment-svc; order_success_rate giữ ≥ 95%.
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`, conf=`<x>`.
- **Khớp expected?** `<ĐIỀN — nếu không, dẫn chứng metric>`

### 2. payment_packet_loss_30 — network_loss → payment-svc
- **Hypothesis:** steady-state error_rate ≤ 0.5%; 30% loss trong 60s đẩy error_rate >10×; detect error_rate anomaly ≤ 60s; RCA → payment-svc; order_success_rate ≥ 90%.
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`.
- **Khớp expected?** `<ĐIỀN>`

### 3. inventory_pod_kill — availability → inventory-svc
- **Hypothesis:** availability ≥ 99.9%; kill mỗi 60s trong 180s → ≥2 restart gap; detect availability/healthcheck ≤ 30s sau kill đầu; RCA → inventory-svc; order_success_rate ≥ 90% nhờ cached stock.
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`.
- **Khớp expected?** `<ĐIỀN>`

### 4. api_gateway_cpu_90 — cpu_saturation → api-gateway
- **Hypothesis:** api-gateway_p99 ≤ 400ms; 4 worker @90% trong 60s cascade latency xuống mọi downstream; detect multi-service latency ≤ 60s; correlator giữ MỘT incident; RCA → api-gateway (không phải leaf ồn nhất).
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`, số incident=`<n>`.
- **Khớp expected?** `<ĐIỀN — correlator split hay merge?>`

### 5. payment_db_memory_95 — memory → payment-db
- **Hypothesis:** conn-pool wait ≤ 50ms; lấp 95% RAM → swap/OOM pressure → pool của payment-svc bão hòa; detect ≤ 90s; RCA → payment-db (KHÔNG phải payment-svc, caller).
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`.
- **Khớp expected?** `<ĐIỀN — quy lỗi caller-vs-dependency?>`

### 6. auth_clock_skew_60s — time_skew → auth-svc
- **Hypothesis:** auth success ≥ 99.9%; skew +60s phá JWT exp/nbf + hiệu lực TLS → spike 401/handshake ở mọi caller; detect ≤ 60s; RCA → auth-svc (lateral root).
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`.
- **Khớp expected?** `<ĐIỀN>`

### 7. log_collector_disk_fill_95 — disk_fill → log-collector
- **Hypothesis:** ingestion lag ≤ 5s; 95% disk làm nghẽn ingestion; test META-MONITORING (§7.5) — detect ingestion-lag/meta-alert ≤ 120s; RCA → log-collector. Miss ở đây là gap đáng học nhất.
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`.
- **Khớp expected?** `<ĐIỀN — pipeline có bị mù trên chính input của nó không?>`

### 8. frontend_apigw_partition_30s — network_partition → frontend↔api-gateway
- **Hypothesis:** edge availability ≥ 99.9%; partition toàn phần 30s → mọi downstream timeout tại edge; detect ≤ 30s; correlator gộp fan-out thành MỘT incident; RCA → edge (api-gateway/frontend).
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`, số incident=`<n>`.
- **Khớp expected?** `<ĐIỀN>`

### 9. dns_slow_lookup_2s — dns_latency → dns-resolver
- **Hypothesis:** lookup ≤ 20ms; +2s DNS latency → connection-setup chậm chập chờn khắp các service; detect intermittent latency/error ≤ 90s; RCA phụ thuộc topology — chấp nhận dns-resolver; rủi ro: đổ lỗi caller ngẫu nhiên.
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`.
- **Khớp expected?** `<ĐIỀN>`

### 10. checkout_retry_storm — cascade_retry → checkout-svc (root = payment-svc)
- **Hypothesis:** checkout_p99 ≤ 800ms; 20% failure trên checkout→payment làm checkout retry ~10× và phát alert ỒN NHẤT; root thật = payment-svc; detect ≤ 60s; RCA PHẢI → payment-svc và KHÔNG được pick checkout-svc (trap §7.3).
- **Observed:** detected=`<Y/N>`, MTTD=`<s>`, RCA=`<service>`.
- **Khớp expected?** `<ĐIỀN — RCA có dính bẫy kẻ retry ồn ào không?>`

## 4. Gap analysis — top 3 pipeline weakness
> Lấy từ block "Gaps identified" của scoreboard; chọn 3 cái quan trọng nhất.

### Gap 1
- **Symptom:** `<experiment # nào, số/quan sát gì>`
- **Likely cause in pipeline:** `<detector | correlator | RCA>` — `<lý do>`
- **Recommended fix:** `<cụ thể>` — tham chiếu §7.`<n>` (`<tên failure mode>`)

### Gap 2
- **Symptom:** `<...>`
- **Likely cause in pipeline:** `<...>`
- **Recommended fix:** `<...>` — tham chiếu §7.`<n>`

### Gap 3
- **Symptom:** `<...>`
- **Likely cause in pipeline:** `<...>`
- **Recommended fix:** `<...>` — tham chiếu §7.`<n>`

## 5. Hypothesis cho gap chưa khẳng định (optional)
- `<gap nào cần experiment bổ sung để khoanh vùng, và experiment gì>`
