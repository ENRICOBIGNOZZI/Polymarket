"""Synthetic historical receipt fixtures. No execution owner."""
from test_v7_opportunity import envelope

def forward_envelope(key: str = "lead-lag-forward") -> dict:
    value = envelope(action="TAKE", ev=0.0, key=key, authority="PAPER_EXPLORATION")
    value["uncertainty"] = {"lower_bound": 0.0, "upper_bound": 1.0, "status": "IMMATURE"}
    value["calibration_status"] = "NOT_APPLICABLE"
    value["latency"]["profile_valid"] = False
    value["fair_value"] = {"lower": 0.0, "point": 0.55, "upper": 1.0}
    value["forward_test"] = {
        "mode": "PAPER_FORWARD_TEST", "strategy_id": "LEAD_LAG_TAKER_V1",
        "protocol_hash": "f" * 64, "research_only": True, "automatic_promotion": False,
        "one_entry_per_market": True, "hold_to_settlement": True,
        "entry_uses_absolute_fair": False, "probability_source": "POLYMARKET_PRIOR_ONLY",
    }
    return value
