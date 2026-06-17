# Bài nộp W3-D3 — ourlife937

## Sự cố đã chọn

- **ID:** 3
- **Tên:** Cloudflare WAF regex, 2019-07-02
- **Vì sao chọn cái này:** Tôi muốn một sự cố có **zero thay đổi mã và zero thay đổi
  lưu lượng** — nơi biến duy nhất là một artifact cấu hình — để kiểm tra xem một RCA
  topology-aware có tìm được nguyên nhân không-phải-một-dịch-vụ-hỏng hay không. Nó
  cũng tái hiện được hoàn toàn trong môi trường tối giản (Python thuần), nên timeline
  là *đo được*, không phải kể lại.
- **Failure mode:** regex / **catastrophic backtracking** (§4.3).

## 3 điều tôi học được từ sự cố này

1. **Lỗi nằm ở *nội dung của một artifact cấu hình*, không phải mã hay năng lực.**
   Đúng payload đó mất **31,0 ms** dưới rule safe và **9.154 ms** dưới rule vulnerable.
   Một sự cố có thể không deploy mã nào và lưu lượng không đổi — chỉ một phiên bản
   rule. RCA chỉ nhìn "dịch vụ nào hỏng" đã bắt đầu thấp hơn một tầng so với câu hỏi
   thật: "cái gì đã thay đổi?".
2. **Một request nặng CPU có thể đóng băng cả worker.** CPython `re` giữ GIL trong
   lúc match, nên một request backtracking đơn lẻ làm đứng cả `/healthz` (không phụ
   thuộc gì) **9.145 ms**. "Một endpoint chậm" thực ra là "worker ngừng phục vụ".
   Bán kính ảnh hưởng của một request tồi là toàn bộ tiến trình — đó là lý do một
   regex có thể lan toàn cầu.
3. **Phát hiện ≠ chẩn đoán.** Pipeline phát hiện nhanh (~9 giây) và khoanh đúng thành
   phần bị nghẽn (`waf-engine`), nhưng không gọi được tên nguyên nhân và action khuyến
   nghị hàng đầu ("Mở rộng bộ nhớ cdn-cache") vay từ một sự cố quá khứ trùng dịch vụ
   nhưng không liên quan cơ chế. Nhanh, tự tin, và chỉ sai chỗ.

## 1 điều pipeline của tôi vẫn sẽ bỏ sót nếu sự cố này xảy ra thật

- **Mẫu hình:** một **thay đổi cấu hình/deploy nguyên tử toàn cầu** mà dấu vết duy
  nhất là "mọi thứ cùng suy giảm một lúc, trong cùng một khoảnh khắc".
- **Vì sao bỏ sót:** pipeline chỉ nạp ngưỡng-metric-bị-vượt — **không có change-event
  feed**, nên sự thật chẩn đoán quan trọng nhất (một phiên bản rule được đẩy 9 giây
  trước cú tăng) bị vô hình về mặt cấu trúc. Tệ hơn, tính đồng thời nguyên tử xoá
  thứ tự first-drift/causal-lag mà RCA topology-thời-gian dựa vào, nên nó suy biến
  về topology + severity và tự tin gọi tên hub được-phụ-thuộc-nhiều-nhất — rồi kéo
  một biện pháp khắc phục sai từ truy hồi trùng-dịch-vụ.
- **Ý tưởng giảm thiểu:** ADR-001 (nạp sự kiện thay đổi, xếp artifact tương quan-thay-đổi
  trên hub topology) **cộng** hai biện pháp phòng ngừa khiến phát hiện runtime trở
  nên không cần thiết: một cổng ReDoS/độ-phức-tạp pre-deploy, và một canary để 1% tập
  con nghẽn trước.

## 1 quyết định trong ADR tôi không hoàn toàn chắc

Xếp sự-kiện-thay-đổi **trên** topology (ADR-001). Trong một môi trường deploy dày
đặc, gần như luôn có một deploy gần *bất kỳ* sự cố nào, nên "đổ cho thay đổi mới
nhất" có rủi ro kích hoạt quá mức và bào mòn niềm tin (cry-wolf). Tôi đã rào bằng
quy tắc yêu-cầu-cả-hai (đi-trước-về-thời-gian **và** giao bán-kính-ảnh-hưởng) và
một rollout chỉ-gợi-ý, nhưng tôi không chắc độ chính xác có đủ cao để cho
sự-kiện-thay-đổi *vượt* topology mặc định — nó có thể nên là một tín hiệu **ngang
hàng** thay vì cao hơn. Cần đo trên lượng thay đổi thật trước khi đề bạt qua
chỉ-gợi-ý.

## Phán quyết mô hình chi phí cho stack của tôi

Cho stack đã spec (100 dịch vụ, 5 sự cố/tháng × 2 h, downtime ~$50k/giờ, AIOps
~$25k/tháng):

- **ROI:** 8.0
- **Hoàn vốn (payback):** 0,125 tháng (~3,75 ngày)
- **Phán quyết:** **worth_it**

(Với lĩnh vực edge/CDN của sự cố tái hiện — downtime $500k/giờ — ROI ~10.0. Điểm hoà
vốn cho stack e-commerce là chi phí downtime ≈ $6.250/giờ; dưới mức đó, hoặc dưới ~3
sự cố/tháng, khuyến nghị lật sang "đầu tư SLO + observability trước".)
