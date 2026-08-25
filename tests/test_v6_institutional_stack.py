from __future__ import annotations

import math
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v6_dynamic_factor_intents as factor
import v6_execution_model as ex
import v6_global_risk as gr
import v6_micro_taker_institutional as micro


class InstitutionalExecutionTests(unittest.TestCase):
    def test_depth_walk_never_invents_liquidity(self):
        r = ex.walk_levels([(0.52, 10), (0.53, 5)], 12, buy=True)
        self.assertTrue(r.depth_complete)
        self.assertAlmostEqual(r.vwap, (0.52 * 10 + 0.53 * 2) / 12)
        missing = ex.walk_levels([(0.52, 10)], 12, buy=True)
        self.assertFalse(missing.depth_complete)
        self.assertEqual(missing.shares, 10)

    def test_fill_probability_respects_queue_and_flow(self):
        base = ex.queue_fill_probability(queue_ahead=100, order_shares=10, contra_flow_shares_per_second=2, ttl_seconds=30)
        deep = ex.queue_fill_probability(queue_ahead=500, order_shares=10, contra_flow_shares_per_second=2, ttl_seconds=30)
        fast = ex.queue_fill_probability(queue_ahead=100, order_shares=10, contra_flow_shares_per_second=8, ttl_seconds=30)
        self.assertGreater(base, deep)
        self.assertGreater(fast, base)

    def test_cost_and_uncertainty_are_monotone(self):
        low = ex.state_slippage_bps(base_bps=2, spread=.01, short_vol=.001, participation=.1, liquidity_score=.9)
        high = ex.state_slippage_bps(base_bps=2, spread=.02, short_vol=.005, participation=2, liquidity_score=.2)
        self.assertGreater(high, low)
        e1 = ex.robust_edge_lcb(fair_probability=.56, all_in_entry_price=.54, prediction_sigma=.005, uncertainty_z=.5)
        e2 = ex.robust_edge_lcb(fair_probability=.56, all_in_entry_price=.54, prediction_sigma=.02, uncertainty_z=.5)
        self.assertGreater(e1, e2)

    def test_micro_robust_ridge_learns_direction(self):
        rows = []
        for i in range(240):
            z = (i - 120) / 120
            rows.append({"x": [1.0, z] + [0.0] * 11, "y": .001 + .01 * z})
        beta, sigma, count = micro.weighted_ridge(rows, 13, half_life=10000)
        self.assertEqual(count, 240)
        self.assertGreater(beta[1], .007)
        self.assertLess(sigma, .002)

    def test_dynamic_factor_is_estimated_on_returns(self):
        n = 160
        common = [.008 * math.sin(i / 8) for i in range(n - 1)]
        levels = {}
        for name, loading, phase in [("a", 1.0, 0.0), ("b", .7, .5), ("c", -.6, 1.0), ("d", 1.3, 1.5)]:
            cur = 0.0
            values = [cur]
            for i, f in enumerate(common):
                cur += loading * f + .001 * math.sin(i / 3 + phase)
                values.append(cur)
            levels[name] = values
        latent, loadings, residuals, scales = factor.dynamic_factor_panel(levels, half_life=48)
        self.assertEqual(len(latent), n - 1)
        self.assertEqual(set(loadings), set(residuals))
        self.assertGreater(loadings["a"] * loadings["b"], 0)
        self.assertLess(loadings["a"] * loadings["c"], 0)
        self.assertTrue(all(value > 0 for value in scales.values()))


class GlobalRiskTests(unittest.TestCase):
    @staticmethod
    def sleeve(name, equity, gross=0, killed=False, stale=False):
        return gr.SleeveRisk(name, equity, gross, killed, int(time.time()), stale, "test")

    def test_global_risk_safe(self):
        sleeves = {
            "maker": self.sleeve("maker", 1200, 200),
            "micro_taker": self.sleeve("micro_taker", 800, 100),
            "broker": self.sleeve("broker", 5000, 1000),
            "hard_arb": self.sleeve("hard_arb", 1500, 300),
            "external": self.sleeve("external", 1000, 200),
        }
        result = gr.evaluate_global_risk(
            total_capital=10000,
            reserve_fraction=.05,
            expected_allocations={"maker": .12, "micro_taker": .08, "broker": .5, "hard_arb": .15, "external": .1},
            sleeves=sleeves,
            shock_multipliers={"maker": .45, "micro_taker": .65, "broker": .4, "hard_arb": .1, "external": 1},
            previous_peak=10000,
            max_drawdown=.15,
            max_gross_fraction=.45,
            max_scenario_loss_fraction=.12,
            within_startup_grace=False,
        )
        self.assertFalse(result["kill"])

    def test_global_risk_fails_closed_on_stale_and_scenario(self):
        sleeves = {
            "maker": self.sleeve("maker", 1200, 1200, stale=True),
            "micro_taker": self.sleeve("micro_taker", 800, 800),
            "broker": self.sleeve("broker", 5000, 2500),
            "hard_arb": self.sleeve("hard_arb", 1500, 1000),
            "external": self.sleeve("external", 1000, 1000),
        }
        result = gr.evaluate_global_risk(
            total_capital=10000,
            reserve_fraction=.05,
            expected_allocations={"maker": .12, "micro_taker": .08, "broker": .5, "hard_arb": .15, "external": .1},
            sleeves=sleeves,
            shock_multipliers={"maker": .45, "micro_taker": .65, "broker": .4, "hard_arb": .1, "external": 1},
            previous_peak=10000,
            max_drawdown=.15,
            max_gross_fraction=.45,
            max_scenario_loss_fraction=.12,
            within_startup_grace=False,
        )
        self.assertTrue(result["kill"])
        self.assertIn("global_gross", result["kill_reasons"])
        self.assertIn("scenario_loss", result["kill_reasons"])
        self.assertTrue(any(reason.startswith("stale_sleeve") for reason in result["kill_reasons"]))

    def test_external_engine_durable_state_is_reconciled(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ext = root / "external"
            ext.mkdir()
            (ext / "risk_state.csv").write_text("cash,peak_equity,killed\n800,1000,0\n", encoding="utf-8")
            (ext / "broker_state.csv").write_text(
                "market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid\n"
                "m,e,s,YES,t,100,0.5,50,0\n",
                encoding="utf-8",
            )
            result = gr._external_engine_sleeve(root, {"external": .1}, 10000, int(time.time()), 180)
            self.assertIsNotNone(result)
            assert result is not None
            self.assertAlmostEqual(result.gross, 50)
            self.assertAlmostEqual(result.equity, 849)
            self.assertFalse(result.stale)


if __name__ == "__main__":
    unittest.main()
