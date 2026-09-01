from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "scripts/paper_v7_execution_loop.sh"


class V7PaperLoopBash3PortabilityTest(unittest.TestCase):
    def test_research_graph_scanner_has_no_execution_worker(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertNotIn("joint_args=()", text)
        self.assertNotIn('${joint_args[@]}', text)
        self.assertIn("v7_graph_rv_executable_intents.py", text)
        self.assertIn('--status "$RUN_ROOT/graph_rv/status.json"', text)
        self.assertNotIn("python3 scripts/v7_graph_rv.py", text)

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

    def test_runtime_readiness_requires_full_external_fair_chain(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        ready = text[text.index("paper_router_ready()") : text.index("write_runtime_status()")]
        for required in (
            'FULL_FAIR_SHADOW_OPERATIONAL',
            'external_fair_required_markets',
            'rules_hash_recognized',
            'settlement_reference',
            'fair.get("valid") is True',
            'oracle.get("healthy") is True',
            'external.get("healthy") is True',
            'book_requests',
            'decision.get("books") or 0)==2',
        ):
            self.assertIn(required, ready)


if __name__ == "__main__":
    unittest.main()
