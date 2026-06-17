# Bản tái hiện — Sự cố Cloudflare WAF regex (2019-07-02)

Tái hiện môi-trường-tối-giản failure mode **catastrophic backtracking** (một regex
chạy thời gian hàm mũ trên input đối kháng). Tái hiện *mẫu hình* (pattern), không
phải đồng hồ thực — sự cố thật kéo 27 phút; lần chạy này ~15 giây (xem anti-pattern
"tái hiện 1:1 với prod" trong note W3-D3).

## Cái gì thực sự hỏng (và cái này mô hình hoá gì)

Ngày 2019-07-02 Cloudflare deploy một WAF managed rule chứa regex có lượng từ lồng
nhau trên một nhóm chồng lấn. Với input dạng HTML, engine khám phá theo hàm mũ các
cách phân hoạch chuỗi → **CPU nghẽn 100% trên mọi máy edge, toàn cầu và đồng thời**,
vì rule được deploy nguyên tử ở mọi nơi. Lưu lượng tụt ~82% trong 27 phút.

Bản tái hiện giữ cơ chế cốt lõi và lược bỏ mọi thứ khác:

| Sự cố thật | Bản tái hiện này |
|---|---|
| WAF managed rule, deploy nguyên tử toàn cầu | `waf_edge.py` hot-swap rule qua `POST /admin/rule` |
| Lượng từ lồng kiểu `(?:…\|…)+…(?:.*=.*)` | `^([A-Za-z0-9]+)+!$` (hàm mũ đáng tin) |
| Mọi edge worker nghẽn cùng lúc | một worker nghẽn; "toàn cầu" là nhãn trên alert |
| Sự cố 27 phút | lần chạy ~15 giây đã nén |
| Khắc phục: kill-switch rule toàn cầu | `POST /admin/rule {"mode":"safe"}` (regex tuyến tính) |

## Chi tiết trung thực: một request làm đứng cả worker

CPython `re` **giữ GIL** trong lúc match. Nên một request backtracking đơn lẻ không
chỉ làm *chính nó* chậm — nó đóng băng cả tiến trình worker. Trong lần chạy đã thu,
`GET /healthz` không-regex mất **9.145 ms** trong khi một lần match đối kháng chạy.
Đó là bài học thật của sự cố Cloudflare thu nhỏ: sự cố không phải "một endpoint
chậm", mà là **"worker ngừng phục vụ"**. Cùng payload dưới rule safe: **31,0 ms**.

## Các file

```
waf_edge.py             edge worker dễ tổn thương (http.server chuẩn; không cần FastAPI)
drive_incident.py       khởi động worker → baseline → deploy rule tồi → storm →
                        detect → respond → rollback → recover. Thu dữ liệu thật.
run_pipeline.py         nạp alerts_observed.json vào pipeline w2/d3 THẬT
                        (correlate.py + rca.py) trên topology edge này.
topology.json           đồ thị phụ thuộc edge (cùng schema với w2/d3 services.json)
incidents_history.json  gieo để truy hồi lôi ra "tiền lệ" SAI cơ chế
start_reproduction.sh   dựng worker để chơi thủ công
inject.sh               kích hoạt thủ công (burst curl) trên worker đang chạy
```

Đầu ra đặt ở thư mục cha (`w3/d3/`): `timeline.json`, `alerts_observed.json`,
`metrics_samples.json`, `rca_observed.json`.

## Chạy

```bash
# Thu một-phát (cái đã sinh ra các artifact đã nộp):
python drive_incident.py --port 8093 --storm 6
python run_pipeline.py

# Hoặc điều khiển thủ công:
bash start_reproduction.sh 8080
bash inject.sh 8080
curl -s -XPOST http://127.0.0.1:8080/admin/rule -d '{"mode":"safe"}'   # mitigate
```

`drive_incident.py` hiệu chỉnh độ dài payload tại chỗ để một lần match có giới hạn
(~1 giây) và cơn bão không bao giờ chạy mất kiểm soát — quan trọng trên Windows, nơi
không thể ngắt `re` từ luồng khác.

## Pipeline đã làm gì với nó (xem `../rca_observed.json`)

- **Phát hiện nhanh** (~9 giây trigger→alert, < mục tiêu 30 giây) và **khoanh đúng
  thành phần bị nghẽn** `waf-engine` (confidence 0.515) bằng topology + severity.
- **Không gọi được tên nguyên nhân.** Trigger là một *sự kiện thay đổi cấu hình*
  (`ruleset_deployed`), không phải metric bị vượt, nên nó nằm trong `timeline.json`
  mà **không bao giờ** là input của pipeline. Pipeline không thể nói "rule v-vulnerable
  đẩy lúc 09:58:36 là nguyên nhân".
- **Khuyến nghị sai cách sửa trước tiên.** Action hàng đầu đến từ một sự cố quá khứ
  không liên quan cơ chế: *"Mở rộng bộ nhớ cdn-cache", "Làm nóng trước các object
  nóng"* — vô dụng với một regex nghẽn CPU.
- **Truy hồi khớp dịch vụ, không khớp cơ chế**: cả hai sự cố "tương tự" (0.7) đều là
  vấn đề cache/DNS, không phải backtracking.

Các quan sát này dẫn dắt `../postmortem.md` (Phát hiện) và `../ADR.md`.
