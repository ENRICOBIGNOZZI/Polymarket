from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("promotion_gate", ROOT / "scripts" / "promotion_gate.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

class PromotionGateV7OnlyTests(unittest.TestCase):
    def test_only_canonical_v7_execution_loop_is_recovery_surface(self) -> None:
        self.assertTrue(MODULE.OPERATIONAL_RECOVERY_PATH.fullmatch("scripts/paper_v7_execution_loop.sh"))
        self.assertFalse(MODULE.OPERATIONAL_RECOVERY_PATH.fullmatch("scripts/paper_loop.sh"))

    def test_v7_paper_config_is_economic_surface(self) -> None:
        self.assertTrue(MODULE.is_economic_surface("config/paper_v7.json"))
        self.assertTrue(MODULE.requires_source_content_match("config/paper_v7.json"))
        self.assertEqual(MODULE.promotion_class(["config/paper_v7.json"]), "economic")

    def test_operational_docs_do_not_require_economic_evidence(self) -> None:
        self.assertEqual(MODULE.promotion_class(["docs/README.md"]), "operational")

if __name__ == "__main__":
    unittest.main()
