# W2-D2 — RCA Findings

## Main cluster `c-001-000` — root cause: **payment-svc** (`connection_pool_exhaustion`)

Sáu service cùng alert (`edge-lb, checkout-svc, payment-svc, cart-svc, inventory-svc,
notification-svc`), nhưng **payment-svc là thủ phạm** còn lại là victim của cascade. Hai
tín hiệu độc lập đồng thuận:

- **Topology (PageRank trên dependency subgraph).** payment-svc là node được-phụ-thuộc
  nhiều nhất — `checkout-svc`, `cart-svc`, và `notification-svc` đều gọi nó. Rank tích lũy ở
  sink đó, cho nó `0.365`, gấp khoảng **2.2× service kế tiếp** (`cart-svc 0.162`,
  `checkout-svc 0.134`, `inventory/notification 0.123`, `edge-lb 0.094`).
- **Thời gian.** payment-svc bắn alert đầu tiên của incident (`03:14:05`); `edge-lb`, load
  balancer ở đỉnh stack, alert cuối cùng (`03:16:30`) — chữ ký victim kinh điển. Blend
  PageRank (0.6) với điểm alert-sớm-nhất (0.4) đặt payment-svc ở `1.00`, vượt xa
  `checkout-svc 0.54` và `cart-svc 0.51`.

`checkout-svc` tạo **nhiều** alert nhất (12) vì mọi order đều đi qua nó, đúng là lý do
heuristic ngây thơ "nhiều-alert-nhất = root-cause" chọn nhầm service. Retrieval kéo về
`INC-2026-05-30` (payment-svc pool exhaustion, similarity 1.0), nên action đề xuất là biện
pháp khắc phục đã được chứng minh: rollback payment-svc và nâng connection pool.

## Confidence — tôi có auto-remediate không?

Pipeline báo cáo `0.9`, nhưng confidence trung thực hơn là **độ tập trung PageRank**,
`top/sum = 0.365` — chỉ "moderately confident". Với cluster này tôi *sẽ* gate auto rollback
sau một one-click SRE confirm thay vì chạy không giám sát: các tín hiệu nhất quán và biện
pháp (rollback + nâng pool) blast-radius thấp và reversible, nhưng concentration 0.365 chưa
đủ mạnh để bỏ qua con người. Tôi chỉ auto-remediate không giám sát khi trên ~0.8
concentration **và** có incident quá khứ similarity cao khớp.

## Một case tôi không chắc — `c-002-000` (cart-redis)

Ở đây candidate top là **cart-redis**, một cache (node store terminal). Graph luôn xếp store
cao nhất vì mọi thứ phụ thuộc nó, nên graph score đơn thuần không phân biệt được *culprit*
với *victim*. `store_culprit_check` của tôi giải quyết bằng timing: cart-redis alert
**trước** các app phụ thuộc nó (`store_is_culprit`), nên tôi gọi nó là
`connection_pool_exhaustion` thật (maxclients). **Nhưng tôi không hoàn toàn chắc** — failure
mode đối xứng là một connection *leak trong cart-svc* làm cạn redis, trong trường hợp đó
redis là victim còn cart-svc là culprit. Khoảng cách timing 30 giây là bằng chứng mỏng, nên
đây là cluster tôi sẽ giao cho SRE trước khi đụng vào bất cứ thứ gì.

## Bonus path — vì sao retrieval-only đã đủ ở đây

Tôi **không** chọn bonus path nào (không decision tree, không TF-IDF, không LLM).
Retrieval-only (1-NN top-1 trên lịch sử 30 incident, chấm theo service overlap + trùng
root-cause-service + severity) là đủ vì lịch sử **phủ dày các failure mode lặp lại của
GeekShop**: payment-svc pool exhaustion lặp lại (`INC-2026-05-30`, `INC-2026-05-24`),
cart-redis exhaustion lặp lại (`INC-2026-05-12`, `INC-2026-05-06`), kafka rebalance lặp lại
(`INC-2026-04-30`, `INC-2026-04-24`). Nhờ đó nearest-neighbour trả về đúng `class` +
`remediation` ở **similarity 1.0 cho cả ba cluster** — không còn lỗi phân loại nào để một
model nặng hơn sửa.

- **Bonus 1 (decision tree)** và **Bonus 2 (TF-IDF cosine)** sẽ thêm độ phức tạp model và
  một bước train/fit để đổi lấy **zero accuracy gain** khi 1-NN đã đạt similarity 1.0.
  TF-IDF chỉ bắt đầu có ý nghĩa nếu phải match incident trên *summary* free-text thay vì
  `services_involved` + severity có cấu trúc mà tôi key vào — không phải trường hợp này.
- **Bonus 3 (LLM)** chỉ đáng giá cho một failure mode **mới** vắng mặt trong lịch sử (nơi
  1-NN sẽ rơi về `class: "other"`). Không cluster nào trong ba cái này là mới, nên LLM sẽ
  thêm độ trễ, cost, và rủi ro hallucination mà không đổi đáp án. Đó đúng là lý do đường LLM
  được nối dây nhưng giữ inactive (xem SUBMIT.md Q2): nó là bản nâng cấp drop-in cho long
  tail, không phải nhu cầu cho dataset này.
