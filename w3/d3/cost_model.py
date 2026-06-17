"""Mô hình hoà vốn (break-even) cho nền tảng AIOps (W3-D3 §8).

Quyết định nền tảng AIOps có tự trả tiền cho nó không, bằng cách so giá trị của
phần downtime nó loại bỏ (nhờ MTTR nhanh hơn) với chi phí hằng tháng của nó.

    monthly_value = monthly_downtime_hours
                    * expected_mttr_reduction_pct
                    * downtime_cost_per_hour
    roi           = monthly_value / aiops_monthly_cost

Phán quyết:  roi > 1.5 -> worth_it ;  1.0 < roi <= 1.5 -> marginal ;  còn lại -> not_worth_it

Chạy:  python cost_model.py
"""
from __future__ import annotations

import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # in được tiếng Việt trên console Windows (cp1252)


def is_worth_it(
    num_services: int,
    incidents_per_month: int,
    avg_incident_duration_hours: float,
    downtime_cost_per_hour: float,
    expected_mttr_reduction_pct: float = 0.4,
    aiops_monthly_cost: float = 15_000,
) -> dict:
    """Trả về phán quyết hoà vốn AIOps cho một môi trường.

    Trả về:
      {
        "monthly_value": float,
        "monthly_cost": float,
        "roi": float,
        "payback_months": float,   # float('inf') khi không tạo ra giá trị
        "verdict": "worth_it" | "marginal" | "not_worth_it"
      }

    num_services nhận vào để hoàn chỉnh interface / dùng cho trọng số tương lai;
    bản thân điểm hoà vốn do số lượng sự cố, thời lượng và chi phí downtime quyết định.
    """
    monthly_downtime_hours = incidents_per_month * avg_incident_duration_hours
    monthly_value = (
        monthly_downtime_hours
        * expected_mttr_reduction_pct
        * downtime_cost_per_hour
    )
    roi = monthly_value / aiops_monthly_cost if aiops_monthly_cost > 0 else float("inf")
    payback_months = (
        aiops_monthly_cost / monthly_value if monthly_value > 0 else float("inf")
    )

    if roi > 1.5:
        verdict = "worth_it"
    elif roi > 1.0:
        verdict = "marginal"
    else:
        verdict = "not_worth_it"

    return {
        "monthly_value": round(monthly_value, 2),
        "monthly_cost": float(aiops_monthly_cost),
        "roi": round(roi, 3),
        "payback_months": round(payback_months, 3) if payback_months != float("inf") else float("inf"),
        "verdict": verdict,
    }


def _show(label: str, result: dict) -> None:
    print(f"{label}\n  {json.dumps(result)}\n")


if __name__ == "__main__":
    # Kịch bản 1 (note §8.4): quá ít sự cố để biện minh cho chi phí.
    _show(
        "20 dịch vụ, 2 sự cố/tháng x 1h, $10k/h, AIOps $15k",
        is_worth_it(num_services=20, incidents_per_month=2,
                    avg_incident_duration_hours=1, downtime_cost_per_hour=10_000,
                    aiops_monthly_cost=15_000),
    )

    # Kịch bản 2 (note §8.4): đúng quy mô + chi phí downtime thực.
    _show(
        "100 dịch vụ, 5 sự cố/tháng x 2h, $20k/h, AIOps $25k",
        is_worth_it(num_services=100, incidents_per_month=5,
                    avg_incident_duration_hours=2, downtime_cost_per_hour=20_000,
                    aiops_monthly_cost=25_000),
    )

    # Kịch bản 3 (của tôi): nhà cung cấp edge/CDN/WAF toàn cầu -- đúng lĩnh vực
    # của sự cố được tái hiện.
    #
    # downtime_cost_per_hour = $500,000. Biện minh: một nhà cung cấp edge/CDN đứng
    # trước lưu lượng tạo doanh thu của *nhiều doanh nghiệp khác*, nên một giờ edge
    # suy giảm toàn cầu cộng dồn downtime của tất cả họ. ITIC 2024 đặt downtime ứng
    # dụng trọng yếu của một doanh nghiệp lớn trên $300k/h; một tầng edge đứng trước
    # hàng nghìn ứng dụng như vậy nằm gọn trong dải quy mô streaming (note §8.2:
    # ~$500k/h). Sự cố Cloudflare 2019 đánh ~82% lưu lượng trong 27 phút -- không
    # phải tình huống giả định ở đuôi phân phối.
    #
    # 300 dịch vụ, 4 sự cố/tháng, 0.5h trung bình (phát hiện chín nên sự cố ngắn),
    # AIOps $40k (observability đa-vùng lớn + on-call).
    _show(
        "CỦA TÔI: 300 dịch vụ, 4 sự cố/tháng x 0.5h, $500k/h (edge/CDN), AIOps $40k",
        is_worth_it(num_services=300, incidents_per_month=4,
                    avg_incident_duration_hours=0.5, downtime_cost_per_hour=500_000,
                    aiops_monthly_cost=40_000),
    )
