"""Tái hiện sự cố Cloudflare 2019-07-02 (WAF regex, catastrophic backtracking),
bản môi trường tối giản.

Một edge worker đơn chạy mọi request body qua một "managed rule" của WAF. Rule
có hai phiên bản:

    vulnerable  ^([A-Za-z0-9]+)+!$   <- lượng từ lồng nhau trên một nhóm ký tự
                                        chồng lấn. Với input toàn chữ-số mà thiếu
                                        ký tự '!' ở cuối, regex engine thử O(2^n)
                                        cách phân hoạch chuỗi -> CPU nghẽn, request
                                        đứng hình.
    safe        ^[A-Za-z0-9]+!$       <- cùng ý đồ, không lồng lượng từ -> thời
                                        gian tuyến tính. Đây là "rollback rule".

Chi tiết trung thực: CPython `re` GIỮ GIL trong lúc match, nên MỘT request bệnh
lý làm đứng cả tiến trình worker -- ngay cả /healthz rẻ tiền cũng phải xếp hàng
sau nó. Đó là lý do một regex tồi có thể kéo edge của Cloudflare sập toàn cầu:
sự cố không phải "một endpoint chậm", mà là "worker ngừng phục vụ".

Endpoint (http.server chuẩn, đa luồng):
    GET  /healthz            liveness rẻ (không regex) -- dùng để đo worker còn
                             phản hồi được dưới tải hay không
    GET  /version            chế độ rule hiện tại
    POST /inspect            body {"payload": "..."} -> chạy WAF rule hiện tại.
                             Trả {blocked, rule_mode, match_ms}. Đường nóng.
    POST /admin/rule         body {"mode": "safe"|"vulnerable"} -> hot-swap rule
                             (mô phỏng rollback/kill-switch ruleset toàn cầu)

Chạy độc lập:  python waf_edge.py --port 8080
Thường được drive_incident.py điều khiển (tự khởi động/dọn dẹp).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # in được tiếng Việt trên console Windows (cp1252)

# Hai phiên bản của cùng một WAF managed rule. Bản vulnerable là bản được "đẩy
# toàn cầu"; bản safe là đích để rollback.
RULES = {
    "vulnerable": re.compile(r"^([A-Za-z0-9]+)+!$"),
    "safe": re.compile(r"^[A-Za-z0-9]+!$"),
}

# Chặn cứng độ dài để một payload quá khổ vô tình không thể treo worker hàng phút
# -- trên Windows không thể ngắt `re` từ luồng khác, nên rào chắn phải đặt ở độ
# dài input chứ không phải timeout. Driver hiệu chỉnh payload nằm dưới ngưỡng này.
MAX_PAYLOAD = 34

_state_lock = threading.Lock()
_rule_mode = "vulnerable"  # mặc định: rule tồi đang chạy


def current_mode() -> str:
    with _state_lock:
        return _rule_mode


def set_mode(mode: str) -> None:
    global _rule_mode
    if mode not in RULES:
        raise ValueError(f"chế độ rule không hợp lệ: {mode}")
    with _state_lock:
        _rule_mode = mode


class Handler(BaseHTTPRequestHandler):
    # Tắt log mỗi-request mặc định ra stderr; driver mới là nơi ghi timeline.
    def log_message(self, *args) -> None:  # noqa: D401
        return

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if self.path.startswith("/healthz"):
            self._send(200, {"status": "ok"})
        elif self.path.startswith("/version"):
            self._send(200, {"service": "waf-engine", "rule_mode": current_mode()})
        else:
            self._send(404, {"error": "không tìm thấy"})

    def do_POST(self) -> None:
        if self.path.startswith("/inspect"):
            self._inspect()
        elif self.path.startswith("/admin/rule"):
            self._admin_rule()
        else:
            self._send(404, {"error": "không tìm thấy"})

    def _inspect(self) -> None:
        try:
            payload = str(self._read_json().get("payload", ""))
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": f"yêu cầu sai: {exc}"})
            return
        if len(payload) > MAX_PAYLOAD:
            self._send(400, {"error": f"payload quá dài (>{MAX_PAYLOAD})"})
            return

        rule = RULES[current_mode()]
        t0 = time.perf_counter()
        matched = rule.match(payload) is not None  # <- backtracking xảy ra ở đây
        match_ms = (time.perf_counter() - t0) * 1000
        self._send(200, {
            "blocked": matched,
            "rule_mode": current_mode(),
            "match_ms": round(match_ms, 2),
        })

    def _admin_rule(self) -> None:
        try:
            mode = str(self._read_json().get("mode", ""))
            set_mode(mode)
        except Exception as exc:  # noqa: BLE001
            self._send(400, {"error": str(exc)})
            return
        self._send(200, {"rule_mode": current_mode()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mode", choices=list(RULES), default="vulnerable")
    args = ap.parse_args()
    set_mode(args.mode)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"waf-edge đang lắng nghe :{args.port} (rule_mode={current_mode()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
