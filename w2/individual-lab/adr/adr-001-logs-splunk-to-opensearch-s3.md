# ADR-001 — Thay Splunk Cloud + Datadog Logs bằng nền tảng log 2 tầng OpenSearch + S3/Athena 

**Trạng thái:** đề xuất · **Ngày:** 2026-06-11

## Bối cảnh 

Logs chiếm 37% bill observability ($13,900 Splunk + $1,800 Datadog Logs = $15,700/tháng cho 52 GB/ngày) và đồng thời là capability hoạt động tệ nhất: độ trễ search vượt 25 giây khi query quá 7 ngày (pain point #1), việc xoay vòng index theo quý trả về kết quả rỗng ngay giữa lúc đang xử lý sự cố (pain point #6), và cùng một dữ liệu bị trả tiền hai lần (bản sao hot trên Datadog + Splunk). Hợp đồng Splunk kết thúc sau 7 tháng với cửa sổ báo trước 90 ngày mà team đã từng lỡ một lần (pain point #10), và export hàng loạt bị giới hạn theo hợp đồng ở mức 100 GB/ngày — nên *khi nào* quyết định cũng quan trọng ngang *quyết định cái gì*.

Hôm nay có hai nhóm người dùng log khác biệt: kỹ sư on-call (cần: search nhanh trên dữ liệu vài giờ–vài ngày gần nhất) và security/compliance (cần: giữ 12 tháng, báo cáo định kỳ, tính đầy đủ). Nhét chung hai nhu cầu này vào một hot index đắt tiền chính là gốc rễ của vấn đề chi phí.

## Quyết định 

Tách capability theo người dùng: **OpenSearch Service làm tầng hot 14 ngày** cho on-call search (3× r6g.xlarge.search, $978/tháng), và **S3 với lifecycle xuống Glacier + Athena** làm tầng cold 12 tháng cho compliance ($113/tháng). OTel Collector ghi song song (dual-write) qua Firehose. Splunk nhận thông báo không gia hạn vào tuần 8 của migration, sau khi các gate kiểm chứng tương đương đã pass; việc export 30 ngày dữ liệu tồn trên Splunk (~1.6 TB) bắt đầu tuần 3 và nằm gọn trong giới hạn 100 GB/ngày trong ~16 ngày.

## Các phương án đã cân nhắc và bị loại

1. **Đàm phán giảm giá Splunk (ở lại).** Một hợp đồng cam kết năm có thể giảm 25–35%, nhưng các vấn đề *capability* vẫn còn nguyên: độ trễ search 25 giây là vấn đề sizing/kiến trúc ở tier workload của họ, và ta lại chui vào khóa 12 tháng với đúng cái bẫy cửa sổ báo trước đã từng đốt team một lần. Loại: trả rất nhiều tiền để giữ lại nỗi đau.
2. **Grafana Loki (tự host) làm tầng hot.** Phương án khả tín rẻ nhất (object-storage-first, ~$400/tháng hạ tầng ở volume của ta) và nhất quán với bề mặt query Grafana. Loại vì Loki index *label, không index nội dung*: thói quen của on-call và hai chẩn đoán incident đã ghi nhận đều dựa trên search toàn văn trên dòng log thô, và việc tái huấn luyện team rời khỏi full-text search trong cùng cửa sổ với bốn migration khác là rủi ro không cần thiết. Hẹn xem lại ở mốc 12 tháng khi stack đã ổn định.
3. **CloudWatch Logs làm tầng duy nhất.** Đơn giản vận hành nhất (không node nào phải quản), nhưng ingest $0.50/GB → 1.56 TB/tháng ≈ $780 *chỉ riêng tiền nạp*, cộng $0.005/GB scan trên mỗi query Insights, và giữ 12 tháng trong CW Logs đắt ~10× S3/Glacier. Loại vì kinh tế đơn vị: đây là con đường đắt nhất ở đúng tier khối lượng mà ta đang cố cắt.

## Hệ quả 

**Tích cực:** −$14,600/tháng trên hai dòng log (dòng 4–7 của cost model); tầng hot được size theo đỉnh 14:00 với gate load-test trước cutover; dữ liệu cold ở định dạng mở trong bucket của chính mình — lần migrate *tiếp theo* của capability này sẽ là chuyện không đáng kể; báo cáo của security team chuyển từ UI vendor sang Athena SQL có version trong repo.

**Tiêu cực:**
- Team giờ sở hữu một cụm OpenSearch: index lifecycle, shard sizing, nâng cấp version, và bị page lúc 3 giờ sáng khi chính tầng hot suy giảm. Đã dự toán 0.5 FTE (dòng 15 cost model), nhưng *kỹ năng* hôm nay chưa có trong team — phải đào tạo hoặc tuyển (risk register R6).
- Query dữ liệu cũ hơn 14 ngày rơi từ mức tương tác tức thì (Splunk, hot 30 ngày) xuống Athena với độ trễ tính bằng phút. Phản ứng sự cố trực tiếp không bị ảnh hưởng (làm việc trên dữ liệu mới — median MTTD là 11 phút), nhưng "khảo cổ" postmortem chậm hơn (định lượng ở FINDINGS Q2).
- Security/compliance team phải viết lại các báo cáo SPL thành Athena SQL và ký xác nhận trước khi Splunk chết — họ là dependency cứng trên critical path (gate G2, risk R5), và ta không thể ép lịch của họ.
- Trong nhà vẫn còn hai ngôn ngữ query (OpenSearch DSL cho hot, SQL cho cold) — một bước lùi cục bộ so với lời phàn nàn "quá nhiều ngôn ngữ query" của pain point #8, chấp nhận như cái giá của chênh lệch chi phí hot/cold 50×.
