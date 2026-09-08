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
            'trap cleanup EXIT',
            'trap shutdown INT TERM',
            'cleanup_started=0',
            'exit 73',
            '"version":7',
            '"paper_only":true',
            '"authenticated_execution":false',
            '"real_order_submission":false',
        ):
            self.assertIn(required, text)
        self.assertNotIn('trap cleanup EXIT INT TERM', text)

    def test_live_scope_has_one_v7_execution_owner(self) -> None:
        scope = json.loads((ROOT / "config/v7_live_model_scope.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["version"], 7)
        self.assertTrue(scope["paper_only"])
        self.assertFalse(scope["real_order_submission"])
        self.assertTrue(scope["runtime_invariants"]["single_execution_owner"])
        self.assertEqual(scope["runtime_invariants"]["global_portfolio_coordinator"], "V7_GLOBAL_PORTFOLIO_COORDINATOR")

    def test_every_main_sha_gets_exact_single_writer_proof(self) -> None:
        workflow = (ROOT / ".github/workflows/private-runtime-single-writer-validation.yml").read_text(encoding="utf-8")
        push_start = workflow.index("  push:\n")
        pr_start = workflow.index("  pull_request:\n", push_start)
        push_block = workflow[push_start:pr_start]
        self.assertIn("branches: [main]", push_block)
        self.assertNotIn("paths:", push_block)
        pr_block = workflow[pr_start:workflow.index("\npermissions:", pr_start)]
        self.assertIn("branches: [main]", pr_block)
        self.assertNotIn("paths:", pr_block)
        self.assertIn("name: single-writer-v7", workflow)
        self.assertIn(
            'VALIDATION_SHA: ${{ github.event.inputs.expected_sha || github.event.pull_request.head.sha || github.sha }}',
            workflow,
        )
        self.assertIn(
            'ref: ${{ github.event.inputs.expected_sha || github.event.pull_request.head.sha || github.sha }}',
            workflow,
        )
        self.assertIn('test "$(git rev-parse HEAD)" = "$VALIDATION_SHA"', workflow)


if __name__ == "__main__":
    unittest.main()
