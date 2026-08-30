#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "scripts" / "paper_v7_execution_loop.sh"


class MakerChampionChallengerRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = LOOP.read_text(encoding="utf-8")

    def test_runtime_reads_champion_only(self) -> None:
        self.assertIn('MAKER_CHAMPION_MODEL="$RUN_ROOT/micro_maker/execution_model.json"', self.text)
        self.assertIn('export PM_V7_MAKER_EXECUTION_MODEL="$MAKER_CHAMPION_MODEL"', self.text)
        self.assertIn('--model "$MAKER_CHAMPION_MODEL"', self.text)

    def test_refit_writes_challenger_only(self) -> None:
        self.assertIn(
            'MAKER_CHALLENGER_MODEL="$RUN_ROOT/micro_maker/execution_model_challenger.json"',
            self.text,
        )
        refit_start = self.text.index("# Slow-plane exact-SHA fill/markout fit.")
        runtime_start = self.text.index("# Canonical Maker cohort:", refit_start)
        refit_block = self.text[refit_start:runtime_start]
        self.assertIn('--artifact-role challenger', refit_block)
        self.assertIn('--output "$MAKER_CHALLENGER_MODEL"', refit_block)
        self.assertNotIn('--output "$MAKER_CHAMPION_MODEL"', refit_block)
        self.assertIn('scripts/v7_maker_model_registry.py', refit_block)
        self.assertNotIn('--promote', refit_block)

    def test_paper_loop_contains_no_real_execution_enablement(self) -> None:
        self.assertIn('assert v7.get("authenticated_execution") is False', self.text)
        self.assertIn('assert v7.get("real_order_submission") is False', self.text)
        self.assertNotIn('authenticated_execution=true', self.text.lower())
        self.assertNotIn('real_order_submission=true', self.text.lower())


if __name__ == "__main__":
    unittest.main()
