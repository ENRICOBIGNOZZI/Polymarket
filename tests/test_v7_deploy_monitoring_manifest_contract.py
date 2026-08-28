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


if __name__ == "__main__":
    unittest.main()
