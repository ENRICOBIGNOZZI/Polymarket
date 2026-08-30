from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.v7_generate_economic_artifacts import generate  # noqa: E402


class EconomicArtifactPackTests(unittest.TestCase):
    def test_missing_runtime_is_explicit_and_all_required_reports_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "artifacts"
            files = generate(ROOT, root / "missing-run", output, ROOT / "artifacts/v7_economic_loop_baseline.json")
            expected = {
                "v7_economic_loop_postchange.json", "v7_replay_comparison.json",
                "v7_profitability_audit.json", "v7_capability_runtime_proof.json",
                "v7_reconciliation_report.json", "v7_external_fair_forecast_to_pnl.json",
                "v7_maker_bilateral_fillability_report.json", "v7_arb_coverage_report.json",
                "v7_research_shadow_report.json", "v7_lineage_report.json",
            }
            self.assertEqual(set(files), expected)
            for name in expected:
                value = json.loads((output / name).read_text())
                self.assertTrue(value.get("paper_only"))
                self.assertFalse(value.get("authenticated_execution"))
            proof = json.loads((output / "v7_capability_runtime_proof.json").read_text())
            self.assertFalse(proof["runtime_evidence_available"])
            self.assertFalse(proof["profitability_proven"])


if __name__ == "__main__":
    unittest.main()
