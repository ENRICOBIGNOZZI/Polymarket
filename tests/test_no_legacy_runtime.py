from __future__ import annotations

import json
import re
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

    def test_retired_runtime_and_control_plane_paths_are_absent(self) -> None:
        retired = (
            # Numerical-generation / compatibility entrypoints.
            "scripts/paper_latest_loop.sh",
            "scripts/multi_strategy_paper.py",
            "scripts/incumbent_health_gate.py",
            "scripts/tiny_live_pilot.py",
            "scripts/build_v4_intents.py",
            "scripts/merge_v4_intents.py",
            "scripts/walk_forward_v4.py",
            "scripts/walk_forward_v4_lineage.py",
            "scripts/filter_coherent_hedges.py",
            # Deleted monolithic engine and parallel state owners.
            "include/pm/engine.hpp",
            "src/engine.cpp",
            "src/main.cpp",
            "src/trade_recorder.cpp",
            "src/rewards_scan.cpp",
            "scripts/run_paper.sh",
            "scripts/runtime_action_report.py",
            "scripts/runtime_contract_health.py",
            "scripts/runtime_singleton_launcher.py",
            # Deleted duplicate maker/Fast surfaces.
            "config/fast_arb_shadow.json",
            "config/v7_complete_set_maker.json",
            "scripts/v7_complete_set_maker.py",
            "tests/test_v7_complete_set_maker.py",
            "tests/test_v7_complete_set_maker_rolling_flow.py",
            # Deleted duplicate orchestration/evidence generations.
            ".github/workflows/live-smoke.yml",
            ".github/workflows/control-plane.yml",
            ".github/workflows/control-plane-event-bridge.yml",
            ".github/workflows/post-merge-validation.yml",
            ".github/workflows/alpha-factory.yml",
            "scripts/summarize_live_smoke.py",
            "scripts/meta_supervisor.py",
            "scripts/meta_supervisor_v2.py",
            "scripts/alpha_factory.py",
            "scripts/calibrate_forward_maker.py",
            "scripts/finalize_forward_probe.py",
            "scripts/forward_maker_probe.py",
            "scripts/select_forward_candidates.py",
            # Generic deployment/bootstrap paths replaced by V7-specific paths.
            "ops/apply_runtime_config_macos.sh",
            "ops/bootstrap_macos.sh",
            "ops/bootstrap_server.sh",
            "ops/capture_runtime_health_macos.sh",
            "ops/finish_bootstrap_macos.sh",
            "ops/install_autoupdate_macos.sh",
            "ops/macos_service_control.sh",
            "ops/update_server.sh",
            "ops/update_server_macos.sh",
            # Retired monitoring/runtime binaries.
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
        self.assertEqual(offenders, [], "retired executable/control-plane paths remain")

    def test_active_runtime_surfaces_do_not_reference_retired_entrypoints(self) -> None:
        forbidden = (
            "paper_v3",
            "paper_v4",
            "paper_v5",
            "paper_v6",
            "paper.example",
            "polymarket_engine",
            "pm/engine.hpp",
            "broker_state.csv",
            "legacy_compatibility",
            "fast_arb_shadow.json",
            "v7_complete_set_maker",
            "runtime_singleton_launcher.py",
            "scripts/run_paper.sh",
            "live-smoke.yml",
            "alpha-factory.yml",
        )
        files: list[Path] = []
        for directory in ("src", "include/pm", "ops", "monitoring"):
            base = ROOT / directory
            if base.exists():
                files.extend(path for path in base.rglob("*") if path.is_file())
        for relative in (
            "scripts/paper_v7_execution_loop.sh",
            "config/paper_v7.json",
            "config/live_champion.json",
            "config/scheduler_registry.json",
        ):
            path = ROOT / relative
            if path.is_file():
                files.append(path)

        offenders: list[str] = []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8").lower()
            except UnicodeDecodeError:
                continue
            hits = [token for token in forbidden if token in text]
            if hits:
                offenders.append(f"{path.relative_to(ROOT)}: {', '.join(hits)}")
        self.assertEqual(offenders, [], "active V7 surfaces reference retired entrypoints")

    def test_only_canonical_v7_operational_ownership_exists(self) -> None:
        required = (
            "scripts/paper_v7_execution_loop.sh",
            "config/paper_v7.json",
            "scripts/v7_execution_ledger.py",
            "scripts/v7_ledger_spool.py",
            "scripts/v7_capital_allocator.py",
            "scripts/v7_portfolio_guard.py",
            "monitoring/exporter_v7.py",
            "monitoring/prometheus_v7.yml",
            "monitoring/grafana/dashboards/polymarket-v7.json",
            "ops/update_server_v7.sh",
            "src/v7_trade_recorder.cpp",
        )
        missing = [path for path in required if not (ROOT / path).is_file()]
        self.assertEqual(missing, [], "canonical V7 ownership surfaces missing")

        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertNotIn("polymarket_engine", cmake)
        self.assertNotIn("src/engine.cpp", cmake)
        self.assertNotIn("src/trade_recorder.cpp", cmake)
        self.assertIn("polymarket_v7_trade_recorder", cmake)

    def test_champion_manifest_is_forced_v7_only(self) -> None:
        manifest = json.loads((ROOT / "config/live_champion.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["paper_only"])
        self.assertFalse(manifest["authenticated_execution"])
        self.assertTrue(manifest["enabled"])
        self.assertEqual(manifest["version"], 7)
        self.assertFalse(manifest.get("real_order_submission"))
        self.assertFalse(manifest.get("legacy_fallback_allowed"))
        self.assertEqual(manifest["loop"], "scripts/paper_v7_execution_loop.sh")
        self.assertEqual(manifest["config"], "config/paper_v7.json")
        self.assertEqual(manifest["run_root"], "runs/paper_v7_live")
        self.assertEqual(manifest["deployment_ref"], "paper-validated")
        self.assertTrue((ROOT / manifest["loop"]).is_file())
        self.assertTrue((ROOT / manifest["config"]).is_file())

    def test_scheduler_registry_has_no_deleted_workflows(self) -> None:
        registry = json.loads((ROOT / "config/scheduler_registry.json").read_text(encoding="utf-8"))
        entries = registry.get("schedulers") or []
        workflows = [str(entry.get("workflow") or "") for entry in entries]
        ids = [str(entry.get("id") or "") for entry in entries]
        for workflow in workflows:
            self.assertTrue((ROOT / workflow).is_file(), f"registered workflow missing: {workflow}")
        retired_ids = {
            "meta-supervisor",
            "alpha-factory",
            "live-api-smoke",
            "control-plane-event-bridge",
            "post-merge-validation",
        }
        self.assertTrue(retired_ids.isdisjoint(ids))


if __name__ == "__main__":
    unittest.main()
