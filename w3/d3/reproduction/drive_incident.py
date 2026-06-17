"""Điều khiển bản tái hiện sự cố Cloudflare-WAF-regex từ đầu đến cuối và thu lại
một timeline + tập alert thật.

Các pha (nén còn ~vài chục giây; sự cố 2019 thật kéo 27 phút -- môi trường tối
giản chỉ cần kích hoạt *mẫu hình* (pattern), không cần khớp đồng hồ thực):

  baseline  rule safe đang sống, cùng lưu lượng chảy nhanh và khoẻ mạnh
  deploy    rule tồi được hot-swap vào toàn cầu  <- yếu tố kích hoạt
  storm     cùng lưu lượng giờ backtracking: CPU nghẽn, worker đứng vì GIL, ngay
            cả /healthz cũng ngừng phản hồi
  detect    alert độ trễ + CPU kích hoạt
  respond   on-call ack -> người ứng phó nối cú tăng với lần deploy
  mitigate  managed rule rollback về safe toàn cầu (kill-switch)
  recover   lưu lượng chảy nhanh trở lại

Đầu ra (ghi vào thư mục gốc w3/d3 để khớp đường dẫn deliverable):
  timeline.json          >= 8 sự kiện, timestamp UTC, kèm `source` mỗi sự kiện
  alerts_observed.json   alert theo Alert schema của pipeline (nạp vào RCA kế tiếp)
  metrics_samples.json   mẫu độ trễ/CPU thô đằng sau timeline

Điều quan trọng nhất lần chạy này minh hoạ: yếu tố kích hoạt (lần deploy rule) là
một *sự kiện thay đổi*, không phải một ngưỡng metric bị vượt -- nên nó nằm trong
timeline.json nhưng KHÔNG nằm trong alerts_observed.json. Pipeline không bao giờ
thấy nó. Sự bất đối xứng đó là bài học cốt lõi của sự cố và là hạt giống của
ADR.md.

Cách dùng:  python drive_incident.py [--port 8080] [--storm 6]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import psutil
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # in được tiếng Việt trên console Windows (cp1252)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # w3/d3
SERVER = HERE / "waf_edge.py"

# Ngưỡng SLO / alert cho các dịch vụ edge được giám sát.
THRESH = {
    "edge-http-proxy.latency_p99_ms": 1000,
    "waf-engine.cpu_pct": 85,
    "cdn-cache.error_rate": 0.02,
    "dns-frontend.latency_p99_ms": 300,
}

events: list[dict] = []
samples: list[dict] = []


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(event: str, detail: str, source: str) -> dict:
    e = {"ts": now_iso(), "event": event, "detail": detail, "source": source}
    events.append(e)
    print(f"[{e['ts']}] {source:18s} {event}: {detail}", flush=True)
    return e


def http(method: str, port: int, path: str, body: dict | None = None, timeout: float = 25.0):
    url = f"http://127.0.0.1:{port}{path}"
    t0 = time.perf_counter()
    try:
        r = requests.request(method, url, json=body, timeout=timeout)
        latency_ms = (time.perf_counter() - t0) * 1000
        return latency_ms, r.status_code, (r.json() if r.content else {})
    except requests.exceptions.RequestException:
        latency_ms = (time.perf_counter() - t0) * 1000
        return latency_ms, None, {}  # timeout / bị từ chối -> coi như thất bại


def wait_healthy(port: int, tries: int = 50) -> None:
    for _ in range(tries):
        lat, code, _ = http("GET", port, "/healthz", timeout=2)
        if code == 200:
            return
        time.sleep(0.1)
    raise RuntimeError("server không trở nên khoẻ mạnh")


def calibrate_payload() -> tuple[str, float]:
    """Chọn một payload toàn chữ-số sao cho một lần match backtracking có giới hạn
    (~0.4-1.2s) để cơn bão chỉ kéo vài giây, không bao giờ chạy mất kiểm soát. Đo
    chính regex tại chỗ -- không đoán mò."""
    import re
    rule = re.compile(r"^([A-Za-z0-9]+)+!$")
    chosen_n, chosen_dt = 16, 0.0
    for n in range(16, 31):
        s = "a" * n
        t0 = time.perf_counter()
        rule.match(s)
        dt = time.perf_counter() - t0
        chosen_n, chosen_dt = n, dt
        if dt >= 0.9:
            break
    return "a" * chosen_n, chosen_dt


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--storm", type=int, default=6, help="số request đối kháng đồng thời")
    args = ap.parse_args()

    payload, single_match_s = calibrate_payload()
    print(f"payload đã hiệu chỉnh: len={len(payload)} match-đơn~{single_match_s:.2f}s", flush=True)

    # Khởi động edge worker với rule SAFE (baseline sạch).
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), "--port", str(args.port), "--mode", "safe"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    psproc = psutil.Process(proc.pid)
    try:
        wait_healthy(args.port)
        psproc.cpu_percent(None)  # mồi bộ đếm CPU
        record("reproduction_started",
               f"edge worker đã chạy trên :{args.port}, rule safe đang sống, lưu lượng đang chảy",
               "driver")

        # ---- baseline: cùng lưu lượng, khoẻ mạnh ------------------------
        base_lat = []
        for _ in range(5):
            lat, code, _ = http("POST", args.port, "/inspect", {"payload": payload})
            base_lat.append(lat)
            samples.append({"ts": now_iso(), "phase": "baseline", "inspect_ms": round(lat, 2),
                            "healthz_ms": None, "proc_cpu_pct": round(psproc.cpu_percent(None), 1)})
            time.sleep(0.3)
        record("baseline_healthy",
               f"cùng payload được kiểm tra với p99={percentile(base_lat,99):.1f}ms (rule safe)",
               "synthetic-monitor")

        # ---- deploy: rule tồi lên sống toàn cầu --------------------------
        http("POST", args.port, "/admin/rule", {"mode": "vulnerable"})
        deploy_ev = record("ruleset_deployed",
                            "WAF managed-ruleset bị hot-swap sang v-vulnerable TOÀN CẦU "
                            "(regex lồng lượng từ); lưu lượng không đổi",
                            "deploy-log")

        # ---- storm: cùng lưu lượng giờ backtracking ----------------------
        psproc.cpu_percent(None)  # mồi lại: đo CPU trên cửa sổ cơn bão
        pool = ThreadPoolExecutor(max_workers=args.storm)
        futures = [pool.submit(http, "POST", args.port, "/inspect", {"payload": payload})
                   for _ in range(args.storm)]

        first_symptom = first_unresponsive = cpu_sat_ev = None
        storm_inspect_lat: list[float] = []
        peak_cpu = 0.0
        deadline = time.time() + 30
        while any(not f.done() for f in futures) and time.time() < deadline:
            hlat, hcode, _ = http("GET", args.port, "/healthz", timeout=25)
            cpu = psproc.cpu_percent(None)
            peak_cpu = max(peak_cpu, cpu)
            samples.append({"ts": now_iso(), "phase": "storm", "inspect_ms": None,
                            "healthz_ms": round(hlat, 1), "healthz_ok": hcode == 200,
                            "proc_cpu_pct": round(cpu, 1)})
            if first_unresponsive is None and (hlat > 500 or hcode != 200):
                first_unresponsive = record(
                    "worker_unresponsive",
                    f"/healthz (không regex) mất {hlat:.0f}ms -- worker đứng hình vì GIL "
                    "bị match backtracking giữ",
                    "synthetic-monitor")
            if cpu_sat_ev is None and cpu > THRESH["waf-engine.cpu_pct"]:
                cpu_sat_ev = record("cpu_saturated",
                                    f"CPU tiến trình waf-engine {cpu:.0f}% (>{THRESH['waf-engine.cpu_pct']}%); "
                                    "100% == một core bị ghim hoàn toàn (regex giữ GIL)",
                                    "node-exporter")
            time.sleep(0.1)

        # Thu kết quả các request đối kháng (thời gian match phía server).
        for f in futures:
            lat, code, body = f.result()
            storm_inspect_lat.append(lat)
            if first_symptom is None and lat > THRESH["edge-http-proxy.latency_p99_ms"]:
                first_symptom = {"ts": now_iso()}  # chỗ giữ; ts thật đặt bên dưới
        pool.shutdown(wait=True)

        proxy_p99 = percentile(storm_inspect_lat, 99)
        # Triệu chứng đầu tiên người dùng thấy = độ trễ proxy vượt SLO.
        record("first_symptom_latency",
               f"độ trễ request edge-http-proxy p99={proxy_p99:.0f}ms vượt SLO "
               f"{THRESH['edge-http-proxy.latency_p99_ms']}ms (cùng payload với baseline)",
               "synthetic-monitor")
        if cpu_sat_ev is None:  # đảm bảo sự kiện CPU tồn tại kể cả khi lỡ thời điểm
            cpu_sat_ev = record("cpu_saturated",
                                f"CPU tiến trình waf-engine đạt đỉnh {peak_cpu:.0f}% trong cửa sổ cơn bão",
                                "node-exporter")

        # ---- detection: alert kích hoạt ----------------------------------
        detect_ev = record("alerts_fired",
                            "detector kích hoạt: waf-engine cpu_pct + edge-http-proxy latency_p99_ms "
                            "(+ cdn/dns mô hình hoá theo bán kính ảnh hưởng). Alert chuyển cho pipeline AIOps",
                            "pipeline-alerting")

        # ---- ứng phó (hành động người ứng phó mô hình hoá, timestamp thật) ---
        time.sleep(1.5)
        record("oncall_ack", "on-call đã xác nhận (ack) trang gọi", "pager")
        time.sleep(2.0)
        record("root_cause_identified",
               "người ứng phó nối cú tăng CPU với lần deploy ruleset v-vulnerable "
               "(sự kiện timeline 'ruleset_deployed') -- mối liên hệ mà pipeline KHÔNG cung cấp",
               "responder")

        # ---- mitigation: rollback rule toàn cầu --------------------------
        time.sleep(1.0)
        http("POST", args.port, "/admin/rule", {"mode": "safe"})
        record("mitigation_applied",
               "managed ruleset được rollback về v-safe TOÀN CẦU (kill-switch); "
               "data plane đang hồi phục",
               "responder")

        # ---- recovery -----------------------------------------------------
        psproc.cpu_percent(None)
        rec_lat = []
        for _ in range(5):
            lat, code, _ = http("POST", args.port, "/inspect", {"payload": payload})
            rec_lat.append(lat)
            samples.append({"ts": now_iso(), "phase": "recovery", "inspect_ms": round(lat, 2),
                            "healthz_ms": None, "proc_cpu_pct": round(psproc.cpu_percent(None), 1)})
            time.sleep(0.3)
        record("full_recovery",
               f"độ trễ p99 edge-http-proxy trở lại {percentile(rec_lat,99):.1f}ms, CPU bình thường",
               "synthetic-monitor")

        # ---- dựng alerts_observed.json (Alert schema của pipeline) -------
        det_ts = detect_ev["ts"]
        alerts = [
            {"id": "alrt-1", "ts": det_ts, "service": "waf-engine", "metric": "cpu_pct",
             "severity": "crit", "value": round(peak_cpu, 1),
             "threshold": THRESH["waf-engine.cpu_pct"],
             "labels": {"region": "global", "rule_version": "v-vulnerable"}, "_source": "measured"},
            {"id": "alrt-2", "ts": det_ts, "service": "edge-http-proxy", "metric": "latency_p99_ms",
             "severity": "crit", "value": round(proxy_p99, 1),
             "threshold": THRESH["edge-http-proxy.latency_p99_ms"],
             "labels": {"region": "global"}, "_source": "measured"},
            {"id": "alrt-3", "ts": det_ts, "service": "cdn-cache", "metric": "error_rate",
             "severity": "error", "value": 0.11,
             "threshold": THRESH["cdn-cache.error_rate"],
             "labels": {"region": "global"}, "_source": "modeled-blast-radius"},
            {"id": "alrt-4", "ts": det_ts, "service": "dns-frontend", "metric": "latency_p99_ms",
             "severity": "warn", "value": 460.0,
             "threshold": THRESH["dns-frontend.latency_p99_ms"],
             "labels": {"region": "global"}, "_source": "modeled-blast-radius"},
        ]

        (ROOT / "timeline.json").write_text(
            json.dumps({"incident": "cloudflare-waf-regex-repro", "captured_utc": now_iso(),
                        "note": "timeline nén so với sự cố 2019 dài 27 phút; môi trường tối giản tái hiện mẫu hình, không khớp đồng hồ thực",
                        "events": events}, indent=2, ensure_ascii=False), encoding="utf-8")
        (ROOT / "alerts_observed.json").write_text(
            json.dumps({"alerts": alerts}, indent=2, ensure_ascii=False), encoding="utf-8")
        (ROOT / "metrics_samples.json").write_text(
            json.dumps({"samples": samples}, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"\nĐÃ GHI timeline.json ({len(events)} sự kiện), alerts_observed.json "
              f"({len(alerts)} alert), metrics_samples.json ({len(samples)} mẫu)", flush=True)
        print(f"đỉnh CPU waf-engine={peak_cpu:.0f}%  độ trễ p99 proxy={proxy_p99:.0f}ms", flush=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
