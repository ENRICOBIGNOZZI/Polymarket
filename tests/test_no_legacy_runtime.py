from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class NoLegacyRuntimeContractTest(unittest.TestCase):
    def test_versioned_v3_v6_surfaces_are_absent(self) -> None:
        patterns = (
            "config/paper_v[3-6].json",
            "config/v[3-6]_*.json",
            "scripts/paper_v[3-6]*",
            "scripts/v[3-6]_*.py",
            "scripts/v[3-6]_*.sh",
            ".github/workflows/v[3-6]*.yml",
            ".github/workflows/v[3-6]*.yaml",
            "tests/test_v[3-6]_*.py",
            "monitoring/exporter_v[3-6].py",
            "research/**/*v[3-6]*",
            "research/**/*V[3-6]*",
            "docs/**/*v[3-6]*",
            "docs/**/*V[3-6]*",
        )
        offenders: list[str] = []
        for pattern in patterns:
            offenders.extend(str(path.relative_to(ROOT)) for path in ROOT.glob(pattern))
        self.assertEqual(sorted(set(offenders)), [], "legacy versioned surfaces remain")

    def test_known_compatibility_entrypoints_are_absent(self) -> None:
        retired = (
            "scripts/paper_latest_loop.sh",
            "scripts/multi_strategy_paper.py",
            "scripts/incumbent_health_gate.py",
            "scripts/tiny_live_pilot.py",
            "scripts/build_v4_intents.py",
            "scripts/merge_v4_intents.py",
            "scripts/walk_forward_v4.py",
            "scripts/walk_forward_v4_lineage.py",
            "scripts/filter_coherent_hedges.py",
            ".github/workflows/forward-maker-research.yml",
            ".github/workflows/deploy-paper-server.yml",
            ".github/workflows/server-health.yml",
            ".github/workflows/grafana-access.yml",
            "monitoring/exporter.py",
            "monitoring/exporter_latest.py",
            "monitoring/grafana/dashboards/polymarket-fast-paper.json",
            "monitoring/grafana/dashboards/polymarket-multi-strategy.json",
            "monitoring/grafana/dashboards/polymarket-v6-model-operations.json",
            "src/negrisk_arb.cpp",
            "src/stat_arb.cpp",
            "src/pca_stat_arb.cpp",
            "src/maker_paper.cpp",
            "src/multileg_paper.cpp",
        )
        offenders = [path for path in retired if (ROOT / path).exists()]
        self.assertEqual(offenders, [], "known compatibility entrypoints remain")

    def test_champion_manifest_is_disabled_candidate_or_operator_forced_v7(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        if manifest["enabled"] is False:
            for key in ("version", "loop", "config", "run_root"):
                self.assertIsNone(manifest[key])
            return

        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["version"], 7)
        self.assertFalse(manifest.get("real_order_submission"))
        self.assertEqual(manifest["loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v7.json")
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertTrue((ROOT / manifest["loop"]).is_file())
        self.assertTrue((ROOT / manifest["config"]).is_file())

        # "operator_forced_v7_paper_champion" means V7 is the only allowed
        # destination; it does not waive the exact-SHA lifecycle.  Before
        # main -> paper-validated -> deploy, the same manifest may therefore be
        # candidate-only.  Project-context validation separately requires the
        # runtime/Grafana operational state when this flag becomes false.
        self.assertIn(manifest.get("candidate_only_until_promoted"), (True, False))
        self.assertFalse(manifest.get("legacy_fallback_allowed"))


if __name__ == "__main__":
    unittest.main()
