#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from portfolio_supervisor import build_snapshot


class PortfolioSupervisorTest(unittest.TestCase):
    def test_cross_bootstrap_is_bounded_and_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            alpha_status = root / "alpha.json"
            alpha_status.write_text(
                json.dumps({"timestamp": now, "equity": 10010.0, "killed": False}),
                encoding="utf-8",
            )
            config = {
                "starting_capital": 10000.0,
                "global_max_drawdown": 0.15,
                "alpha_allocation_fraction": 0.75,
                "cross_venue_allocation_fraction": 0.25,
                "alpha_engine_baseline_equity": 10000.0,
                "cross_venue_engine_baseline_equity": 2500.0,
                "cross_venue_max_bundle_usd": 25.0,
                "cross_venue_max_bundle_fraction": 0.02,
                "stale_after_seconds": 90,
                "cross_bootstrap_grace_seconds": 180,
                "alpha_status": str(alpha_status),
                "alpha_run_root": str(root / "alpha-run"),
                "cross_venue_status": str(root / "missing-cross.json"),
                "require_alpha_health_for_cross": False,
                "alpha_required": True,
                "cross_venue_required": True,
                "manual_kill_file": str(root / "KILL"),
            }
            result, state = build_snapshot(config, {"started_at": now, "peak_equity": 10000.0})
            gate = result["limits"]["engines"]["cross_venue"]
            self.assertTrue(gate["new_exposure_allowed"])
            self.assertTrue(gate["bootstrap"])
            self.assertLessEqual(gate["max_bundle_usd"], 25.0)
            self.assertEqual(state["global_kill"], False)

    def test_drawdown_kills_both_engines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = int(time.time())
            alpha_status = root / "alpha.json"
            cross_status = root / "cross.json"
            alpha_status.write_text(
                json.dumps({"timestamp": now, "equity": 8000.0, "killed": False}),
                encoding="utf-8",
            )
            cross_status.write_text(
                json.dumps({"timestamp": now, "healthy": True, "equity": 2500.0, "killed": False}),
                encoding="utf-8",
            )
            config = {
                "starting_capital": 10000.0,
                "global_max_drawdown": 0.15,
                "alpha_allocation_fraction": 0.75,
                "cross_venue_allocation_fraction": 0.25,
                "alpha_engine_baseline_equity": 10000.0,
                "cross_venue_engine_baseline_equity": 2500.0,
                "stale_after_seconds": 90,
                "alpha_status": str(alpha_status),
                "alpha_run_root": str(root / "alpha-run"),
                "cross_venue_status": str(cross_status),
                "manual_kill_file": str(root / "KILL"),
            }
            result, state = build_snapshot(config, {"started_at": now, "peak_equity": 10000.0})
            self.assertTrue(result["limits"]["global_kill"])
            self.assertFalse(result["limits"]["engines"]["alpha"]["new_exposure_allowed"])
            self.assertFalse(result["limits"]["engines"]["cross_venue"]["new_exposure_allowed"])
            self.assertTrue(state["global_kill"])


if __name__ == "__main__":
    unittest.main()
