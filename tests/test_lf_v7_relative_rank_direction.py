#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lf_v7_relative_rank_direction_audit.py"
spec = importlib.util.spec_from_file_location("lf_v7_relative_rank_direction_audit", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class RelativeRankDirectionAuditTest(unittest.TestCase):
    def test_negative_relative_target_does_not_imply_buy_no_profit(self) -> None:
        examples = {x.name: x for x in mod.build_counterexamples()}
        ex = examples["negative_relative_but_positive_absolute_yes_move"]
        self.assertLess(ex.relative_targets[ex.selected_market], 0.0)
        self.assertGreater(ex.absolute_logit_moves[ex.selected_market], 0.0)
        self.assertEqual(ex.mapped_side, "NO")
        self.assertLess(ex.single_leg_markout, 0.0)

    def test_positive_relative_target_does_not_imply_buy_yes_profit(self) -> None:
        examples = {x.name: x for x in mod.build_counterexamples()}
        ex = examples["positive_relative_but_negative_absolute_yes_move"]
        self.assertGreater(ex.relative_targets[ex.selected_market], 0.0)
        self.assertLess(ex.absolute_logit_moves[ex.selected_market], 0.0)
        self.assertEqual(ex.mapped_side, "YES")
        self.assertLess(ex.single_leg_markout, 0.0)

    def test_relative_pair_can_profit_when_single_leg_bottom_loses(self) -> None:
        pnl = mod.paired_relative_markout([0.50, 0.50, 0.50], [0.70, 0.65, 0.60])
        self.assertAlmostEqual(pnl, 0.10, places=12)

    def test_report_keeps_production_fail_closed(self) -> None:
        report = mod.audit_report()
        self.assertEqual(report["research_state"], "MORE_EVIDENCE_REQUIRED")
        self.assertFalse(report["production_changed"])
        self.assertFalse(report["authenticated_execution"])


if __name__ == "__main__":
    unittest.main()
