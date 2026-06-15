# W3-D1 Submission 

## 3 thứ tôi học được

1. **Burn rate cũng là tín hiệu để chọn SLO, không chỉ là input cho alert.** Chuẩn hóa
   error rate thô bằng `1−SLO` cho phép một ngưỡng dùng được cho mọi service — nhưng SLO
   target khi đó điều khiển trực tiếp *recall*. Ở SLO API 99%, sự cố ×10 nhẹ (id5, ~20%
   fail, 20 phút) có burn cửa sổ 1h chỉ ~6.7, dưới Tier-1, nên thành **false negative**; ở
   99.9% nó là ~67 và `fn = 0`. Chọn SLO và độ nhạy detection là cùng một núm vặn.

2. **Cửa sổ dài trong MWMBR là khoản "thuế" độ trễ detection cho các burst sắc, ngắn.**
   `AND` long+short là thứ cho recovery nhanh và noise thấp, nhưng trung bình hóa một
   outage 12 phút qua cửa sổ 1h kéo burn phút đầu xuống ~14, nên Tier-1 chuẩn 14.4 của tôi
   page trễ một phút (`mttd_delta = 60s`). Cửa sổ short = "còn đang xảy ra không?", cửa sổ
   long = "đủ lớn chưa?" — và cửa sổ long phải trả giá bằng MTTD.

3. **Định nghĩa "good event" dịch chuyển SLI nhiều ngang với thực tế.** Loại 4xx do bot
   (đo 2.01%) so với tính nó vào làm lệch availability báo cáo ~2 điểm (99.65% so với
   97.63% — đúng bằng `success_rate` của `baseline.json`) trên cùng một traffic. Định nghĩa
   tử số sai thì dashboard nói dối.

## 1 thứ vẫn chưa rõ

Con số noise-reduction 86.4% thực sự bền đến đâu trước các lựa chọn mô hình hóa của
`validate.py`. Checker mô phỏng việc vượt ngưỡng cửa sổ tức thời, **không có `for:` dwell**,
và so rule của tôi với một static baseline cố định duy nhất (`fail_rate > 0.005` trên 5m).
Prometheus thật có thêm `for:` hold-down, còn Alertmanager thêm grouping/inhibition/dedup —
tất cả đều định hình lại cả noise lẫn MTTD. Từ bài lab này tôi chưa thể biết bao nhiêu phần
của thắng lợi là do bản thân thiết kế MWMBR so với cái static rule cụ thể mà nó được chấm
điểm cùng.

## 1 trade-off trong SLO decision của tôi mà tôi không chắc

SLO API 99.9% **chặt hơn mức đo 99.65%**, và ngay cả sàn không-sự-cố (~99.85%) cũng nằm
ngay dưới nó — nên trong một tháng hoàn toàn sạch budget vẫn chạy âm nhẹ. Tôi chọn 99.9% vì
nó khớp kiến trúc 4-instance/LB (§3.2) và, quyết định hơn, giữ id5 detect được (`fn = 0`).
Nhưng nếu 5xx steady-state thật ~0.35%, budget sẽ cạn kinh niên và team học cách phớt lờ nó
(anti-pattern §10). Phương án trung thực là **99.5%** (thoải mái trên mức hiện tại) cộng một
SLO latency chặt hơn — đánh đổi việc đảm bảo detect sự cố nhẹ lấy một budget khả thi, đáng
tin. Tôi tối ưu cho an toàn-detection; tôi không chắc đó là lựa chọn đúng cho một service
production với baseline này.

## Validation report

- noise_reduction_pct: **86.4%**
- mttd_delta_s: **0s**
- false_negative: **0**
- verdict: **pass**

(static baseline fired 22 alert → 19 false positive; MWMBR của tôi fired 3 → 3 true
positive, 0 false positive, bắt được cả 3 sự cố API.)
