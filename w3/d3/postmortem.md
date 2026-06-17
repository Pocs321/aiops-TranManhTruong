# Postmortem: WAF managed-rule catastrophic backtracking (cạn CPU tại edge)

**Trạng thái:** hoàn tất
**Ngày:** 2026-06-15
**Tác giả:** ourlife937 (tác giả bản tái hiện / nền tảng AIOps)
**Mức độ:** SEV1
**Thời lượng:** 15,4 giây (tái hiện) — trigger 09:58:36.533 UTC → hồi phục hoàn toàn 09:58:51.953 UTC. Tái hiện sự cố lớp-CVE Cloudflare 2019-07-02, thời lượng thực 27 phút.

> Đây là postmortem của một **bản tái hiện** viết từ lần chạy môi trường tối giản,
> không phải từ telemetry production. Số liệu gắn nhãn *(đo được)* lấy từ lần chạy
> đã thu (`timeline.json`, `metrics_samples.json`, `rca_observed.json`); số liệu
> gắn nhãn *(mô hình hoá)* được quy chiếu theo postmortem công khai gốc
> (https://blog.cloudflare.com/details-of-the-cloudflare-outage-on-july-2-2019).

## Tóm tắt

Một WAF managed rule chứa regex có lượng từ lồng nhau trên một nhóm ký tự chồng
lấn (`^([A-Za-z0-9]+)+!$`) được đẩy lên toàn cầu. Với input chữ-số bình thường,
regex engine backtracking theo hàm mũ, đẩy CPU của worker WAF lên 100%. Vì CPython
`re` giữ GIL trong lúc match, một request bệnh lý đơn lẻ làm đóng băng **toàn bộ**
tiến trình worker — ngay cả `/healthz` (không phụ thuộc gì) cũng đứng 9,1 giây
*(đo được)*. Bản thân lưu lượng không hề thay đổi; chỉ phiên bản rule thay đổi.
Hồi phục tức thì ngay khi managed ruleset được rollback về phiên bản thời-gian-tuyến-tính
qua kill-switch.

## Ảnh hưởng

- **Người dùng bị ảnh hưởng:** toàn bộ lưu lượng đi qua edge worker trong cửa sổ
  cơn bão *(đo được: mọi probe trong pha storm đều thấy độ trễ nhiều giây hoặc
  timeout)*. Quy chiếu sang sự cố gốc: ~toàn cầu, tụt ~82% lượng request.
- **Ảnh hưởng doanh thu:** *(mô hình hoá)* với một doanh nghiệp CDN/edge có chi phí
  downtime ~$500k/giờ, tương đương 27 phút thực ≈ **$225k**. Lần chạy tái hiện:
  không áp dụng (lab).
- **Error budget tiêu hao:** SLO khả dụng + độ trễ của edge-http-proxy bị đốt sạch
  trong cửa sổ sự cố; *(mô hình hoá)* một sự cố 27 phút trên SLO khả dụng 99.9%/tháng
  tiêu ~62% của 43 phút error budget tháng chỉ trong một lần.
- **Truyền thông đối ngoại:** *(mô hình hoá)* sự cố trên status-page + blog hậu sự
  cố, phản chiếu bản gốc.

## Dòng thời gian (UTC, 2026-06-15)

| Thời điểm | Sự kiện |
|------|-------|
| 09:58:34 | Edge worker chạy, rule **safe** đang sống, lưu lượng chảy (baseline) |
| 09:58:36 | Baseline khoẻ mạnh: cùng payload được kiểm tra với p99 **31,0 ms** *(đo được)* |
| 09:58:36 | **Trigger** — WAF managed-ruleset hot-swap sang `v-vulnerable` toàn cầu; lưu lượng không đổi |
| 09:58:45 | Triệu chứng đầu tiên người dùng thấy — `/healthz` (không regex) mất **9.145 ms**; worker đứng vì GIL bị match backtracking giữ *(đo được)* |
| 09:58:45 | CPU tiến trình `waf-engine` **96%** (>85%); một core bị ghim hoàn toàn *(đo được)* |
| 09:58:45 | Độ trễ request `edge-http-proxy` p99 **9.154 ms** vượt SLO 1.000 ms *(đo được)* |
| 09:58:45 | Alert đầu tiên kích hoạt; alert chuyển cho pipeline AIOps (phát hiện) |
| 09:58:47 | On-call xác nhận (ack) trang gọi |
| 09:58:49 | Xác định nguyên nhân gốc — người ứng phó nối cú tăng CPU với lần deploy `v-vulnerable` (mối liên hệ pipeline **không** cung cấp) |
| 09:58:50 | Khắc phục — managed ruleset rollback về `v-safe` toàn cầu (kill-switch) |
| 09:58:51 | Hồi phục hoàn toàn — p99 trở lại **34,4 ms**, CPU bình thường *(đo được)* |

Suy ra: thời gian-tới-phát-hiện tính từ trigger ≈ **9,3 giây** (< mục tiêu 30 giây);
thời gian-tới-khắc-phục tính từ lúc phát hiện ≈ **4,5 giây**; từ khắc phục →
hồi phục ≈ **1,6 giây**.

## Nguyên nhân gốc

Khiếm khuyết nằm ở **nội dung của một artifact cấu hình**, không phải ở mã nguồn
hay năng lực của bất kỳ dịch vụ nào. Rule `^([A-Za-z0-9]+)+!$` chứa một lượng từ
lồng nhau (`(…+)+`) trên một nhóm chồng lấn. Với input gồm *n* ký tự chữ-số mà
không có `!` kết thúc, engine phải thử O(2ⁿ) cách phân hoạch trước khi thất bại —
catastrophic backtracking kinh điển. Match này nặng CPU và giữ interpreter lock,
nên một request như vậy độc chiếm cả worker.

Hai yếu tố khuếch đại biến một rule tồi thành sự cố toàn cầu:

1. **Deploy nguyên tử toàn cầu.** Rule được đẩy đi mọi nơi cùng lúc, nên bán kính
   ảnh hưởng là 100% ngay khoảnh khắc nó lên sống — không có một tập con nhỏ hơn
   nào để học hỏi từ đó.
2. **WAF chia sẻ luồng/lock phục vụ request.** Việc đánh giá rule không được cô lập
   (pool riêng, ngân sách CPU-time), nên một rule đơn lẻ có thể bỏ đói liveness của
   cả worker.

## Yếu tố góp phần

- Kiểm tra pre-merge / pre-deploy của pipeline rule chưa bao gồm một cổng phân tích
  tĩnh độ phức tạp regex / ReDoS (catastrophic-backtracking).
- Quá trình rollout mang tính nguyên tử thay vì tăng dần (không có canary 1% → 10% →
  100%), nên một lỗi độ phức tạp không thể bị bắt trên một tập con nhỏ trước.
- Không có ngân sách CPU-time hay watchdog cho mỗi rule để giới hạn chi phí của một
  lần match đơn.
- Pipeline AIOps nạp ngưỡng-metric-bị-vượt nhưng **không** nạp sự kiện deploy/thay-đổi-cấu-hình,
  nên tín hiệu chẩn đoán quan trọng nhất bị vô hình về mặt cấu trúc với nó.

## Phát hiện

**Phát hiện thế nào:** giám sát tổng hợp (synthetic) thấy độ trễ `/healthz` và p99
của `edge-http-proxy` vượt SLO và CPU `waf-engine` vượt 85%; detector kích hoạt
~9,3 giây sau deploy và alert tới pipeline AIOps.

**Có thể phát hiện sớm hơn không?** Không phải bởi pipeline này. Trigger là một
deploy nguyên tử toàn cầu, nên bán kính ảnh hưởng đã là 100% trước khi bất kỳ metric
nào dịch chuyển. Phát hiện sớm hơn đòi hỏi một tín hiệu *trước runtime*: một kiểm
tra tĩnh ReDoS pre-deploy, hoặc một pha canary cho phép 1% tập con nghẽn trước. Cả
hai đều nằm ngoài pipeline và được ghi nhận thành action item P0.

**Các lỗ hổng pipeline quan sát được (từ `rca_observed.json`):**

- **Lỗ hổng 1 — không có tín hiệu sự-kiện-thay-đổi; không gọi được tên nguyên nhân.**
  Pipeline khoanh đúng thành phần bị nghẽn (`waf-engine`, confidence 0.515) bằng
  topology + severity, nhưng không thể gọi tên *cái gì* đã làm hỏng nó. Trigger là
  một sự kiện thay đổi cấu hình (`ruleset_deployed`), không phải metric alert, nên
  nó xuất hiện trong `timeline.json` mà **không bao giờ** là input của pipeline.
  Phần lớn thời gian MTTR bị một con người tiêu vào việc tự suy lại "một rule vừa
  được đẩy" — đúng cái sự thật mà một change feed lẽ ra đã trao ngay. *(Dẫn tới ADR-001.)*
- **Lỗ hổng 2 — truy hồi khớp dịch vụ, không khớp cơ chế.** Truy hồi sự-cố-tương-tự
  lôi ra `INC-2026-02-11` (cache eviction) và `INC-2026-04-03` (độ trễ DNS) ở mức
  tương tự 0.7 — cùng *dịch vụ*, khác *cơ chế* — và đặt playbook của chúng lên **đầu**
  danh sách action khuyến nghị: *"Mở rộng bộ nhớ cdn-cache", "Làm nóng trước các
  object nóng"*. Làm theo gợi ý #1 của pipeline sẽ chẳng giải quyết được một regex
  nghẽn CPU.
- **Lỗ hổng 3 — tính đồng thời đánh bại xếp hạng theo độ-trễ-nhân-quả.** Một deploy
  nguyên tử toàn cầu khiến mọi dịch vụ vượt ngưỡng trong cùng một cửa sổ scrape.
  Bất kỳ RCA nào dựa vào thứ tự first-drift / causal-lag (như ví dụ ADR-007 trong
  note W3-D3) đều không có gradient thời gian để khai thác ở đây, và suy biến về chỉ
  còn topology + severity.

## Ứng phó

**Điều làm tốt**
- Kill-switch (rollback managed-rule) hồi phục data plane trong **1,6 giây**.
- Giữ input không đổi giữa baseline và storm khiến **phiên bản rule** trở thành biến
  độc lập hiển nhiên.
- Phát hiện khoanh vùng thành phần bị nghẽn nhanh và chính xác.

**Điều làm chưa tốt**
- Action khuyến nghị hàng đầu của pipeline gây hiểu lầm chủ động (mở rộng cache).
- Một lỗi độ phức tạp lọt tới production với bán kính ảnh hưởng 100% và không có canary.
- Liveness (`/healthz`) bị ghép cặp với CPU đánh giá rule, nên worker trông như
  *chết* chứ không chỉ là *chậm*, che lấp bản chất của lỗi.

**Nơi ta gặp may**
- Một action chung mang tính bản mẫu ("kiểm tra các deploy gần đây") tình cờ chỉ đúng
  hướng; chẳng có gì trong dữ liệu *xứng đáng* nhận gợi ý đó.
- Thay đổi gây lỗi có một mốc deploy rất gần và rõ ràng để đối chiếu; một cấu hình
  trôi từ từ sẽ khó truy ngược hơn nhiều.
- Việc tắt WAF toàn cầu tự nó không tạo ra một sự cố (an ninh) tệ hơn.

## Hành động khắc phục

| Hạng mục | Phụ trách | Hạn | Ưu tiên |
|------|-------|-----|----------|
| Thêm cổng phân tích tĩnh độ phức tạp regex / ReDoS vào pipeline deploy rule (chặn lượng từ lồng nhau trên nhóm chồng lấn) | Nền tảng WAF | 2026-06-22 | P0 |
| Thay rollout rule nguyên tử toàn cầu bằng canary tăng dần (1% → 10% → 100%) tự dừng khi CPU/độ trễ thoái hoá | Kỹ thuật phát hành | 2026-06-22 | P0 |
| Nạp sự kiện deploy/thay-đổi-cấu-hình làm tín hiệu RCA hạng nhất; xếp artifact tương quan-thay-đổi trên topology thuần (xem ADR-001) | Nền tảng AIOps | 2026-07-06 | P1 |
| Cô lập việc đánh giá WAF rule khỏi đường liveness và thêm ngân sách CPU-time / watchdog cho mỗi lần match | Nền tảng WAF | 2026-07-06 | P1 |
| Gắn nhãn lịch sử sự cố theo lớp-lỗi để truy hồi sự-cố-tương-tự khớp cơ chế, không chỉ trùng dịch vụ | Nền tảng AIOps | 2026-07-31 | P2 |
| Thêm một probe canary "input đối kháng" vào bộ healthcheck của edge | SRE On-call | 2026-07-31 | P2 |
