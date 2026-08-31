from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v7_polymarket_v2_contracts_test", ROOT / "scripts" / "v7_polymarket_v2_contracts.py")
assert spec and spec.loader
contracts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = contracts
spec.loader.exec_module(contracts)
REGISTRY = ROOT / "config" / "v7_polymarket_v2_contracts.json"


class V2ContractRegistryTests(unittest.TestCase):
    def test_pinned_registry_is_v2_pusd_polygon_and_checked_in_safe(self) -> None:
        value = contracts.load(REGISTRY)
        self.assertEqual(value["collateral"]["asset"], "pUSD")
        self.assertEqual(value["clob"]["exchange_eip712_domain_version"], "2")
        self.assertEqual(value["clob"]["user_websocket_url"], "wss://ws-subscriptions-clob.polymarket.com/ws/user")
        self.assertEqual(len(contracts.registry_hash(REGISTRY)), 64)
        paper = json.loads((ROOT / "config" / "paper_v7.json").read_text(encoding="utf-8"))
        self.assertEqual(paper["v7"]["contract_registry"], "config/v7_polymarket_v2_contracts.json")
        self.assertTrue(paper["v7"]["require_v2_contract_registry"])
        self.assertTrue(paper["v7"]["real_pnl_provenance_required"])
        self.assertTrue(paper["v7"]["real_pnl_economic_scorecard_required"])

    def test_legacy_domain_or_mutated_address_fails_closed(self) -> None:
        value = json.loads(REGISTRY.read_text(encoding="utf-8"))
        value["clob"]["exchange_eip712_domain_version"] = "1"
        with self.assertRaisesRegex(contracts.ContractRegistryError, "v2_identity"):
            contracts.validate(value)
        value = json.loads(REGISTRY.read_text(encoding="utf-8"))
        value["collateral"]["proxy"] = "0x" + "0" * 40
        value["collateral"]["implementation"] = value["collateral"]["proxy"]
        with self.assertRaisesRegex(contracts.ContractRegistryError, "duplicate_contract"):
            contracts.validate(value)


if __name__ == "__main__":
    unittest.main()
