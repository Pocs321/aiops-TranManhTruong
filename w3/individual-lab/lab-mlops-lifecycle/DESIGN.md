# DESIGN.md — MLOps Lifecycle: Anomaly Detection Pipeline

## Tổng quan

Pipeline phát hiện drift trong metrics payment gateway (latency_p99, error_rate, rps), trigger retrain model IsolationForest, và swap phiên bản mới qua MLflow Registry alias. Model được đóng gói thành `sklearn.Pipeline(StandardScaler → IsolationForest)` và log nguyên khối vào registry, nên mọi consumer (serve, drift_detector, post-deploy monitor) load `models:/anomaly-detector@<alias>` đều được scale features nhất quán, không cần quản lý scaler rời.

---

## Sub-checkpoint 1: Drift Threshold

**Giá trị đã chọn: 0.15** (15% features bị drift theo Evidently DataDriftPreset).

**Cách chọn:** Trước tiên chạy drift_detector trên chính baseline.csv, chia 70/30 (reference = 70% đầu, current = 30% cuối). Kết quả đo thực: `drift_score = 0.0000` (is_drift=False) — Evidently không thấy drift nội bộ baseline, đây là "noise floor". Chọn threshold = **0.15** nằm an toàn trên sàn nhiễu 0.0 này, đủ thấp để bắt drift thực. Với `drifted.csv`, score thực đo được là **1.0000** — cả **3/3 features** (`error_rate`, `latency_p99`, `rps`) đều drift — vượt threshold rất rõ.

**Rủi ro nếu threshold quá thấp (ví dụ 0.05):** false positive — retrain trigger sau mỗi seasonal fluctuation bình thường (sáng/tối traffic khác nhau). Tốn compute và gây alert fatigue.

**Rủi ro nếu threshold quá cao (ví dụ 0.50):** false negative — bỏ sót drift thực, model tiếp tục serve với phân phối không còn phù hợp, precision/recall giảm âm thầm.

---

## Sub-checkpoint 2: Loại Drift

**Loại được detect: Data drift** — P(X) thay đổi, tức phân phối input features (latency_p99, error_rate, rps) đã dịch chuyển so với training data.

**Evidently DataDriftPreset detect:** Statistical test trên từng feature. Mặc định dùng Wasserstein distance cho numerical features. Khi share_of_drifted_columns > threshold → flag.

**Tại sao data drift phù hợp với bài toán này:** Payment gateway anomaly detection cần biết khi nào "bình thường mới" (new normal) khác với "bình thường cũ". Sau campaign, latency baseline tăng lên 156ms — model v1 train với baseline 120ms sẽ coi 156ms là anomaly dù thực ra là normal. Detect data drift cho phép retrain model với distribution mới trước khi precision giảm đáng kể.

**Concept drift (P(Y|X) thay đổi)** không bị `DataDriftPreset` bắt (feature distribution có thể không đổi). Pipeline bổ sung `--check-mode combined`: ngoài data drift, nó đánh giá precision/recall của model trên tập có nhãn để phát hiện concept/performance drift (chi tiết ở Sub-checkpoint 5). Performance cũng được log vào MLflow để visualize trend.

---

## Sub-checkpoint 3: Retrain Trigger Configuration

**Trigger type: Manual approval gate** — semi-automatic.

**Cadence:** Không có schedule cố định. Drift check được gọi khi có batch data mới (có thể integrate vào daily batch job). Nhưng promotion từ staging → production luôn yêu cầu human approval.

**Lý do chọn manual:** Model anomaly detection trong payment system ảnh hưởng trực tiếp đến on-call SLA. Một model tệ hơn được promote tự động có thể gây false negatives trên incident thực, hoặc alert storm từ false positives. Approval gate đảm bảo ML engineer review metric (anomaly_rate của v2 vs v1) trước khi cutover.

**Approval timeout:** Không implement timeout trong lab. Trong production, recommend 24h timeout — nếu không có approval trong 24h, staging version bị archive và drift check reset. Tránh trạng thái "staging model treo mãi không ai review".

**Nếu tự động hoàn toàn:** Có thể dùng A/B shadow mode (optional D trong HANDOUT) — serve.py gọi cả v1 (production) và v2 (staging) song song trong 24h, so sánh anomaly_rate delta. Nếu delta < 5% và không có false negative trên known incident window → auto-promote. Ngưỡng 5% là conservative cho payment domain.

---

## Sub-checkpoint 4: Versioning và Rollback

**Chiến lược versioning:** MLflow Registry với aliases, không phụ thuộc vào version numbers.

- `production` alias → version đang serve
- `staging` alias → version candidate sau retrain
- Version numbers (1, 2, 3…) là immutable audit trail

**Tại sao alias tốt hơn version number trong code serve.py:** `mlflow.pyfunc.load_model("models:/anomaly-detector@production")` không thay đổi khi swap. Nếu hardcode version number, phải redeploy serve.py mỗi lần retrain.

**Rollback path:**
1. Phát hiện v2 underperform (precision giảm, alert storm): `MlflowClient.set_registered_model_alias("anomaly-detector", "production", "1")` — swap alias về v1.
2. Gọi `POST /reload` trên serve.py — load lại v1 từ registry.
3. Toàn bộ quá trình < 30 giây, không cần redeploy container.

**Ai có quyền rollback:** ML engineer on-call (có MLflow admin access). Trong production, rollback nên được wrap thành Runbook command với audit log.

**Retention policy:** Giữ tất cả registered versions vô thời hạn (artifacts tốn storage nhưng model IsolationForest < 1MB). Không xóa version cũ vì cần cho audit và rollback bất kỳ lúc nào.

---

## Kiến trúc component

```
baseline.csv (reference)
     │
     ├──► pipeline.py ──► MLflow Run ──► Registry v1 @production
     │
drifted.csv (current window)
     │
     ├──► drift_detector.py
     │         │ score=1.00 > threshold=0.15
     │         ▼
     └──► retrain.py
               │
               ├── train Pipeline(scaler+IsoForest) trên sliding window (baseline+drift)
               ├── MLflow Run → Registry v2 @staging
               ├── [HUMAN APPROVAL]
               ├── set alias production → v2
               └── POST /reload → serve.py
```

---

---

## Sub-checkpoint 5: Cơ chế phát hiện drift — tại sao cần combined mode

Chỉ dùng `DataDriftPreset` (data drift) là chưa đủ. Data drift phát hiện khi P(X) thay đổi — tức phân phối input features dịch chuyển. Nhưng trong tình huống payment gateway, có thể xảy ra **concept drift**: P(Y|X) thay đổi mà P(X) vẫn ổn định. Ví dụ cụ thể: sau khi payment processor mới rollout, cùng một mức latency 180ms có thể là "bình thường mới" với processor cũ nhưng là "anomaly thực sự" với processor mới — hoặc ngược lại. Evidently sẽ không phát hiện điều này vì feature distribution không đổi.

`--check-mode combined` chạy song song 2 cơ chế: (1) Evidently `DataDriftPreset` trên feature distribution, và (2) đánh giá precision/recall của model trên tập production có nhãn (`--labeled-current`, ở đây là `drifted.csv` — chứa 25% nhãn bị flip mô phỏng concept drift). Nếu một trong hai flag — `is_drift = True` hoặc `perf_is_degraded = True` — retrain được trigger. Ngưỡng performance mặc định precision ≥ 0.70. **Số đo thực:** trên `drifted.csv`, data drift score = 1.0, đồng thời precision của model tụt còn **0.3164** (recall 0.785) < 0.70 → `perf_is_degraded = True`. Đây là bằng chứng cụ thể vì sao cần combined: chạy `--check-mode data` chỉ thấy "có drift" nhưng không lộ ra việc precision đã rớt; combined phơi bày cả hai loại drift.

---

## Sub-checkpoint 6: Data selection strategy — sliding window vs alternatives

Khi retrain chỉ trên drift window (7 ngày gần nhất), model v2 overfit vào phân phối mới: nó học rằng latency 156ms là "bình thường" nhưng quên pattern cũ. **Sliding window strategy** (baseline + drift window concat) cho model thấy cả 2 regime: `baseline.csv` (4320 rows) + `drifted.csv` (1008 rows) = **5328 rows** training set — đủ để IsolationForest không bị dominated bởi phân phối mới.

**Lưu ý trung thực về metric holdout:** `holdout.csv` trong data-pack chứa **toàn bộ 500 rows nhãn normal (label=0), không có anomaly nào**. Do đó precision/recall của *lớp anomaly* trên holdout suy biến về **0.0 cho cả v1 lẫn v2** (không có true positive để tính). Tiêu chí "v2 precision ≥ v1 precision trên holdout" thỏa theo nghĩa `0.0 ≥ 0.0`, nhưng metric này **không phân biệt được** hai model. Để đánh giá overfit-prevention một cách có ý nghĩa nên dùng *false-positive rate* trên holdout (model tốt → ít cờ nhầm normal thành anomaly), hoặc đo trên tập có nhãn cân bằng như `post_deploy_eval.csv` — ở đó cả v1 và v2 đều đạt precision **1.0**.

Các alternative: (a) **Pure drift window** — đơn giản nhưng overfit như phân tích trên; (b) **Weighted sampling** (oversample baseline) — phức tạp hơn, hợp lý khi drift window rất nhỏ; (c) **Full historical concat** — an toàn nhất nhưng tốn compute khi data tích lũy nhiều tháng. Sliding window là trade-off tốt nhất cho lab này.

---

## Sub-checkpoint 7: Auto-rollback — threshold và policy

Sau khi v2 được promote lên `@production`, `post_deploy_monitor` chạy N polling cycles đánh giá precision trên `post_deploy_eval.csv` (200 rows có nhãn rõ ràng: 60% clear-normal, 40% clear-anomaly). Ngưỡng mặc định: `precision < 0.65` → auto-rollback.

Tại sao 0.65? Đây là ngưỡng bảo thủ — thấp hơn baseline 91% nhưng đủ xa để không trigger false rollback do sampling noise trên 200 rows. Tính toán: với 80 anomaly rows (40%), nếu model miss 30 → precision = 50/57 ≈ 0.88; nếu model hoàn toàn confused → precision ≈ 0.40. Ngưỡng 0.65 nằm ở điểm "model rõ ràng đang sai lệch nghiêm trọng".

Rollback flow: `client.set_registered_model_alias(MODEL_NAME, "archived", v2_version)` → `client.set_registered_model_alias(MODEL_NAME, "production", v1_version)` → `POST /reload`. Toàn bộ < 5 giây. Mọi sự kiện được append vào `outputs/audit_log.jsonl` với event key `auto_rollback_v2_to_v1`, bao gồm version bị demote, version được restore, precision trigger value, và cycle number.

**Số đo thực (2 nhánh):** (a) Happy path — với Pipeline(scaler+iso), v2 đạt precision = **1.0** trên `post_deploy_eval.csv` qua cả **24/24 cycle** → STABLE, không rollback. (b) Rollback path — chạy lại với `--post-deploy-eval data/drifted.csv` (tập concept-drift, precision của model ~**0.32 < 0.65**) → kích hoạt `auto_rollback_v2_to_v1` ngay, v1 được khôi phục về `@production`, v2 → `@archived`.

---

## Observability: tại sao các metrics này quan trọng trong MLOps

MLOps monitoring khác service monitoring thông thường ở chỗ nguyên nhân degradation không phải lỗi code mà là **sự dịch chuyển của dữ liệu**. Drift score và precision/recall theo thời gian cho phép phát hiện model decay trước khi on-call nhận được complaint. Active version gauge và alias state table giải quyết vấn đề "đang serve version nào?" — câu hỏi thường mất nhiều phút tra cứu trong MLflow UI. Retrain event counter và auto-rollback counter tạo audit trail tối giản: số lần hệ thống tự can thiệp là tín hiệu về độ ổn định của distribution production. Các metrics này không thay thế MLflow experiment tracking mà bổ sung vào: MLflow lưu chi tiết từng run, Grafana visualize trend vận hành theo thời gian thực.

---

## Trade-offs đã chấp nhận

| Quyết định | Được | Mất |
|---|---|---|
| Manual approval gate | An toàn, human oversight | Latency trong retrain loop (hours, không phải minutes) |
| Combined drift (data + performance) | Bắt cả data drift lẫn concept drift (precision rớt còn 0.32) | Cần tập có nhãn cho nhánh performance |
| IsolationForest (không LSTM-AE) | Train < 1s, explainable, no GPU | Không capture temporal patterns, mỗi row độc lập |
| Local artifact store | Không cần S3 setup | Không scale multi-node, artifacts mất khi volume bị xóa |
