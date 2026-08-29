from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V6DataStartupContractTest(unittest.TestCase):
    def test_market_proxy_is_warmed_before_paper_consumers(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        warm_call = 'warm_market_proxy || { echo "fatal: V6 market proxy has no usable public market data"'
        consumers = "start_recorder;start_broker;start_external;write_supervisor"
        self.assertIn("seed_market_proxy_cache(){", loop)
        self.assertIn("polymarket_v6_market_proxy_cache_v1", loop)
        self.assertIn("age <= 300", loop)
        self.assertIn("warm_market_proxy(){", loop)
        self.assertIn("/markets?active=true&closed=false&limit=100", loop)
        self.assertIn("liquidity_num_min=0", loop)
        self.assertIn("for _ in {1..4}; do", loop)
        self.assertIn("--max-time 5", loop)
        self.assertIn("assert isinstance(rows,list) and len(rows)>0", loop)
        self.assertIn("market_proxy_status.json", loop)
        self.assertIn(warm_call, loop)
        self.assertIn(consumers, loop)
        self.assertLess(loop.index("seed_market_proxy_cache\nstart_proxy"), loop.index(warm_call))
        self.assertLess(loop.index(warm_call), loop.index(consumers))

    def test_external_feed_is_materialized_before_external_engine(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        startup_refresh = "refresh_external_feed\nstart_recorder;start_broker;start_external;write_supervisor"
        self.assertIn("refresh_external_feed(){", loop)
        self.assertIn("v6_external_bridge.py", loop)
        self.assertIn("market_key,q_yes,confidence,source,timestamp", loop)
        self.assertIn(startup_refresh, loop)
        self.assertIn("report_startup_ready", loop)
        self.assertIn("v6_startup_data_ready", loop)
        self.assertIn("external_feed_rows=", loop)
        self.assertIn('last_external="$(date +%s)"', loop)

    def test_orphan_recovery_has_a_defined_repository_root(self) -> None:
        loop = (ROOT / "scripts" / "paper_v6_loop.sh").read_text(encoding="utf-8")
        self.assertIn('ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"', loop)
        self.assertIn('pgrep -f "$ROOT/scripts/paper_v6_loop.sh"', loop)


if __name__ == "__main__":
    unittest.main()
