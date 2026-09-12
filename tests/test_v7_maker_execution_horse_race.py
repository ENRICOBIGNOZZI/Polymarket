from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_maker_execution_horse_race as horse  # noqa: E402


def maker_rows(markout: float = -0.04, outcome: str = "YES", fill_ms: int = 2000, shares: float = 10.0):
    base = {
        "model_sha": "a" * 40,
        "order_id": "o1",
        "market_id": "m1",
        "event_id": "e1",
        "side": "BUY",
    }
    return [
        {
            **base,
            "record_id": "submit",
            "event_type": "ORDER_SUBMITTED",
            "recorded_ts_ms": 1000,
            "metadata": {
                "component": "professional_maker",
                "execution_semantics_version": horse.SEMANTICS,
                "placement_action": "JOIN",
                "outcome": outcome,
                "opportunity_envelope": {"crypto_context": {"asset": "BTC", "horizon": "M5"}},
            },
        },
        {
            **base,
            "record_id": "fill",
            "event_type": "FILL",
            "recorded_ts_ms": fill_ms,
            "filled_size": shares,
            "fill_id": "f1",
            "metadata": {"component": "professional_maker"},
        },
        {
            **base,
            "record_id": "mark",
            "event_type": "MARKOUT",
            "recorded_ts_ms": fill_ms + 1000,
            "fill_id": "f1",
            "markouts": {"1s": markout},
            "metadata": {"component": "professional_maker"},
        },
    ]


def fills(markout: float = -0.04):
    result, diagnostics = horse.build_fills(maker_rows(markout), markout_horizon="1s", action="JOIN")
    assert diagnostics["fills_with_markout"] == 1
    return result


def test_toxic_fill_saved_by_hard_cancel() -> None:
    events = horse.hard_events([{
        "market_id": "m1", "stale_buy_outcome": "YES",
        "publish_wall_ns": 1_500_000_000, "valid": True,
    }])
    summary, rows = horse.evaluate(fills(), events, 100_000_000)
    assert rows[0]["avoided"] is True
    assert abs(summary["net_markout_improvement_dollars"] - 0.4) < 1e-12
    assert abs(summary["net_improvement_per_baseline_share"] - 0.04) < 1e-12


def test_cancel_effective_at_fill_is_too_late() -> None:
    events = horse.hard_events([{
        "market_id": "m1", "stale_buy_outcome": "YES",
        "publish_wall_ns": 1_900_000_000, "valid": True,
    }])
    summary, rows = horse.evaluate(fills(), events, 100_000_000)
    assert rows[0]["avoided"] is False
    assert summary["net_markout_improvement_dollars"] == 0.0


def test_pre_order_signal_is_not_reused_as_placement_veto() -> None:
    events = horse.hard_events([{
        "market_id": "m1", "stale_buy_outcome": "YES",
        "publish_wall_ns": 900_000_000, "valid": True,
    }])
    _summary, rows = horse.evaluate(fills(), events, 0)
    assert rows[0]["avoided"] is False


def test_learned_shadow_side_orientation_and_zero_authority() -> None:
    shadow = [{
        "schema": horse.SHADOW_SCHEMA,
        "paper_only": True,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "family": "PM_PLUS_EXTERNAL",
        "horizon_ms": 250,
        "threshold_ticks": 1.0,
        "market_id": "m1",
        "origin_id": "x",
        "scored_wall_ns": 1_500_000_000,
        "would_veto_yes_buy": True,
        "would_veto_no_buy": False,
    }]
    events = horse.learned_events(shadow, family="PM_PLUS_EXTERNAL", horizon_ms=250, threshold=1.0)
    assert len(events) == 1 and events[0]["outcome"] == "YES"
    _summary, rows = horse.evaluate(fills(), events, 25_000_000)
    assert rows[0]["avoided"] is True


def test_avoiding_favorable_fill_is_counted_as_opportunity_cost() -> None:
    events = horse.hard_events([{
        "market_id": "m1", "stale_buy_outcome": "YES",
        "available_ns": 1_500_000_000, "valid": True,
    }])
    summary, _rows = horse.evaluate(fills(0.02), events, 0)
    assert abs(summary["forgone_favorable_markout_dollars"] - 0.2) < 1e-12
    assert abs(summary["net_markout_improvement_dollars"] + 0.2) < 1e-12


def test_global_hard_scope_requires_exact_crypto_scope_and_opt_in() -> None:
    events = horse.hard_events([{
        "stale_buy_outcome": "YES", "available_ns": 1_500_000_000,
        "asset": "BTC", "horizon": "M5", "valid": True,
    }])
    _summary, rows = horse.evaluate(fills(), events, 0, global_hard=False)
    assert rows[0]["avoided"] is False
    _summary, rows = horse.evaluate(fills(), events, 0, global_hard=True)
    assert rows[0]["avoided"] is True


def test_horse_race_is_zero_authority_and_hashes_fixed_anchors() -> None:
    hard = horse.hard_events([{
        "market_id": "m1", "stale_buy_outcome": "YES",
        "available_ns": 1_400_000_000, "valid": True,
    }])
    learned = [{"available_ns": 1_500_000_000, "market_id": "m1", "outcome": "YES", "signal_id": "l1"}]
    result = horse.race(
        fills(), hard, learned,
        latency_ms=25.0, bootstrap=20, seed=1, global_hard=False,
    )
    assert result["execution_authority"] == "ZERO_AUTHORITY_RESEARCH_ONLY"
    assert result["automatic_promotion"] is False
    assert result["both_policies_avoid_same_fill_count"] == 1
    assert len(result["fill_anchor_sha256"]) == 64
    assert result["results"]["HARD_EXTERNAL_CANCEL"]["event_clusters"] == 1


def test_runtime_launcher_includes_fill_conditioned_markout_evidence() -> None:
    launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
    start = launcher.index("python3 scripts/v7_maker_execution_horse_race.py")
    block = launcher[start:]
    block = block[:block.index("last_horse_race_at=", 1)]
    assert '--maker-evidence "$RUN_ROOT/ledger/execution.jsonl"' in block
    assert '--maker-evidence "$RUN_ROOT/research/evidence/maker_markout"' in block

if __name__ == "__main__":
    test_toxic_fill_saved_by_hard_cancel()
    test_cancel_effective_at_fill_is_too_late()
    test_pre_order_signal_is_not_reused_as_placement_veto()
    test_learned_shadow_side_orientation_and_zero_authority()
    test_avoiding_favorable_fill_is_counted_as_opportunity_cost()
    test_global_hard_scope_requires_exact_crypto_scope_and_opt_in()
    test_horse_race_is_zero_authority_and_hashes_fixed_anchors()
    test_runtime_launcher_includes_fill_conditioned_markout_evidence()
