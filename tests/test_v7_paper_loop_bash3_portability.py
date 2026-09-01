from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "scripts/paper_v7_execution_loop.sh"


class V7PaperLoopBash3PortabilityTest(unittest.TestCase):
    def test_only_two_live_algorithms_are_registered(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertNotIn("joint_args=()", text)
        self.assertNotIn('${joint_args[@]}', text)
        self.assertIn("CRYPTO_SETTLEMENT_ENGINE", text)
        self.assertIn("STRUCTURAL_ARB_ENGINE", text)
        for removed in ("v7_graph_rv_executable_intents.py", "v7_micro_taker_worker.py", "v7_research_shadow_supervisor.py"):
            self.assertNotIn(removed, text)

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

    def test_removed_collectors_are_not_launched(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        for removed in ("limitless", "kalshi", "sports_latency", "cross_platform", "osint"):
            self.assertNotIn(removed, text.lower())


if __name__ == "__main__":
    unittest.main()
