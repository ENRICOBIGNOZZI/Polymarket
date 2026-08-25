from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "b2_sparse_hedge_frontier",
    ROOT / "scripts" / "b2_sparse_hedge_frontier.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
build_report = MODULE.build_report
stress_edges = MODULE.stress_edges


FIELDS = [
    "market",
    "side",
    "hedges",
    "hedge_error",
    "raw_expected_edge",
    "maker_entry_net_edge",
    "executable_notional",
]


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class B2SparseHedgeFrontierTest(unittest.TestCase):
    def test_execution_drag_stress_is_fail_closed(self) -> None:
        row = {"raw_expected_edge": "0.010", "maker_entry_net_edge": "0.004"}
        stressed = stress_edges(row)
        self.assertAlmostEqual(stressed["execution_drag"], 0.006)
        self.assertAlmostEqual(stressed["maker_1_5x"], 0.001)
        self.assertAlmostEqual(stressed["maker_2x"], -0.002)

    def test_sparse_scan_can_create_but_not_overclaim_maker_alpha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incumbent = root / "k8.csv"
            sparse = root / "k3.csv"
            write_rows(
                incumbent,
                [
                    {
                        "market": "m1",
                        "side": "YES",
                        "hedges": 7,
                        "hedge_error": 0.10,
                        "raw_expected_edge": 0.010,
                        "maker_entry_net_edge": -0.001,
                        "executable_notional": 100,
                    }
                ],
            )
            write_rows(
                sparse,
                [
                    {
                        "market": "m1",
                        "side": "YES",
                        "hedges": 3,
                        "hedge_error": 0.20,
                        "raw_expected_edge": 0.010,
                        "maker_entry_net_edge": 0.006,
                        "executable_notional": 90,
                    }
                ],
            )
            report = build_report([(8, incumbent), (3, sparse)])
            self.assertEqual(report["comparisons"]["3"]["new_maker_positive"], 1)
            self.assertEqual(report["comparisons"]["3"]["new_robust_2x_positive"], 1)
            self.assertEqual(report["evidence_state"], "MORE_EVIDENCE_REQUIRED")

    def test_hedge_error_blocks_robust_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incumbent = root / "k8.csv"
            sparse = root / "k2.csv"
            base = {
                "market": "m1",
                "side": "NO",
                "hedges": 6,
                "hedge_error": 0.10,
                "raw_expected_edge": 0.020,
                "maker_entry_net_edge": -0.002,
                "executable_notional": 100,
            }
            alt = dict(base)
            alt.update(hedges=2, hedge_error=0.90, maker_entry_net_edge=0.015)
            write_rows(incumbent, [base])
            write_rows(sparse, [alt])
            report = build_report([(8, incumbent), (2, sparse)])
            self.assertEqual(report["comparisons"]["2"]["new_maker_positive"], 1)
            self.assertEqual(report["comparisons"]["2"]["new_robust_2x_positive"], 0)


if __name__ == "__main__":
    unittest.main()
