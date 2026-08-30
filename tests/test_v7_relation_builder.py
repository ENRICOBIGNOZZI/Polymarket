from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_relation_builder import build  # noqa: E402

SHA = "f" * 40


def universe(markets: list[dict]) -> dict:
    return {
        "schema": "polymarket_v7_adaptive_universe_snapshot_v1",
        "paper_only": True, "model_sha": SHA, "membership_sha256": "membership",
        "markets": markets,
    }


def market(identifier: str, event: str, *, neg_risk: bool = True) -> dict:
    return {
        "market_id": identifier, "condition_id": f"condition-{identifier}",
        "event_ids": [event], "clob_token_ids": [f"yes-{identifier}", f"no-{identifier}"],
        "outcomes": ["Yes", "No"], "neg_risk": neg_risk,
        "resolution_source": "official-rules", "fee_schedule": {"rate": 0},
    }


class RelationBuilderTests(unittest.TestCase):
    def test_same_event_negrisk_membership_builds_verified_mutex_relation(self) -> None:
        registry = build(universe([market("a", "e"), market("b", "e")]),
                         model_sha=SHA, now_ms=1000)
        self.assertEqual(registry["verified_relations"], 1)
        relation = registry["relations"][0]
        self.assertEqual(relation["relation_type"], "MUTUAL_EXCLUSION")
        self.assertEqual(relation["authority"], "VERIFIED_DETERMINISTIC")
        self.assertEqual(relation["settlement_compatibility"], "SAME_NEGRISK_EVENT")
        self.assertEqual(len(relation["rules_hash"]), 64)

    def test_text_similarity_and_unverified_membership_never_activate(self) -> None:
        registry = build(universe([
            market("a", "e1"), market("b", "e2"),
            market("looks-similar", "e1", neg_risk=False),
        ]), model_sha=SHA, now_ms=1000)
        self.assertEqual(registry["verified_relations"], 0)
        self.assertFalse(registry["text_similarity_confers_authority"])


if __name__ == "__main__":
    unittest.main()
