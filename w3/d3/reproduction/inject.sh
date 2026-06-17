#!/usr/bin/env bash
# Kích hoạt failure mode trên một waf-edge đang chạy (xem start_reproduction.sh):
# đẩy rule vulnerable lên toàn cầu, rồi gửi một loạt payload đối kháng (toàn
# chữ-số, không có '!' ở cuối) ép catastrophic backtracking. Đồng thời probe
# /healthz để xem worker đứng hình.
#
# Mitigate sau đó bằng:
#   curl -s -XPOST "http://127.0.0.1:$PORT/admin/rule" -d '{"mode":"safe"}'
set -euo pipefail
PORT="${1:-8080}"
CONCURRENCY="${2:-6}"
PAYLOAD="${3:-aaaaaaaaaaaaaaaaaaaaaaa}"   # 23 chữ 'a': backtrack có giới hạn ~1s/lần match
BASE="http://127.0.0.1:$PORT"

echo "[inject] đẩy managed rule v-vulnerable lên toàn cầu"
curl -s -XPOST "$BASE/admin/rule" -d "{\"mode\":\"vulnerable\"}"; echo

echo "[inject] bắn $CONCURRENCY request /inspect đối kháng"
for _ in $(seq 1 "$CONCURRENCY"); do
  curl -s -XPOST "$BASE/inspect" -d "{\"payload\":\"$PAYLOAD\"}" >/dev/null &
done

echo "[inject] probe /healthz trong lúc worker backtracking (kỳ vọng độ trễ nhiều giây):"
for _ in $(seq 1 5); do
  curl -s -o /dev/null -w "  /healthz  %{time_total}s  http=%{http_code}\n" "$BASE/healthz" || true
done
wait
echo "[inject] xong. Rollback:  curl -s -XPOST $BASE/admin/rule -d '{\"mode\":\"safe\"}'"
