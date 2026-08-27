from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_fast_data_health", ROOT / "scripts" / "validate_fast_data_health.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
validate_status = MODULE.validate_status


def healthy_status() -> dict[str, object]:
    return {
        "mode": "shadow",
        "real_order_submission": False,
        "ws_workers": 4,
        "ws_messages": 120,
        "book_updates": 100,
        "feed_stale_ms": 1200.0,
        "rest_resyncs": 5,
    }


class FastDataHealthTest(unittest.TestCase):
    def test_healthy_read_only_feed_passes(self) -> None:
        failures = validate_status(
            healthy_status(),
            "timestamp_ms,component,message\n",
            max_feed_stale_ms=45000.0,
            min_rest_resyncs=2,
        )
        self.assertEqual(failures, [])

    def test_stale_feed_fails_closed(self) -> None:
        status = healthy_status()
        status["feed_stale_ms"] = 60001.0
        failures = validate_status(
            status,
            "timestamp_ms,component,message\n",
            max_feed_stale_ms=45000.0,
            min_rest_resyncs=2,
        )
        self.assertTrue(any("stale" in failure.lower() for failure in failures))

    def test_missing_rest_refreshes_fail_closed(self) -> None:
        status = healthy_status()
        status["rest_resyncs"] = 1
        failures = validate_status(
            status,
            "timestamp_ms,component,message\n",
            max_feed_stale_ms=45000.0,
            min_rest_resyncs=2,
        )
        self.assertTrue(any("REST book refreshes" in failure for failure in failures))

    def test_rate_limit_is_reported_as_degraded_data(self) -> None:
        failures = validate_status(
            healthy_status(),
            "1710000000000,websocket_shard_0,REST resync: CLOB books HTTP 429: rate limit\n",
            max_feed_stale_ms=45000.0,
            min_rest_resyncs=2,
        )
        self.assertTrue(any("rate limit" in failure.lower() for failure in failures))

    def test_no_websocket_updates_fails_closed(self) -> None:
        status = healthy_status()
        status["ws_messages"] = 0
        status["book_updates"] = 0
        status["feed_stale_ms"] = -1
        failures = validate_status(
            status,
            "timestamp_ms,component,message\n",
            max_feed_stale_ms=45000.0,
            min_rest_resyncs=2,
        )
        self.assertGreaterEqual(len(failures), 3)

    def test_hourly_shadow_uses_current_v7_research_authority(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "fast-arb-hourly.yml").read_text()
        directives = json.loads((ROOT / "config" / "operator_directives.json").read_text())[
            "paper_v7_authorization"
        ]
        config = json.loads((ROOT / "config" / "fast_arb_v7_shadow.json").read_text())
        policy = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text())

        self.assertIn("--config config/fast_arb_v7_shadow.json", workflow)
        self.assertNotIn("paper_v4.json", workflow)
        self.assertIn("--markets 1000", workflow)
        self.assertIn("--min-liquidity 2", workflow)
        self.assertIn("--snapshot-refresh-seconds 5", workflow)

        self.assertEqual(config["gamma_url"], "https://gamma-api.polymarket.com")
        self.assertEqual(config["clob_url"], "https://clob.polymarket.com")
        self.assertTrue(config["scan_only"])
        self.assertFalse(config["history_bootstrap"])
        self.assertTrue(config["paper_only"])
        self.assertFalse(config["authenticated_execution"])
        self.assertEqual(config["market_limit"], directives["market_limit"])
        self.assertEqual(float(config["min_liquidity"]), directives["min_liquidity"])
        self.assertEqual(float(config["min_net_edge"]), directives["min_net_edge"])
        self.assertEqual(float(config["uncertainty_penalty"]), directives["uncertainty_penalty"])
        self.assertEqual(float(config["fractional_kelly"]), directives["fractional_kelly_ceiling"])
        self.assertEqual(float(config["max_drawdown"]), directives["max_drawdown"])
        self.assertEqual(float(policy["min_net_edge"]), directives["min_net_edge"])
        self.assertEqual(float(policy["external_uncertainty_penalty"]), directives["uncertainty_penalty"])


if __name__ == "__main__":
    unittest.main()
