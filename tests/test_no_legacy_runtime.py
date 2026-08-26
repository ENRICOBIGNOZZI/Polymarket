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

    def test_champion_is_explicitly_disabled(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["enabled"])
        for key in ("version", "loop", "config", "run_root"):
            self.assertIsNone(manifest[key])
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])

    def test_live_smoke_is_v7_only_and_fail_closed(self) -> None:
        cfg = json.loads((ROOT / "config/v7_live_smoke.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["engine_version"], 7)
        self.assertTrue(cfg["paper_only"])
        self.assertFalse(cfg["authenticated_execution"])
        self.assertTrue(cfg["scan_only"])
        self.assertEqual(cfg["fractional_kelly"], 0.0)
        self.assertTrue(cfg["expert_weights"])
        self.assertTrue(all(float(value) == 0.0 for value in cfg["expert_weights"].values()))
        self.assertNotIn("v6", cfg)
        self.assertNotIn("legacy_compatibility", cfg)

        workflow = (ROOT / ".github/workflows/live-smoke.yml").read_text(encoding="utf-8")
        self.assertIn("--config config/v7_live_smoke.json", workflow)
        self.assertIn("--paper --scan-only", workflow)
        self.assertIn("fills.csv", workflow)
        self.assertNotIn("config/paper.example.json", workflow)

    def test_engine_default_config_is_existing_v7_smoke(self) -> None:
        source = (ROOT / "src/main.cpp").read_text(encoding="utf-8")
        self.assertIn('config/v7_live_smoke.json', source)
        self.assertNotIn('config/paper.example.json', source)
        self.assertTrue((ROOT / "config/v7_live_smoke.json").is_file())


if __name__ == "__main__":
    unittest.main()
