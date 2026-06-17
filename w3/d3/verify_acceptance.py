"""Kiểm tra các deliverable W3-D3 theo checklist nghiệm thu (note §9.10).

Chạy:  python verify_acceptance.py
Thoát khác 0 nếu bất kỳ kiểm tra cứng nào trượt.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # in được tiếng Việt trên console Windows (cp1252)

ROOT = Path(__file__).resolve().parent
results: list[tuple[bool, str]] = []
warnings: list[str] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def load(name: str) -> dict:
    return json.loads(read(name))


# 1. Các artifact JSON parse được ------------------------------------------
try:
    timeline = load("timeline.json")
    alerts = load("alerts_observed.json")
    rca = load("rca_observed.json")
    metrics = load("metrics_samples.json")
    check(True, "Mọi artifact JSON parse được (timeline/alerts/rca/metrics)")
except Exception as exc:  # noqa: BLE001
    check(False, f"Artifact JSON parse: {exc}")
    timeline = alerts = rca = metrics = {}

# 2. timeline >= 8 sự kiện, đều UTC ----------------------------------------
events = timeline.get("events", [])
check(len(events) >= 8, f"timeline.json có >= 8 sự kiện (được {len(events)})")
utc_ok = True
for e in events:
    try:
        dt = datetime.fromisoformat(e["ts"])
        utc_ok = utc_ok and dt.tzinfo is not None and dt.utcoffset() == timezone.utc.utcoffset(None)
    except Exception:  # noqa: BLE001
        utc_ok = False
check(utc_ok, "mọi sự kiện timeline có timestamp UTC")

# 3. alert đúng schema pipeline; rca có root cause -------------------------
alert_keys = {"id", "ts", "service", "metric", "severity", "value", "threshold"}
check(all(alert_keys <= set(a) for a in alerts.get("alerts", [])),
      "các entry alerts_observed.json khớp Alert schema của pipeline")
check(rca.get("root_cause", {}).get("service") is not None,
      f"rca_observed.json có root cause (được {rca.get('root_cause', {}).get('service')!r})")

# 4. postmortem: field, blameless, >=8 dòng timeline, >=2 lỗ hổng phát hiện -
pm = read("postmortem.md")
required = ["**Trạng thái:**", "**Ngày:**", "**Tác giả:**", "**Mức độ:**", "**Thời lượng:**",
            "## Tóm tắt", "## Ảnh hưởng", "## Dòng thời gian", "## Nguyên nhân gốc",
            "## Yếu tố góp phần", "## Phát hiện", "## Ứng phó", "## Hành động khắc phục"]
missing = [r for r in required if r not in pm]
check(not missing, f"postmortem.md có đủ field theo template (thiếu: {missing})")
n_rows = len(re.findall(r"\| \d{2}:\d{2}:\d{2}", pm))
check(n_rows >= 8, f"timeline postmortem có >= 8 dòng (được {n_rows})")
n_gaps = len(re.findall(r"Lỗ hổng \d", pm))
check(n_gaps >= 2, f"postmortem nêu >= 2 lỗ hổng phát hiện (được {n_gaps})")

blame = [w for w in ["quên", "cẩu thả", "bất cẩn", "đổ lỗi", "thiếu trách nhiệm",
                     "kém cỏi", "ngu ngốc", "forgot", "negligent", "incompetent",
                     "blame", "should have", "stupid", "careless"]
         if re.search(re.escape(w), pm, re.IGNORECASE)]
check(not blame, f"postmortem.md dùng ngôn ngữ blameless (từ cấm xuất hiện: {blame})")

# 5. ADR: >=2 phương án có ưu/nhược, >=2 hệ quả, tham chiếu 1 lỗ hổng -------
adr = read("ADR.md")
alt_block = adr.split("## Các phương án đã cân nhắc")[-1].split("## Hệ quả")[0]
n_alts = len(re.findall(r"^\s*-\s+\*\*[A-D]\.", alt_block, re.MULTILINE))
check(n_alts >= 2, f"ADR có >= 2 phương án (được {n_alts})")
n_uu = alt_block.lower().count("ưu:")
n_nhuoc = alt_block.lower().count("nhược:")
check(n_uu >= 2 and n_nhuoc >= 2,
      f"mỗi phương án ADR có ưu/nhược (ưu={n_uu}, nhược={n_nhuoc})")
cons_block = adr.split("## Hệ quả")[-1].split("## Tham chiếu")[0]
n_pos = cons_block.count("(+)")
n_neg = cons_block.count("(−)") + cons_block.count("(-)")
check(n_pos >= 1 and n_neg >= 1 and (n_pos + n_neg) >= 2,
      f"ADR có >= 2 hệ quả gồm >=1 tích cực và >=1 đánh đổi (+{n_pos} / -{n_neg})")
check("rca_observed.json" in adr, "ADR tham chiếu một lỗ hổng quan sát được (rca_observed.json)")

# 6. cost_model: schema + luật phán quyết + 3 ví dụ ------------------------
sys.path.insert(0, str(ROOT))
try:
    from cost_model import is_worth_it
    r = is_worth_it(100, 5, 2, 20_000, aiops_monthly_cost=25_000)
    schema = {"monthly_value", "monthly_cost", "roi", "payback_months", "verdict"}
    check(schema <= set(r), f"is_worth_it trả về đúng schema (được {set(r)})")
    check(is_worth_it(20, 2, 1, 10_000)["verdict"] == "not_worth_it"
          and r["verdict"] == "worth_it"
          and is_worth_it(1, 1, 1, 1, aiops_monthly_cost=1_000_000)["verdict"] == "not_worth_it",
          "ngưỡng phán quyết is_worth_it đúng (worth_it / not_worth_it)")
    cm = read("cost_model.py")
    n_ex = cm.count("is_worth_it(") - cm.count("def is_worth_it(")
    check(n_ex >= 3, f"cost_model.py có >= 3 ví dụ (được {n_ex})")
except Exception as exc:  # noqa: BLE001
    check(False, f"import/chạy cost_model: {exc}")

# 7. SPEC có 7 mục; SUBMIT có 5 mục ----------------------------------------
spec = read("SPEC.md")
n_spec = len(re.findall(r"^## \d", spec, re.MULTILINE))
check(n_spec >= 7, f"SPEC.md có 7 mục (được {n_spec})")
submit = read("SUBMIT.md")
n_sub = len(re.findall(r"^## ", submit, re.MULTILINE))
check(n_sub >= 5, f"SUBMIT.md có 5 mục (được {n_sub})")

# Báo cáo -------------------------------------------------------------------
print("\n=== Checklist nghiệm thu W3-D3 ===")
for ok, label in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
for w in warnings:
    print(f"  [warn] {w}")
passed = sum(1 for ok, _ in results if ok)
print(f"\n{passed}/{len(results)} kiểm tra đạt")
sys.exit(0 if passed == len(results) else 1)
