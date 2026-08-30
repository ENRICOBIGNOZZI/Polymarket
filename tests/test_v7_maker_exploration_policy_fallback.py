from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7MakerExplorationPolicyFallbackTest(unittest.TestCase):
    def test_canonical_policy_is_used_when_environment_override_is_absent(self) -> None:
        text = (ROOT / "src" / "v7_maker_execution_cells.cpp").read_text(encoding="utf-8")
        function = text[text.index("fs::path maker_policy_path()") : text.index("find_value", text.index("fs::path maker_policy_path()"))]
        self.assertIn('std::getenv("PM_V7_MAKER_POLICY")', function)
        self.assertIn('config/v7_professional_market_maker.json', function)
        self.assertNotIn("return {};", function)

    def test_exploration_policy_remains_paper_only_fail_closed(self) -> None:
        text = (ROOT / "src" / "v7_maker_execution_cells.cpp").read_text(encoding="utf-8")
        function = text[text.index("void populate_exploration_policy") : text.index("Action parse_action")]
        self.assertIn('"paper_only"', function)
        self.assertIn('"authenticated_execution"', function)
        self.assertIn('"real_order_submission"', function)
        self.assertIn('"explore_then_exploit"', function)
        self.assertIn('"confidence_z"', function)
        self.assertIn("confidence_z > std::max(0.0, model.robust_ev_z)", function)
        self.assertIn("model.exploration_enabled = 0", function)


if __name__ == "__main__":
    unittest.main()
