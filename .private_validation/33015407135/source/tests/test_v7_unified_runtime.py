#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_research_manifest_keeps_incumbent_until_v7_promotion_and_defines_exact_candidate():
    incumbent = load("config/live_champion.json")
    candidate = load("config/v7_champion_candidate.json")
    assert incumbent["version"] in {6, 7}
    assert candidate["version"] == 7
    assert candidate["loop"] == "scripts/paper_v7_loop.sh"
    assert candidate["config"] == "config/paper_v7.json"
    assert candidate["run_root"] == "runs/paper_v7_live"
    assert candidate["paper_only"] is True
    assert candidate["authenticated_execution"] is False
    assert candidate["promotion_contract"] == "research_head_must_be_exact-head-approved_then_integrated"


def test_v7_config_has_no_binding_fixed_dollar_trade_cap_and_100_percent_hard_ceiling():
    cfg = load("config/paper_v7.json")
    directives = load("config/operator_directives.json")["paper_v7_authorization"]
    assert cfg["engine_version"] == 7
    assert cfg["paper_only"] is True
    assert cfg["market_limit"] == directives["market_limit"]
    assert cfg["min_liquidity"] == directives["min_liquidity"]
    assert abs(cfg["min_net_edge"] - directives["min_net_edge"]) < 1e-12
    assert cfg["fractional_kelly"] == directives["fractional_kelly_ceiling"]
    assert cfg["fixed_dollar_trade_cap_enabled"] is False
    assert float(cfg["max_trade_usd"]) > 1e50
    for key in ("max_trade_fraction", "max_market_fraction", "max_event_fraction", "max_gross_fraction"):
        assert cfg[key] == directives[key] == 1.0
    assert cfg["multi_strategy"]["global_max_gross_fraction"] == 1.0
    assert cfg["max_drawdown"] == directives["max_drawdown"] == 0.15
    assert cfg["v7"]["hard_arb_fixed_dollar_trade_cap_enabled"] is False
    assert float(cfg["v7"]["hard_arb_max_trade_usd"]) > 1e50
    assert cfg["v7"]["hard_arb_max_trade_fraction"] == 1.0
    assert cfg["v7"]["authoritative_fee_required"] is True
    assert cfg["v7"]["shared_execution_ledger_required"] is True
    assert cfg["v7"]["joint_fill_state_required_for_multileg"] is True
    assert cfg["v7"]["authenticated_execution"] is False


def test_frequency_matrix_has_hf_and_30m_to_6h_without_pooling():
    cfg = load("config/v7_frequency_matrix.json")
    maker = cfg["execution_cadences_seconds"]["micro_maker"]
    assert min(maker) <= 1 and max(maker) >= 10
    assert cfg["forecast_horizons_minutes"]["pca_stat_arb"] == [30, 60, 120, 360]
    assert cfg["forecast_horizons_minutes"]["cross_sectional_rank"] == [30, 60, 120, 360]
    assert cfg["local_factor_fidelity_minutes"] == [30, 60]
    rules = cfg["evidence_rules"]
    assert rules["separate_state_by_frequency"] is True
    assert rules["separate_pnl_by_frequency"] is True
    assert rules["no_pooling_across_horizons_for_pvalues"] is True
    assert rules["no_post_hoc_frequency_selection_on_same_holdout"] is True


def test_v7_entrypoint_has_one_execution_owner_and_separate_shadow_scheduler():
    selector = (ROOT / "scripts/paper_latest_loop.sh").read_text(encoding="utf-8")
    updater = (ROOT / "ops/update_server_macos.sh").read_text(encoding="utf-8")
    singleton = (ROOT / "scripts/runtime_singleton_launcher.py").read_text(encoding="utf-8")
    text = (ROOT / "scripts/paper_v7_loop.sh").read_text(encoding="utf-8")
    assert "runtime_singleton_launcher.py" in selector
    assert "runtime_owner.lock" in selector
    assert "runtime_handoff.request" in selector
    assert "request_runtime_handoff()" in updater
    assert "clear_runtime_handoff()" in updater
    assert "_drain_child_group" in singleton
    assert "start_new_session=True" in singleton
    assert "runtime_singleton_launcher.py" not in text
    assert "runtime_owner.lock" not in text
    assert "paper_v7_execution_loop.sh" in text
    assert "v7_shadow_loop.py" in text
    assert "POLYMARKET_RUNTIME_PARENT_PID=\"$$\"" in text
    assert text.count("start_execution") >= 2


def test_v7_execution_loop_is_canonical_and_has_no_legacy_runtime_calls():
    text = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    required = (
        "v7_market_proxy.py",
        "v7_micro_maker_worker.py",
        "v7_micro_taker_worker.py",
        "v7_hard_arb_guard.py",
        "v7_multileg_broker_runner.py",
        "v7_capacity_lock.py",
        "v7_relation_intents.py",
        "v7_intent_guard.py",
        "v7_bundle_quote_optimizer.py",
        "v7_graph_roundtrip_guard.py",
        "v7_merge_intents.py",
        "v7_external_bridge.py",
        "v7_runtime_status.py",
        "graph_roundtrip_state.json",
        "runtime_primary_seconds",
        "sleep 1",
    )
    for marker in required:
        assert marker in text, marker
    forbidden = (
        "v3_", "v4_", "v5_", "v6_",
        "paper_v3", "paper_v4", "paper_v5", "paper_v6",
        "merge_v4_intents.py",
        "polymarket_multileg_paper",
    )
    for marker in forbidden:
        assert marker not in text, marker


def test_shadow_scheduler_is_research_only_and_frequency_separated():
    text = (ROOT / "scripts/v7_shadow_loop.py").read_text(encoding="utf-8")
    assert "v7_pca_stat_arb_research.py" in text
    assert "v7_local_factor_research.py" in text
    assert "research_v7_local_factor_60m.json" in text
    assert "v7_cross_sectional_rank_forward_multifreq.py" in text
    assert "v7_hf_frequency_probe.py" in text
    assert "authenticated_execution\": False" in text
    assert "len(active) >= 2" in text


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print(f"ok {len(tests)} unified V7 research/runtime tests")
