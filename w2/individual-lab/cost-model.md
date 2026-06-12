# A3 — Cost model: hiện trạng → trạng thái đích (bản dịch)

Mọi giá đích đều là **giá public list us-east-1** (trang pricing AWS / giá public PagerDuty + Atlassian, kiểm tra tháng 6/2026). Phép tính giữ hiển thị rõ theo từng dòng. Số liệu hiện trạng lấy từ `current-stack.md` (ảnh chụp hóa đơn).

## Giả định quy mô (nêu rõ, vì đầu vào là mô tả định tính)

| Giả định | Giá trị | Căn cứ |
|---|---|---|
| Hosts | 300 | current-stack.md (dòng Datadog infra) |
| Khối lượng log | 52 GB/ngày ≈ 1.56 TB/tháng | current-stack.md (dòng Splunk) |
| Số log event được index | ~1.05 tỉ events/tháng | current-stack.md (dòng Datadog Logs) |
| Số metric series hoạt động sau khi Collector relabel | 450K (1,500/host × 300 hosts) | hiện riêng phần series *thừa* đã là ~440K (pain point #4); relabeling loại các tag cardinality tự do như `customer_id` |
| Chu kỳ scrape | 30 s → 2 samples/phút/series | thực hành mặc định Prometheus |
| Khối lượng request | ~1.2 tỉ requests/tháng (~460 rps trung bình) | 10 services, profile e-commerce; nêu rõ là giả định |
| Tỉ lệ giữ tail-sampling | ~2.5% (100% lỗi ~0.5% + outlier p99 ~1% + baseline 1%) | ADR-002 |

## Mô hình

| # | Hạng mục | Hiện tại | Component đích | Đơn giá (list price) | Phép tính | Đích |
|---|---|---:|---|---|---|---:|
| 1 | Infra metrics | $5,400 | AMP | $0.90 / 10M samples (2B đầu), $0.35 / 10M (250B tiếp theo) | 450K series × 2/phút × 43,800 phút ≈ 39.4B samples → 2B×$0.90/10M = $180; 37.4B×$0.35/10M = $1,309; lưu trữ+query ≈ $30 | **$1,520** |
| 2 | Custom metrics vượt hạn mức | $2,200 | (gộp vào dòng 1 — AMP không phụ thu theo series; cardinality bị chặn bởi relabel rules) | — | — | **$0** |
| 3 | APM hosts + premium | $12,100 | OTel SDK + X-Ray | $5 / 1M traces ghi; $0.50 / 1M lấy ra | 1.2B req × 2.5% giữ = 30M traces × $5/M = $150; truy xuất ~40M × $0.50/M = $20 | **$170** |
| 4 | Log search nóng | $13,900 (Splunk) | OpenSearch Service | r6g.xlarge.search $0.335/giờ; EBS gp3 $0.122/GB-tháng | 3 nodes × $0.335 × 730h = $734; hot 14 ngày: 52GB/ngày×14×1.3 (index) ×2 (replica) ≈ 1.9TB → 2TB × $0.122 = $244 | **$978** |
| 5 | Log indexing (bản sao hot) | $1,800 (Datadog Logs) | (capability gộp vào dòng 4 — một tầng hot, không phải hai) | — | — | **$0** |
| 6 | Log lưu lạnh + query | (nằm trong Splunk) | S3 + Glacier + Athena | S3 $0.023/GB-tháng; Glacier Flexible $0.0036/GB-tháng; Athena $5/TB scan | 30 ngày standard: 1.6TB×$0.023 = $37; lưu trữ trung bình 17TB×$0.0036 = $61; Athena ~3TB/tháng scan = $15 | **$113** |
| 7 | Vận chuyển log | — | Data Firehose | $0.029/GB (500TB đầu) | 1.56TB × 1,000 × $0.029 | **$45** |
| 8 | Dashboards + SLO | $1,050 (Grafana Cloud) | Amazon Managed Grafana | $9/editor, $5/viewer | 10 editors × $9 + 50 viewers × $5 | **$340** |
| 9 | Bus audit alert/action | — | EventBridge | $1 / 1M custom events | ~10M events/tháng | **$10** |
| 10 | Paging | $3,900 (65 × $60) | PagerDuty Business, ít seat hơn | $41/user/tháng (thanh toán năm, giá public) | 30 responders × $41 | **$1,230** |
| 11 | Synthetics | $1,360 (270 × $5) | CloudWatch Synthetics + Blackbox Exporter | $0.0012/lượt chạy canary | 60 canaries × 6 lượt/giờ × 730h = 263K lượt × $0.0012 = $315; check nội bộ qua Blackbox ≈ $0 | **$315** |
| 12 | Status page | $290 | Statuspage (giữ) | gói không đổi | — | **$290** |
| 13 | Metrics/alarms AWS-native | — | CloudWatch | alarms $0.10/tháng, API/dashboards | khoản dự phòng | **$300** |
| 14 | Runtime cho Collector + tooling | — | EKS + 3× m6g.large | EKS $0.10/giờ; m6g.large $0.077/giờ | $73 + 3 × $0.077 × 730 = $73 + $169 | **$242** |
| 15 | **Chi phí ops cho các phần tự vận hành** | (đang ẩn: 3 kỹ sư part-time lo alert routing) | 0.5 FTE platform engineer | $160K/năm tổng chi phí | $160,000 / 12 × 0.5 | **$6,667** |
| | **Tổng** | **$42,000** | | | | **$12,220** |

## Kết quả

- **Mức giảm: $42,000 → $12,220 = −70.9%** (mục tiêu là ≥40%).
- Nếu loại dòng nhân sự (để so sánh tương đương với hóa đơn hiện tại, vốn cũng giấu chi phí người): $5,553/tháng = −86.8%.
- Biên an toàn là cố ý: kể cả khi mọi dòng AWS đắt hơn mô hình 50%, tổng = $15,000 → vẫn −64%.

## Dòng độ nhạy (bắt buộc): khối lượng dữ liệu tăng nhanh gấp 2× dự kiến

| Dòng | Ở mức 2× volume | Chênh lệch | Hành vi |
|---|---:|---:|---|
| AMP (1) | $2,870 | +$1,350 | **Tuyến tính** — tăng tuyệt đối lớn nhất |
| OpenSearch hot (4) | $1,956 | +$978 | **Bậc thang** — số node nhân đôi 3→6; đây là thứ vỡ trước về mặt vận hành (heap chịu áp lực ở đỉnh 14:00 trước khi bill kịp nhúc nhích) |
| Firehose (7) + S3 (6) | $316 | +$158 | tuyến tính, nhỏ |
| X-Ray (3) | $340 | +$170 | tuyến tính, nhỏ |
| Phần còn lại | không đổi | — | tính giá theo seat/gói |
| **Tổng** | **~$14,880** | +$2,660 | vẫn **−64.6%** so với hiện tại |

**Cái gì phá ngân sách trước:** ở mức 2× không có gì phá *ngân sách* (vẫn dưới xa mức trần $25,200 của mục tiêu −40%). Cái vỡ *trước* là tầng hot OpenSearch — nó hỏng về mặt vận hành (độ trễ query lúc cao điểm, đúng kiểu hỏng của pain point #1) trước khi hỏng về tài chính, vì năng lực tăng theo bậc node. Van xả, theo thứ tự: rút cửa sổ ILM hot 14 ngày → 7 ngày (giảm nửa storage, không thêm node), rồi sampling log mức DEBUG tại Collector, rồi +3 nodes. Ngược lại, trên stack *hiện tại*, 2× volume cộng thêm ≈ +$17K/tháng (Splunk workload pricing + các dòng per-host/per-event của Datadog đều tăng tuyến tính theo giá list SaaS) — đó là lập luận cấu trúc cho việc tự sở hữu pipeline.
