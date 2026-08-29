from __future__ import annotations

import importlib.util
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


if __name__ == "__main__":
    unittest.main()
