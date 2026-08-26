from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("v7_local_factor_pairs_test", SCRIPTS / "v7_local_factor_pairs.py")
assert spec and spec.loader
pairs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pairs
spec.loader.exec_module(pairs)


@dataclass(frozen=True)
class Market:
    market_id: str
    question: str


class StructuralPairGraphTest(unittest.TestCase):
    def test_threshold_parser_ignores_calendar_year(self) -> None:
        self.assertEqual(pairs.threshold_value("Will BTC be above $100,000 by the end of 2026?"), 100000.0)
        self.assertAlmostEqual(pairs.threshold_value("Will inflation exceed 4% in 2026?"), 0.04)
        self.assertIsNone(pairs.threshold_value("Will candidate X win in 2026?"))

    def test_payoff_family_uses_non_overlapping_nearest_threshold_pairs(self) -> None:
        markets = [
            Market("a", "Will BTC be above $90,000 by 2026?"),
            Market("b", "Will BTC be above $100,000 by 2026?"),
            Market("c", "Will BTC be above $110,000 by 2026?"),
            Market("d", "Will BTC be above $150,000 by 2026?"),
        ]
        graph = pairs.build_structural_pair_graph("payoff:btc threshold", markets, min_controls=2)
        self.assertEqual(graph.method, "threshold_matching_plus_text_matching")
        self.assertEqual(set(graph.pairs), {("a", "b"), ("c", "d")})
        self.assertEqual(graph.threshold_markets, 4)
        flattened = [market_id for pair in graph.pairs for market_id in pair]
        self.assertEqual(len(flattened), len(set(flattened)))

    def test_event_matching_is_deterministic_and_sparse(self) -> None:
        markets = [
            Market("a", "Will Alice win the Democratic primary?"),
            Market("b", "Will Bob win the Democratic primary?"),
            Market("c", "Will Alice win the Republican primary?"),
            Market("d", "Will Bob win the Republican primary?"),
            Market("e", "Will rainfall exceed 10 inches?"),
        ]
        first = pairs.build_structural_pair_graph("event:123", markets, min_controls=2, minimum_text_similarity=0.20)
        second = pairs.build_structural_pair_graph("event:123", list(reversed(markets)), min_controls=2, minimum_text_similarity=0.20)
        self.assertEqual(first.pairs, second.pairs)
        self.assertLessEqual(first.pair_count, len(markets) // 2)
        flattened = [market_id for pair in first.pairs for market_id in pair]
        self.assertEqual(len(flattened), len(set(flattened)))

    def test_pair_graph_does_not_require_or_read_price_fields(self) -> None:
        class PriceBomb:
            market_id = "a"
            question = "Will BTC be above $100,000?"

            @property
            def price(self):
                raise AssertionError("price must not be touched")

            @property
            def liquidity(self):
                raise AssertionError("liquidity must not be touched")

        class Other:
            market_id = "b"
            question = "Will BTC be above $110,000?"

        class Control1:
            market_id = "c"
            question = "Will BTC be above $120,000?"

        class Control2:
            market_id = "d"
            question = "Will BTC be above $130,000?"

        graph = pairs.build_structural_pair_graph(
            "payoff:btc",
            [PriceBomb(), Other(), Control1(), Control2()],
            min_controls=2,
        )
        self.assertEqual(graph.pair_count, 2)

    def test_insufficient_controls_produces_no_hypothesis(self) -> None:
        graph = pairs.build_structural_pair_graph(
            "event:x",
            [Market("a", "Will A win?"), Market("b", "Will B win?"), Market("c", "Will C win?")],
            min_controls=2,
        )
        self.assertEqual(graph.method, "insufficient_controls")
        self.assertEqual(graph.pairs, ())


if __name__ == "__main__":
    unittest.main()
