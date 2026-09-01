from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_external_source_registry as registry


class ExternalSourceRegistryTest(unittest.TestCase):
    def test_canonical_registry_is_v7_paper_only_and_fingerprinted(self) -> None:
        result = registry.validate(registry.load(ROOT / "config/v7_external_source_registry.json"))
        self.assertEqual(result["source_count"], 37)
        self.assertEqual(len(result["registry_sha256"]), 64)
        self.assertIn("deribit_btc", result["source_ids"])
        self.assertIn("coinbase_spot_btcusd_rest_snapshot", result["source_ids"])
        self.assertIn("kalshi_public_rest", result["source_ids"])
        value = registry.load(ROOT / "config/v7_external_source_registry.json")
        crypto_assets = {row["asset"] for row in value["sources"] if row["asset"] in {"BTC", "ETH", "SOL", "XRP"}}
        self.assertEqual(crypto_assets, {"BTC", "ETH", "SOL", "XRP"})
        self.assertTrue(all(
            row["enabled"] is False and row["research_only"] is True
            for row in value["sources"] if row["asset"] in {"ETH", "SOL", "XRP"}
        ))

    def test_rejects_private_authority_and_unknown_event(self) -> None:
        value = registry.load(ROOT / "config/v7_external_source_registry.json")
        value["execution_authority"] = True
        with self.assertRaisesRegex(registry.SourceRegistryError, "private_authority"):
            registry.validate(value)
        value["execution_authority"] = False
        value["sources"][0]["event_kinds"] = ["INVENTED_EVENT"]
        with self.assertRaisesRegex(registry.SourceRegistryError, "event_kind"):
            registry.validate(value)

    def test_registry_is_json_roundtrip_stable(self) -> None:
        value = registry.load(ROOT / "config/v7_external_source_registry.json")
        copy = json.loads(json.dumps(value))
        self.assertEqual(registry.validate(value)["registry_sha256"], registry.validate(copy)["registry_sha256"])

    def test_launcher_uses_the_registry_and_single_compatibility_boundary(self) -> None:
        launcher = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        self.assertIn("v7_external_source_registry.py", launcher)
        for canonical, aliases in registry.load(ROOT / "config/v7_external_source_registry.json")["environment_compatibility"].items():
            self.assertIn(canonical, launcher)
            for alias in aliases:
                self.assertIn(alias, launcher)


if __name__ == "__main__":
    unittest.main()
