"""Chạy pipeline AIOps THẬT (w2/d3) trên tập alert quan sát được của bản tái hiện.

Đây là bước "chạy pipeline AIOps trên reproduction" của bài tập. Thay vì viết lại
bất cứ thứ gì, nó import chính các module pipeline đang được đánh giá --
correlate.py (L1) và rca.py (L2) từ w2/d3 -- và nạp cho chúng:

  * đồ thị topology edge CỦA CHÍNH bản tái hiện (topology.json), vì đây mới là
    stack đang được giám sát ở đây (đồ thị demo của w2 là e-commerce); và
  * các alert mà bản tái hiện thực sự phát ra (alerts_observed.json).

Nó ghi rca_observed.json (phán quyết của pipeline) và in so sánh kỳ-vọng-vs-quan-sát
với postmortem gốc của Cloudflare.

Kiểm tra trọng tâm mà bài tập yêu cầu: pipeline có phát hiện nhanh không, có chọn
đúng root không, và bỏ sót gì? Điểm bỏ sót mang tính cấu trúc và được nêu rõ ở đây.

LƯU Ý trung thực: các trường root_cause.reasoning và 2 action chung trong
rca_observed.json do rca.py (mã tiếng Anh của W2, KHÔNG sửa) sinh ra nên giữ
nguyên tiếng Anh. Phần do file này / incidents_history.json sở hữu đã dịch sang
tiếng Việt; thêm khối `dien_giai_vi` tóm tắt bằng tiếng Việt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # in được tiếng Việt trên console Windows (cp1252)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                      # w3/d3


def _find_pipeline_dir(start: Path) -> Path:
    """Tìm w2/d3 (chứa rca.py) bằng cách đi ngược lên các thư mục cha, để script
    chạy được dù cây thư mục nộp bài lồng sâu bao nhiêu cấp."""
    for base in [start, *start.parents]:
        cand = base / "w2" / "d3"
        if (cand / "rca.py").exists():
            return cand
    raise FileNotFoundError("không tìm thấy pipeline w2/d3 (rca.py) ở các thư mục cha")


PIPELINE_DIR = _find_pipeline_dir(HERE)   # .../AWS/w2/d3

# Import các module pipeline thật đang được đánh giá.
sys.path.insert(0, str(PIPELINE_DIR))
from correlate import build_graph_from_json, correlate  # noqa: E402
from rca import run_rca                                   # noqa: E402

GAP_SEC = 120   # CONFIG.correlate_gap_sec trong pipeline thật
MAX_HOP = 2     # CONFIG.correlate_max_hop trong pipeline thật


def main() -> None:
    graph = build_graph_from_json(HERE / "topology.json")
    history = json.loads((HERE / "incidents_history.json").read_text(encoding="utf-8"))["incidents"]
    alerts = json.loads((ROOT / "alerts_observed.json").read_text(encoding="utf-8"))["alerts"]
    timeline = json.loads((ROOT / "timeline.json").read_text(encoding="utf-8"))["events"]

    clusters = correlate(alerts, graph, gap_sec=GAP_SEC, max_hop=MAX_HOP)
    primary = max(clusters, key=lambda c: c["alert_count"]) if clusters else None
    rca = run_rca(primary, graph, history) if primary else {
        "root_cause": "unknown", "confidence": 0.0, "reasoning": "no clusters",
        "candidates": [], "actions": [], "similar_incidents": [], "method": "none"}

    # Yếu tố kích hoạt thật có hiển thị với pipeline không? Nó là sự kiện thay đổi,
    # không phải metric alert -- nên nằm trong timeline mà không bao giờ là input.
    deploy_events = [e for e in timeline if e["event"] == "ruleset_deployed"]
    alert_services = sorted({a["service"] for a in alerts})

    out = {
        "pipeline": {
            "modules": "w2/d3 correlate.py + rca.py (thật)",
            "topology": "reproduction/topology.json (edge)",
            "config": {"gap_sec": GAP_SEC, "max_hop": MAX_HOP, "llm": "tắt (không có key)"},
        },
        "input_alert_services": alert_services,
        "clusters": [
            {"cluster_id": c["cluster_id"], "alert_count": c["alert_count"],
             "services": c["services"], "time_range": c["time_range"]}
            for c in clusters
        ],
        "root_cause": {
            "service": rca["root_cause"], "confidence": rca["confidence"],
            "reasoning": rca["reasoning"], "method": rca.get("method"),
        },
        "candidates": rca.get("candidates", []),
        "recommended_actions": rca["actions"],
        "similar_incidents": [
            {"id": s["id"], "similarity": s["similarity"], "summary": s["summary"]}
            for s in rca["similar_incidents"]
        ],
        "expected_vs_observed": {
            "actual_root_cause": "Nội dung của WAF managed-rule v-vulnerable "
                                 "(regex catastrophic-backtracking), đẩy toàn cầu",
            "pipeline_root_cause": rca["root_cause"],
            "localized_pegged_component": rca["root_cause"] == "waf-engine",
            "named_the_offending_artifact": False,
            "trigger_change_event_in_pipeline_inputs": len(
                [a for a in alerts if a.get("metric") in ("deploy", "config_change")]) > 0,
            "trigger_present_in_timeline_only": bool(deploy_events),
            "similar_incidents_mechanism_match": False,
        },
        "dien_giai_vi": (
            f"Pipeline khoanh đúng thành phần bị nghẽn ('{rca['root_cause']}', "
            f"confidence {rca['confidence']}) bằng topology + severity, NHƯNG không "
            "gọi được tên nguyên nhân: yếu tố kích hoạt là sự kiện thay đổi "
            "('ruleset_deployed') chỉ có trong timeline.json, không bao giờ là input. "
            "Hai action đầu lấy từ một sự cố quá khứ TRÙNG DỊCH VỤ nhưng SAI cơ chế "
            "(cache eviction). reasoning và 2 action chung giữ nguyên tiếng Anh vì do "
            "pipeline w2/d3 sinh ra."
        ),
    }

    (ROOT / "rca_observed.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- so sánh dạng đọc được --------------------------------------------
    print("=== Pipeline AIOps trên bản tái hiện WAF-regex ===")
    print(f"dịch vụ alert đầu vào : {alert_services}")
    print(f"cụm hình thành        : {[c['cluster_id'] + '(' + str(c['alert_count']) + ')' for c in clusters]}")
    print(f"root cause pipeline   : {rca['root_cause']}  (confidence {rca['confidence']})")
    print(f"  reasoning (pipeline): {rca['reasoning']}")
    print(f"ứng viên hàng đầu     : {rca.get('candidates')}")
    print(f"action khuyến nghị    : {rca['actions']}")
    print(f"sự cố tương tự        : {[(s['id'], s['similarity']) for s in rca['similar_incidents']]}")
    print()
    print("--- so với postmortem gốc ---")
    print("root cause THẬT      : một regex trong WAF managed-rule v-vulnerable, đẩy toàn cầu")
    print(f"pipeline khoanh tới  : {rca['root_cause']} "
          f"({'thành phần bị nghẽn, đúng' if rca['root_cause']=='waf-engine' else 'KHÔNG phải thành phần bị nghẽn'})")
    print("gọi được tên artifact?: KHÔNG (không có tín hiệu rule-version / thay đổi trong input)")
    print(f"trigger (ruleset_deployed) có trong input pipeline? : "
          f"{'CÓ' if out['expected_vs_observed']['trigger_change_event_in_pipeline_inputs'] else 'KHÔNG -- chỉ trong timeline'}")
    print("sự cố tương tự có trùng CƠ CHẾ?                     : KHÔNG (cache-eviction / dns -- không phải regex CPU)")
    print()
    print("ĐÃ GHI rca_observed.json")


if __name__ == "__main__":
    main()
