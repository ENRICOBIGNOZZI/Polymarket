from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "scripts/paper_v7_execution_loop.sh"


class V7PaperLoopBash3PortabilityTest(unittest.TestCase):
    def test_only_crypto_live_algorithm_is_registered(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertNotIn("joint_args=()", text)
        self.assertNotIn('${joint_args[@]}', text)
        self.assertIn("CRYPTO_SETTLEMENT_ENGINE", text)

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

    def test_fencing_supervisor_launcher_matches_current_cli_contract(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        start = text.index("scripts/v7_multi_az_fencing_supervisor.py")
        end = text.index('v7_register_child "$!"', start)
        block = text[start:end]
        self.assertIn('--config "$ROOT/config/v7_failover_fencing.json"', block)
        self.assertIn('--run-id "$RUN_ID"', block)
        self.assertIn('--server-id "$SERVER_ID"', block)
        self.assertIn('--interval-seconds 1', block)
        for stale in (
            "--owner-id", "--lease-id", "--region", "--lease-ms",
            "--renew-ms", "--minimum-remaining-ms", "--poll-ms",
        ):
            self.assertNotIn(stale, block)

    def test_runtime_readiness_requires_live_native_engine_identity(self) -> None:
        text = LOOP.read_text(encoding="utf-8")
        ready = text[text.index("native_engine_ready()") : text.index("write_runtime_status()")]
        for required in (
            'polymarket_v7_native_engine_manager_status_v1',
            'value.get("model_sha")==sys.argv[2]',
            'value.get("state")=="RUNNING"',
            'value.get("paper_only") is True',
            'value.get("authenticated_execution") is False',
            'value.get("real_order_submission") is False',
            'value.get("real_capital_at_risk") is False',
            'value.get("single_native_hot_path") is True',
            'not value.get("blocker")',
            'pid>0 and 0<=age<=15000',
            'os.kill(pid,0)',
        ):
            self.assertIn(required, ready)


if __name__ == "__main__":
    unittest.main()
