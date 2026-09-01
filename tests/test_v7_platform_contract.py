import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_platform_drift_monitor as platform  # noqa: E402


def load_contract_registry():
    spec = importlib.util.spec_from_file_location("v7_polymarket_v2_contracts_for_platform_test",
                                                  ROOT / "scripts/v7_polymarket_v2_contracts.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


contracts = load_contract_registry()


class PlatformContractTest(unittest.TestCase):
    def test_platform_registry_is_pinned_to_the_v2_pusd_contract_registry(self) -> None:
        value = platform.load(ROOT / "config/v7_platform_contract.json")
        platform.validate_registry(value)
        v2 = contracts.load(ROOT / "config/v7_polymarket_v2_contracts.json")
        self.assertEqual(value["api"]["chain_id"], v2["chain_id"])
        self.assertEqual(value["api"]["production_url"], v2["clob"]["production_url"])
        self.assertEqual(value["contracts"]["pUSD"], v2["collateral"]["proxy"])
        self.assertEqual(value["contracts"]["conditional_tokens"], v2["clob"]["conditional_tokens"])
        self.assertEqual(value["contracts"]["ctf_exchange"], v2["clob"]["ctf_exchange"])
        self.assertEqual(value["contracts"]["neg_risk_ctf_exchange"], v2["clob"]["neg_risk_ctf_exchange"])
        self.assertEqual(value["contracts"]["ctf_collateral_adapter"], v2["collateral"]["ctf_collateral_adapter"])
        self.assertEqual(value["contracts"]["neg_risk_ctf_collateral_adapter"], v2["collateral"]["neg_risk_ctf_collateral_adapter"])
        self.assertEqual(value["data_api"]["activity_max_limit"], 500)
        self.assertTrue(value["data_api"]["activity_timestamp_windows_required"])

    def test_contract_registry_drift_cannot_be_hidden_by_boolean_requirements(self) -> None:
        value = json.loads((ROOT / "config/v7_platform_contract.json").read_text(encoding="utf-8"))
        value["contracts"]["pUSD"] = "0x" + "0" * 40
        with self.assertRaisesRegex(platform.DriftError, "registry_contracts"):
            platform.validate_registry(value)
        value = json.loads((ROOT / "config/v7_platform_contract.json").read_text(encoding="utf-8"))
        value["market_contract"]["fee_schedule"]["snapshot_hash_required"] = False
        with self.assertRaisesRegex(platform.DriftError, "registry_market_contract"):
            platform.validate_registry(value)
        value = json.loads((ROOT / "config/v7_platform_contract.json").read_text(encoding="utf-8"))
        value["data_api"]["activity_sort_direction"] = "DESC"
        with self.assertRaisesRegex(platform.DriftError, "registry_data_api"):
            platform.validate_registry(value)


if __name__ == "__main__":
    unittest.main()
