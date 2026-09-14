import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_selective_pnl_challenger", ROOT / "scripts" / "v7_selective_pnl_challenger.py"
)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MOD)
CONFIG = json.loads((ROOT / "config" / "v7_selective_pnl_challenger.json").read_text())


def packet(*, make_ev=0.02, fill_lower=0.1, toxic_upper=0.2, mature=True):
    return {
        "evidence_status": "MATURE" if mature else "IMMATURE",
        "features": {name: 1.0 for name in MOD.EXECUTION_FEATURES},
        "fill_probability": {"lower": fill_lower, "point": max(fill_lower, 0.2), "upper": 0.4},
        "toxic_fill_probability": {"lower": 0.05, "point": min(toxic_upper, 0.15), "upper": toxic_upper},
        "action_ev": {
            "MAKE": {"point": max(make_ev, 0.03), "conservative": make_ev},
            "TAKE": {"point": 0.0, "conservative": 0.0},
            "CANCEL": {"point": 0.0, "conservative": 0.0},
            "NOTHING": {"point": 0.0, "conservative": 0.0},
        },
    }


def directional(**overrides):
    row = {
        "kind": "DIRECTIONAL",
        "side": "YES",
        "model_probability": 0.60,
        "best_ask": 0.55,
        "fee_per_share": 0.01,
        "risk_allowance_per_share": 0.005,
        "visible_depth_shares": 100.0,
        "decision_to_arrival_ms": 100.0,
        "arrival_revalidated": True,
        "contract_verified": True,
        "external_features_fresh": True,
        "model_mature": True,
        "book_complement_consistent": True,
        "tte_seconds": 200.0,
        "external_disagreement_bp": 3.0,
        "external_shock_abs_100ms_bp": 0.5,
        "spread_ticks": 1.0,
        "volatility_ticks": 0.5,
        "oracle_distance_bp": 3.0,
        "book_imbalance": 0.2,
    }
    row.update(overrides)
    return row


def test_config_is_zero_authority_and_frozen():
    MOD.validate_config(CONFIG)
    assert CONFIG["paper_only"] is True
    assert CONFIG["authenticated_execution"] is False
    assert CONFIG["real_order_submission"] is False
    assert CONFIG["automatic_promotion"] is False
    assert CONFIG["maker_gate"]["quote_everywhere"] is False
    assert CONFIG["directional_lane"]["primary_minimum_net_edge_after_2x_cost_per_share"] == 0.01


def test_external_cancel_preempts_maker():
    result = MOD.maker_shadow_decision({
        "external_cancel_state": "ACTIVE",
        "active_order": True,
        "market_retained": True,
        "execution_alpha": packet(),
    }, CONFIG)
    assert result["action"] == "CANCEL_SHADOW"
    assert result["eligible"] is False


def test_unknown_cancel_state_fails_closed():
    result = MOD.maker_shadow_decision({
        "external_cancel_state": "UNKNOWN",
        "active_order": False,
        "market_retained": True,
        "execution_alpha": packet(),
    }, CONFIG)
    assert result["action"] == "WITHDRAW_SHADOW"
    assert "EXTERNAL_CANCEL_STATE_UNKNOWN" in result["reasons"]


def test_maker_requires_positive_fill_adjusted_conservative_edge_and_low_toxicity():
    good = MOD.maker_shadow_decision({
        "external_cancel_state": "CLEAR", "market_retained": True,
        "book_complement_consistent": True,
        "execution_alpha": packet(make_ev=0.01, fill_lower=0.05, toxic_upper=0.20),
    }, CONFIG)
    toxic = MOD.maker_shadow_decision({
        "external_cancel_state": "CLEAR", "market_retained": True,
        "book_complement_consistent": True,
        "execution_alpha": packet(make_ev=0.01, fill_lower=0.05, toxic_upper=0.90),
    }, CONFIG)
    zero_ev = MOD.maker_shadow_decision({
        "external_cancel_state": "CLEAR", "market_retained": True,
        "book_complement_consistent": True,
        "execution_alpha": packet(make_ev=0.0, fill_lower=0.05, toxic_upper=0.20),
    }, CONFIG)
    assert good["action"] == "MAKE_SHADOW_ELIGIBLE"
    assert toxic["action"] == "WITHDRAW_SHADOW"
    assert "TOXICITY_TOO_HIGH" in toxic["reasons"]
    assert "MAKE_CONSERVATIVE_EV_NOT_POSITIVE" in zero_ev["reasons"]


def test_rollover_incoherence_blocks_new_maker_and_directional_risk():
    maker = MOD.maker_shadow_decision({
        "external_cancel_state": "CLEAR", "market_retained": True,
        "book_complement_consistent": False, "execution_alpha": packet(),
    }, CONFIG)
    directional_result = MOD.directional_shadow_decision(
        directional(book_complement_consistent=False), CONFIG
    )
    assert maker["action"] == "WITHDRAW_SHADOW"
    assert "BOOK_COMPLEMENT_NOT_CONFIRMED" in maker["reasons"]
    assert directional_result["action"] == "NOTHING_SHADOW"
    assert "BOOK_COMPLEMENT_NOT_CONFIRMED" in directional_result["reasons"]


def test_directional_primary_requires_one_cent_after_two_x_costs():
    # 0.60 - 0.55 - 2*(0.01+0.005) = 0.02/share.
    good = MOD.directional_shadow_decision(directional(), CONFIG)
    # 0.585 - 0.55 - 0.03 = 0.005/share: research arm only, not primary.
    weak = MOD.directional_shadow_decision(directional(model_probability=0.585), CONFIG)
    assert good["action"] == "TAKE_SHADOW_ELIGIBLE"
    assert abs(good["net_edge_after_2x_cost_per_share"] - 0.02) < 1e-12
    assert weak["action"] == "NOTHING_SHADOW"
    assert weak["threshold_grid"]["edge_ge_0.005"] is True
    assert weak["threshold_grid"]["edge_ge_0.01"] is False


def test_directional_fails_on_latency_depth_or_stale_external_features():
    slow = MOD.directional_shadow_decision(directional(decision_to_arrival_ms=251.0), CONFIG)
    shallow = MOD.directional_shadow_decision(directional(visible_depth_shares=4.99), CONFIG)
    stale = MOD.directional_shadow_decision(directional(external_features_fresh=False), CONFIG)
    assert "ENTRY_LATENCY_EXPIRED" in slow["reasons"]
    assert "INSUFFICIENT_VISIBLE_DEPTH" in shallow["reasons"]
    assert "EXTERNAL_FEATURES_NOT_FRESH" in stale["reasons"]


def test_latency_slo_and_regime_grid_are_deterministic():
    assert MOD.latency_gate([50, 80, 100, 120, 150], CONFIG)["pass"] is True
    assert MOD.latency_gate([100, 200, 300, 600, 1000], CONFIG)["pass"] is False
    regime = MOD.regime_key(directional(), CONFIG)
    assert regime == {
        "tte_seconds": "2",
        "external_disagreement_bp": "1",
        "external_shock_abs_100ms_bp": "1",
        "spread_ticks": "0",
        "volatility_ticks": "1",
        "oracle_distance_bp": "1",
        "book_imbalance": "2",
        "side": "YES",
    }


def test_report_never_grants_execution_authority():
    rows = [
        {"kind": "MAKER", "external_cancel_state": "CLEAR", "market_retained": True,
         "book_complement_consistent": True,
         "execution_alpha": packet(), "side": "YES"},
        directional(),
        {"kind": "LATENCY", "decision_to_arrival_ms": 100.0},
    ]
    report = MOD.evaluate_rows(rows, CONFIG)
    assert report["execution_authority"] == "ZERO_AUTHORITY_RESEARCH_ONLY"
    assert report["real_order_submission"] is False
    assert report["automatic_promotion"] is False
    assert report["actions"] == {"MAKE_SHADOW_ELIGIBLE": 1, "TAKE_SHADOW_ELIGIBLE": 1}
