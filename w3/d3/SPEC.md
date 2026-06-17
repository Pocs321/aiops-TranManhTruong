# Spec Nền tảng AIOps thu nhỏ — ourlife937

> Spec tổng hợp W3. Mục 3 và 5–6 tham chiếu artifact **thật, chạy được** (pipeline
> W2 và bản tái hiện W3-D3 này). Mục 2 và 4 là **bản thay thế tổng hợp**, ghi rõ:
> deliverable W3-D1 (`slo_spec.yaml`) và W3-D2 (`chaos_report.md`) chưa được tạo
> trong cây thư mục này (`w3/d1` và `w3/d2` trống) — hãy thay bằng artifact thật
> khi có.

## 1. Tổng quan nền tảng

Nền tảng giám sát một stack microservice production (`api-gateway` →
`checkout/order/payment/auth/user-svc` → `db-primary`, `redis-cache`,
`kafka-broker`; đồ thị phụ thuộc và lịch sử sự cố nằm ở `w2/d3/lab/dataset/`). Nó
nạp alert, tương quan thành sự cố, xếp hạng nguyên nhân gốc trên đồ thị phụ thuộc,
và phục vụ báo cáo sự cố qua HTTP (`POST /incident`). Người dùng là kỹ sư SRE/on-call
trong lúc sự cố và chủ dịch vụ khi xem postmortem sau đó. Với W3-D3, chúng tôi còn
chạy nền tảng như một **bài drill red-team** trên một mẫu sự cố ngoài-phân-phối
(Cloudflare-2019 WAF regex) trên một topology edge riêng — đó chính là cách phát
hiện điểm mù sự-kiện-thay-đổi ở mục 5.

## 2. Định nghĩa SLO (từ W3-D1) — *bản thay thế tổng hợp*

> Thay bằng `w3/d1/slo_spec.yaml` thật. Ba dịch vụ, mỗi cái có SLI + SLO + error
> budget trên cửa sổ 28 ngày (40.320 phút/cửa sổ).

| Dịch vụ | SLI | SLO | Error budget (28 ngày) |
|---|---|---|---|
| api-gateway | tỷ lệ thành công = non-5xx ÷ tổng request | 99.9% | 0.1% ≈ **40,3 phút** |
| api-gateway | độ trễ: tỷ lệ request có p99 < 1000 ms | 99.0% | 1.0% số request |
| payment-svc | tỷ lệ authorize thanh toán thành công | 99.95% | 0.05% ≈ **20,2 phút** |
| db-primary | tỷ lệ query có p99 < 50 ms | 99.9% | 0.1% ≈ 40,3 phút |

```yaml
# slo_spec.yaml (bản thay thế)
window_days: 28
services:
  - name: api-gateway
    slis:
      - {name: availability, query: "1 - (rate(5xx[5m]) / rate(req[5m]))", objective: 0.999}
      - {name: latency_p99,  query: "histogram_p99(req_latency[5m]) < 1000ms", objective: 0.99}
  - name: payment-svc
    slis:
      - {name: authz_success, query: "rate(authz_ok[5m]) / rate(authz_total[5m])", objective: 0.9995}
  - name: db-primary
    slis:
      - {name: query_latency_p99, query: "histogram_p99(query_latency[5m]) < 50ms", objective: 0.999}
```

## 3. Stack Phát hiện + Tương quan + RCA (từ W1+W2) — *thật (`w2/d3/`)*

- **Phát hiện (L0/W1).** Detector ngưỡng + bất thường phát ra alert theo schema cố
  định (`{id, ts, service, metric, severity, value, threshold, labels}`). Note W3
  khuyến nghị ensemble (3σ + Isolation Forest + LSTM-AE); stub đã nối phát ra ngưỡng
  bị vượt.
- **Tương quan (L1 — `correlate.py`).** Hai tín hiệu: sessionize theo thời gian
  (phiên mới khi khoảng cách liên-đến > 120 giây) và lân cận topology (các dịch vụ
  trong `max_hop = 2` trên đồ thị phụ thuộc được union-find vào một cụm).
- **RCA (L2 — `rca.py`).** Xếp hạng đồ thị: điểm của một dịch vụ = trọng số severity
  của chính nó + 1.5 × số (có trọng số severity) các dịch vụ đang alert khác phụ
  thuộc vào nó trong 3 hop. Cộng truy hồi sự-cố-tương-tự bằng Jaccard, cộng enrichment
  LLM tuỳ chọn best-effort (`enrich.py`, tắt khi không có API key).
- **Phục vụ (L4 — `serve.py`).** FastAPI: `/incident`, `/healthz`, `/readyz`,
  `/version`, `/metrics`. Liveness độc lập với LLM; RCA suy biến về chỉ-đồ-thị khi
  provider chết.
- **Quyết định thiết kế ghi tại:** ADR-001 (repo này) — tín hiệu RCA sự-kiện-thay-đổi.

## 4. Kiểm chứng độ tin cậy (từ W3-D2) — *bản thay thế tổng hợp*

> Thay bằng `w3/d2/chaos_report.md` thật. Bảng điểm các thí nghiệm chaos trên stack
> ở mục 1.

| # | Thí nghiệm | Giả thuyết | Kết quả | MTTD | MTTR |
|---|---|---|---|---|---|
| 1 | Giết bản sao `db-primary` | Failover trong suốt, không đốt SLO | **ĐẠT** | 22 s | 95 s |
| 2 | +300 ms độ trễ vào `payment-svc` | Phát hiện được; RCA gọi tên payment-svc | **ĐẠT** | 18 s | n/a (chèn vào) |
| 3 | `redis-cache` eviction do maxmemory | Auth suy biến mượt về DB | **MỘT PHẦN** | 31 s | 240 s |
| 4 | Phân mảnh leader `kafka-broker` | Order/notify backpressure, không mất dữ liệu | **ĐẠT** | 40 s | 180 s |
| 5 | CPU-hog một node `api-gateway` | Load-balancer loại node tồi | **TRƯỢT** | 75 s | thủ công |

**Top 3 lỗ hổng**
1. **CPU-hog một node không tự loại (TN 5)** — phát hiện chậm (75 s) và hồi phục thủ
   công. *(Báo trước sự cố WAF-CPU của W3-D3.)*
2. **Hồi phục cache-eviction chậm (TN 3, 240 s)** — không có pre-warm hot-key.
3. **Confidence RCA thấp khi bán kính ảnh hưởng nhiều dịch vụ** — xếp hạng đồ thị
   trải confidence mỏng khi nhiều dịch vụ cùng alert một lúc.

## 5. Mẫu vận hành (từ W3-D3) — *bản tái hiện thật*

Sự cố tái hiện: **Cloudflare 2019-07-02 WAF regex** (catastrophic backtracking) —
xem `reproduction/`, `timeline.json`, `rca_observed.json`. Cùng lưu lượng mất **31,0
ms** dưới rule safe và **9.154 ms** dưới rule vulnerable; một `/healthz` không-regex
đứng **9.145 ms** vì CPython `re` giữ GIL trong lúc match (một request đóng băng cả
worker). Đỉnh CPU **96%**.

**Bài học chính:** RCA topology-aware khoanh đúng thành phần bị nghẽn (`waf-engine`,
conf 0.515) nhưng **không gọi được tên nguyên nhân** — trigger là một sự kiện
thay-đổi-cấu-hình, không bao giờ là input của pipeline — và nó đẩy lên một biện pháp
khắc phục sai cơ chế ("Mở rộng bộ nhớ cdn-cache") từ lịch sử trùng dịch vụ. Phát hiện
≠ chẩn đoán. → **ADR-001** (nạp sự kiện thay đổi làm tín hiệu RCA hạng nhất, xếp trên
topology thuần).

## 6. Mô hình chi phí (từ W3-D3) — *thật (`cost_model.py`)*

Cho stack ở mục 1 (hồ sơ e-commerce hạng trung): 100 dịch vụ, 5 sự cố/tháng × 2 h,
downtime ≈ $50k/giờ, AIOps ≈ $25k/tháng:

```
is_worth_it(100, 5, 2, 50_000, aiops_monthly_cost=25_000)
=> {"monthly_value": 200000.0, "monthly_cost": 25000.0,
    "roi": 8.0, "payback_months": 0.125, "verdict": "worth_it"}
```

**Điểm hoà vốn:** với hồ sơ sự cố này (10 giờ-downtime/tháng, giảm MTTR 40%), nền
tảng tự trả tiền cho nó khi chi phí downtime vượt ≈ **$6.250/giờ** (25.000 ÷ (10 ×
0.4)). Dưới ngưỡng đó — hoặc dưới ~3 sự cố/tháng — hãy đầu tư vào SLO, observability
và văn hoá on-call trước (note §8.5).

## 7. Rủi ro còn mở

| # | Rủi ro | Mức độ | Giảm thiểu (phụ trách, hạn) |
|---|---|---|---|
| 1 | **Mù sự-kiện-thay-đổi** — RCA không gọi được tên artifact deploy/cấu hình; tiêu MTTR để tự suy lại "cái gì đã thay đổi" | CAO | ADR-001: nạp sự kiện thay đổi xếp trên topology (Nền tảng AIOps, 2026-07-06) |
| 2 | **Không có cổng ReDoS / độ phức tạp pre-deploy** — rule backtracking lọt tới prod | CAO | Cổng phân tích tĩnh trong pipeline rule (Nền tảng WAF, 2026-06-22, P0) |
| 3 | **Rollout nguyên tử toàn cầu** — bán kính 100% trước khi tín hiệu nào dịch chuyển | CAO→TB | Canary tăng dần 1%→10%→100% tự dừng (Kỹ thuật phát hành, 2026-06-22, P0) |
| 4 | **Truy hồi khớp dịch vụ, không khớp cơ chế** — action khuyến nghị hàng đầu có thể sai chủ động | TB | Gắn nhãn lớp-lỗi cho lịch sử sự cố (Nền tảng AIOps, 2026-07-31, P2) |
| 5 | **Artifact W3-D1/D2 chưa được tạo** (§2, §4 là bản thay thế) | TB (quy trình) | Tạo `slo_spec.yaml` + `chaos_report.md` thật và nối lại |
