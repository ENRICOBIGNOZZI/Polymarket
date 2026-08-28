from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7DeployMonitoringManifestContractTest(unittest.TestCase):
    def test_deploy_preflight_matches_canonical_monitoring_manifest_v2(self) -> None:
        workflow = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text(encoding="utf-8")
        manifest = json.loads((ROOT / "monitoring/v7_monitoring_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "polymarket_v7_monitoring_manifest_v2")
        self.assertEqual(manifest["version"], 7)
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertIn("assert m.get('schema') == 'polymarket_v7_monitoring_manifest_v2'", workflow)
        self.assertNotIn("polymarket_v7_monitoring_manifest_v1", workflow)
        self.assertIn("assert m.get('run_root') == 'runs/paper_v7_live'", workflow)
        required = set(manifest["required_surfaces"])
        for surface in (
            "control/runtime_status.json",
            "control/portfolio_state.json",
            "control/allocations/manifest.json",
            "graph_rv/status.json",
            "canonical_economics.json",
            "ledger/execution.jsonl",
            "trade_tape.csv",
        ):
            self.assertIn(surface, required)
            self.assertIn(surface, workflow)

    def test_macos_ssh_deploy_bootstraps_homebrew_and_runtime_tools(self) -> None:
        workflow = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn('/opt/homebrew/bin/brew', workflow)
        self.assertIn('/usr/local/bin/brew', workflow)
        self.assertIn('eval "$("$brew_bin" shellenv)"', workflow)
        self.assertIn('for cmd in cmake pkg-config prometheus grafana', workflow)
        self.assertIn('brew install $missing', workflow)
        for command in ("cmake", "pkg-config", "prometheus", "grafana"):
            self.assertIn(f'command -v "$cmd" >/dev/null 2>&1', workflow)
        reconcile = workflow.index('Reconcile exact paper-validated V7 SHA on server')
        bootstrap = workflow.index('eval "$("$brew_bin" shellenv)"', reconcile)
        fetch = workflow.index('git fetch --no-tags origin main paper-validated', reconcile)
        self.assertLess(bootstrap, fetch)

    def test_recovery_is_reserved_for_ssh_transport_loss_and_rechecks_full_health(self) -> None:
        workflow = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text(encoding="utf-8")
        self.assertIn('if [[ "$primary_status" -ne 255 ]]; then', workflow)
        self.assertIn('V7 remote deploy failed with non-transport status=$primary_status', workflow)
        self.assertIn('test "$(cat "$root/control/deployed_sha")" = "$EXPECTED_VALIDATED_SHA"', workflow)
        for required in (
            "control/runtime_status.json",
            "control/portfolio_state.json",
            "graph_rv/status.json",
            "canonical_economics.json",
            "trade_tape.csv",
            "polymarket_v7_execution_alive 1",
            "polymarket_v7_ledger_valid 1",
            "127.0.0.1:9090/-/ready",
            "127.0.0.1:3000/api/health",
            "api/dashboards/uid/$uid",
        ):
            self.assertIn(required, workflow)


if __name__ == "__main__":
    unittest.main()
