# SUBMIT.md — Kết quả chạy 6 chaos scenarios (log thực tế)

> File này thay cho bản ví dụ mẫu, chứa **log thật** capture trên máy test
> (Windows 11 + Docker Desktop + Git Bash). Cả 6 kịch bản đều **PASS**.
> Log được trích các sự kiện then chốt (bỏ bớt VERIFY_SAMPLE / RUNBOOK_EXEC /
> RUNBOOK_RESULT cho gọn). Mỗi dòng là 1 bản ghi JSON in ra stdout của orchestrator.

## Thông tin & môi trường

- Decision engine: **Rule-based** (`runbook_map` trong `config.yaml`) — không dùng Anthropic API.
- Python 3.13 (qua `uv run`), Docker Compose v5.1.3, Docker 29.4.1, Git Bash 5.2 trên Windows 11.
- Lệnh chạy orchestrator:
  ```bash
  cd data-pack/sample-solution
  uv run --with requests --with pyyaml --with prometheus_client \
    python closed_loop.py --config config.yaml 2>&1 | tee audit_log.jsonl
  ```

### Điều chỉnh để chạy được trên Windows (lab vốn thiết kế cho Linux)
- `closed_loop.py`: `cmd = ["/bin/bash", ...]` → `["bash", ...]` (Windows Python không resolve `/bin/bash`).
- Remap cổng do một stack `mlops/mlflow` đang chiếm 9090/3000: **Prometheus 9190**, Grafana 3001;
  `config.yaml prometheus_url → http://localhost:9190`.
- Lab không kèm bộ sinh tải → chạy 1 vòng `curl` nền vào `/` của 5 service để `verify` có dữ liệu latency.
- `tc`/`nsenter` không có trên Docker Desktop → thay fault `latency` bằng `kill`/`pause` (→ `InstanceDown`)
  và **bơm alert tổng hợp** qua Alertmanager `POST /api/v2/alerts`.
- Sửa alert `InstanceDown` → `up{job!="closed-loop"} == 0` (tránh orchestrator tự báo động chính nó).
- `baseline.json`: `verify_timeout_seconds` 60→120 cho đường success (container mock chạy lại
  `pip install` ~30s mỗi lần restart nên cần cửa sổ verify dài hơn).

---

## Scenario 1 — Action thành công

**Inject:** `bash data-pack/scripts/inject_fault.sh kill ronki-inventory-svc`
(thay cho `latency` Linux-only; kill → `up==0` → alert `InstanceDown`).

```json
{"event_type":"ORCHESTRATOR_START","config":"config.yaml","dry_run":false,"poll_interval_s":15}
{"event_type":"ALERT_DETECTED","alertname":"InstanceDown","service":"inventory-svc","severity":"critical"}
{"event_type":"DECIDE_RUNBOOK","alertname":"InstanceDown","service":"inventory-svc","runbook":"runbooks/restart_service.sh"}
{"event_type":"BLAST_RADIUS_OK","service":"inventory-svc"}
{"event_type":"DRY_RUN_PASS","runbook":"runbooks/restart_service.sh","service":"inventory-svc"}
{"event_type":"ACTION_EXECUTED","runbook":"runbooks/restart_service.sh","service":"inventory-svc"}
{"event_type":"VERIFY_START","service":"inventory-svc","timeout_s":120}
{"event_type":"VERIFY_PASS","service":"inventory-svc","samples":4}
{"event_type":"ACTION_SUCCESS","alertname":"InstanceDown","service":"inventory-svc","runbook":"runbooks/restart_service.sh"}
```
**Kết quả: PASS.** Detect → Decide → dry-run → restart → verify (sample đầu chưa có latency do container
đang `pip install`, sau đó 3 sample liên tiếp khỏe) → `ACTION_SUCCESS`. (04:38:16 → 04:39:33 UTC)

---

## Scenario 2 — Action fail → rollback

**Thiết lập:** đặt `verify_thresholds.latency_p99_max_ms: 1` trong `baseline.json` (verify luôn fail),
`verify_timeout_seconds: 30`. **Lưu ý:** ngưỡng verify nằm ở `baseline.json`, không phải `config.yaml`
(README/expected.json ghi nhầm).

**Inject:** `bash data-pack/scripts/inject_fault.sh kill ronki-checkout-svc`

```json
{"event_type":"ALERT_DETECTED","alertname":"InstanceDown","service":"checkout-svc","severity":"critical"}
{"event_type":"DECIDE_RUNBOOK","alertname":"InstanceDown","service":"checkout-svc","runbook":"runbooks/restart_service.sh"}
{"event_type":"BLAST_RADIUS_OK","service":"checkout-svc"}
{"event_type":"DRY_RUN_PASS","runbook":"runbooks/restart_service.sh","service":"checkout-svc"}
{"event_type":"ACTION_EXECUTED","runbook":"runbooks/restart_service.sh","service":"checkout-svc"}
{"event_type":"VERIFY_START","service":"checkout-svc","timeout_s":30}
{"event_type":"VERIFY_FAIL","service":"checkout-svc","samples":2}
{"event_type":"ROLLBACK_TRIGGERED","service":"checkout-svc","rollback_runbook":"runbooks/restart_service.sh"}
{"event_type":"ROLLBACK_EXECUTED","service":"checkout-svc","rollback_runbook":"runbooks/restart_service.sh"}
```
**Kết quả: PASS.** Verify fail (latency thực ~248ms > ngưỡng ép 1ms) → tự động `ROLLBACK_TRIGGERED` →
`ROLLBACK_EXECUTED`, `failure_count = 1`. Không cần can thiệp tay.

---

## Scenario 3 — Circuit breaker (3 lần fail liên tiếp)

**Thiết lập:** giữ ngưỡng ép fail. Kill **3 service khác nhau** (checkout/payment/inventory) — vì
orchestrator dedup theo `fingerprint`, kill lại cùng 1 service sẽ bị bỏ qua nên không tạo đủ 3 fail.

**Inject:**
```bash
bash data-pack/scripts/inject_fault.sh kill ronki-checkout-svc
bash data-pack/scripts/inject_fault.sh kill ronki-payment-svc
bash data-pack/scripts/inject_fault.sh kill ronki-inventory-svc
```

```json
{"event_type":"ALERT_DETECTED","alertname":"InstanceDown","service":"inventory-svc"}
{"event_type":"ACTION_EXECUTED","runbook":"runbooks/restart_service.sh","service":"inventory-svc"}
{"event_type":"VERIFY_FAIL","service":"inventory-svc","samples":2}
{"event_type":"ROLLBACK_TRIGGERED","service":"inventory-svc"}
{"event_type":"ROLLBACK_EXECUTED","service":"inventory-svc"}
{"event_type":"ALERT_DETECTED","alertname":"InstanceDown","service":"checkout-svc"}
{"event_type":"VERIFY_FAIL","service":"checkout-svc","samples":2}
{"event_type":"ROLLBACK_TRIGGERED","service":"checkout-svc"}
{"event_type":"ROLLBACK_EXECUTED","service":"checkout-svc"}
{"event_type":"ALERT_DETECTED","alertname":"InstanceDown","service":"payment-svc"}
{"event_type":"VERIFY_FAIL","service":"payment-svc","samples":2}
{"event_type":"ROLLBACK_TRIGGERED","service":"payment-svc"}
{"event_type":"ROLLBACK_EXECUTED","service":"payment-svc"}
{"event_type":"CIRCUIT_BREAKER_HALT","consecutive_failures":3,"threshold":3,"message":"Automation halted. Manual intervention required."}
```
**Kết quả: PASS.** Sau failure thứ 3 → `CIRCUIT_BREAKER_HALT` (consec=3), không thực thi thêm action nào;
vòng poll sau đó chỉ log HALT. (Lưu ý: đồng hồ máy nhảy ~1h38m giữa lúc verify checkout-svc do máy
sleep/NTP — chỉ ảnh hưởng timestamp, không ảnh hưởng logic.)

---

## Scenario 4 — Multi-step transactional rollback

**Thiết lập:** bản mẫu **không nối** multi-step (xem Khiếm khuyết #1) → tạo 5 script per-step
(`step_a/b/c.sh`, `rollback_a/b.sh`, step-C cố ý `exit 1`) + thêm `multi_step_map` /
`multi_step_rollback_map` vào `config.yaml`.

**Inject:** bơm alert tổng hợp `alertname=MultiStepDeploy` qua Alertmanager API.

```json
{"event_type":"ALERT_DETECTED","alertname":"MultiStepDeploy","service":"checkout-svc","severity":"warning"}
{"event_type":"DECIDE_RUNBOOK","alertname":"MultiStepDeploy","service":"checkout-svc","runbook":"runbooks/step_a.sh"}
{"event_type":"BLAST_RADIUS_OK","service":"checkout-svc"}
{"event_type":"DRY_RUN_PASS","runbook":"runbooks/step_a.sh","service":"checkout-svc"}
{"event_type":"TRANSACTIONAL_STEP_FAIL","step":"runbooks/step_c.sh","service":"checkout-svc","completed_before_failure":["runbooks/step_a.sh","runbooks/step_b.sh"]}
{"event_type":"TRANSACTIONAL_ROLLBACK_STEP","step":"runbooks/rollback_b.sh","service":"checkout-svc"}
{"event_type":"TRANSACTIONAL_ROLLBACK_STEP","step":"runbooks/rollback_a.sh","service":"checkout-svc"}
{"event_type":"TRANSACTIONAL_ROLLBACK_COMPLETE","service":"checkout-svc","rolled_back":["runbooks/rollback_b.sh","runbooks/rollback_a.sh"]}
```
**Kết quả: PASS.** Step A,B chạy ok; step C fail → rollback đúng **thứ tự ngược** (B trước, A sau);
`TRANSACTIONAL_ROLLBACK_COMPLETE` liệt kê `[rollback_b, rollback_a]`; **không** có `ACTION_SUCCESS`.

---

## Scenario 5 — Concurrent alert race

**Inject:** bơm 2 alert `HighLatency` cùng lúc cho `payment-svc` và `inventory-svc` (1 request API).

```json
{"event_type":"ALERT_DETECTED","alertname":"HighLatency","service":"payment-svc"}        // ts 04:41:06.111
{"event_type":"DRY_RUN_PASS","runbook":"runbooks/restart_service.sh","service":"payment-svc"}   // ts 04:41:06.556
{"event_type":"ACTION_EXECUTED","service":"payment-svc"}
{"event_type":"VERIFY_START","service":"payment-svc","timeout_s":120}
{"event_type":"VERIFY_PASS","service":"payment-svc","samples":3}
{"event_type":"ACTION_SUCCESS","service":"payment-svc"}                                   // ts 04:42:05.769
{"event_type":"ALERT_DETECTED","alertname":"HighLatency","service":"inventory-svc"}       // ts 04:42:05.769
{"event_type":"DRY_RUN_PASS","runbook":"runbooks/restart_service.sh","service":"inventory-svc"} // ts 04:42:06.028
{"event_type":"ACTION_EXECUTED","service":"inventory-svc"}
{"event_type":"VERIFY_PASS","service":"inventory-svc","samples":3}
{"event_type":"ACTION_SUCCESS","service":"inventory-svc"}                                 // ts 04:43:04.861
```
**Kết quả: PASS một phần (lộ ra giới hạn thiết kế).** Cả 2 service đều xử lý độc lập tới `ACTION_SUCCESS`,
**nhưng tuần tự**: `DRY_RUN_PASS` của payment (04:41:06) và inventory (04:42:06) **cách nhau 60s** — đúng
bằng thời gian xử lý đầy đủ (restart + verify) của payment. Yêu cầu của đề là <1s (song song).
`SERVICE_LOCK_BUSY` = 0 (không bao giờ kích hoạt trong vòng lặp đơn luồng). Xem Khiếm khuyết #2.

---

## Scenario 6 — LLM hallucination defense

**Thiết lập:** thêm `runbook_registry` **tường minh** (chỉ chứa runbook thật) + map
`TestHallucination → runbooks/nonexistent_runbook.sh` vào `config.yaml`.

**Inject:** bơm alert tổng hợp `alertname=TestHallucination` qua Alertmanager API.

```json
{"event_type":"ALERT_DETECTED","alertname":"TestHallucination","service":"payment-svc","severity":"warning"}
{"event_type":"DECISION_VALIDATION_FAILED","bad_runbook":"runbooks/nonexistent_runbook.sh","alertname":"TestHallucination","raw_decision":"runbooks/nonexistent_runbook.sh","action":"escalate_no_auto_action"}
```
**Kết quả: PASS.** `DECISION_VALIDATION_FAILED` đủ 4 trường (`bad_runbook`, `alertname`, `raw_decision`,
`action=escalate_no_auto_action`). **Không** có `DRY_RUN_PASS`/`ACTION_EXECUTED`/`RUNBOOK_EXEC` (0 subprocess);
circuit breaker không đổi (validation fail ≠ action fail).

---

## Tổng kết

| # | Kịch bản | Kết quả |
|---|---|---|
| 1 | Action success | ✅ PASS — `ACTION_SUCCESS` |
| 2 | Action fail → rollback | ✅ PASS — `ROLLBACK_EXECUTED` |
| 3 | Circuit breaker | ✅ PASS — `CIRCUIT_BREAKER_HALT` (consec=3) |
| 4 | Multi-step transactional rollback | ✅ PASS — rollback B→A, không `ACTION_SUCCESS` |
| 5 | Concurrent race | ⚠️ PASS một phần — chạy đúng nhưng **tuần tự**, không song song |
| 6 | Hallucination defense | ✅ PASS — `DECISION_VALIDATION_FAILED`, 0 subprocess |

## Khiếm khuyết của bản mẫu (đáng ghi vào DESIGN.md)
1. **Multi-step chưa nối**: `run_runbook` chỉ truyền `--service`, không truyền cờ `--step-*`, nên
   `multi_step_deploy.sh` (1-file theo cờ) luôn rơi vào nhánh lỗi. Phải tách script per-step (đã làm cho S4).
2. **Không xử lý song song**: main loop đơn luồng và block ở `verify_service` (tới timeout) → 2 service
   khác nhau xử lý cách nhau ~thời gian verify; `mutex`/`SERVICE_LOCK_BUSY` về mặt thực tế không bao giờ
   được kích hoạt. Để đạt S5 đúng nghĩa cần spawn 1 thread/process mỗi alert.
3. **Thiếu `runbook_registry` mặc định** → registry mặc định lấy `runbook_map.values()`, khiến runbook
   "ảo" lọt qua validation. Phải khai báo allowlist tường minh.
4. **Ngưỡng verify ở `baseline.json`**, không phải `config.yaml` như tài liệu ghi.
5. **Self-remediation**: job `closed-loop` của chính orchestrator nằm trong alert `up==0` → orchestrator
   tự báo động/tự "restart" chính nó (đã sửa rule loại trừ `job="closed-loop"`).
6. Alert `HighErrorRate` tham chiếu `http_errors_total` — service mock không phát metric này (alert chết).

## Điều học được
Checkpoint khó nhất là **Verify + Rollback** kết hợp với đặc thù hạ tầng: vì mỗi lần `docker restart`
container mock chạy lại `pip install` (~30s), cửa sổ verify 60s mặc định không đủ để gom đủ 3 sample khỏe
liên tiếp → action đúng vẫn bị coi là fail và rollback. Phải nâng `verify_timeout` lên 120s. Bài học:
ngưỡng verify (timeout/poll/min_samples) phải phản ánh **thời gian phục hồi thực tế** của service, nếu không
sẽ tạo rollback giả và có thể kéo circuit breaker mở oan. Ngoài ra `seen`-set dedup theo fingerprint khiến
cùng một alert chỉ xử lý 1 lần trong vòng đời tiến trình — hữu ích để chống xử lý lặp, nhưng cần lưu ý khi
test lặp lại trên cùng một service.
