import sys
import unittest
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_control_plane as control  # noqa: E402


class V7ControlPlaneTest(unittest.TestCase):
    def test_checked_in_control_plane_is_fail_closed_and_valid(self) -> None:
        hashes = control.validate_repository(ROOT)
        self.assertEqual(set(hashes), {"config/v7_execution_modes.json", "config/v7_live_caps_zero.json", "config/v7_risk_tiers.json", "config/v7_claim_registry.json"})

    def test_nonlive_modes_cannot_submit(self) -> None:
        modes = control.load_json(ROOT / "config/v7_execution_modes.json")
        modes["modes"]["PAPER_SIMULATED"]["submit_orders"] = True
        modes["modes"]["PAPER_SIMULATED"]["signing"] = True
        with self.assertRaisesRegex(control.ControlPlaneError, "unsafe_submit|nonlive_value_authority"):
            control.validate_execution_modes(modes)

    def test_checked_in_live_caps_must_remain_zero(self) -> None:
        caps = control.load_json(ROOT / "config/v7_live_caps_zero.json")
        caps["maximum_order_base_units"] = 1
        with self.assertRaisesRegex(control.ControlPlaneError, "live_caps:nonzero"):
            control.validate_live_caps(caps)

    def test_checked_in_risk_tiers_must_remain_zero(self) -> None:
        tiers = control.load_json(ROOT / "config/v7_risk_tiers.json")
        tiers["tiers"]["MICRO_LIVE"]["maximum_order_base_units"] = 1
        with self.assertRaisesRegex(control.ControlPlaneError, "risk_tiers:nonzero"):
            control.validate_risk_tiers(tiers)

    def test_live_manifest_needs_no_extra_control_field(self) -> None:
        modes = control.load_json(ROOT / "config/v7_execution_modes.json")
        manifest = {"schema_version": 1, "exact_code_sha": "a" * 40, "build_manifest_hash": "b" * 64, "config_bundle_hash": "c" * 64, "strategy_registry_hash": "d" * 64, "model_registry_hash": "e" * 64, "policy_hash": "f" * 64, "dataset_manifest_hash": "1" * 64, "execution_mode": "MICRO_LIVE", "wallet_id_hash": "2" * 64, "signer_session_id_hash": "3" * 64, "server_id": "private-server", "region": "private-region", "run_id": "run", "start_time": "2026-08-31T00:00:00Z"}
        control.validate_control_manifest(manifest, modes)

    def test_control_manifest_schema_matches_the_validator(self) -> None:
        schema = json.loads((ROOT / "schemas/v7/control_manifest.schema.json").read_text(encoding="utf-8"))
        expected = {"schema_version", "exact_code_sha", "build_manifest_hash", "config_bundle_hash",
                    "strategy_registry_hash", "model_registry_hash", "policy_hash", "dataset_manifest_hash",
                    "execution_mode", "wallet_id_hash", "signer_session_id_hash", "server_id", "region",
                    "run_id", "start_time"}
        self.assertEqual(set(schema["required"]), expected)
        self.assertEqual(set(schema["properties"]), expected)
        self.assertNotIn("approval", json.dumps(schema).lower())

if __name__ == "__main__":
    unittest.main()
