# W2-D2 — EOD Checkpoint

Cluster lớn nhất tôi xử lý: **`c-001-000`** (root cause **payment-svc**,
`connection_pool_exhaustion`). `confidence` báo cáo 0.9; `graph_score` 1.0;
`graph_top3` = payment-svc 1.00, checkout-svc 0.54, cart-svc 0.51.

## 1. Confidence của top-1 trong cluster lớn nhất — và ngưỡng auto-rollback

Pipeline in ra `confidence: 0.9`, nhưng tín hiệu **trung thực** là *độ tập trung* PageRank,
`top/sum = 0.365` — payment-svc chỉ ~2.2× service kế tiếp, nên đây là "top-3 chứa đáp án",
không phải "chắc chắn là cái nào". 0.365 rơi vào dải *moderately-confident*.

Nếu phải đặt ngưỡng cho auto-rollback **không người giám sát** (không cần SRE confirm), tôi
chọn **concentration ≥ 0.85 VÀ có incident quá khứ truy hồi ở similarity ≥ 0.9 trên cùng
service VÀ action reversible, blast-radius thấp** — và kể cả vậy vẫn đặt sau guardrail
canary / single-AZ với auto-revert khi không cải thiện. Lý do: rollback hướng ra ngoài và
gây gián đoạn; kích hoạt nó trên tín hiệu dải-0.6 có rủi ro rollback nhầm *victim* (vd
checkout-svc — vốn alert **nhiều nhất** chỉ vì mọi traffic đi qua nó) và *kéo dài* outage.
Ở mức 0.365, nước đi đúng là đẩy top-3 đã xếp hạng + action đề xuất cho on-call dưới dạng
**one-click approval**: model làm triage, con người sở hữu hành động không thể đảo ngược.

## 2. Variant classifier đã chọn — và trade-off

**Variant A — rule-based / retrieval (kNN top-1).** `heuristic_rca` lấy candidate
graph+temporal top-1, truy hồi các incident quá khứ tương tự nhất bằng keyword similarity
(service overlap + trùng root-cause-service + severity), rồi copy **class + remediation từ
incident khớp nhất** — một bộ phân loại 1-NN trên lịch sử 30 incident. Nó chạy hoàn toàn
deterministic và offline: môi trường này không có `ANTHROPIC_API_KEY`, nên
`method = graph+retrieval-heuristic`, `llm_used: false`, và tái tạo đúng `rca_output.json`
đã lưu (cả 3 cluster phân loại với similarity 1.0).

**Vì sao A thay vì B/C:** lịch sử phủ dày các failure mode lặp lại của GeekShop, nên 1-NN
đã đạt similarity 1.0 trên mọi cluster — LLM không thêm gì cho *độ chính xác phân loại* ở
đây mà còn thêm độ trễ (~vài giây), cost mỗi call, và rủi ro hallucination. Variant A cũng
auditable và miễn phí.

**Trade-off tôi chấp nhận:** 1-NN chỉ phát ra được class đã tồn tại trong lịch sử; một
failure mode thực sự mới sẽ rơi về `class: "other"`, nơi LLM (variant C) sẽ tổng quát hóa
và đưa ra giải thích + action có thứ tự hữu ích. Vì vậy tôi **giữ variant C đã nối dây nhưng
inactive** — `call_llm_rca` thực sự gọi Claude (`claude-opus-4-8`, Messages API) với output
theo JSON-schema, được bảo vệ bởi `validate_llm_output` (loại root_cause ngoài cluster,
class ngoài enum, confidence ∉ [0,1], hoặc actions rỗng) và fallback về A khi có bất kỳ lỗi
nào. Nên đường live là A deterministic; C là bản nâng cấp drop-in ngay khi có key, với
hallucination đã được rào sẵn.

## 3. Industry landscape — pipeline gần product nào nhất, và có hợp với GeekShop?

Gần **Dynatrace Davis** nhất: cả hai coi **topology là source of truth** và chạy xếp hạng
nhân quả trên service map (Davis trên Smartscape, của tôi trên dependency graph
`services.json` qua PageRank + blend temporal theo alert sớm nhất). Tôi *không* gần
BigPanda/Moogsoft (cluster alert agnostic với topology) hay Causely (học causal graph từ
data, không giả định service-map).

Với **GeekShop** — alert volume cao, service map **tương đối ổn định** — lựa chọn kiểu Davis
là **hợp lý**: khi dependency graph đáng tin và đổi chậm, graph traversal là lối tắt rẻ,
deterministic vượt qua causal inference (RCA dưới giây, không cần time-series dài stationary).
Trade-off là failure mode đã biết: nếu graph drift hoặc thiếu, ranking lệch — đúng là lý do
tôi blend thêm điểm temporal (0.6/0.4), thêm `store_culprit_check` cho edge-case cache/DB,
và giữ causal inference (Granger / cross-correlation lag) làm fallback đã ghi tài liệu. Tôi
**sẽ không** đổi sang Causely ở đây: lợi thế của nó xuất hiện khi *không* có service map
đáng tin, không phải tình huống của GeekShop; áp dụng nó nghĩa là trả giá cho causal
learning lịch-sử-dài để giải một bài toán mà graph ổn định đã giải xong.

---

## Phụ lục — Q&A mở rộng (checkpoint trước, giữ lại để có chiều sâu)

### A1. Culprit vs victim trong cluster của tôi

Trong `c-001-000`:

- **Culprit — `payment-svc`.** Nó alert đầu tiên (`03:14:05`), là dependency sâu nhất (được
  checkout-svc, cart-svc, notification-svc gọi), và có PageRank cao nhất. Đây là lỗi độc
  lập: nó tự hỏng (connection-pool exhaustion sau một bad deploy), không phải vì thứ nó phụ
  thuộc bị hỏng.
- **Victim — `edge-lb`.** Nó alert **cuối cùng** (`03:16:30`), nằm ở **đỉnh** stack (gọi
  checkout, checkout gọi payment), và có PageRank thấp nhất (`0.094`). Spike 5xx của nó chỉ
  xuất hiện khi lỗi đã cascade lên tới đỉnh. `checkout-svc` cũng là victim dù có nhiều alert
  nhất (12) — alert *count* cao phản ánh lượng traffic, không phải nhân quả.

### A2. Output PageRank cho main cluster (top-3, raw scores)

| Hạng | Service | PageRank |
|------|---------|---------:|
| 1 | payment-svc | **0.365** |
| 2 | cart-svc | 0.162 |
| 3 | checkout-svc | 0.134 |

(rồi inventory-svc 0.123, notification-svc 0.123, edge-lb 0.094). Confidence
`top/sum = 0.365`. Sau khi blend với điểm temporal theo alert sớm nhất (0.6 PageRank /
0.4 time), `graph_top3` kết hợp là **payment-svc 1.00, checkout-svc 0.54, cart-svc 0.51**.

### A3. Tôi có dùng Granger causality không?

Không. (a) **Sample size** — cửa sổ incident ~5 phút và tôi chỉ có stream *alert* rời rạc,
không phải time-series metric dài, lấy mẫu đều; Granger cần ~50+ điểm stationary mỗi cặp,
tôi không có. (b) **Stationarity** — series latency/error trong incident đầy trend và level
shift, nên phải diff hoặc STL-decompose trước, thêm sự mong manh đổi lấy lợi ích nhỏ.
(c) **Nó pairwise** — Granger `A→B` không thấy được common cause `C` lái cả hai, nên trên
cluster 6 service nó sẽ tạo edge gây hiểu lầm; service graph đã encode hướng phụ thuộc thật
một cách deterministic. Tôi để Granger ngoài đường live và dùng graph + temporal; nó thuộc
về công việc causal-graph post-mortem offline nơi có lịch sử dài.

### A4. LLM có hallucinate không?

Môi trường này **không có `ANTHROPIC_API_KEY`**, nên pipeline chạy đường **fallback**
graph+retrieval (`method: "graph+retrieval-heuristic"`, `llm_used: false`) — không có lần
chạy LLM live nào để hallucinate. Code vẫn thực hiện call Claude thật (`rca.call_llm_rca`,
`claude-opus-4-8`, Messages API) và **chống hallucination theo hai cách**: (1) request dùng
structured output (`output_config.format` với JSON schema và enum `class`), nên reply luôn
là JSON hợp-schema; (2) `validate_llm_output` loại bất kỳ đáp án nào có `root_cause` không
thuộc service của cluster, `class` ngoài enum cho phép, `confidence` ngoài `[0,1]`, hoặc
`actions` rỗng — và khi bị loại, pipeline fallback về kết quả graph+retrieval deterministic.
Nên tên service hallucinate không bao giờ tới được output.

### A5. Pipeline confidence là 0.6 — auto-rollback ngay, hay đợi SRE?

**Đợi SRE confirm.** 0.6 là vùng "top-3 chứa đáp án", không phải vùng "chắc chắn là cái
nào" — mục tiêu thực tế của ta là *top-3 ≥ 80%*, không phải chắc chắn đáp án đơn. Rollback
hướng ra ngoài và gây gián đoạn; kích hoạt trên tín hiệu 0.6 có rủi ro rollback nhầm service
(vd một victim) và *kéo dài* outage. Nước đi đúng là **đẩy top-3 đã xếp hạng cùng action đề
xuất cho on-call dưới dạng one-click approval** — model làm triage (tiết kiệm phút), con
người sở hữu hành động không-thể-đảo-ngược. Tôi chỉ để auto-rollback chạy không giám sát khi
confidence cao (≈0.85+) **và** action blast-radius thấp và reversible **và** một incident
quá khứ gần-giống đã được truy hồi — và kể cả vậy, vẫn sau guardrail (canary / single AZ
trước, auto-revert khi không cải thiện).
