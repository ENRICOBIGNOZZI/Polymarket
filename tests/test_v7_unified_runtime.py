#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_research_branch_keeps_incumbent_manifest_and_defines_v7_candidate():
    incumbent = load("config/live_champion.json")
    candidate = load("config/v7_champion_candidate.json")
    assert incumbent["version"] == 6
    assert incumbent["loop"] == "scripts/paper_v6_loop.sh"
    assert candidate["version"] == 7
    assert candidate["loop"] == "scripts/paper_v7_loop.sh"
    assert candidate["config"] == "config/paper_v7.json"
    assert candidate["paper_only"] is True
    assert candidate["authenticated_execution"] is False
    assert candidate["promotion_contract"] == "research_head_must_be_exact-head-approved_then_integrated"


def test_v7_config_matches_authorized_aggressive_paper_envelope():
    cfg = load("config/paper_v7.json")
    assert cfg["engine_version"] == 7
    assert cfg["paper_only"] is True
    assert cfg["market_limit"] == 1000
    assert cfg["min_liquidity"] == 2.0
    assert abs(cfg["min_net_edge"] - 0.00005) < 1e-12
    assert cfg["uncertainty_penalty"] == 0.0
    assert cfg["fractional_kelly"] == 0.25
    assert cfg["fixed_dollar_trade_cap_enabled"] is True
    assert cfg["max_trade_usd"] == 125.0
    assert cfg["max_market_fraction"] == 0.05
    assert cfg["max_event_fraction"] == 0.15
    assert cfg["max_gross_fraction"] == 0.70
    assert cfg["multi_strategy"]["global_max_gross_fraction"] == 0.70
    assert cfg["max_drawdown"] == 0.15
    assert cfg["v7"]["hard_arb_fixed_dollar_trade_cap_enabled"] is True
    assert cfg["v7"]["hard_arb_max_trade_usd"] == 125.0
    assert cfg["v7"]["authoritative_fee_required"] is True
    assert cfg["v7"]["shared_execution_ledger_required"] is True
    assert cfg["v7"]["joint_fill_state_required_for_multileg"] is True
    assert cfg["v7"]["authenticated_execution"] is False


def test_frequency_matrix_has_hf_and_30m_to_6h_without_pooling():
    cfg = load("config/v7_frequency_matrix.json")
    maker = cfg["execution_cadences_seconds"]["micro_maker"]
    assert min(maker) <= 1
    assert max(maker) >= 10
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
    text = (ROOT / "scripts/paper_v7_loop.sh").read_text(encoding="utf-8")
    assert "runtime_singleton_launcher.py" in selector
    assert "runtime_owner.lock" in selector
    assert "runtime_handoff.request" in selector
    assert "request_runtime_handoff()" in updater
    assert "clear_runtime_handoff()" in updater
    assert "runtime_singleton_launcher.py" not in text
    assert "runtime_owner.lock" not in text
    assert "paper_v7_execution_loop.sh" in text
    assert "paper_v6_loop.sh" not in text
    assert "v7_shadow_loop.py" in text
    assert "POLYMARKET_RUNTIME_PARENT_PID=\"$$\"" in text
    assert text.count("start_execution") >= 2


def test_v7_execution_loop_uses_corrected_workers_and_prospective_joint_state_gate():
    text = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
    assert "v7_micro_maker_worker.py" in text
    assert "v7_micro_taker_worker.py" in text
    assert "v7_multileg_broker_runner.py" in text
    assert "v7_capacity_lock.py" in text
    assert "v6_bundle_quote_optimizer.py" in text  # compatibility adapter -> V7 optimizer
    assert "v7_graph_forward_guard.py" in text
    assert "v6_bundle_state_guard.py" not in text
    assert "polymarket_multileg_paper" not in text
    assert "runtime_primary_seconds" in text
    assert "sleep 1" in text


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
    print(f"ok {len(tests)} unified V7 runtime tests")
