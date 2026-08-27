from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7SingleWriterContractTest(unittest.TestCase):
    def test_direct_v7_runtime_owns_one_lock_and_pid(self) -> None:
        text = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        for required in (
            'LOCK="$CONTROL/runtime.lock"',
            'mkdir "$LOCK"',
            'echo $$ > "$LOCK/pid"',
            'rm -rf "$LOCK"',
            'trap cleanup EXIT INT TERM',
            'exit 73',
            '"version":7',
            '"paper_only":true',
            '"authenticated_execution":false',
            '"real_order_submission":false',
        ):
            self.assertIn(required, text)

    def test_champion_points_only_to_v7_runtime(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["version"], 7)
        self.assertEqual(manifest["loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertFalse(manifest["real_order_submission"])
        self.assertFalse(manifest["legacy_fallback_allowed"])


if __name__ == "__main__":
    unittest.main()
