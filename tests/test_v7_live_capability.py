from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v7_live_capability_test", ROOT / "scripts" / "v7_live_capability.py")
assert spec and spec.loader
capability = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = capability
spec.loader.exec_module(capability)
class LiveCapabilityTests(unittest.TestCase):
    def test_checked_in_config_has_no_live_authority(self) -> None:
        config = json.loads((ROOT / "config" / "paper_v7.json").read_text(encoding="utf-8"))
        capability.validate_checked_in_config(config)

    def test_full_history_secret_findings_block_pre_canary_use(self) -> None:
        with self.assertRaisesRegex(capability.LiveCapabilityError, "secret_scan_not_clean"):
            capability.validate_pre_canary_security(ROOT)

    def test_security_summary_has_no_credential_or_approval_fields(self) -> None:
        summary = capability.security_summary({"safe_for_authenticated_execution": True})
        self.assertEqual(summary, {
            "schema": "polymarket_v7_pre_canary_security_summary_v1",
            "paper_only": True,
            "full_history_secret_scan_clean": True,
        })


if __name__ == "__main__":
    unittest.main()
