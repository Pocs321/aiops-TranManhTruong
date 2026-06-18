# Ronki Closed-Loop Orchestrator — Submission

Closed-loop auto-remediation orchestrator (Detect → Decide → Act → Verify → Rollback)
với đủ 5 sub-checkpoint + 3 stress scenario. Engine: **rule-based** (xem `DESIGN.md`).

## Nội dung folder

```
submission/
├── closed_loop.py          # orchestrator chính (poll Alertmanager, 5 checkpoint, mutex, transactional, validation)
├── config.yaml             # runbook map, blast-radius, circuit-breaker, multi-step & validation registry
├── engine/                 # logger / metrics (Prometheus :9100) / safety / verify
├── runbooks/               # 3 runbook cơ bản + multi_step_deploy + 5 script per-step cho S4
├── data/baseline.json      # ngưỡng verify + PromQL (bản tự chứa)
├── DESIGN.md               # bảo vệ thiết kế (4 câu hỏi + mutex/transactional/validation/metrics)
├── SUBMIT.md               # log thật của 6 kịch bản nghiệm thu
└── README.md               # file này
```

## Chạy

Cần stack lab đang chạy (`data-pack/scripts/start_stack.sh`) — Prometheus `:9090`, Alertmanager `:9093`.

```bash
cd submission
uv run --with requests --with pyyaml --with prometheus_client \
  python closed_loop.py --config config.yaml 2>&1 | tee audit_log.jsonl
# cờ --dry-run: chỉ detect + decide + dry-run, không thực thi action thật
```

Metrics Prometheus phơi ở `:9100` (Prometheus scrape qua `host.docker.internal:9100`).

## Lưu ý môi trường (đã kiểm chứng trên Windows + Docker Desktop)

Lab thiết kế cho Linux. Bản nộp này đã chỉnh để portable + chạy được trên Windows:

- **`run_runbook` dùng `bash`** (không hardcode `/bin/bash`) → chạy cả trên Linux lẫn Git Bash/Windows.
- **Cổng Prometheus**: `config.yaml` để mặc định `9090`. Nếu máy bị chiếm cổng 9090, remap container
  Prometheus `9190:9090` và đổi `prometheus_url` → `http://localhost:9190`. (Khuyến nghị chạy stack lab
  dưới project riêng: `docker compose -p ronki -f configs/docker-compose.yml up -d`.)
- **`verify_timeout_seconds: 120`** (không phải 60): container mock chạy lại `pip install` ~30s mỗi lần
  restart nên cần cửa sổ verify dài hơn — xem `DESIGN.md §3`.
- **Sinh tải**: lab không kèm bộ sinh tải; cần có request tới `/` của các service thì `verify` mới có
  dữ liệu latency. Có thể dùng vòng `curl` đơn giản tới `localhost:8080-8084`.
- **Fault latency**: cần `tc`/`nsenter` (Linux). Trên Docker Desktop dùng `kill`/`pause` (→ `InstanceDown`)
  hoặc bơm alert tổng hợp `POST /api/v2/alerts` để nghiệm thu.
- **Alert rule `InstanceDown`** nên loại trừ job `closed-loop` (`up{job!="closed-loop"} == 0`) để
  orchestrator không tự báo động chính scrape-target của nó.

## Kết quả nghiệm thu

Cả 6 kịch bản **PASS** — log đầy đủ trong `SUBMIT.md`:
S1 ACTION_SUCCESS · S2 ROLLBACK · S3 CIRCUIT_BREAKER_HALT · S4 transactional rollback (B→A) ·
S5 concurrent (chạy đúng nhưng tuần tự — xem ghi chú giới hạn thiết kế) · S6 DECISION_VALIDATION_FAILED.

`config.yaml` có sẵn mapping `TestHallucination` (S6) và `MultiStepDeploy` (S4) để tái hiện 2 stress test.
