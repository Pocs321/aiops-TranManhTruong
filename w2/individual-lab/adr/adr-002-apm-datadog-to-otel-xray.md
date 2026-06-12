# ADR-002 — Thay Datadog APM bằng OpenTelemetry + tail-based sampling + X-Ray, và đưa Grafana làm bề mặt correlation 

**Trạng thái:** đề xuất · **Ngày:** 2026-06-11

## Bối cảnh 

Datadog APM là dòng chi phí đơn lẻ lớn nhất ($11,800 + $300 add-on = $12,100/tháng, 29% bill) **và đồng thời** là capability mà mục tiêu MTTR phụ thuộc nhiều nhất — chính design hint của đề bài cảnh báo: "nếu mục tiêu là giảm MTTR, đừng moi ruột tracing." Hiện tại tracing sample 1% theo kiểu head-based (pain point #2): khi một lỗi chạm 0.3% requests, những trace có thể giải thích nó đã bị sample bỏ — hai incident quý trước phải chẩn đoán từ log vì trace không tồn tại. Trong khi đó correlation là thao tác thủ công trên bốn UI (pain point #3: 8 phút từ lúc bị page đến giả thuyết đầu tiên; pain point #5: 47 page chưa gom nhóm trong một cú sập dây chuyền).

Vậy quyết định này phải cắt dòng chi phí lớn nhất *đồng thời cải thiện* độ hữu dụng của trace — đúng cái thế căng "rẻ **và** nhanh hơn" mà đề bài nói sẽ khiến một kế hoạch lười bị bác.

## Quyết định 

Instrument cả 10 services bằng **OpenTelemetry SDK**, chạy **tail-based sampling ở tầng OTel Collector** (giữ 100% trace lỗi, 100% outlier latency p99, baseline 1% — ~2.5% của ~1.2 tỉ requests/tháng ≈ 30 triệu traces), lưu trong **AWS X-Ray** ($170/tháng theo giá list), và hiển thị traces + service map trong **Amazon Managed Grafana** cạnh metrics và logs, nối với nhau bằng `trace_id` được chèn vào dòng log.

Phép tính thẳng thừng: head sampling 1% ghi trace *ngẫu nhiên*; tail sampling 2.5% ghi những trace *có giá trị*. Ta lưu nhiều trace hơn hôm nay 2.5 lần với 1.4% chi phí APM hiện tại, và kịch bản "query chậm ảnh hưởng 0.3% requests" đi từ ~0 trace giữ được lên 100% giữ được.

## Các phương án đã cân nhắc và bị loại

1. **Giữ Datadog APM, giảm số host.** Bỏ infra agent trùng lặp và cắt xuống ~150 APM hosts tiết kiệm ~$6K/tháng — không tệ, nhưng vẫn giữ per-host pricing (mỗi lần scale-out là một sự kiện hóa đơn), vẫn giữ sampling 1% trừ khi trả *thêm* tiền ingest, và vẫn giữ vấn đề đối chiếu 4 UI gây ra độ trễ 8 phút tới giả thuyết. Loại: trượt vế MTTR của nhiệm vụ.
2. **Grafana Tempo tự host làm kho trace.** Rẻ nhất ở quy mô lớn (object storage, ~$250/tháng hạ tầng) và TraceQL native trong Grafana. Loại *cho cửa sổ 6 tháng này*: nó thêm một hệ stateful tự vận hành thứ hai trong cùng cửa sổ với OpenSearch (ADR-001), trong khi ngân sách kỹ năng self-host (0.5 FTE) đã cam kết hết. X-Ray là managed, tích hợp sẵn với hệ thống IAM/account AWS hiện có, và ở mức 30M traces/tháng chỉ tốn $170 — phương án Tempo không tiết kiệm được gì đáng kể cho đến khi volume trace tăng ~10×. Khi đó xem lại; nhờ OTel, việc đổi chỉ là thay config Collector.
3. **Vặn head-sampling của Datadog lên 10–20% thay vì tail sampling.** Giải quyết độ phủ trace về mặt thống kê (nhiều trace hơn ≈ nhiều cơ hội trace xấu sống sót hơn), nhưng chi phí tăng theo hệ số nhân trên giá ingest của Datadog mà vẫn trượt trace của sự kiện hiếm theo xác suất. Tail sampling đảo ngược logic này: quyết định *sau khi* thấy kết quả. Loại: trả nhiều hơn cho một bảo đảm yếu hơn.

## Hệ quả 
**Tích cực:** −$11,930/tháng trên dòng APM; trace lỗi và trace tail-latency trở nên khả dụng *một cách tất định* (hai incident "mù trace" quý trước đều sẽ có trace đầy đủ dưới chính sách này); service map + trace + metrics + logs hội tụ trên một màn hình Grafana, đánh thẳng vào khoản thuế 8 phút đối chiếu thủ công; instrumentation thành OTel trung lập vendor — ADR này là lần cuối cùng "migrate APM" đồng nghĩa với việc đụng vào code ứng dụng.

**Tiêu cực:**
- **Tầng tail-sampling của Collector trở thành một component trọng yếu, stateful, ngốn RAM mà ta sở hữu.** Nó phải buffer toàn bộ span của một trace đang bay trước khi ra quyết định; cấu hình sai sẽ âm thầm vứt đúng bằng chứng mà cả thiết kế này được mua để giữ. Đây là component bất định nhất của thiết kế — nó là chủ đề của POC A7 và risk R1, với một game-day gate (replay sự cố kiểu INC-2025-11-08, khẳng định ≥99% trace lỗi được bắt) trước khi tắt Datadog APM.
- X-Ray nghèo tính năng hơn Datadog APM: retention 30 ngày, UX flame-graph yếu hơn, không có anomaly detection kiểu watchdog trên trace. Ta mất độ bóng bẩy mà team đã quen; ván cược là *có đúng trace* thắng *có UI đẹp hơn trên những trace sai*.
- Công sức instrumentation: 10 services × OTel SDK + chèn `trace_id` vào log ≈ 2–3 engineer-tuần trải đều trên các team sở hữu (tuần 5–6 của migration plan) — một chi phí điều phối thật trên 8 team.
- Lịch sử trace không migrate được (Datadog giữ 15 ngày, export theo API từng trace) — chấp nhận cắt đứt sạch; các incident lịch sử vẫn được ghi lại trong postmortem.
