from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "scripts/paper_v7_execution_loop.sh"


class V7PaperLoopBash3PortabilityTest(unittest.TestCase):
    def test_optional_joint_model_does_not_expand_empty_array_under_set_u(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertNotIn("joint_args=()", text)
        self.assertNotIn('${joint_args[@]}', text)
        self.assertIn('joint_policy="$RUN_ROOT/learned_execution/joint_policy.json"', text)
        self.assertIn('if [[ -s "$joint_policy" ]]; then', text)
        self.assertIn('--joint-model "$joint_policy"', text)

    def test_cleanup_empty_pid_array_is_guarded(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        self.assertIn('for pid in "${pids[@]:-}"; do kill "$pid"', text)
        self.assertIn('for pid in "${pids[@]:-}"; do wait "$pid"', text)


if __name__ == "__main__":
    unittest.main()
