# W2-D1 — Alert Correlation: Từ Noise sang Signal — Bài nộp

**Mục tiêu:** gộp một flood alert thành ít cluster có nghĩa, để D2 (RCA) chỉ phải làm việc
trên vài cluster thay vì hàng trăm noise. Correlation **không** tìm root cause — nó chỉ
**rút gọn số việc** RCA phải làm.

- Notebook: [`assignment.ipynb`](assignment.ipynb) (mirror nguồn: [`assignment.py`](assignment.py))
- Engine: [`correlate.py`](correlate.py) — 4 layer: dedup → time-window → topology → semantic
- Kết quả: [`results/cluster_summary.json`](results/cluster_summary.json)

> **Lưu ý dữ liệu:** dataset chính thức (`alerts.jsonl`, 200 alert) phát hành Thursday. Bài
> này chạy trên `lab/dataset/alerts_sample.jsonl` (20 alert) — bản **dựng lại trung thực
> theo kịch bản trong bài học**: sự cố `payment-svc` pool-exhaustion lúc 09:42 lan lên
> `checkout-svc → edge-lb` (+ `payments-db`, `cart-svc`, `cart-redis`); cùng lúc
> `recommender-svc` OOM do batch retrain (nhiễu trùng giờ); `search-svc` degrade; và một
> alert `notification-svc` đến muộn 18 phút. `services.json` dựng theo graph ở §4.1.
> Khi có dataset thật chỉ cần thay file `alerts_sample.jsonl` → chạy lại, code không đổi.

---

## Kết quả

| metric | giá trị |
|---|---|
| input_alerts | **20** |
| output_clusters | **4** |
| reduction_ratio | **0.80** (≥ 0.5 ✓) |

| cluster_id | n | unique fp | max_sev | services |
|---|---|---|---|---|
| `c-000-000` | 14 | 11 | crit | payment-svc, payments-db, checkout-svc, edge-lb, cart-svc, cart-redis |
| `c-000-001` | 3 | 2 | crit | recommender-svc |
| `c-000-002` | 2 | 2 | warn | search-svc, search-es |
| `c-001-000` | 1 | 1 | warn | notification-svc |

`c-000-000` gộp đúng chuỗi cascade payment pool-exhaustion: 14 alert, nhưng chỉ **11 loại
fingerprint** → 3 duplicate đã bị collapse (a002/a003 trùng a001 `latency_p99_ms`; a010
trùng a009 `http_5xx_ratio`). 20 → 4 nghĩa là RCA ngày mai chỉ xét 4 thứ thay vì 20.

---

## Lựa chọn tham số (design choices)

### `gap_sec = 120` (2 phút)
Sweet spot production điển hình cho session window. Trong sample, toàn bộ sự cố 09:42–09:47
có gap liên tiếp lớn nhất là 60s (a017 09:46:00 → a018 09:47:00) nên gom trọn vào **một**
session; alert `notification-svc` lúc 10:05 cách 18 phút → tự rơi sang session riêng (đúng
ý đồ: nó là sự kiện khác). Cách đo đúng ở production: vẽ histogram `time_since_last_alert`
trong 30 ngày, chọn `gap_sec` ở ~p95 của *intra-incident gap*.

### `max_hop = 2`
1 hop chỉ gom service kề trực tiếp (caller↔callee), sẽ **cắt rời** chuỗi `payment-svc →
checkout-svc → edge-lb` vì `payment-svc`↔`edge-lb` cách nhau 2 hop. `max_hop = 2` (kết hợp
Union-Find bắc cầu/transitive) gom đúng cả chuỗi cascade + store kề (`payments-db`, `cart-redis`)
mà chưa "với quá xa" sang hệ thống không liên quan. `max_hop ≥ 3` bắt đầu nuốt nhầm — xem
phần Limitation.

### Trade-off đã cân nhắc — `max_severity` không dùng `max()` trên string
Code mẫu ở bài học dùng `max(a['severity'] for a in group)`. Đây là **bug tinh vi**:
`max("crit", "warn") == "warn"` theo alphabet → một cluster có cả `crit` lẫn `warn` sẽ báo
`warn` (nhẹ hơn thực tế) → on-call có thể deprioritize sai một sự cố crit. Tôi thay bằng
`SEVERITY_RANK` (`crit > warn > info`) và `max(..., key=rank)`. Trade-off: thêm một bảng
ánh xạ phải bảo trì khi có severity mới, đổi lấy độ đúng ngữ nghĩa — đáng.

---

## EOD Checkpoint

**1. Vì sao fingerprint không include `timestamp`/`value`?**
Vì cả hai **đổi mỗi lần fire**. `timestamp` luôn khác → mọi alert thành unique → không gì
duplicate gì → dedup vô dụng (store phình bằng đúng số alert thô). `value` cũng dao động
(`latency 3200 → 3450 → 3680`). Trong sample, a001/a002/a003 cùng
`payment-svc|latency_p99_ms|crit`: bỏ ts/value đi thì 3 cái collapse thành 1 + count=3;
nếu include value thì thành 3 cluster rác cho cùng một triệu chứng.

**2. "Duplicate" vs "correlated" khác gì? Ví dụ từ dataset.**
*Duplicate* = **giống hệt fingerprint**, cùng service+metric+severity fire lại (a009 & a010
đều `checkout-svc|http_5xx_ratio|crit`) → Layer 1 gộp. *Correlated* = **khác fingerprint
nhưng cùng nguyên nhân**, gom nhờ thời gian + topology: `payment-svc latency` (a001) và
`edge-lb http_5xx` (a012) khác hẳn metric/service nhưng cùng một sự cố pool-exhaustion →
Layer 2+3 gom. Dedup giảm *trùng lặp*; correlation gom *triệu chứng liên quan*.

**3. `gap_sec = 30` vs `gap_sec = 600`.**
- `30s`: session vỡ vụn — a017→a018 (gap 60s) đã đứt, sự cố bị tách thành nhiều cluster nhỏ → RCA phải ghép tay lại.
- `600s`: session quá rộng — alert `notification-svc` lúc 10:05 (lẽ ra riêng) bị kéo chung session với sự cố payment → false correlation.

**4. [Câu "soul"] Correlator có gom `recommender-svc` (batch retrain) vào cluster chính không?**
**Không** — và đó là điều đúng. `recommender-svc` alert (a015–a017) **cùng session thời
gian** với sự cố payment (09:45, trong cửa sổ 120s), nên *time-window đơn thuần sẽ gom nhầm*.
Nhưng Layer 3 topology chặn lại: `recommender-svc` chỉ phụ thuộc `model-store`, **không có
path** nào nối tới chuỗi `payment/checkout/edge` trên service graph (`nx.NetworkXNoPath`) ⇒
Union-Find để nó thành component riêng `c-000-001`. Đây chính là giá trị của việc **kết hợp**
time-window VÀ topology: trùng giờ nhưng không trùng đường đi nhân quả ⇒ không phải cùng
incident. OOM do retrain là sự cố độc lập, gom chung sẽ làm RCA đi sai hướng.

**5. Limitation lớn nhất của topology grouping + cách khắc phục.**
Bài toán **hub (node fan-out cao)**. Topology dùng *undirected distance + Union-Find* nên
một gateway/DB dùng chung sẽ nối mọi nhánh con lại với nhau dù chúng không liên quan nhân
quả. Demo trong notebook: chỉ cần thêm cạnh `edge-lb → search-svc`, lập tức `search-svc` +
`search-es` (sự cố riêng) bị **nuốt nhầm** vào cluster payment → cluster lớn nhất phình từ
14 lên 16 alert (false correlation). **Khắc phục:** (a) traversal **có hướng/causal** thay vì
undirected — chỉ gom theo đường phụ thuộc thật, không gom hai nhánh chỉ vì chung tổ tiên;
(b) **down-weight** node fan-out cao bằng centrality (PageRank-style) để khoảng cách qua hub
"đắt" hơn. Đây đúng là cầu nối sang RCA của D2.

---

## Scale — 10.000 alert thay vì 200, nghẽn ở đâu?

- **Layer 1 dedup:** `O(N)` hash lookup — rẻ. Rủi ro thật là **memory**: `store` grow vô hạn → bắt buộc `evict_stale` (TTL theo `last_seen`) chạy định kỳ.
- **Layer 2 session:** sort `O(N log N)` + một lượt quét — ổn ở 10k.
- **Layer 3 topology = chỗ nghẽn chính.** `topology_group` lặp **mọi cặp service** `O(S²)`
  và gọi `nx.shortest_path_length` mỗi cặp. `S` là số *service* (không phải alert) nên thường
  nhỏ; nhưng một session burst lớn có thể chứa hàng trăm service → `O(S² · path_cost)` đắt.
  **Cách sửa:** precompute all-pairs distance ≤ `max_hop` một lần bằng BFS giới hạn độ sâu,
  hoặc lấy `connected_components` trên subgraph các service-có-alert thay vì gọi shortest_path
  mỗi cặp.

---

## Cách reproduce
```powershell
python -m pip install networkx jupytext nbconvert jupyter
# Chạy nhanh engine:
python correlate.py lab/dataset
# Dựng lại notebook end-to-end:
python -m jupytext --to notebook assignment.py -o assignment.ipynb
python -m jupyter nbconvert --to notebook --execute --inplace assignment.ipynb
```
