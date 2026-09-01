from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_fast_structural_feasibility import build_report  # noqa: E402


def test_funnel_capacity_and_inaccessible_latency_freeze() -> None:
    records = []
    for index in range(20):
        candidate_id, bundle_id = f"candidate-{index}", f"bundle-{index}"
        records.append({
            "event_type": "CANDIDATE", "strategy": "FAST_STRUCTURAL",
            "record_id": f"candidate-record-{index}", "candidate_id": candidate_id,
            "bundle_id": bundle_id,
            "metadata": {"structured_legs": [{"leg_id": "yes"}, {"leg_id": "no"}]},
        })
        records.append({
            "event_type": "OPPORTUNITY", "strategy": "FAST_STRUCTURAL",
            "record_id": f"opportunity-{index}", "candidate_id": candidate_id,
            "bundle_id": bundle_id,
            "metadata": {"fast_structural_feasibility": {
                "structurally_valid": True, "full_depth_positive": True,
                "positive_after_fees": True, "positive_after_latency": True,
                "q_star": 5.0, "tau_star_ms": 50.0,
                "capacity_curve": [{"quantity": 5.0, "net_after_latency": 0.1}],
            }},
        })
        for leg in ("yes", "no"):
            records.append({
                "event_type": "FILL", "strategy": "FAST_STRUCTURAL",
                "record_id": f"fill-{index}-{leg}", "bundle_id": bundle_id,
                "leg_id": leg,
            })
        records.append({
            "event_type": "FINAL", "strategy": "FAST_STRUCTURAL",
            "record_id": f"final-{index}", "bundle_id": bundle_id,
        })
    report = build_report(
        records, {"fail_closed": False, "records": len(records)},
        p99_latency_ms=100.0,
    )
    assert report["funnel"] == {
        "detected": 20, "structurally_valid": 20,
        "full_depth_positive": 20, "positive_after_fees": 20,
        "positive_after_latency": 20, "all_legs_filled": 20, "terminal": 20,
    }
    assert report["capacity"]["q_star_p50"] == 5.0
    assert report["latency"]["p99_exceeds_tau_star_fraction"] == 1.0
    assert report["freeze_recommended"] is True
    assert report["promotion_eligible"] is False
    assert report["automatic_strategy_state_change"] is False


if __name__ == "__main__":
    test_funnel_capacity_and_inaccessible_latency_freeze()
