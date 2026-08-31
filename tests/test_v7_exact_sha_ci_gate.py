from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_exact_sha_ci_gate", ROOT / "scripts" / "v7_exact_sha_ci_gate.py")
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


class ExactShaCiGateTest(unittest.TestCase):
    def test_latest_successful_attempt_for_every_required_v7_check_is_green(self) -> None:
        value = GATE.receipt("ENRICOBIGNOZZI/Polymarket", "a" * 40, [
            {"id": 1, "name": "ci-v7-Debug", "status": "completed", "conclusion": "failure",
             "completed_at": "2026-08-31T09:00:00Z"},
            {"id": 2, "name": "ci-v7-Debug", "status": "completed", "conclusion": "success",
             "completed_at": "2026-08-31T10:00:00Z"},
            {"id": 3, "name": "ci-v7-Release", "status": "completed", "conclusion": "success",
             "completed_at": "2026-08-31T10:00:00Z"},
        ], 1)
        self.assertTrue(value["exact_sha_ci_green"])
        self.assertEqual(value["checks"]["ci-v7-Debug"]["id"], 2)

    def test_missing_or_failed_required_check_is_not_green(self) -> None:
        value = GATE.receipt("ENRICOBIGNOZZI/Polymarket", "b" * 40, [
            {"id": 1, "name": "ci-v7-Release", "status": "completed", "conclusion": "success",
             "completed_at": "2026-08-31T10:00:00Z"},
        ], 1)
        self.assertFalse(value["exact_sha_ci_green"])
        self.assertIsNone(value["checks"]["ci-v7-Debug"]["id"])


if __name__ == "__main__":
    unittest.main()
