from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V6ExternalFeedStartupTest(unittest.TestCase):
    def test_external_feed_is_materialized_before_external_engine_starts(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn("refresh_external_feed(){", loop)
        self.assertIn("market_key,q_yes,confidence,source,timestamp", loop)
        startup = loop.index("wait_for_owned_proxy ||")
        refresh = loop.index("refresh_external_feed\n", startup)
        external_start = loop.index("start_recorder;start_broker;start_external;write_supervisor", startup)
        self.assertLess(refresh, external_start)
        self.assertIn('last_external="$(date +%s)"', loop)

    def test_periodic_refresh_uses_same_fail_closed_materializer(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        periodic = loop.index("if ((now-last_external>=60));then")
        tail = loop[periodic: periodic + 220]
        self.assertIn("refresh_external_feed", tail)
        self.assertNotIn("v6_external_bridge.py --output", tail)


if __name__ == "__main__":
    unittest.main()
