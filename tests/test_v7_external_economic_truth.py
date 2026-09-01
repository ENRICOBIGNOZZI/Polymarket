from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_exact_sha_economic_bundle import build_bundle, verify_seal  # noqa: E402
from v7_execution_latency_distribution import build_latency_report, nearest_rank  # noqa: E402
from v7_external_loss_attribution import build_attribution  # noqa: E402
from v7_external_policy_replay import (  # noqa: E402
    build_replay, capacity_curve, day_block_lcb95, walk_buy,
)
from v7_external_economic_common import load_counterfactual_evidence  # noqa: E402


SHA_A = "a" * 40
SHA_B = "b" * 40
POLICY = "c" * 64
RULES = "d" * 64


def lifecycle(index: int, *, sha: str = SHA_A, final_sha: str | None = None,
              won: bool = True, day: int = 1) -> list[dict]:
    final_sha = final_sha or sha
    market = f"market-{index}"
    fill_id = f"fill-{index}"
    candidate_id = f"candidate-{index}"
    base = day * 86_400_000
    outcome = "YES"
    size, price, fee = 10.0, 0.4, 0.1
    payout = size if won else 0.0
    pnl = payout - size * price - fee
    common = {
        "schema": "polymarket_v7_external_fair_counterfactual_v1",
        "policy_sha256": POLICY,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": "SHADOW_ZERO_AUTHORITY",
    }
    candidate = {
        **common, "record_id": f"candidate-record-{index}", "event_type": "CANDIDATE",
        "model_sha": sha, "counterfactual_id": candidate_id, "candidate_id": candidate_id,
        "fill_id": fill_id, "market_id": market, "event_id": f"event-{index}",
        "timestamp_ms": base + 1_000, "decision_ts_ms": base + 1_000,
        "exchange_ts_ms": base + 980, "receive_ts_ms": base + 990,
        "bid": 0.39, "ask": 0.40, "bid_depth": 20.0, "ask_depth": 30.0,
        "metadata": {
            "outcome": outcome, "contract_rules_hash": RULES, "reference_version": base,
            "fair_yes": 0.8, "fair_lower": 0.75, "fair_upper": 0.85,
            "robust_probability": 0.75, "pm_mid": 0.5, "tte_seconds": 90.0,
            "tte_bucket_id": "180_60", "robust_ev_per_share": 0.33,
            "expected_execution_risk": 0.01,
        },
    }
    fill = {
        **common, "record_id": f"fill-record-{index}", "event_type": "VIRTUAL_FILL",
        "model_sha": sha, "counterfactual_id": candidate_id, "fill_id": fill_id,
        "position_id": f"position-{index}", "market_id": market, "event_id": f"event-{index}",
        "token_id": f"yes-{index}", "timestamp_ms": base + 1_100,
        "exchange_ts_ms": base + 1_075, "receive_ts_ms": base + 1_100,
        "fill_price": price, "filled_size": size, "fee": fee, "slippage": 0.0,
        "metadata": {
            **candidate["metadata"], "arrival_robust_probability": 0.75,
            "arrival_robust_ev_per_share": 0.33, "robust_net_ev": 3.3,
            "arrival_pm_mid": 0.5, "arrival_tte_seconds": 89.9,
        },
    }
    markout = {
        **common, "record_id": f"markout-record-{index}", "event_type": "VIRTUAL_MARKOUT",
        "model_sha": final_sha, "counterfactual_id": candidate_id, "fill_id": fill_id,
        "position_id": f"position-{index}", "market_id": market,
        "timestamp_ms": base + 2_100, "receive_ts_ms": base + 2_100,
        "markouts": {"1s": 0.05 if won else -0.10},
    }
    final = {
        **common, "record_id": f"final-record-{index}", "event_type": "VIRTUAL_FINAL",
        "model_sha": final_sha, "counterfactual_id": candidate_id, "fill_id": fill_id,
        "position_id": f"position-{index}", "market_id": market, "event_id": f"event-{index}",
        "token_id": f"yes-{index}", "timestamp_ms": base + 301_000,
        "counterfactual_pnl": pnl, "virtual_cashflow": payout,
        "metadata": {
            "won": won, "settlement_outcome": "Up" if won else "Down",
            "winning_token_id": f"yes-{index}" if won else f"no-{index}",
        },
    }
    return [candidate, fill, markout, final]


def opportunity(index: int, timestamp: int) -> dict:
    return {
        "schema": "polymarket_v7_external_fair_counterfactual_v1",
        "record_id": f"opportunity-{index}-{timestamp}",
        "event_type": "OPPORTUNITY_SET", "model_sha": SHA_A,
        "policy_sha256": POLICY, "paper_only": True,
        "authenticated_execution": False, "real_order_submission": False,
        "execution_authority": "SHADOW_ZERO_AUTHORITY",
        "global_policy_gates_passed": True,
        "market_id": f"market-{index}", "decision_ts_ms": timestamp,
        "timestamp_ms": timestamp, "fee_schedule": {"rate": 0.0, "exponent": 1},
        "actions": [
            {"outcome": "YES", "robust_probability": 0.8,
             "execution_risk_per_share": 0.0},
            {"outcome": "NO", "robust_probability": 0.2,
             "execution_risk_per_share": 0.0},
        ],
        "books": {
            "YES": {"asks": [[0.40, 5.0], [0.45, 10.0]]},
            "NO": {"asks": [[0.60, 20.0]]},
        },
    }


def test_economic_truth() -> None:
    rows = [
        *lifecycle(1, won=True, day=1),
        *lifecycle(2, final_sha=SHA_B, won=False, day=2),
    ]
    quality = {
        "fail_closed": False, "input_manifests": [], "unique_records": len(rows),
        "malformed_records": 0, "conflicting_record_ids": 0,
    }
    attribution = build_attribution(rows, quality, SHA_A, {
        "minimum_robust_ev_per_share": 0.002,
        "tte_bucket_policy": [{
            "minimum_seconds": 60.0, "maximum_seconds": 180.0,
            "minimum_robust_ev_per_share": 0.002,
        }],
    })
    assert attribution["summary"]["terminal_trades"] == 2
    assert attribution["summary"]["lineage_states"] == {"EXACT_SHA": 1, "MIXED_SHA": 1}
    assert attribution["summary"]["all_terminal_accounting_reconciled"] is True
    losing = next(trade for trade in attribution["trades"] if trade["realized_pnl"] < 0)
    assert "SELECTED_SIDE_SETTLED_FALSE" in losing["attribution_flags"]
    assert losing["lineage"]["state"] == "MIXED_SHA"

    latency = build_latency_report(rows, quality, SHA_A)
    assert latency["components"]["decision_to_arrival"]["p50_ms"] == 100.0
    assert nearest_rank([3.0, 1.0, 2.0], 0.90) == 3.0

    replay = build_replay(rows, quality, SHA_A)
    assert replay["replay_scope"] == "SELECTED_HISTORICAL_FILLS_ONLY"
    assert replay["historical_terminal_fills"] == 2
    assert any(row["policy"] == "EXIT_1S" for row in replay["exit_policy_comparison_base_observed_costs"])
    assert replay["promotion"]["eligible"] is False

    with_opportunities = [
        *rows,
        opportunity(1, 86_400_000 + 500),
        opportunity(1, 86_400_000 + 900),
    ]
    opportunity_quality = {**quality, "unique_records": len(with_opportunities)}
    opportunity_replay = build_replay(with_opportunities, opportunity_quality, SHA_A)
    assert opportunity_replay["replay_scope"] == "ALL_RECORDED_OPPORTUNITY_BOOKS_AND_SELECTED_HISTORICAL_FILLS"
    arrival = next(
        row for row in opportunity_replay["all_opportunity_arrival_book_scenarios"]
        if row["threshold_per_share"] == 0.0 and row["cost_multiplier"] == 1.0
        and row["latency_profile"] == "p50"
    )
    assert arrival["trades"] == 1
    assert arrival["net_pnl"] > 2.9
    rejected = opportunity(1, 86_400_000 + 1_300)
    rejected["global_policy_gates_passed"] = False
    rejected_replay = build_replay(
        [*with_opportunities, rejected],
        {**quality, "unique_records": len(with_opportunities) + 1}, SHA_A,
    )
    assert rejected_replay["opportunity_sets"] == 3
    assert rejected_replay["policy_gate_eligible_opportunity_sets"] == 2
    assert rejected_replay["policy_gate_rejected_opportunity_sets"] == 1
    assert rejected_replay["capacity_curve"] == opportunity_replay["capacity_curve"]

    book_walk = walk_buy([[0.4, 5], [0.5, 5]], 8, {"rate": 0.0, "exponent": 1})
    assert book_walk["complete"] is True
    assert abs(book_walk["average_price"] - 0.4375) < 1e-12
    assert walk_buy([[0.4, 5]], 8, {"rate": 0.0, "exponent": 1})["complete"] is False
    curve = capacity_curve([opportunity(1, 1000)], quantities=[5.0, 20.0])
    assert curve[0]["complete_action_books"] == 2
    assert curve[1]["complete_action_books"] == 1

    confidence = day_block_lcb95([
        {"timestamp_ms": 86_400_000, "pnl": 1.0},
        {"timestamp_ms": 2 * 86_400_000, "pnl": 2.0},
    ], draws=200)
    assert confidence["day_blocks"] == 2
    assert confidence["lcb95"] is not None

    bundle = build_bundle(
        rows=rows, quality=quality,
        repository={
            "head": SHA_A, "dirty": False, "worktree_change_count": 0,
            "worktree_change_digest": "e" * 64, "exact_committed_code_identity": True,
        },
        config={"taker": {"latency_risk_per_second": 0.001}},
    )
    assert bundle["evidence_state"] == "EXACT_SHA_EVIDENCE_AVAILABLE_WITH_PARTITIONED_HISTORY"
    assert bundle["lineage_partition"]["exact_sha_terminal_trades"] == 1
    assert verify_seal(bundle)
    damaged = json.loads(json.dumps(bundle))
    damaged["lineage_partition"]["terminal_trades"] = 99
    assert not verify_seal(damaged)


def test_loader_fails_closed_on_conflict() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "counterfactuals.jsonl"
        first = lifecycle(3)[0]
        second = {**first, "ask": 0.9}
        path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n")
        rows, quality = load_counterfactual_evidence([path])
        assert len(rows) == 1
        assert quality["conflicting_record_ids"] == 1
        assert quality["fail_closed"] is True


if __name__ == "__main__":
    test_economic_truth()
    test_loader_fails_closed_on_conflict()
