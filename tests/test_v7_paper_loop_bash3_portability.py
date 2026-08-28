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

    def test_cleanup_empty_pid_array_is_guarded_and_bounded(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        cleanup = text[text.index("pids=()") : text.index('if [[ ! -x "$RECORDER" ]]')]
        self.assertIn('for pid in "${pids[@]:-}"; do', cleanup)
        self.assertIn('kill -TERM "$pid"', cleanup)
        self.assertIn('for _ in $(seq 1 50); do', cleanup)
        self.assertIn('kill -KILL "$pid"', cleanup)
        self.assertIn('wait "$pid"', cleanup)
        self.assertIn("cleanup_started=0", cleanup)
        self.assertIn("shutdown()", cleanup)
        self.assertIn("trap cleanup EXIT", cleanup)
        self.assertIn("trap shutdown INT TERM", cleanup)


if __name__ == "__main__":
    unittest.main()
