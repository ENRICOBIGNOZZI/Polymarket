from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MONITORING = ROOT / "monitoring"
if str(MONITORING) not in sys.path:
    sys.path.insert(0, str(MONITORING))

import exporter_v7 as exporter  # noqa: E402


def test_canonical_exporter_emits_pure_arb_metrics() -> None:
    status = {
        "schema": "polymarket_v7_pure_arb_paper_status_v1",
        "state": "running",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "cycles_total": 3,
        "paper_locked_pnl_pre_gas_total": 1.25,
        "conservative_locked_pnl_after_reserve_total": 1.10,
        "reserve_per_share": 0.0005,
        "maximum_leg_skew_ms": 100,
        "maximum_receive_to_decision_ns": 50_000_000,
        "active_contexts": 30,
        "subscribed_contexts": 42,
        "preloaded_contexts": 12,
        "subscribed_fee_ready_contexts": 42,
        "evaluations": 100,
        "fee_blocked_evaluations": 2,
        "fee_ready_contexts": 30,
        "last_decision_compute_ns": 1200,
        "max_decision_compute_ns": 9000,
        "last_receive_to_decision_ns": 18000,
        "max_receive_to_decision_ns": 42000,
        "latency_window_samples": 100,
        "receive_to_decision_ns": {"p50":18000,"p90":25000,"p99":40000,"p99_9":41000,"max":42000},
        "decision_compute_ns": {"p50":1000,"p90":1500,"p99":8000,"p99_9":8500,"max":9000},
        "funnel": {"book_updates":150,"book_valid":120,"buy_raw_positive":4,"buy_after_reserve_positive":2},
        "contexts": [{
            "asset": "BTC",
            "horizon": "M5",
            "active_window": True,
            "buy_complete_set": {
                "active": True,
                "cycles": 2,
                "paper_locked_pnl_pre_gas": 1.0,
                "conservative_locked_pnl_after_reserve": 0.9,
                "last_edge_per_share": 0.002,
                "max_edge_per_share": 0.003,
                "last_executable_shares_l1": 12.0,
                "last_executable_shares_l10": 20.0,
                "last_locked_pnl_pre_gas": 0.024,
                "last_conservative_locked_pnl_after_reserve": 0.018,
            },
            "sell_complete_set": {
                "active": False,
                "cycles": 1,
                "paper_locked_pnl_pre_gas": 0.25,
                "conservative_locked_pnl_after_reserve": 0.2,
                "last_edge_per_share": -0.001,
                "max_edge_per_share": 0.001,
                "last_executable_shares_l1": 5.0,
                "last_executable_shares_l10": 8.0,
                "last_locked_pnl_pre_gas": 0.005,
                "last_conservative_locked_pnl_after_reserve": 0.003,
            },
        }],
    }
    rendered = "\n".join(exporter._render_pure_arb_metrics(status))
    assert "polymarket_pure_arb_up 1" in rendered
    assert "polymarket_pure_arb_cycles_total 3" in rendered
    assert "polymarket_pure_arb_paper_locked_pnl_pre_gas_usd_total 1.25" in rendered
    assert 'asset="BTC"' in rendered
    assert 'horizon="M5"' in rendered
    assert 'kind="BUY_COMPLETE_SET"' in rendered
    assert "polymarket_pure_arb_context_last_edge_per_share" in rendered
    assert "polymarket_pure_arb_context_last_executable_shares_l10" in rendered
    assert 'stage="buy_after_reserve_positive"' in rendered
    assert 'kind="receive_to_decision"' in rendered
    assert "polymarket_pure_arb_preloaded_contexts 12" in rendered


def test_unsafe_status_never_exports_context_economics() -> None:
    status = {
        "schema": "polymarket_v7_pure_arb_paper_status_v1",
        "state": "running",
        "paper_only": False,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "contexts": [{
            "asset": "BTC", "horizon": "M5",
            "buy_complete_set": {"active": True, "cycles": 99},
        }],
    }
    rendered = "\n".join(exporter._render_pure_arb_metrics(status))
    assert "polymarket_pure_arb_up 0" in rendered
    assert "polymarket_pure_arb_context_cycles_total" not in rendered


def test_frequency_expansion_shadow_metrics_are_fail_closed() -> None:
    settlement = {
        "schema": "polymarket_v7_settlement_source_arb_status_v2",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "state": "COLLECTING", "cycles": 6, "paper_locked_arbitrages": 2,
        "locked_pnl": 0.4, "pending_outcomes": 1, "cached_markets": 12,
        "states": {"PAPER_LOCKED_ARBITRAGE": 2, "NO_TRADE_EDGE": 4},
        "by_kind": {
            "BUY_WINNER": {"cycles": 3, "paper_locked_arbitrages": 1, "locked_pnl": 0.3},
            "SELL_LOSER": {"cycles": 3, "paper_locked_arbitrages": 1, "locked_pnl": 0.1},
        },
    }
    maker = {
        "schema": "polymarket_v7_two_sided_complete_set_shadow_status_v2",
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "real_capital_at_risk": False,
        "state": "COLLECTING", "cycles": 10, "active_cycles": 3,
        "paired_fill_probability_direct": 0.2,
        "both_any_probability_direct": 0.4,
        "one_leg_probability_direct": 0.3,
        "total_shadow_pnl": 1.5, "mean_total_shadow_pnl": 0.15,
        "total_legging_loss": 0.4, "mean_legging_loss": 0.04,
        "states": {"BOTH_FULL": 2, "YES_ONLY": 2},
        "by_context": {"BTC:M5": {"BOTH_FULL": 2, "YES_ONLY": 1}},
    }
    rendered = "\n".join(
        exporter._render_settlement_source_arb_metrics(settlement)
        + exporter._render_complete_set_maker_metrics(maker)
    )
    assert "polymarket_settlement_source_arb_up 1" in rendered
    assert 'kind="BUY_WINNER"' in rendered
    assert "polymarket_complete_set_maker_shadow_up 1" in rendered
    assert "polymarket_complete_set_maker_paired_fill_probability 0.2" in rendered
    assert 'state="BOTH_FULL"' in rendered

    maker["paper_only"] = False
    unsafe = "\n".join(exporter._render_complete_set_maker_metrics(maker))
    assert "polymarket_complete_set_maker_shadow_up 0" in unsafe
    assert "polymarket_complete_set_maker_state_total" not in unsafe


def test_unified_exact_graph_metrics_are_zero_authority_only() -> None:
    status={"schema":"polymarket_v7_unified_exact_arb_graph_shadow_status_v1","state":"COLLECTING",
            "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
            "real_capital_at_risk":False,"automatic_promotion":False,
            "execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY","relations_compiled":4,
            "relations_evaluated":12,"funnel":{"candidate_emitted":2,"books_ready":5},"rejection_reasons":{"truncated_depth":3},
            "funnel_by_family":{"SAME_MARKET_BINARY_COMPLETE_SET":{"books_ready":5}},
            "evaluation_latency_us":{"p99":4.0},"graph_generation_compiled_at_ms":1}
    rendered="\n".join(exporter._render_unified_exact_arb_graph_metrics(status))
    assert "exact_arb_graph_up 1" in rendered and "exact_arb_candidates_total 2" in rendered
    assert "exact_arb_graph_family_funnel_total" in rendered and "exact_arb_graph_evaluation_latency_microseconds" in rendered
    assert "exact_arb_graph_generation_age_seconds" in rendered
    status["real_order_submission"]=True
    assert "exact_arb_graph_up 0" in "\n".join(exporter._render_unified_exact_arb_graph_metrics(status))


def test_unified_graph_exports_near_arb_distribution() -> None:
    status={"schema":"polymarket_v7_unified_exact_arb_graph_shadow_status_v1","paper_only":True,
      "authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,
      "automatic_promotion":False,"execution_authority":"ZERO_AUTHORITY_RESEARCH_ONLY",
      "near_arbitrage":{"SAME_MARKET_BINARY_COMPLETE_SET:distance_to_raw_arbitrage":{"min":-.01,"p50":.002}}}
    rendered="\n".join(exporter._render_unified_exact_arb_graph_metrics(status))
    assert "exact_arb_distance_to_arbitrage" in rendered and 'percentile="p50"' in rendered


def test_exchange_universe_metrics_are_zero_authority_only() -> None:
    status={"schema":"polymarket_v7_exact_arb_exchange_universe_status_v1","state":"READY",
      "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
      "real_capital_at_risk":False,"automatic_promotion":False,"execution_authority":False,
      "events":120,"markets":900,"negrisk_events":20,"verified_negrisk_events":12,
      "discovery_exhaustive":True,"pagination_loop_guard_hit":False,"scan_duration_ms":123.0}
    rendered="\n".join(exporter._render_exact_arb_exchange_universe_metrics(status))
    assert "exact_arb_exchange_universe_up 1" in rendered
    assert "exact_arb_exchange_universe_verified_negrisk_events 12" in rendered
    status["real_order_submission"]=True
    assert "exact_arb_exchange_universe_up 0" in "\n".join(
        exporter._render_exact_arb_exchange_universe_metrics(status))


def test_warm_screen_metrics_cannot_claim_actionable_candidates() -> None:
    status={"schema":"polymarket_v7_exact_arb_warm_screen_status_v1","state":"SCREENING",
      "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
      "real_capital_at_risk":False,"automatic_promotion":False,"execution_authority":False,
      "evidence_quality":"NONATOMIC_PUBLIC_REST_SCREEN_ONLY","actionable_candidates":0,
      "relations_selected":30,"relations_screened":29,"tokens_requested":80,"books_missing":1,
      "raw_positive_screen_only":3,"after_fee_positive_screen_only":2,
      "after_reserve_positive_screen_only":1,"hotset_relations":20,"hotset_tokens":50,
      "scan_duration_ms":55.0,"relations_by_family":{"NEGRISK_COMPLETE_SET":4},
      "near_arbitrage":{"after_reserve":{"min":-.01,"p50":.02}}}
    rendered="\n".join(exporter._render_exact_arb_warm_screen_metrics(status))
    assert "exact_arb_warm_screen_up 1" in rendered
    assert "exact_arb_warm_after_reserve_positive_screen_only_total 1" in rendered
    assert 'family="NEGRISK_COMPLETE_SET"' in rendered
    status["actionable_candidates"]=1
    assert "exact_arb_warm_screen_up 0" in "\n".join(
        exporter._render_exact_arb_warm_screen_metrics(status))


def test_hotset_selection_and_causal_observer_metrics_are_separate_from_champion() -> None:
    selection={"schema":"polymarket_v7_exact_arb_hotset_selection_status_v1","state":"READY",
      "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
      "real_capital_at_risk":False,"automatic_promotion":False,"execution_authority":False,
      "selected_relations":12,"selected_markets":20,"selected_tokens":40}
    rendered="\n".join(exporter._render_exact_arb_hotset_selection_metrics(selection))
    assert "exact_arb_hotset_selection_up 1" in rendered
    assert "exact_arb_hotset_selected_markets 20" in rendered

    observer={"schema":"polymarket_v7_maker_fillability_ws_status_v1","state":"running",
      "paper_only":True,"authenticated_execution":False,"real_order_submission":False,
      "pure_arb_paper_enabled":False,"graph_continuous_deep_evidence":True,
      "subscribed_markets":20,"subscribed_tokens":40,"observed_markets":20,"observed_tokens":40,
      "subscription_coverage_complete":True,"feed_workers":1,"feed_connected_workers":1,
      "feed_messages":100,"feed_reconnects":0,"feed_errors":0,"dropped_events":0,
      "decoder_failures":0,"pure_arb_deep_snapshots_written":20,"pure_arb_deep_queue_drops":0}
    rendered="\n".join(exporter._render_exact_arb_hotset_observer_metrics(observer))
    assert "exact_arb_hotset_observer_up 1" in rendered
    assert "exact_arb_hotset_observer_subscription_coverage_complete 1" in rendered
    observer["pure_arb_paper_enabled"]=True
    assert "exact_arb_hotset_observer_up 0" in "\n".join(
        exporter._render_exact_arb_hotset_observer_metrics(observer))


def test_unified_graph_execution_metrics_remain_zero_authority_only() -> None:
    status={"schema":"polymarket_v7_pure_arb_exchange_execution_status_v1","state":"COLLECTING",
      "paper_only":True,"authenticated_execution":False,"real_order_submission":False,"real_capital_at_risk":False,
      "execution_authority":"ZERO_AUTHORITY_EXCHANGE_EXECUTION_SHADOW","evaluated":3,
      "counterfactual_only_scenarios":2,"states":{"COMPLETE_PAIRED":1,"ONE_LEG_UNWOUND":1},
      "by_execution_mode":{"SEQUENTIAL":{"paired":1,"one_leg_unwound":1,"sum_pnl_after_reserve":.2}}}
    rendered="\n".join(exporter._render_unified_exact_arb_graph_execution_metrics(status))
    assert "exact_arb_graph_execution_shadow_up 1" in rendered
    assert "exact_arb_graph_counterfactual_one_leg_unwound_total" in rendered
    status["real_order_submission"]=True
    assert "exact_arb_graph_execution_shadow_up 0" in "\n".join(exporter._render_unified_exact_arb_graph_execution_metrics(status))


if __name__ == "__main__":
    test_canonical_exporter_emits_pure_arb_metrics()
    test_unsafe_status_never_exports_context_economics()
    test_frequency_expansion_shadow_metrics_are_fail_closed()
