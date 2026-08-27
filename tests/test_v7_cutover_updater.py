from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7CutoverUpdaterTest(unittest.TestCase):
    def test_updater_prevalidates_exact_candidate_before_checkout_mutation(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn('log "Validating exact V7 candidate $EXPECTED_SHA before active-checkout mutation"', text)
        self.assertIn("git -C \"$APP_DIR\" worktree add --detach \"$candidate\" \"$EXPECTED_SHA\"", text)
        self.assertIn("scripts/v7_cutover_contract.py", text)
        self.assertIn("ctest --test-dir build --output-on-failure", text)
        self.assertLess(text.index("prevalidate_candidate"), text.rindex('git checkout --detach "$EXPECTED_SHA"'))

    def test_updater_stops_only_registered_production_runtime_owner(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn('control/runtime_status.json', text)
        self.assertIn('Stopping production V7 pid=$pid only', text)
        self.assertNotIn("pkill -f 'scripts/paper_v7_execution_loop.sh'", text)
        self.assertNotIn('pgrep -af "paper_v7_execution_loop.sh"', text)

    def test_updater_deploys_new_canonical_loop_and_health_surfaces(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        for required in (
            "scripts/paper_v7_execution_loop.sh",
            "config/paper_v7.json",
            "control/runtime_status.json",
            "control/portfolio_state.json",
            "control/allocations/manifest.json",
            "graph_rv/status.json",
            "canonical_economics.json",
            "ledger/execution.jsonl",
            "trade_tape.csv",
            "monitoring/exporter_v7.py",
            "polymarket_v7_ledger_valid 1",
            "api/dashboards/uid",
            "control/deployed_sha",
        ):
            self.assertIn(required, text)
        for retired in (
            "scripts/paper_v7_loop.sh",
            "v7_supervisor.json",
            "v7_execution_supervisor.json",
            "market_proxy_status.json",
            "v7_execution_evidence.json",
        ):
            self.assertNotIn(retired, text)

    def test_updater_requires_main_and_paper_validated_same_exact_sha(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn('[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]]', text)
        self.assertIn('[[ "$VALIDATED_SHA" == "$EXPECTED_SHA" ]]', text)
        self.assertIn('[[ "$(git rev-parse HEAD)" == "$EXPECTED_SHA" ]]', text)

    def test_updater_never_restores_legacy_writer(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        self.assertIn("assert_no_legacy_writer", text)
        # Retired generation names are allowed only as kill/fail-closed
        # detection patterns. They must never become start/deploy commands.
        for generation in ("v3", "v4", "v5", "v6"):
            pattern = f"scripts/paper_{generation}_loop.sh"
            self.assertIn(pattern, text)
            self.assertNotIn(f"bash {pattern}", text)
            self.assertNotIn(f"nohup {pattern}", text)
        self.assertNotIn("start_legacy", text)
        self.assertNotIn("rollback_v6", text)
        self.assertNotIn("git checkout paper-validated~", text)

    def test_monitoring_manifest_is_v2_and_canonical(self) -> None:
        manifest = json.loads((ROOT / "monitoring/v7_monitoring_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "polymarket_v7_monitoring_manifest_v2")
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertIn("control/runtime_status.json", manifest["required_surfaces"])
        self.assertIn("canonical_economics.json", manifest["required_surfaces"])
        self.assertIn("ledger/execution.jsonl", manifest["required_surfaces"])


if __name__ == "__main__":
    unittest.main()
