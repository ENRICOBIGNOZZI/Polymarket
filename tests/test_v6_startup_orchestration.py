#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V6StartupOrchestrationTests(unittest.TestCase):
    def test_live_v6_phases_gamma_consumers_and_keeps_hard_safety(self) -> None:
        champion = json.loads((ROOT / "config" / "live_champion.json").read_text(encoding="utf-8"))
        self.assertEqual(champion["version"], 6)
        self.assertEqual(champion["loop"], "scripts/paper_v6_loop.sh")

        config = json.loads((ROOT / "config" / "paper_v6.json").read_text(encoding="utf-8"))
        self.assertTrue(config["v6"]["paper_only"])
        self.assertEqual(config["max_drawdown"], 0.15)
        self.assertEqual(config["max_gross_fraction"], 0.45)
        self.assertEqual(config["slippage_bps"], 5.0)

        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn('RECORDER_MARKETS="${V6_RECORDER_MARKETS:-$MARKETS}"', loop)
        self.assertIn('RECORDER_INTERVAL_SECONDS="${V6_RECORDER_INTERVAL_SECONDS:-10}"', loop)
        self.assertIn('CORE_STARTUP_GAP_SECONDS="${V6_CORE_STARTUP_GAP_SECONDS:-20}"', loop)
        self.assertIn('MAKER_STARTUP_DELAY_SECONDS="${V6_MAKER_STARTUP_DELAY_SECONDS:-40}"', loop)
        self.assertIn('MICRO_STARTUP_DELAY_SECONDS="${V6_MICRO_STARTUP_DELAY_SECONDS:-55}"', loop)
        self.assertIn('HARD_ARB_STARTUP_DELAY_SECONDS="${V6_HARD_ARB_STARTUP_DELAY_SECONDS:-70}"', loop)
        self.assertIn('RELATION_STARTUP_DELAY_SECONDS="${V6_RELATION_STARTUP_DELAY_SECONDS:-90}"', loop)
        self.assertIn('FACTOR_STARTUP_DELAY_SECONDS="${V6_FACTOR_STARTUP_DELAY_SECONDS:-120}"', loop)
        self.assertIn("child['interval_seconds']=max(30,int(child.get('interval_seconds',5)))", loop)

        recorder = loop.index("start_recorder;start_broker")
        gap = loop.index('sleep "$CORE_STARTUP_GAP_SECONDS"', recorder)
        external = loop.index("start_external;write_supervisor", gap)
        self.assertLess(recorder, gap)
        self.assertLess(gap, external)
        self.assertIn("if ((now-startup_epoch>=MAKER_STARTUP_DELAY_SECONDS));then", loop)
        self.assertIn("last_factor=$((startup_epoch-60+FACTOR_STARTUP_DELAY_SECONDS))", loop)
        self.assertNotIn('V6_RECORDER_MARKETS:-1200', loop)


if __name__ == "__main__":
    unittest.main()
