#!/usr/bin/env bash
# Dựng stack tái hiện WAF-edge (một worker) và chờ tới khi khoẻ mạnh.
# Để có toàn bộ incident đã thu, dùng drive_incident.py (nó tự khởi động, inject,
# thu, mitigate và dọn dẹp trong một lần chạy).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8080}"

python "$HERE/waf_edge.py" --port "$PORT" --mode safe &
SERVER_PID=$!
echo "waf-edge đang khởi động (pid=$SERVER_PID) trên :$PORT"

for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "khoẻ mạnh trên :$PORT  (rule safe đang sống). Kích hoạt bằng:  bash inject.sh $PORT"
    echo "pid server: $SERVER_PID  (dừng bằng: kill $SERVER_PID)"
    exit 0
  fi
  sleep 0.1
done
echo "server không trở nên khoẻ mạnh" >&2
kill "$SERVER_PID" 2>/dev/null || true
exit 1
