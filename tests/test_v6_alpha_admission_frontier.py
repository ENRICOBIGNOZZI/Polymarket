from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v6_alpha_admission_frontier.py"
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("v6_alpha_admission_frontier", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


class V6AlphaAdmissionFrontierTest(unittest.TestCase):
    def test_parse_thresholds_includes_canonical_and_rejects_negative(self):
        self.assertEqual(
            module.parse_thresholds("0,0.0001,0.0001", 0.0002),
            [0.0, 0.0001, 0.0002],
        )
        with self.assertRaises(ValueError):
            module.parse_thresholds("-0.0001", 0.0002)

    def test_relation_stress_rejection_is_execution_bound(self):
        result = module.classify_relation(
            {"bundles": 1, "intent_rows": 5, "best_edge": 0.001},
            {
                "accepted_rows": 0,
                "best_edge": 0.0,
                "rejections": {"stress_edge": 5},
            },
        )
        self.assertEqual(result["bottleneck"], "EXECUTION_STRESS_BOUND")
        self.assertEqual(result["raw_bundles"], 1)
        self.assertEqual(result["accepted_rows"], 0)

    def test_lower_floor_counts_only_pairs_surviving_all_cost_stresses(self):
        candidate = SimpleNamespace(name="candidate")
        by_cluster = {"cluster-a": [candidate]}
        eligible = {id(candidate)}

        def fake_builder(
            cluster,
            signals,
            books,
            now,
            min_edge,
            max_trade,
            fee_rate,
            fee_exp,
            slip_bps,
            serial,
        ):
            del cluster, books, now, max_trade, fee_rate, fee_exp, serial
            if not signals:
                return []
            # The lower 1bp floor admits the pair at 1x/1.5x but it disappears
            # at 2x cost. The zero floor admits a different pair even at 2x.
            if min_edge >= 0.0002:
                return []
            if min_edge >= 0.0001 and slip_bps >= 10.0:
                return []
            if min_edge < 0.0001:
                market_a, market_b = "robust-a", "robust-b"
            else:
                market_a, market_b = "fragile-a", "fragile-b"
            return [
                {"market_id": market_a, "side": "YES", "expected_edge": 0.0003},
                {"market_id": market_b, "side": "NO", "expected_edge": 0.0003},
            ]

        grid = module.evaluate_threshold_grid(
            by_cluster=by_cluster,
            eligible=eligible,
            books={},
            now=1,
            thresholds=[0.0, 0.0001, 0.0002],
            max_trade_usd=60.0,
            fee_rate=0.07,
            fee_exp=1.0,
            slippage_bps=5.0,
            build_pair=fake_builder,
        )
        by_threshold = {row["min_edge"]: row for row in grid}
        self.assertEqual(by_threshold[0.0002]["price_cost_robust_pairs"], 0)
        self.assertEqual(by_threshold[0.0001]["price_cost_robust_pairs"], 0)
        self.assertEqual(by_threshold[0.0]["price_cost_robust_pairs"], 1)
        self.assertEqual(
            [row["bundles"] for row in by_threshold[0.0001]["stress"]],
            [1, 1, 0],
        )
        self.assertEqual(
            [row["bundles"] for row in by_threshold[0.0]["stress"]],
            [1, 1, 1],
        )


if __name__ == "__main__":
    unittest.main()
