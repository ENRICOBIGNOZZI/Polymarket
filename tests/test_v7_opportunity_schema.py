from __future__ import annotations

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OpportunitySchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads((ROOT / "schemas/v7/opportunity_envelope.schema.json").read_text())

    def test_schema_covers_every_typed_optional_surface(self) -> None:
        properties = self.schema["properties"]
        for name in (
            "exploration", "forward_test", "multi_crypto_forward",
            "execution_alpha", "maker_execution_identity", "settlement_model",
        ):
            self.assertIn(name, properties)
        self.assertFalse(self.schema["additionalProperties"])

    def test_crypto_context_has_shared_six_asset_registry(self) -> None:
        context = self.schema["properties"]["crypto_context"]["oneOf"][1]
        assets = context["properties"]["asset"]["enum"]
        self.assertEqual(assets, ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"])

    def test_multi_crypto_forward_lineage_is_mandatory(self) -> None:
        multi = self.schema["properties"]["multi_crypto_forward"]
        required = set(multi["required"])
        self.assertEqual(
            required,
            {
                "mode", "experiment_id", "protocol_hash", "feature_schema_hash",
                "model_hash", "fill_model_hash", "cost_model_hash",
                "settlement_semantic_hash", "latency_profile_id", "asset", "horizon",
                "research_only", "automatic_promotion", "one_entry_per_market",
                "hold_to_settlement", "entry_uses_absolute_fair", "probability_source",
            },
        )
        self.assertEqual(multi["properties"]["mode"]["const"], "PAPER_MULTI_CRYPTO_FORWARD")
        self.assertEqual(multi["properties"]["asset"]["enum"], ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"])
        self.assertEqual(multi["properties"]["horizon"]["enum"], ["M5", "M15"])
        for name in (
            "protocol_hash", "feature_schema_hash", "model_hash", "fill_model_hash",
            "cost_model_hash", "settlement_semantic_hash",
        ):
            self.assertEqual(multi["properties"][name]["pattern"], "^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
