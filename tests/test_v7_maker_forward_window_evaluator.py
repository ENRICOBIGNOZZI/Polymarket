from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_maker_forward_window_evaluator",
    ROOT / "scripts" / "v7_maker_forward_window_evaluator.py",
)
assert SPEC and SPEC.loader
fw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fw
SPEC.loader.exec_module(fw)
SHA = "a" * 40
START = 1_800_000_000_000
END = START + 8 * 60 * 60 * 1000


def manifest(*, clusters: int = 20, shares: float = 50.0) -> dict:
    return {
        "schema": fw.MANIFEST_SCHEMA,
        "experiment_id": "maker-forward-test",
        "code_sha": SHA,
        "window_start_ms": START,
        "window_end_ms": END,
        "maximum_post_window_fill_ms": 60_000,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "automatic_promotion": False,
        "required_authority_basis": "FRESH_OPPOSITE_FLOW",
        "markout_horizons": ["100ms", "250ms", "500ms", "1s", "5s", "30s"],
        "evidence_sufficiency": {
            "minimum_independent_fill_clusters": clusters,
            "minimum_filled_shares": shares,
        },
    }


def common_meta(*, provenance: bool = True) -> dict:
    alpha = {
        "features": {"queue_ahead": 20.0},
        "fill_probability": {"point": 0.15},
    }
    if provenance:
        alpha["flow_provenance"] = {
            "authority_basis": "FRESH_OPPOSITE_FLOW",
            "flow_source": "ANCHOR_CAUSAL_WS_FLOW",
            "opposite_flow_is_fresh": True,
            "opposite_prints_2m": 3,
            "opposite_prints_10m": 8,
            "last_opposite_flow_age_ms": 250,
        }
    return {
        "component": "professional_maker",
        "paper_exploration": True,
        "economic_authority": "PAPER_EXPLORATION",
        "counterfactual": False,
        "excluded_from_portfolio_equity": False,
        "paper_bootstrap_probe": False,
        "coordinator_receipt": {
            "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
            "action": "MAKE",
            "paper_only": True,
            "paper_exploration_authorized": True,
            "authenticated_execution": False,
            "real_order_submission": False,
        },
        "execution_alpha": alpha,
        "placement_features": {
            "microstructure_shadow_delta_250ms": 0.001,
            "imbalance": 0.4,
            "ofi": 0.2,
            "cancel_intensity": 0.1,
            "trade_intensity": 0.3,
            "aggressive_sell_prints_per_second": 0.8,
            "short_return_ticks": 0.1,
            "spread_ticks": 1.0,
            "distance_from_touch_ticks": 0.0,
            "local_latency_ms": 0.1,
        },
    }


def event(event_type: str, index: int, *, provenance: bool = True, pnl: float = 0.12) -> dict:
    market = f"m{index:03d}"
    order = f"o{index:03d}"
    fill = f"f{index:03d}"
    base = {
        "event_type": event_type,
        "strategy": fw.STRATEGY,
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "market_id": market,
        "event_id": f"e{index:03d}",
        "order_id": order,
        "fill_id": fill if event_type != "ORDER_SUBMITTED" else "",
        "recorded_ts_ms": START + 1_000 + index * 10_000,
        "metadata": common_meta(provenance=provenance),
    }
    if event_type == "FILL":
        base["recorded_ts_ms"] += 100
        base["filled_size"] = 3.0
        base["fill_price"] = 0.45
    elif event_type == "FINAL":
        base["recorded_ts_ms"] = END + 1_000 + index
        base["final_pnl"] = pnl
    return base


def dataset(count: int = 20, *, missing_provenance_index: int | None = None, pnl: float = 0.12):
    rows = []
    markouts = {}
    for index in range(count):
        provenance = index != missing_provenance_index
        rows.extend([
            event("ORDER_SUBMITTED", index, provenance=provenance, pnl=pnl),
            event("FILL", index, provenance=provenance, pnl=pnl),
            event("FINAL", index, provenance=provenance, pnl=pnl),
        ])
        markouts[f"f{index:03d}"] = {
            "100ms": 0.006,
            "250ms": 0.008,
            "500ms": 0.009,
            "1s": 0.010,
            "5s": 0.012,
            "30s": 0.015,
        }
    return rows, markouts


class ForwardWindowEvaluatorTests(unittest.TestCase):
    def test_manifest_requires_exact_eight_hour_window(self) -> None:
        value = manifest()
        fw.validate_manifest(value)
        bad = dict(value)
        bad["window_end_ms"] = END - 1
        with self.assertRaisesRegex(ValueError, "forward_manifest_window"):
            fw.validate_manifest(bad)

    def test_positive_window_requires_positive_cluster_lower_bound_and_final_pnl(self) -> None:
        rows, markouts = dataset(20)
        report = fw.evaluate(manifest(), rows, markouts, bootstrap_draws=500, seed=1)
        self.assertEqual(report["state"], "PRIMARY_ENDPOINTS_POSITIVE_NO_AUTOMATIC_PROMOTION")
        self.assertEqual(report["metrics"]["submitted_orders"], 20)
        self.assertEqual(report["metrics"]["independent_fill_clusters"], 20)
        self.assertEqual(report["metrics"]["filled_shares"], 60.0)
        self.assertTrue(report["metrics"]["authority_provenance_complete"])
        self.assertEqual(report["metrics"]["authority_basis_violations"], 0)
        self.assertGreater(report["metrics"]["markout_per_share"]["250ms"]["value"], 0.0)
        self.assertGreater(report["metrics"]["markout_cluster_bootstrap"]["250ms"]["ci95"][0], 0.0)
        self.assertGreater(report["metrics"]["canonical_final_pnl_per_filled_share"], 0.0)
        self.assertFalse(report["real_money_gate"]["ready"])
        self.assertFalse(report["automatic_promotion"])

    def test_missing_authority_provenance_is_hard_correctness_failure(self) -> None:
        rows, markouts = dataset(20, missing_provenance_index=3)
        report = fw.evaluate(manifest(), rows, markouts, bootstrap_draws=100, seed=2)
        self.assertEqual(report["state"], "HARD_CORRECTNESS_FAILURE")
        self.assertFalse(report["metrics"]["authority_provenance_complete"])
        self.assertIn("o003", report["metrics"]["authority_provenance_missing_order_ids"])

    def test_below_cluster_and_share_gate_is_insufficient(self) -> None:
        rows, markouts = dataset(4)
        report = fw.evaluate(manifest(), rows, markouts, bootstrap_draws=100, seed=3)
        self.assertEqual(report["state"], "INSUFFICIENT_EVIDENCE")
        self.assertFalse(report["metrics"]["evidence_sufficient"])

    def test_missing_primary_markout_is_incomplete_after_evidence_gate(self) -> None:
        rows, markouts = dataset(20)
        del markouts["f007"]["250ms"]
        report = fw.evaluate(manifest(), rows, markouts, bootstrap_draws=100, seed=4)
        self.assertEqual(report["state"], "MARKOUT_EVIDENCE_INCOMPLETE")
        self.assertEqual(report["metrics"]["markout_per_share"]["250ms"]["fill_observations"], 19)

    def test_negative_250ms_endpoint_fails_even_if_settlement_positive(self) -> None:
        rows, markouts = dataset(20)
        for values in markouts.values():
            values["250ms"] = -0.005
        report = fw.evaluate(manifest(), rows, markouts, bootstrap_draws=200, seed=5)
        self.assertEqual(report["state"], "PRIMARY_ENDPOINTS_NOT_POSITIVE")
        self.assertLess(report["metrics"]["markout_cluster_bootstrap"]["250ms"]["ci95"][1], 0.0)


if __name__ == "__main__":
    unittest.main()
