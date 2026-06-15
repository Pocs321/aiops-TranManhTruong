# W3-D1 — DESIGN: bảo vệ các quyết định SLO / alerting

Mọi con số bên dưới lấy từ `baseline.json` (mẫu 3 ngày, seed-42, 2.07M request API)
và `validation_report.json`, cùng hai script phân tích tái lập được trong thư mục này
(`analyze_for_design.py`, `tune_probe.py`).

---

## Q1 — Frontend SLI: vì sao chọn "good page load" tổng hợp, và loại bỏ gì

RUM cung cấp 4 tín hiệu ứng viên: page-load time và DOM-ready (hai mặt của cùng một
trường latency `dom_ready_ms`), `js_error`, và `network_error`. Tôi **không** chọn một
tín hiệu đơn — SLI là tổng hợp *good page load*:
`dom_ready_ms < 3000 AND js_error == false AND network_error == false`.
Baseline good-rate = **0.9861** (`frontend.success_rate`), dom-ready p99 = 1430 ms.

Vì sao mỗi tín hiệu đơn lẻ không đủ làm SLI:
- **Load-time / DOM-ready đơn lẻ:** trang có thể render trong 800 ms nhưng vẫn ném JS
  error làm hỏng checkout — nhanh nhưng không dùng được, đúng cái bẫy "không proportional
  với user pain" như khi dùng CPU. Hơn nữa dom p99 (1430 ms) nằm dưới xa ngưỡng 3000 ms
  nên latency đơn lẻ gần như không bao giờ trip.
- **js_error đơn lẻ:** bỏ sót độ chậm — trang load 8 s bị bỏ dù không có JS error nào.
- **network_error đơn lẻ:** chỉ là tập con của các failure mode; quá hẹp.

Mỗi tín hiệu bắt một pain mode riêng, không trùng nhau, nên SLI dạng event-based phải OR
chúng lại. Sự cố CDN 90 phút (id4) — một regression về network/latency — chính là thứ mà
một SLI chỉ-js-error sẽ bỏ lỡ.

---

## Q2 — SLO target cho API: vì sao 99.9% (không phải 99% hay 99.99%)

Availability đo trên mẫu = 1 − `api.fail_rate` = 1 − 0.003488 = **99.65%**, nhưng cửa sổ
này dày sự cố (3 sự cố API, 2 cái gần như outage toàn bộ). Sàn không-sự-cố ~99.85%
(0.1% 5xx nền + 0.05% 429). Topology: API = 4 instance FastAPI sau load balancer → §3.2
ánh xạ kiến trúc này vào tier **99.9%** (multi-instance, LB, auto-failover).

- **99% quá lỏng:** cho phép 7h18m downtime/tháng và, quyết định hơn, làm yếu detection —
  tại `1−SLO = 0.01` sự cố ×10 nhẹ (id5, ~20% fail, 20 phút) có burn cửa sổ 1h chỉ ~6.7,
  *dưới* ngưỡng Tier-1 → **false negative**. Report của tôi có `fn = 0` chính vì 99.9%
  giữ burn 1h của id5 ở mức ~67.
- **99.99% chặt hơn cả mức đo 99.65%** → miss ngay tháng đầu (anti-pattern §10) và cần
  multi-AZ + 24/7 + automated runbook (chi phí 3–10×, §3.2) mà stack này không có.

99.9% là chặt có chủ đích: mức clean-month ~99.85% nằm ngay dưới nó, nên budget (20,738
failure ≈ **43 phút/tháng**, `slo_spec.yaml`) chạy sát và mọi tải sự cố lập tức hiện ra
dưới dạng burn.

---

## Q3 — Ngưỡng latency: vì sao cut tại 500 ms

Phân phối latency API đo được (2.07M request):

| pctl  | ms   | | dải           | tỉ lệ  |
|-------|------|-|---------------|--------|
| p50   | 45   | | < 100 ms      | 94.04% |
| p95   | 104  | | 100–200 ms    | 5.53%  |
| p99   | 156  | | 200–500 ms    | 0.37%  |
| p99.9 | 394  | | 500–1000 ms   | 0.05%  |
| max   | 2553 | | ≥ 1000 ms     | 0.007% |

Tỉ lệ ≥ 200 ms = 0.43%, ≥ **500 ms = 0.060%**, ≥ 1000 ms = 0.007%.

- **200 ms** nằm ngay trên p99 (156 ms): sẽ gắn cờ "chậm" cho 0.43% traffic *bình thường*,
  làm SLI dính vào jitter thường nhật và đốt budget khi user không cảm thấy gì (100–200 ms
  là không thể cảm nhận trên một API call).
- **1 s** chỉ bắt 0.007% — lỏng đến mức một regression làm latency tăng gấp đôi vào dải
  300–400 ms vẫn vô hình. Bảo vệ thiếu.
- **500 ms** ≈ 3.2× p99: chỉ 0.060% traffic baseline — đúng phần tail pathology thật. Nó
  cộng không đáng kể vào error budget 0.349% (5xx+429) ở steady state, nên objective
  latency chỉ "cắn" khi có regression latency thật, đồng thời vẫn bắt được mốc mà một thao
  tác e-commerce tương tác bắt đầu thấy ì.

---

## Q4 — Vì sao loại 4xx (trừ 429) khỏi error count

Theo §2.4, 4xx (không-429) là client error — request malformed/unauthorized, phần lớn là
bot và scraper — không phải lỗi server, nên không được đốt budget. 4xx đo được = **2.01%**
tổng, trải đều khắp endpoint (mỗi path 1.98–2.04%; cao nhất là `/api/cart` 2.04%). Nếu
tính như failure, error rate sẽ nhảy từ 0.349% (5xx+429) lên ~2.36%, kéo availability báo
cáo xuống ~97.64% — gần đúng bằng `api.success_rate` **0.9763** trong `baseline.json`, vì
metric chặt hơn đó đã loại 4xx khỏi "good". Vậy khoảng ~2 điểm chênh giữa availability thật
(99.65%) và success_rate (97.63%) là **hoàn toàn do 4xx của bot**: tính nó vào sẽ làm SLI
bám theo lượng scraper, không phải reliability (anti-pattern §10).

**429 *được* tính** là failure — đó là hệ thống rate-limit một user hợp lệ, tức pain do
hệ thống gây ra.

Ở đây không endpoint nào vượt 5% 4xx, nên không cái nào che giấu một contract break thật.
Lưu ý: một endpoint vọt lên >5% (bão 401 sau khi đổi config auth, hay 400 do client release
hỏng) *là* regression contract/hệ thống và đáng có alert riêng dù mang mác "4xx".

---

## Q5 — Tuning MWMBR: dùng Google default hay tự chỉnh

Tôi giữ Google default cho **Tier 2 và 3** (burn ≥ 6 trên 6h/30m; ≥ 1 trên 3d/6h) và
**chỉnh Tier-1 từ 14.4 → 12** sau khi đo detection trên dữ liệu seed-42 cố định
(`tune_probe.py`).

Tại 14.4 report cho `mttd_delta_s = 60` — đúng ngay biên chấp nhận. Nguyên nhân: sự cố id3
(×50, 12 phút, giờ thấp tải 23h) là một burst sắc; trung bình hóa qua cửa sổ dài 1h khiến
burn của nó rơi xuống ~14 ở phút *đầu tiên* của sự cố, nên `AND` long+short fire trễ một
phút — và vì là median của 3 sự cố API nên nó đặt `mttd_p50 = 60 s`. Sweep ngưỡng cho thấy
mọi giá trị từ 6 đến 13 đều giữ **fired = 3, fp = 0, fn = 0, noise = 86.4%**; chỉ độ trễ
từng sự cố thay đổi. 12 làm id3 page ngay phút 0 → **`mttd_delta = 0`**, có biên an toàn
dưới điểm gãy thực nghiệm là 13.

Vậy việc tuning cải thiện MTTD với **chi phí 0 về noise lẫn recall**. Report cuối: static
fired 22 so với tôi 3 (**noise_reduction 86.4%**), `fp = 0`, `fn = 0`, `mttd_delta_s = 0`.
FP = 0 vì burn baseline (~1.5) thấp hơn 12 rất nhiều; FN = 0 vì burn cửa sổ short của mọi
sự cố vượt xa 12.
