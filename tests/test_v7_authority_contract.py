from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_authority_contract import AuthorityContractError, OWNER_KEYS, validate  # noqa: E402


def registry() -> dict:
    return json.loads((ROOT / "config/v7_authority_registry.json").read_text())


class AuthorityContractTests(unittest.TestCase):
    def test_exactly_one_owner_for_every_authority(self) -> None:
        report = validate(registry())
        self.assertTrue(report["passed"])
        self.assertEqual(report["economic_engine_count"], 2)
        self.assertEqual(report["owner_counts"], {key: 1 for key in OWNER_KEYS})
        self.assertEqual(report["research_zero_authority_count"], 10)

    def test_duplicate_owner_injection_fails_closed(self) -> None:
        value = copy.deepcopy(registry())
        value["owners"]["capital_allocator"] = [
            "V7_CANONICAL_ALLOCATOR", "SECOND_ALLOCATOR",
        ]
        with self.assertRaisesRegex(AuthorityContractError, "capital_allocator"):
            validate(value)

    def test_component_cannot_belong_to_both_engines(self) -> None:
        value = copy.deepcopy(registry())
        value["economic_engines"]["STRUCTURAL_ARB_ENGINE"]["components"].append(
            "professional_maker"
        )
        with self.assertRaises(AuthorityContractError):
            validate(value)

    def test_research_family_cannot_enter_an_economic_engine(self) -> None:
        value = copy.deepcopy(registry())
        value["economic_engines"]["CRYPTO_SETTLEMENT_ENGINE"]["components"].append("osint")
        with self.assertRaises(AuthorityContractError):
            validate(value)


if __name__ == "__main__":
    unittest.main()
