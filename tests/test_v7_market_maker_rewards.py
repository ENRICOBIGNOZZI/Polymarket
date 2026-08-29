from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "v7_market_maker_rewards", ROOT / "scripts" / "v7_market_maker_rewards.py"
)
assert SPEC and SPEC.loader
rewards = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rewards
SPEC.loader.exec_module(rewards)
SHA = "a" * 40


def _universe(path: Path, *, timestamp_ms: int, model_sha: str = SHA) -> Path:
    path.write_text(json.dumps({
        "schema": "polymarket_v7_adaptive_universe_snapshot_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "model_sha": model_sha,
        "timestamp_ms": timestamp_ms,
        "discovery_exhaustive": True,
        "pagination_loop_guard_hit": False,
        "membership_sha256": "membership",
        "markets": [{
            "market_id": "m1",
            "condition_id": "c1",
            "event_ids": ["e1"],
            "question": "Question?",
            "slug": "question",
            "clob_token_ids": ["yes", "no"],
            "liquidity": 1000.0,
            "volume_24h": 500.0,
            "score": 12.0,
            "active": True,
            "closed": False,
            "accepting_orders": True,
        }],
    }), encoding="utf-8")
    return path


class MakerRewardSelectorTests(unittest.TestCase):
    def test_reward_failure_publishes_safe_fresh_universe_fallback(self) -> None:
        with self.subTest("fresh exact-SHA universe"):
            from tempfile import TemporaryDirectory
            with TemporaryDirectory() as directory:
                now_ms = 1_000_000
                universe = _universe(Path(directory) / "current.json", timestamp_ms=now_ms - 1000)
                with mock.patch.object(rewards, "_primary_snapshot", side_effect=TimeoutError("endpoint")):
                    snapshot = rewards.build_snapshot(
                        ROOT / "config" / "v7_professional_market_maker.json",
                        fallback_universe_path=universe,
                        model_sha=SHA,
                        now_ms=now_ms,
                    )
        self.assertEqual(snapshot["source"], "adaptive_universe_fallback")
        self.assertEqual(snapshot["selection_mode"], "LIQUIDITY_FALLBACK")
        self.assertTrue(snapshot["degraded"])
        self.assertFalse(snapshot["reward_data_available"])
        self.assertTrue(snapshot["paper_only"])
        self.assertFalse(snapshot["authenticated_execution"])
        self.assertFalse(snapshot["real_order_submission"])
        self.assertEqual(snapshot["model_sha"], SHA)
        self.assertEqual(snapshot["selected_count"], 1)
        self.assertEqual(snapshot["markets"][0]["reward_intensity"], 0.0)
        status = rewards.selector_status(snapshot)
        self.assertTrue(status["ready"])
        self.assertEqual(status["state"], "OPERATIONAL_FALLBACK")

    def test_fallback_rejects_stale_or_wrong_sha_universe(self) -> None:
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            universe = _universe(Path(directory) / "current.json", timestamp_ms=1, model_sha="b" * 40)
            with mock.patch.object(rewards, "_primary_snapshot", side_effect=TimeoutError("endpoint")):
                with self.assertRaisesRegex(ValueError, "fallback_universe_contract_invalid"):
                    rewards.build_snapshot(
                        ROOT / "config" / "v7_professional_market_maker.json",
                        fallback_universe_path=universe,
                        model_sha=SHA,
                        now_ms=1_000_000,
                    )

    def test_request_budget_caps_each_network_call(self) -> None:
        seen: list[float] = []

        def fetcher(_url: str, *, timeout: float) -> dict[str, object]:
            seen.append(timeout)
            return {"data": [], "next_cursor": "LTE="}

        pools = rewards.fetch_reward_pools(
            "https://clob.polymarket.com",
            deadline=rewards.time.monotonic() + 0.5,
            request_timeout=5.0,
            max_pages=2,
            fetcher=fetcher,
        )
        self.assertEqual(pools, {})
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0.0)
        self.assertLessEqual(seen[0], 0.5)


if __name__ == "__main__":
    unittest.main()
