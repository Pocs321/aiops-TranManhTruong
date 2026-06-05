# Detection Approach — DESIGN.md

## Approach tôi dùng

**Adaptive streaming baseline (EWMA z-score) + absolute domain thresholds + debounce**, kết hợp một bộ **phân loại fault dựa trên tín hiệu trực giao (orthogonal signal)**.

Mỗi metric được theo dõi bằng một baseline EWMA (mean + variance tăng dần, O(1) bộ nhớ).
Quyết định anomaly dùng hai loại tín hiệu:
- **Z-score tương đối** cho các metric có baseline trôi theo thời gian (vd `http_requests_per_sec` thay đổi theo chu kỳ ngày/đêm).
- **Ngưỡng tuyệt đối** cho các metric có khoảng "bình thường" cố định và đã biết (memory utilisation, GC pause, upstream timeout rate).

## Tại sao chọn approach này

Phù hợp với streaming vì:
1. **O(1) state** — EWMA cập nhật incremental, không cần lưu cả lịch sử, chạy vô hạn không phình bộ nhớ.
2. **Tự thích nghi** — baseline EWMA bám theo chu kỳ ngày/đêm của traffic, nên một đỉnh traffic ban ngày *không* bị báo nhầm; chỉ phần lệch so với baseline gần đây mới tính.
3. **Chống false positive** — kết hợp 3 lớp: warmup (học baseline sạch trước khi cho phép alert), freeze baseline khi giá trị bất thường (không để chính fault làm hỏng baseline), và debounce (cần nhiều tick liên tiếp đồng thuận).
4. **Phân loại được fault** — mỗi loại fault có một tín hiệu *riêng biệt, trực giao* mà hai loại kia không động tới.

## Cách hoạt động

Với mỗi tick (`POST /ingest`):
1. **Warmup** `WARMUP_TICKS=20` tick đầu chỉ học baseline, không alert (generator đảm bảo fault xảy ra sớm nhất sau 30 phút real-time, nên warmup ~60s luôn hoàn tất trước fault).
2. **Classify** giá trị hiện tại so với baseline, theo thứ tự *cụ thể nhất trước*:
   - `dependency_timeout` → `upstream_timeout_rate` cao (bình thường 0–0.4%, fault đẩy lên 5–80%).
   - `memory_leak` → memory utilisation hoặc GC pause vượt ngưỡng tuyệt đối (bình thường ~40% và 8–18ms).
   - `traffic_spike` → `http_requests_per_sec` cao hơn nhiều lần baseline thích nghi.
   - Các triệu chứng *dùng chung* (5xx rate, p99 latency, CPU, queue depth) chỉ dùng để nâng `severity`, **không** dùng để quyết định `type`.
3. **Update baseline** — fold tick vào EWMA, nhưng *bỏ qua* nếu giá trị đang bất thường (|z| > 4) hoặc fault đã được xác nhận, để baseline luôn là ảnh "bình thường" sạch.
4. **Debounce** — chỉ fire khi cùng một loại fault xuất hiện `DEBOUNCE_K=3` tick liên tiếp.
5. **Ghi alert** — ghi 1 dòng JSON vào `alerts.jsonl`; dedupe sao cho mỗi fault chỉ có tối đa một `warning` rồi một `critical` (khi leo thang), giữ file gọn.

### Vì sao memory_leak dùng ngưỡng tuyệt đối thay vì z-score
Memory leak tăng *rất chậm* (~4MB/tick). Một baseline thích nghi sẽ **hấp thụ dần** độ trôi này và z-score không bao giờ bật lên. Vì khoảng bình thường của memory util (~40%) và GC pause (8–18ms) là cố định và đã biết, ngưỡng tuyệt đối (`util > 0.55` hoặc `gc > 40ms`) vừa chắc chắn vừa phát hiện sớm (~17–18 phút production), trong khi traffic_spike và dependency_timeout ramp đủ nhanh để z-score/ngưỡng bắt được trong vài giây.

## Parameters tôi chọn

| Param | Giá trị | Lý do |
|-------|--------:|-------|
| `WARMUP_TICKS` | 20 (~60s real) | Đủ để EWMA hội tụ; fault sớm nhất là 30 phút real nên không bao giờ bỏ sót |
| `EWMA_ALPHA` | 0.08 | Cửa sổ hiệu dụng ~25 tick — đủ nhanh để bám chu kỳ ngày/đêm, đủ chậm để mượt nhiễu |
| `FREEZE_Z` | 4.0 | Ngừng nạp giá trị bất thường vào baseline để fault không "đầu độc" baseline |
| `DEBOUNCE_K` | 3 tick | Loại nhiễu một-tick; ~9s real ≈ 90s prod, vẫn cho TTD thấp |
| `upstream > 2.0%` | tuyệt đối | Cao hơn hẳn max bình thường 0.4% |
| `mem_util > 0.55` / `gc > 40ms` | tuyệt đối | Cao hơn hẳn bình thường ~0.40 / 18ms |
| `rps_ratio > 2.5x` | tương đối | So với baseline thích nghi, miễn nhiễm với chu kỳ ngày/đêm |

**Kết quả kiểm thử** (replay dữ liệu sinh từ chính `stream_generator.py`):
- `dependency_timeout`: phát hiện ~2 phút prod
- `traffic_spike`: phát hiện ~2 phút prod
- `memory_leak`: phát hiện ~18 phút prod
- **0 false alert** trong giai đoạn baseline cho cả ba loại fault; phân loại đúng 3/3.

## Cải thiện nếu có thêm thời gian

- **Dùng tín hiệu từ logs** (đếm ERROR/FATAL, parse message như "Circuit breaker OPEN", "OutOfMemoryWarning") làm bằng chứng độc lập để corroborate và giảm TTD.
- **MAD (median absolute deviation)** thay cho EWMA variance để robust hơn với outlier nhọn.
- **CUSUM / drift detector** cho memory leak để bắt độ trôi chậm bằng phương pháp thống kê thay vì ngưỡng cứng (tổng quát hơn khi không biết trước khoảng bình thường).
- **Hysteresis / auto-resolve**: phát alert "resolved" khi metric về bình thường, và theo dõi multiple concurrent faults.
- **Config ngoài** (YAML) cho ngưỡng để tune mà không sửa code.
