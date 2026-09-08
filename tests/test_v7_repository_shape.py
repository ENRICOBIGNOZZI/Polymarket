from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TEXT_SCAN_ALLOWLIST = {
    "config/operator_directives.json",    # explicit prohibition/history
    "scripts/v7_archive_market_universe.py",  # archive boundary documentation
}
RETIRED_TEXT = re.compile(
    r"(?i)(?:paper[_-]?v[1-6]|v[1-6][_-](?:runtime|broker|ledger|scheduler|config|paper)|"
    r"(?:fallback|start|run)[_-]?v[1-6])"
)


class V7RepositoryShapeTest(unittest.TestCase):
    def test_no_versioned_v3_v6_paths_remain(self) -> None:
        bad = []
        repository_paths = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT
        ).decode("utf-8").split("\0")
        for raw in repository_paths:
            rel = raw.lower()
            if any(token in rel for token in ("paper_v3", "paper_v4", "paper_v5", "paper_v6", "/v3_", "/v4_", "/v5_", "/v6_", "_v3.", "_v4.", "_v5.", "_v6.")):
                bad.append(rel)
        self.assertEqual(sorted(bad), [])

    def test_private_validation_output_is_ignored_untracked_and_guarded(self) -> None:
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z", "--", ".private_validation", "deploy-evidence.txt"], cwd=ROOT
        ).decode("utf-8").split("\0")
        self.assertEqual([path for path in tracked if path], [])

        for probe in (".private_validation/probe.json", "deploy-evidence.txt"):
            ignored = subprocess.run(
                ["git", "check-ignore", "--quiet", probe], cwd=ROOT, check=False
            )
            self.assertEqual(ignored.returncode, 0, probe)

        for hook in ("pre-commit", "pre-push"):
            path = ROOT / ".githooks" / hook
            self.assertTrue(path.is_file(), hook)
            self.assertTrue(path.stat().st_mode & 0o111, hook)

    def test_all_tracked_operational_text_has_no_retired_generation_surface(self) -> None:
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"], cwd=ROOT
        ).decode("utf-8").split("\0")
        bad: list[str] = []
        for rel in tracked:
            if not rel or rel in TEXT_SCAN_ALLOWLIST or rel.startswith("tests/"):
                continue
            path = ROOT / rel
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            match = RETIRED_TEXT.search(text)
            if match:
                bad.append(f"{rel}:{match.group(0)}")
        self.assertEqual(sorted(bad), [])

    def test_workflows_are_v7_or_core_validation_only(self) -> None:
        allowed = {"ci.yml", "monitoring.yml", "private-runtime-single-writer-validation.yml"}
        bad = []
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            if path.name not in allowed and not path.name.startswith("v7-"):
                bad.append(path.name)
        self.assertEqual(sorted(bad), [])

    def test_live_scope_is_v7_only(self) -> None:
        scope = json.loads((ROOT / "config/v7_live_model_scope.json").read_text(encoding="utf-8"))
        self.assertEqual(scope["version"], 7)
        self.assertTrue(scope["paper_only"])
        self.assertFalse(scope["authenticated_execution"])
        self.assertFalse(scope["real_order_submission"])
        self.assertEqual(scope["live_algorithm_count"], 2)
        self.assertTrue(scope["runtime_invariants"]["single_execution_owner"])
    def test_canonical_v7_surfaces_exist(self) -> None:
        required = (
            "scripts/paper_v7_execution_loop.sh",
            "scripts/v7_execution_ledger.py",
            "scripts/v7_canonical_economics.py",
            "scripts/v7_capital_allocator.py",
            "scripts/v7_portfolio_guard.py",
            "scripts/v7_process_manifest.py",
            "config/paper_v7.json",
            "config/v7_crypto_settlement_engine.json",
            "config/v7_crypto_settlement_markets.json",
            "config/v7_crypto_settlement_model_registry.json",
            "scripts/v7_crypto_settlement.py",
            "config/v7_professional_market_maker.json",
            "monitoring/exporter_v7.py",
            "monitoring/grafana/dashboards/polymarket-v7.json",
            ".github/workflows/v7-live-paper-validation.yml",
            ".github/workflows/v7-deploy-paper-server.yml",
            ".github/workflows/v7-paper-server-health.yml",
        )
        missing = [path for path in required if not (ROOT / path).is_file()]
        self.assertEqual(missing, [])

    def test_main_ruleset_is_machine_actionable_and_requires_unique_v7_checks(self) -> None:
        ruleset = json.loads((ROOT / "artifacts/github_main_ruleset.json").read_text(encoding="utf-8"))
        self.assertEqual(ruleset["target"], "branch")
        self.assertEqual(ruleset["enforcement"], "active")
        self.assertEqual(ruleset["conditions"]["ref_name"]["include"], ["~DEFAULT_BRANCH"])
        by_type = {rule["type"]: rule for rule in ruleset["rules"]}
        self.assertIn("deletion", by_type)
        self.assertIn("non_fast_forward", by_type)
        self.assertIn("required_linear_history", by_type)
        pull_request = by_type["pull_request"]["parameters"]
        self.assertTrue(pull_request["require_code_owner_review"])
        self.assertTrue(pull_request["require_last_push_approval"])
        self.assertEqual(pull_request["required_approving_review_count"], 1)
        required = by_type["required_status_checks"]["parameters"]["required_status_checks"]
        self.assertEqual(
            {row["context"] for row in required},
            {"ci-v7-Release", "ci-v7-Debug", "security-audit-v7", "sanitizer-v7", "monitoring-v7", "single-writer-v7"},
        )

    def test_final_docs_describe_two_engines_and_no_independent_component_authority(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        btc = (ROOT / "docs/v7_world_class/crypto_settlement_engine.md").read_text(encoding="utf-8")
        dashboard = (ROOT / "monitoring/grafana/dashboards/polymarket-v7.json").read_text(encoding="utf-8")
        for token in (
            "CRYPTO_SETTLEMENT_ENGINE", "STRUCTURAL_ARB_ENGINE",
            "V7_GLOBAL_PORTFOLIO_COORDINATOR", "one allocator", "one risk owner",
            "one OMS", "one inventory owner", "one append-only ledger writer",
            "exactly two live PAPER algorithms",
        ):
            self.assertIn(token, readme)
        self.assertIn("None is an authority owner", btc)
        self.assertNotIn("registry authority owner", btc)
        self.assertNotIn("by Sleeve", dashboard)
        self.assertNotIn('\"title\":\"Sleeves\"', dashboard)
        self.assertIn("Crypto Settlement Contexts", dashboard)
        self.assertIn("{{asset}} {{horizon}}", dashboard)

    def test_source_sbom_covers_declared_build_dependencies(self) -> None:
        sbom = json.loads((ROOT / "artifacts/v7_sbom.spdx.json").read_text(encoding="utf-8"))
        self.assertEqual(sbom["spdxVersion"], "SPDX-2.3")
        names = {package["name"] for package in sbom["packages"]}
        self.assertEqual(names, {"polymarket-v7", "Boost", "OpenSSL", "libcurl", "Python"})

    def test_ci_runs_both_full_history_secret_scanners(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("name: security-audit-v7", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("scripts/v7_secret_scan.py --repository-root . --history --fail-on-findings", workflow)
        self.assertIn("scripts/v7_entropy_secret_scan.py --repository-root . --history --fail-on-findings", workflow)

    def test_ci_runs_address_and_undefined_behavior_sanitizers(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("name: sanitizer-v7", workflow)
        self.assertIn("build-ASan-UBSan", workflow)
        self.assertIn("-fsanitize=address,undefined", workflow)
        self.assertIn("ASAN_OPTIONS=detect_leaks=1 UBSAN_OPTIONS=halt_on_error=1", workflow)


if __name__ == "__main__":
    unittest.main()
