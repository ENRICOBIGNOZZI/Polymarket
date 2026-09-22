from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("v7_fee_reward_registry", ROOT / "scripts/v7_fee_reward_registry.py")
assert SPEC and SPEC.loader
registry = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = registry
SPEC.loader.exec_module(registry)
SHA = "a" * 40


def universe(market: dict) -> dict:
    return {
        "schema": "polymarket_v7_crypto_universe_snapshot_v1", "model_sha": SHA,
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "markets": [{"market_id": "m", "condition_id": "c", "clob_token_ids": ["y", "n"],
                     "active": True, "closed": False, "accepting_orders": True, **market}],
    }


class FeeRewardRegistryTests(unittest.TestCase):
    def test_unknown_fee_is_not_executable_and_unknown_reward_is_zero(self) -> None:
        value = registry.build(universe({}), {}, model_sha=SHA, now_ms=1_000)
        row = value["markets"][0]
        self.assertFalse(row["executable_under_registry"])
        self.assertEqual(row["non_executable_reason"], "UNKNOWN_FEE")
        self.assertEqual(row["reward"]["expected_value_usd"], 0.0)
        self.assertFalse(row["reward"]["verified"])

    def test_authoritative_disabled_fee_and_unscoped_reward_is_forced_zero(self) -> None:
        rewards = {
            "source": "crypto_universe_recent_flow", "reward_data_available": True, "timestamp_ms": 900,
            "markets": [{"condition_id": "c", "rewards_max_spread_cents": 3,
                         "rewards_min_size": 20, "total_daily_rate": 5,
                         "reward_touch_qualifies_at_selection": True}],
        }
        value = registry.build(
            universe({"fees_enabled": False, "fees_enabled_explicit": True}), rewards,
            model_sha=SHA, now_ms=1_000,
        )
        row = value["markets"][0]
        self.assertTrue(row["executable_under_registry"])
        self.assertEqual(row["fee"]["source"], "gamma:fees_disabled")
        self.assertFalse(row["reward"]["verified"])
        self.assertEqual(row["reward"]["expected_value_usd"], 0.0)
        self.assertEqual(row["reward"]["payout_status"], "NOT_ATTRIBUTED")

    def test_fee_schedule_formula_is_preserved(self) -> None:
        value = registry.build(universe({"fee_schedule": {"rate": .07, "exponent": 2, "takerOnly": True}}), {}, model_sha=SHA, now_ms=1_000)
        fee = value["markets"][0]["fee"]
        self.assertTrue(fee["verified"])
        self.assertEqual(fee["rate"], .07)
        self.assertEqual(fee["exponent"], 2)


    def test_wallet_reward_audit_is_verified_but_not_market_allocatable(self) -> None:
        user = "0x" + "12" * 20
        payload = {
            "data": {
                "proxy_wallet": user,
                "interval": "1d",
                "fidelity": "1h",
                "source_fidelity": "canonical",
                "points": [
                    {"timestamp": 100, "maker_rebate": 1.0, "reward_income": 2.0, "sponsored_income": 0.5},
                    {"timestamp": 3700, "maker_rebate": 1.4, "reward_income": 2.25, "sponsored_income": 0.7},
                ],
            }
        }
        value = registry.parse_wallet_reward_audit(payload, user, 4_000_000)
        self.assertTrue(value["verified"])
        self.assertFalse(value["allocatable_to_market"])
        self.assertEqual(value["allocation_reason"], "WALLET_LEVEL_NOT_MARKET_ATTRIBUTABLE")
        self.assertAlmostEqual(value["maker_rebate_delta_pusd"], 0.4)
        self.assertAlmostEqual(value["reward_income_delta_pusd"], 0.25)
        self.assertAlmostEqual(value["sponsored_income_delta_pusd"], 0.2)
        self.assertEqual(value["delta_seconds"], 3600)
        rendered = json.dumps(value)
        self.assertNotIn(user.lower(), rendered.lower())
        self.assertEqual(len(value["wallet_sha256"]), 64)

    def test_wallet_reward_audit_fails_closed_when_unconfigured(self) -> None:
        value = registry.parse_wallet_reward_audit({}, "", 1_000)
        self.assertFalse(value["configured"])
        self.assertFalse(value["verified"])
        self.assertFalse(value["allocatable_to_market"])
        self.assertEqual(value["reason"], "WALLET_UNCONFIGURED_OR_INVALID")
        self.assertIsNone(value["wallet_sha256"])



if __name__ == "__main__":
    unittest.main()
