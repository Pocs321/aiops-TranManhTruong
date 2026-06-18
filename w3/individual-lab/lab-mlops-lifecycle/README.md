# MLOps Lifecycle — Anomaly Detection Pipeline

Pipeline phát hiện bất thường (latency/error_rate/rps của payment gateway): train → register → serve → giám sát drift → retrain blue-green → auto-rollback. Stack chạy local bằng Docker Compose (MLflow + PostgreSQL + Prometheus + Pushgateway + Grafana); `serve.py`/`drift_detector.py`/`retrain.py` chạy trên host.

## Cách chạy từ đầu đến cuối

```bash
# 0) Môi trường Python (MLflow 2.13.2 cần pkg_resources → setuptools<81; Python 3.11)
uv venv --python 3.11 .venv
uv pip install --python .venv 'mlflow==2.13.2' 'evidently==0.4.40' scikit-learn pandas \
  numpy fastapi uvicorn prometheus_client requests 'setuptools<81'
export MLFLOW_TRACKING_URI=http://localhost:5000
export PUSHGATEWAY_URL=http://localhost:9091
export PYTHONUTF8=1          # tránh UnicodeEncodeError ký tự '→' trên console Windows

# 1) Dựng stack (đặt project name riêng để không đụng stack khác trên máy)
docker compose -p mlops -f configs/docker-compose.yml up -d
#   nếu cổng 9090/3000 đã bị chiếm: thêm override remap (vd Prometheus 9092 / Grafana 3001)

# 2) Train v1 + register @production
.venv/Scripts/python pipeline.py --data data/baseline.csv

# 3) Serve (chạy bằng uvicorn, KHÔNG dùng `python serve.py` để tránh đăng ký metric trùng)
( cd <thư-mục-chứa-serve.py> && \
  ../.venv/Scripts/python -m uvicorn serve:app --host 127.0.0.1 --port 8000 )
curl -s http://localhost:8000/health/active-version
curl -s -X POST http://localhost:8000/predict -H 'Content-Type: application/json' \
  -d '{"features": [[300,4.0,900],[120,0.8,450]]}'      # -> [-1, 1]

# 4) Drift detection (combined: data drift + concept/performance drift)
.venv/Scripts/python drift_detector.py --reference data/baseline.csv --current data/drifted.csv \
  --check-mode combined --labeled-current data/drifted.csv \
  --model-uri models:/anomaly-detector@production --log-mlflow

# 5) Retrain orchestrator: detect → train v2 (sliding window) → staging → approve → promote → reload
#    + post-deploy monitor 24 cycle, auto-rollback nếu precision < 0.65
.venv/Scripts/python retrain.py --reference data/baseline.csv --current data/drifted.csv \
  --holdout data/holdout.csv --post-deploy-eval data/post_deploy_eval.csv \
  --serve-url http://localhost:8000 --auto-approve

# Demo auto-rollback (v2 kém trên dữ liệu concept-drift):
.venv/Scripts/python retrain.py --reference data/baseline.csv --current data/drifted.csv \
  --post-deploy-eval data/drifted.csv --serve-url http://localhost:8000 --auto-approve
```

Quan sát: Grafana http://localhost:3000 (dashboard "AIOps MLOps Lifecycle"); MLflow http://localhost:5000.
Artefacts sinh ra: `outputs/drift_reports/*.html`, `outputs/audit_log.jsonl`.

## Ghi chú thiết kế (xem DESIGN.md / SUBMIT.md)
- Drift threshold 0.15; combined mode bắt cả data drift (Evidently) lẫn concept drift (precision trên labeled set).
- Versioning bằng MLflow alias (`production`/`staging`/`archived`) → swap không đổi code serve.
- Approval gate `[y/N]` (dùng `--auto-approve` cho CI). Auto-rollback khi v2 precision < 0.65 trong 24 cycle.
- Model là `Pipeline(StandardScaler + IsolationForest)` để mọi nơi load từ registry đều scale nhất quán.
