from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7CutoverUpdaterContractTest(unittest.TestCase):
    def test_platform_wrappers_delegate_only_to_exact_sha_v7_updater(self) -> None:
        for rel in ("ops/update_server.sh", "ops/update_server_macos.sh"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("EXPECTED_VALIDATED_SHA", text)
            self.assertIn("origin/main", text)
            self.assertIn("origin/paper-validated", text)
            self.assertIn("ops/update_server_v7.sh", text)
            self.assertIn('git show "$EXPECTED_SHA:ops/update_server_v7.sh"', text)
            for forbidden in (
                "paper_latest_loop.sh",
                "runtime_contract_health.py",
                "monitoring/exporter.py",
                "monitoring/exporter_latest.py",
                "test_monitoring_exporter.py",
                "test_grafana_multi_strategy_contract.py",
            ):
                self.assertNotIn(forbidden, text)

    def test_canonical_updater_is_v7_only_same_sha_and_fail_closed(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        for required in (
            '[[ "$MAIN_SHA" == "$EXPECTED_SHA" ]]',
            '[[ "$VALIDATED_SHA" == "$EXPECTED_SHA" ]]',
            "scripts/v7_cutover_contract.py",
            "monitoring/v7_monitoring_manifest.json",
            "monitoring/exporter_v7.py",
            "monitoring/v7_ledger_metrics.py",
            "polymarket-v7.json",
            "assert_no_legacy_writer",
            "stop_v7_runtime",
            "failed_health",
            "authenticated_execution",
            "drawdown",
        ):
            self.assertIn(required, text)
        self.assertNotIn("git merge-base --is-ancestor \"$VALIDATED_SHA\" \"$MAIN_SHA\"", text)
        for forbidden in (
            "tests/test_monitoring_exporter.py",
            "tests/test_monitoring_latest_exporter.py",
            "tests/test_runtime_contract_health.py",
            "tests/test_grafana_multi_strategy_contract.py",
            "monitoring/exporter.py monitoring/exporter_latest.py",
            "scripts/paper_latest_loop.sh",
            "v5_runtime_readiness.py",
        ):
            self.assertNotIn(forbidden, text)

    def test_candidate_validation_happens_before_active_checkout_mutation(self) -> None:
        text = (ROOT / "ops/update_server_v7.sh").read_text(encoding="utf-8")
        validate = text.index("Validating exact V7 candidate")
        checkout = text.index('git checkout --detach "$EXPECTED_SHA"')
        self.assertLess(validate, checkout)
        health_fail = text.index("failed_health")
        stop_after = text.rfind("stop_v7_runtime", 0, health_fail)
        self.assertGreater(stop_after, checkout)


if __name__ == "__main__":
    unittest.main()
