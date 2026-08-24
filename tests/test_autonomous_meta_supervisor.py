#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "meta_supervisor_v2", SCRIPTS / "meta_supervisor_v2.py"
)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class AutonomousMetaSupervisorTests(unittest.TestCase):
    def test_skipped_dispatchable_worker_requires_recovery(self) -> None:
        spec = {"dispatchable": True, "requires_current_main": False, "max_age_seconds": 0}
        latest = {
            "status": "completed",
            "conclusion": "skipped",
            "updated_ts": 1_000,
            "head_sha": "a" * 40,
        }
        result = module.classify_workflow(spec, latest, "a" * 40, 2_000, 300)
        self.assertEqual(result["state"], "failed")
        self.assertTrue(result["dispatch_needed"])

    def test_missing_autonomous_product_is_unhealthy(self) -> None:
        health = module._autonomous_product_health({}, {}, 2_000)
        self.assertFalse(health["healthy"])
        self.assertIn("autonomous_research_product_missing", health["reasons"])

    def test_waiting_runtime_is_acceptable_when_server_deploy_is_disabled(self) -> None:
        snapshot = {
            "server_deploy_enabled": False,
            "products": {
                "autonomous_research": {
                    "generated_ts": 1_900,
                    "status": "WAITING_RUNTIME",
                    "invariants": {
                        "append_only_external_store": True,
                        "bounded_allowlisted_research": True,
                        "real_order_submission": False,
                    },
                }
            },
        }
        health = module._autonomous_product_health({}, snapshot, 2_000)
        self.assertTrue(health["healthy"], health)

    def test_waiting_runtime_is_not_acceptable_when_server_deploy_is_enabled(self) -> None:
        snapshot = {
            "server_deploy_enabled": True,
            "products": {
                "autonomous_research": {
                    "generated_ts": 1_900,
                    "status": "WAITING_RUNTIME",
                    "invariants": {
                        "append_only_external_store": True,
                        "bounded_allowlisted_research": True,
                        "real_order_submission": False,
                    },
                }
            },
        }
        health = module._autonomous_product_health({}, snapshot, 2_000)
        self.assertFalse(health["healthy"])
        self.assertTrue(
            any(reason.startswith("autonomous_research_reported_status") for reason in health["reasons"]),
            health,
        )


if __name__ == "__main__":
    unittest.main()
