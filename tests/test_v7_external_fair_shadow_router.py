#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_external_fair_shadow_router import candidates, fee_per_share  # noqa: E402


def snapshot() -> dict:
    return {
        "code_sha": "a" * 40, "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False,
        "contract": {"verified": True, "rules_hash_recognized": True},
        "settlement_reference": {"valid": True}, "oracle": {"healthy": True},
        "external": {"healthy": True},
        "fair": {"valid": True, "lower": 0.75, "upper": 0.80},
        "market": {"yes_token": "yes", "no_token": "no", "best_bid": 0.49,
                   "best_ask": 0.50, "fee_schedule": {"rate": 0.07, "exponent": 1}},
    }


def main() -> None:
    assert abs(fee_per_share(0.5, {"rate": 0.07, "exponent": 1}) - 0.0175) < 1e-12
    rows = candidates(snapshot())
    assert len(rows) == 1 and rows[0]["outcome"] == "YES"
    assert rows[0]["robust_ev"] > 0.20
    invalid = snapshot(); invalid["fair"]["valid"] = False
    assert candidates(invalid) == []
    unsafe = snapshot(); unsafe["real_order_submission"] = True
    assert candidates(unsafe) == []
    source = (ROOT / "scripts" / "v7_external_fair_shadow_router.py").read_text()
    assert 'event_type="CANDIDATE"' in source
    assert 'event_type="ORDER_SUBMITTED"' not in source
    assert 'event_type="FILL"' not in source


if __name__ == "__main__":
    main()
