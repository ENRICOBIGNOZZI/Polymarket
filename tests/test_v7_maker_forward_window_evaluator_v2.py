from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "v7_maker_forward_window_evaluator_v2",
    SCRIPTS / "v7_maker_forward_window_evaluator_v2.py",
)
assert SPEC and SPEC.loader
v2 = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = v2
SPEC.loader.exec_module(v2)
import v7_maker_forward_window_evaluator as base

SHA = "a" * 40
START = 1_800_000_000_000
END = START + 2 * 60 * 60 * 1000
SELECTOR_TS = START + 1_000


def manifest() -> dict:
    return {
        "schema": base.MANIFEST_SCHEMA,
        "experiment_id": "source-audit-test",
        "code_sha": SHA,
        "window_start_ms": START,
        "window_end_ms": END,
        "maximum_post_window_fill_ms": 60_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_promotion": False,
        "required_authority_basis": base.REQUIRED_BASIS,
        "markout_horizons": ["250ms"],
        "evidence_sufficiency": {
            "minimum_independent_fill_clusters": 1,
            "minimum_filled_shares": 1.0,
        },
    }


def receipt() -> dict:
    return {
        "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
        "action": "MAKE",
        "paper_only": True,
        "paper_exploration_authorized": True,
        "authenticated_execution": False,
        "real_order_submission": False,
    }


def envelope() -> dict:
    return {
        "market_id": "m1",
        "contract_id": "t1",
        "side": "YES",
        "source_event_timestamps_ns": [SELECTOR_TS * 1_000_000],
        "execution_plan": {"legs": [{"token_id": "t1", "side": "BUY"}]},
        "reasons": ["VERIFIED_SETTLEMENT_RULE", "PLACEMENT_JOIN"],
    }


def meta(*, env: bool = True) -> dict:
    value = {
        "component": base.COMPONENT,
        "paper_exploration": True,
        "economic_authority": "PAPER_EXPLORATION",
        "counterfactual": False,
        "excluded_from_portfolio_equity": False,
        "paper_bootstrap_probe": False,
        "coordinator_receipt": receipt(),
        "execution_alpha": {
            "features": {"queue_ahead": 10.0},
            "fill_probability": {"point": 0.1},
        },
        "placement_features": {
            "microstructure_shadow_delta_250ms": 0.001,
            "imbalance": 0.4,
            "ofi": 0.2,
            "cancel_intensity": 0.1,
            "trade_intensity": 0.2,
            "aggressive_sell_prints_per_second": 0.6,
            "short_return_ticks": 0.0,
            "spread_ticks": 1.0,
            "distance_from_touch_ticks": 0.0,
            "local_latency_ms": 0.1,
        },
    }
    if env:
        value["opportunity_envelope"] = envelope()
    return value


def ledger(event_type: str) -> dict:
    row = {
        "event_type": event_type,
        "strategy": base.STRATEGY,
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "market_id": "m1",
        "event_id": "e1",
        "order_id": "o1",
        "fill_id": "f1" if event_type != "ORDER_SUBMITTED" else "",
        "token_id": "t1",
        "recorded_ts_ms": START + 2_000,
        "metadata": meta(),
    }
    if event_type == "FILL":
        row["recorded_ts_ms"] += 100
        row["filled_size"] = 2.0
        row["fill_price"] = 0.45
    if event_type == "FINAL":
        row["recorded_ts_ms"] = END + 1_000
        row["final_pnl"] = 0.2
    return row


def snapshot(*, basis: str = "FRESH_OPPOSITE_FLOW", fresh: bool = True) -> dict:
    return {
        "timestamp_ms": SELECTOR_TS,
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "source": "crypto_universe_recent_flow",
        "recent_flow_source": "ANCHOR_CAUSAL_WS_FLOW",
        "markets": [{
            "market_id": "m1",
            "authorized_execution_cells": [{
                "token_id": "t1", "outcome": "YES", "quote_side": "BUY",
                "action": "JOIN", "authority_basis": basis,
                "projected_fill_probability": 0.1,
            }],
            "quote_opportunities": [{
                "token_id": "t1", "outcome": "YES", "quote_side": "BUY",
                "opposite_flow_is_fresh": fresh,
                "opposite_prints_2m": 3, "opposite_prints_10m": 8,
                "last_opposite_flow_age_ms": 200,
                "projected_join_fill_probability": 0.1,
                "flow_source": "ANCHOR_CAUSAL_WS_FLOW",
            }],
        }],
    }


class ForwardAuthorityAuditTests(unittest.TestCase):
    def test_exact_selector_snapshot_proves_fresh_flow_authority(self) -> None:
        proof, state = v2.prove_order(ledger("ORDER_SUBMITTED"), {SELECTOR_TS: snapshot()}, SHA)
        self.assertEqual(state, "PROVEN")
        self.assertEqual(proof["authority_basis"], "FRESH_OPPOSITE_FLOW")
        self.assertTrue(proof["opposite_flow_is_fresh"])
        self.assertEqual(proof["selector_timestamp_ms"], SELECTOR_TS)
        self.assertEqual(proof["proof_source"], "CANONICAL_REWARD_SELECTION_EVENT_LOG")

    def test_nonfresh_or_wrong_basis_fails_closed(self) -> None:
        proof, state = v2.prove_order(
            ledger("ORDER_SUBMITTED"), {SELECTOR_TS: snapshot(basis="LOW_SAMPLE_FRESH_FLOW_CONTROL")}, SHA)
        self.assertIsNone(proof)
        self.assertEqual(state, "NON_FRESH_OPPOSITE_FLOW_AUTHORITY")
        proof, state = v2.prove_order(
            ledger("ORDER_SUBMITTED"), {SELECTOR_TS: snapshot(fresh=False)}, SHA)
        self.assertIsNone(proof)
        self.assertEqual(state, "SELECTOR_FLOW_NOT_FRESH")

    def test_source_proof_in_memory_satisfies_hard_forward_gate(self) -> None:
        rows = [ledger("ORDER_SUBMITTED"), ledger("FILL"), ledger("FINAL")]
        injected, audit = v2.inject_source_proofs(rows, manifest(), {SELECTOR_TS: snapshot()})
        self.assertEqual(audit["orders_proven_from_source"], 1)
        self.assertEqual(audit["orders_unproven"], 0)
        report = base.evaluate(
            manifest(), injected, {"f1": {"250ms": 0.01}}, bootstrap_draws=100, seed=1)
        self.assertEqual(report["state"], "PRIMARY_ENDPOINTS_POSITIVE_NO_AUTOMATIC_PROMOTION")
        self.assertTrue(report["metrics"]["authority_provenance_complete"])

    def test_missing_selector_snapshot_remains_hard_failure(self) -> None:
        rows = [ledger("ORDER_SUBMITTED"), ledger("FILL"), ledger("FINAL")]
        injected, audit = v2.inject_source_proofs(rows, manifest(), {})
        self.assertEqual(audit["orders_unproven"], 1)
        report = base.evaluate(
            manifest(), injected, {"f1": {"250ms": 0.01}}, bootstrap_draws=10, seed=2)
        self.assertEqual(report["state"], "HARD_CORRECTNESS_FAILURE")
        self.assertFalse(report["metrics"]["authority_provenance_complete"])

    def test_existing_copied_provenance_is_never_trusted_without_source(self) -> None:
        row = ledger("ORDER_SUBMITTED")
        row["metadata"]["execution_alpha"]["flow_provenance"] = {
            "authority_basis": "FRESH_OPPOSITE_FLOW",
            "opposite_flow_is_fresh": True,
            "flow_source": "COPIED_METADATA_ONLY",
        }
        injected, audit = v2.inject_source_proofs([row], manifest(), {})
        self.assertEqual(audit["orders_with_existing_copied_provenance"], 1)
        self.assertEqual(audit["orders_proven_from_source"], 0)
        self.assertEqual(audit["orders_unproven"], 1)
        self.assertNotIn("flow_provenance", injected[0]["metadata"]["execution_alpha"])

    def test_ambiguous_selector_timestamp_remains_poisoned_after_third_duplicate(self) -> None:
        first = snapshot()
        conflict = snapshot(fresh=False)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "selector.events.jsonl"
            path.write_text(
                "\n".join(json.dumps(row, sort_keys=True) for row in (first, conflict, first)) + "\n",
                encoding="utf-8",
            )
            index = v2.selection_index([path], SHA)
        self.assertNotIn(SELECTOR_TS, index)


    def test_source_timestamp_preserves_large_integer_nanoseconds(self) -> None:
        raw = 1_789_503_992_318_000_000
        env = {"source_event_timestamps_ns": [raw]}
        self.assertEqual(v2.source_timestamp_ms(env), 1_789_503_992_318)
        self.assertNotEqual(int(float(raw)), raw)

    def test_source_timestamp_rejects_float_nanoseconds(self) -> None:
        env = {"source_event_timestamps_ns": [1.789503992318e18]}
        self.assertEqual(v2.source_timestamp_ms(env), 0)


    def test_bounded_probe_contract_is_validated_before_base_evaluator(self) -> None:
        row = ledger("ORDER_SUBMITTED")
        row["metadata"]["paper_bootstrap_probe"] = True
        row["metadata"]["execution_authority"] = "SIMULATED_PAPER_ONLY"
        row["metadata"]["coordinator_receipt"].update({
            "paper_exploration_probe_authorized": True,
            "paper_exploration_policy": "BTC_M5_BOUNDED_NO_REAL_MONEY",
            "real_capital_at_risk": False,
            "reasons": ["PAPER_EXPLORATION_INFORMATION_GAIN_PROBE"],
            "probe": {
                "mode": "PAPER_BOOTSTRAP_PROBE", "research_only": True,
                "robust_candidate": False, "arrival_revalidated": True,
                "maximum_probe_loss": 2.0, "probe_loss_cap": 2.0,
                "point_expected_wealth_change": 0.01,
            },
        })
        env = row["metadata"]["opportunity_envelope"]
        env["reasons"] = [
            "VERIFIED_SETTLEMENT_RULE", "CONTROL_EXPLORATION_CELL",
            "POSITIVE_POINT_MAKER_EV", "RESEARCH_INFORMATION_PROBE", "PLACEMENT_JOIN",
        ]
        env["execution_plan"]["legs"][0].update(
            {"target_quantity": 2.0, "limit_price": 0.5}
        )
        env["execution_plan"]["timeout_ms"] = 15_000
        m = manifest()
        m["schema"] = "polymarket_v7_maker_forward_window_v2"
        m["policy_preflight"] = {
            "anchor_causal_flow_authority_enabled": True,
            "anchor_execution_authority_enabled": False,
        }
        proven, audit = v2.inject_source_proofs([row], m, {SELECTOR_TS: snapshot()})
        self.assertEqual(audit["bounded_probe_orders_seen"], 1)
        self.assertEqual(audit["bounded_probe_orders_validated"], 1)
        self.assertEqual(audit["bounded_probe_orders_invalid"], 0)
        self.assertFalse(proven[0]["metadata"]["paper_bootstrap_probe"])

    def test_bounded_probe_contract_rejects_oversize_loss(self) -> None:
        row = ledger("ORDER_SUBMITTED")
        row["metadata"]["paper_bootstrap_probe"] = True
        row["metadata"]["execution_authority"] = "SIMULATED_PAPER_ONLY"
        row["metadata"]["coordinator_receipt"].update({
            "paper_exploration_probe_authorized": True,
            "paper_exploration_policy": "BTC_M5_BOUNDED_NO_REAL_MONEY",
            "real_capital_at_risk": False,
            "reasons": ["PAPER_EXPLORATION_INFORMATION_GAIN_PROBE"],
            "probe": {
                "mode": "PAPER_BOOTSTRAP_PROBE", "research_only": True,
                "robust_candidate": False, "arrival_revalidated": True,
                "maximum_probe_loss": 2.01, "probe_loss_cap": 2.01,
                "point_expected_wealth_change": 0.01,
            },
        })
        env = row["metadata"]["opportunity_envelope"]
        env["reasons"] = [
            "VERIFIED_SETTLEMENT_RULE", "CONTROL_EXPLORATION_CELL",
            "POSITIVE_POINT_MAKER_EV", "RESEARCH_INFORMATION_PROBE", "PLACEMENT_JOIN",
        ]
        env["execution_plan"]["legs"][0].update(
            {"target_quantity": 2.0, "limit_price": 0.5}
        )
        env["execution_plan"]["timeout_ms"] = 15_000
        m = manifest()
        m["schema"] = "polymarket_v7_maker_forward_window_v2"
        m["policy_preflight"] = {
            "anchor_causal_flow_authority_enabled": True,
            "anchor_execution_authority_enabled": False,
        }
        proven, audit = v2.inject_source_proofs([row], m, {SELECTOR_TS: snapshot()})
        self.assertTrue(proven[0]["metadata"]["paper_bootstrap_probe"])
        self.assertEqual(audit["bounded_probe_orders_invalid"], 1)
        self.assertEqual(
            audit["unproven_order_reasons"]["o1"], "BOUNDED_PROBE_CONTRACT_INVALID"
        )


if __name__ == "__main__":
    unittest.main()
