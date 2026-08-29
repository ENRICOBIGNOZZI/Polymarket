#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_graph_rv as graph


class GraphRVContractTests(unittest.TestCase):
    def test_queue_never_grants_size(self) -> None:
        self.assertEqual(graph.queue_decoupled_units(risk_units=100.0, weight=1.0, unwind_depth=80.0, unwind_fraction=.25), 20.0)
        self.assertEqual(graph.queue_decoupled_units(risk_units=1000.0, weight=2.0, unwind_depth=80.0, unwind_fraction=.25), 10.0)

    def test_receive_time_is_order_causality(self) -> None:
        self.assertFalse(graph.receive_time_active(999, 1000, 0))
        self.assertTrue(graph.receive_time_active(1000, 1000, 0))
        self.assertTrue(graph.receive_time_active(1499, 1000, 1500))
        self.assertFalse(graph.receive_time_active(1500, 1000, 1500))
        self.assertFalse(graph.receive_time_active(0, 1000, 0))

    def test_public_trade_capacity_is_consumed_once(self) -> None:
        q, f1, rem = graph.allocate_public_trade(5.0, 10.0, 12.0)
        self.assertEqual((q, f1, rem), (0.0, 7.0, 0.0))
        _, f2, rem2 = graph.allocate_public_trade(0.0, 10.0, rem)
        self.assertEqual((f2, rem2), (0.0, 0.0))

    def test_joint_completion_is_direct_not_product_of_marginals(self) -> None:
        model = {"signatures": {"2": {"MAKER/TAKER": {"p_complete": .37, "expected_partial_unwind_per_unit": .02}}}}
        self.assertEqual(graph.direct_joint_state(model, 2, ("MAKER", "TAKER")), (.37, .02))
        self.assertEqual(graph.direct_joint_state({}, 2, ("MAKER", "MAKER")), (None, 0.0))
        self.assertEqual(graph.direct_joint_state({}, 2, ("TAKER", "TAKER")), (1.0, 0.0))

    def test_book_fails_closed_without_causal_exchange_clock(self) -> None:
        receive_ms = 1_800_000_000_000
        raw = {"asset_id":"t", "bids":[{"price":"0.49","size":"10"}], "asks":[{"price":"0.51","size":"10"}], "hash":"h"}
        self.assertIsNone(graph.parse_book(raw, receive_ms))
        raw["timestamp"] = receive_ms - 1000
        self.assertIsNotNone(graph.parse_book(raw, receive_ms))
        raw["timestamp"] = receive_ms + 1000
        self.assertIsNone(graph.parse_book(raw, receive_ms))


if __name__ == "__main__":
    unittest.main()
