from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "v7_native_critical_path_policy.json"
MANIFEST = ROOT / "config" / "v7_process_manifest.json"
CHECKER = ROOT / "ops" / "verify_v7_native_critical_path.py"
CUTOVER = ROOT / "ops" / "v7_london_cutover.sh"
RUNTIME_MANIFEST = ROOT / "deploy" / "london" / "runtime_manifest.json"

spec = importlib.util.spec_from_file_location("native_gate", CHECKER)
assert spec and spec.loader
native_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_gate)


class NativeCutoverGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = json.loads(POLICY.read_text(encoding="utf-8"))
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_current_parallel_hot_path_is_rejected(self) -> None:
        errors = native_gate.validate(self.policy, self.manifest)
        self.assertTrue(errors)
        self.assertTrue(any("exactly one London-deployed HOT_PATH" in e for e in errors), errors)

    def test_one_native_single_owner_engine_passes(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        for process in manifest["processes"]:
            if process.get("london_deployed"):
                process["runtime_class"] = "COLLECTOR"
                process.pop("authority_overrides", None)
        manifest["processes"].append(
            {
                "id": "crypto_settlement_engine",
                "executable": "${CRYPTO_SETTLEMENT_ENGINE}",
                "runtime_class": "HOT_PATH",
                "london_deployed": True,
                "dependencies": [],
                "authority_overrides": {
                    "capital_allocator": True,
                    "global_portfolio_coordinator": True,
                    "inventory": True,
                    "oms": True,
                    "risk_engine": True,
                },
            }
        )
        self.assertEqual(native_gate.validate(self.policy, manifest), [])

    def test_noncanonical_native_owner_fails(self) -> None:
        manifest = {
            "processes": [
                {
                    "id": "other_native_engine",
                    "executable": "${OTHER_NATIVE_ENGINE}",
                    "runtime_class": "HOT_PATH",
                    "london_deployed": True,
                    "dependencies": [],
                    "authority_overrides": {k: True for k in native_gate.CRITICAL_OWNER_KEYS},
                }
            ]
        }
        errors = native_gate.validate(self.policy, manifest)
        self.assertTrue(any("canonical HOT_PATH process id" in e for e in errors), errors)

    def test_structural_or_monitoring_surface_cannot_be_hot_path(self) -> None:
        for process_id in ("structural_arbitrage", "prometheus_exporter"):
            manifest = {
                "processes": [
                    {
                        "id": process_id,
                        "executable": "${NATIVE_COMPONENT}",
                        "runtime_class": "HOT_PATH",
                        "london_deployed": True,
                        "dependencies": [],
                        "authority_overrides": {k: True for k in native_gate.CRITICAL_OWNER_KEYS},
                    }
                ]
            }
            errors = native_gate.validate(self.policy, manifest)
            self.assertTrue(
                any("cold/legacy/structural surface" in e for e in errors),
                (process_id, errors),
            )

    def test_interpreted_hot_path_fails(self) -> None:
        manifest = {
            "processes": [
                {
                    "id": "bad",
                    "executable": "scripts/bad.py",
                    "runtime_class": "HOT_PATH",
                    "london_deployed": True,
                    "dependencies": [],
                    "authority_overrides": {k: True for k in native_gate.CRITICAL_OWNER_KEYS},
                }
            ]
        }
        errors = native_gate.validate(self.policy, manifest)
        self.assertTrue(any("native C++" in e for e in errors), errors)

    def test_cli_fails_closed_on_current_manifest(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--policy", str(POLICY), "--manifest", str(MANIFEST), "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 78)
        receipt = json.loads(result.stdout)
        self.assertFalse(receipt["ready"])

    def test_cutover_gate_runs_before_any_service_mutation(self) -> None:
        text = CUTOVER.read_text(encoding="utf-8")
        gate = text.index("verify_v7_native_critical_path.py")
        first_stop = text.index("systemctl stop")
        first_restart = text.index("systemctl restart")
        self.assertLess(gate, first_stop)
        self.assertLess(gate, first_restart)

    def test_gate_and_policy_are_shipped_in_runtime_bundle(self) -> None:
        runtime = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
        support = set(runtime["support_files"])
        self.assertIn("ops/verify_v7_native_critical_path.py", support)
        self.assertIn("config/v7_native_critical_path_policy.json", support)


if __name__ == "__main__":
    unittest.main()
