from __future__ import annotations

import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import v7_cross_sectional_tail_relative as tail_relative


class FrozenEvaluationHorizonsTest(unittest.TestCase):
    def config(self, name: str) -> dict[str, object]:
        path = ROOT / "config" / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_frozen_run_excludes_new_prospective_horizons(self) -> None:
        cfg = self.config("research_v7_cross_sectional_rank_frozen.json")
        self.assertEqual(tail_relative.frozen_evaluation_horizons(cfg), [120, 360])

    def test_dedicated_prospective_run_uses_its_explicit_horizon(self) -> None:
        cfg = self.config("research_v7_cross_sectional_rank_15m.json")
        self.assertEqual(tail_relative.frozen_evaluation_horizons(cfg), [15])

    def test_registration_must_be_subset_of_explicit_horizons(self) -> None:
        cfg = self.config("research_v7_cross_sectional_rank_frozen.json")
        invalid = deepcopy(cfg)
        invalid["frequency_registration"]["predeclared_discovery_selected_horizons_minutes"] = [120, 720]
        with self.assertRaisesRegex(ValueError, "present in horizons_minutes"):
            tail_relative.frozen_evaluation_horizons(invalid)

    def test_predeclared_and_prospective_horizons_must_be_disjoint(self) -> None:
        cfg = self.config("research_v7_cross_sectional_rank_frozen.json")
        invalid = deepcopy(cfg)
        invalid["frequency_registration"]["new_prospective_challenger_horizons_minutes"] = [60, 120]
        with self.assertRaisesRegex(ValueError, "must be disjoint"):
            tail_relative.frozen_evaluation_horizons(invalid)


if __name__ == "__main__":
    unittest.main()
