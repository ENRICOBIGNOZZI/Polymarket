#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "walk_forward_v4_lineage", SCRIPTS / "walk_forward_v4_lineage.py"
)
assert SPEC and SPEC.loader
lineage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lineage)
Trade = lineage.legacy.Trade


def trade(index: int, *, edge: float = 0.01, net: float = 1.8) -> Trade:
    capital = 100.0
    gross = 2.0
    fees = 0.1
    slippage = 0.1
    return Trade(
        bundle_id=f"bundle-{index}",
        strategy="B1",
        created_ts=100 * index,
        closed_ts=100 * index + 20,
        status="CLOSED",
        expected_edge=edge,
        capital=capital,
        gross_pnl=gross,
        fees=fees,
        slippage=slippage,
        net_pnl=net,
        ret=net / capital,
    )


class WalkForwardLineageTests(unittest.TestCase):
    def test_same_ledger_produces_stable_evidence_id(self) -> None:
        trades = [trade(i) for i in range(1, 5)]
        result = {
            "folds": [{"threshold": 0.001, "test_start": 1, "test_end": 1_000}],
            "cost_stress_multiplier": 1.5,
        }
        first = lineage.attach_lineage(result, trades, batch_size=2, starting_capital=1_000.0)
        second = lineage.attach_lineage(result, trades, batch_size=2, starting_capital=1_000.0)
        evidence = first["independent_evidence"]
        self.assertTrue(evidence["certified"])
        self.assertEqual(evidence["evidence_sequence"], 2)
        self.assertEqual(evidence["bundle_ids"], ["bundle-3", "bundle-4"])
        self.assertEqual(first["evidence_id"], second["evidence_id"])
        self.assertEqual(first["dataset_hash"], second["dataset_hash"])

    def test_incomplete_batch_cannot_be_certified(self) -> None:
        result = {
            "folds": [{"threshold": 0.001, "test_start": 1, "test_end": 1_000}],
            "cost_stress_multiplier": 1.5,
        }
        augmented = lineage.attach_lineage(result, [trade(1)], batch_size=2, starting_capital=1_000.0)
        evidence = augmented["independent_evidence"]
        self.assertFalse(evidence["certified"])
        self.assertFalse(evidence["pass"])
        self.assertIn("no_complete_non_overlapping_oos_batch", evidence["gate_failures"])

    def test_threshold_and_test_window_define_selected_evidence(self) -> None:
        trades = [trade(1, edge=0.0005), trade(2), trade(3)]
        result = {
            "folds": [{"threshold": 0.001, "test_start": 150, "test_end": 350}],
            "cost_stress_multiplier": 1.5,
        }
        augmented = lineage.attach_lineage(result, trades, batch_size=2, starting_capital=1_000.0)
        evidence = augmented["independent_evidence"]
        self.assertTrue(evidence["certified"])
        self.assertEqual(evidence["selected_oos_trades"], 2)
        self.assertEqual(evidence["bundle_ids"], ["bundle-2", "bundle-3"])
        self.assertEqual(evidence["test_window_start"], 200)
        self.assertEqual(evidence["test_window_end"], 320)


if __name__ == "__main__":
    unittest.main()
