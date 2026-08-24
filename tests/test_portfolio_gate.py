from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from apply_portfolio_gate import apply_gate, read_gate


class PortfolioIntentGateTest(unittest.TestCase):
    def test_missing_or_stale_gate_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed, capital, reason = read_gate(root / "missing.json", "alpha", 1000, 30)
            self.assertFalse(allowed)
            self.assertEqual(capital, 0.0)
            self.assertTrue(reason.startswith("gate_unavailable"))

            path = root / "gate.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "timestamp": 900,
                        "global_kill": False,
                        "engines": {
                            "alpha": {
                                "new_exposure_allowed": True,
                                "capital_limit_usd": 100.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            allowed, capital, reason = read_gate(path, "alpha", 1000, 30)
            self.assertFalse(allowed)
            self.assertEqual(capital, 0.0)
            self.assertEqual(reason, "gate_stale")

    def test_global_kill_and_engine_close_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gate.json"
            base = {
                "schema_version": 1,
                "timestamp": 1000,
                "global_kill": True,
                "engines": {
                    "alpha": {
                        "new_exposure_allowed": True,
                        "capital_limit_usd": 100.0,
                    }
                },
            }
            path.write_text(json.dumps(base), encoding="utf-8")
            self.assertFalse(read_gate(path, "alpha", 1000, 30)[0])

            base["global_kill"] = False
            base["engines"]["alpha"]["new_exposure_allowed"] = False
            base["engines"]["alpha"]["reason"] = "risk_closed"
            path.write_text(json.dumps(base), encoding="utf-8")
            allowed, capital, reason = read_gate(path, "alpha", 1000, 30)
            self.assertFalse(allowed)
            self.assertEqual(capital, 0.0)
            self.assertEqual(reason, "risk_closed")

    def test_bundle_allocation_is_atomic_and_cap_bounded(self) -> None:
        def rows(bundle: str, edge: float, cap: float) -> list[dict[str, str]]:
            common = {
                "bundle_id": bundle,
                "strategy": "B1",
                "event_id": bundle,
                "created_ts": "1000",
                "mode": "MAKER",
                "expected_edge": str(edge),
                "max_notional": str(cap),
                "execution_deadline_ts": "1100",
                "hold_deadline_ts": "1200",
            }
            return [
                {**common, "market_id": bundle + "-a", "side": "YES", "weight": "1", "limit_price": "0.4"},
                {**common, "market_id": bundle + "-b", "side": "NO", "weight": "1", "limit_price": "0.4"},
            ]

        admitted, rejected = apply_gate(rows("high", 0.03, 80.0) + rows("low", 0.01, 80.0), True, 100.0)
        self.assertEqual(rejected, 0)
        grouped: dict[str, list[dict[str, str]]] = {}
        for row in admitted:
            grouped.setdefault(row["bundle_id"], []).append(row)
        self.assertEqual(set(grouped), {"high", "low"})
        self.assertEqual({row["max_notional"] for row in grouped["high"]}, {"80"})
        self.assertEqual({row["max_notional"] for row in grouped["low"]}, {"20"})
        self.assertEqual(sum(float(group[0]["max_notional"]) for group in grouped.values()), 100.0)

        suppressed, rejected = apply_gate(rows("x", 0.02, 50.0), False, 100.0)
        self.assertEqual(suppressed, [])
        self.assertEqual(rejected, 1)


if __name__ == "__main__":
    unittest.main()
