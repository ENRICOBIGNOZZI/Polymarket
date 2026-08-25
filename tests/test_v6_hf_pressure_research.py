#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "v6_hf_pressure_research_test", ROOT / "scripts/v6_hf_pressure_research.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class V6HFPressureResearchTest(unittest.TestCase):
    def test_flat_causal_targets_reject_taker_threshold_relaxation(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            samples = [
                {"ts": 100 + i, "market_id": f"m{i % 8}", "mid": 0.5, "x": [1, 0, 0, 0, 0, 0], "y": 0.0}
                for i in range(48)
            ]
            micro = root / "micro_state.json"
            micro.write_text(
                json.dumps({"samples": samples, "beta": [0, 0, 0, 0, 0, 0], "signals": 0, "opened": 0}),
                encoding="utf-8",
            )

            baseline = root / "baseline"
            aggressive = root / "aggressive"
            order_fields = ["timestamp", "action"]
            fill_fields = ["timestamp", "action", "shares"]
            equity_fields = ["timestamp", "equity", "resting_orders", "positions", "reserved_cash"]
            write_csv(baseline / "maker_order_log.csv", order_fields, [{"timestamp": 1, "action": "POST"}] * 2)
            write_csv(aggressive / "maker_order_log.csv", order_fields, [{"timestamp": 1, "action": "POST"}] * 5)
            write_csv(baseline / "maker_fills.csv", fill_fields, [])
            write_csv(aggressive / "maker_fills.csv", fill_fields, [])
            write_csv(
                baseline / "maker_equity.csv",
                equity_fields,
                [
                    {"timestamp": 1, "equity": 1000, "resting_orders": 2, "positions": 0, "reserved_cash": 20},
                    {"timestamp": 2, "equity": 1000, "resting_orders": 2, "positions": 0, "reserved_cash": 20},
                ],
            )
            write_csv(
                aggressive / "maker_equity.csv",
                equity_fields,
                [
                    {"timestamp": 1, "equity": 1000, "resting_orders": 5, "positions": 0, "reserved_cash": 50},
                    {"timestamp": 2, "equity": 1000, "resting_orders": 5, "positions": 0, "reserved_cash": 50},
                ],
            )

            report = module.build_report(micro, baseline, aggressive)
            self.assertEqual(
                report["micro_taker"]["classification"],
                "REJECT_TAKER_THRESHOLD_RELAXATION_FLAT_TARGET",
            )
            self.assertFalse(report["micro_taker"]["taker_threshold_relaxation_supported"])
            self.assertEqual(report["maker_post_delta"], 3)
            self.assertEqual(report["maker_challenger_state"], "SHADOW_MORE_EVIDENCE_REQUIRED")
            self.assertFalse(report["promotion_ready"])
            self.assertFalse(report["authenticated_execution"])

    def test_nonzero_causal_targets_do_not_trigger_flat_target_rejection(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            samples = [
                {"ts": i + 1, "market_id": "m", "mid": 0.5, "x": [1, 0, 0, 0, 0, 0], "y": 0.001 if i % 2 else -0.001}
                for i in range(40)
            ]
            state.write_text(
                json.dumps({"samples": samples, "beta": [0.0, 0.1, 0, 0, 0, 0], "signals": 0}),
                encoding="utf-8",
            )
            out = module.micro_diagnostics(state)
            self.assertFalse(out["flat_causal_target"])
            self.assertTrue(out["taker_threshold_relaxation_supported"])
            self.assertEqual(out["classification"], "NO_EXECUTABLE_TAKER_EDGE")


if __name__ == "__main__":
    unittest.main()
