#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_v6 import V6Collector  # noqa: E402


class MonitoringV6ExporterTests(unittest.TestCase):
    def test_market_proxy_health_and_source_are_exported_without_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "paper_v6_live"
            run.mkdir(parents=True)
            now = int(time.time())
            (run / "runtime_status.json").write_text(
                json.dumps({"strategies": {}, "relations": {}, "local_factor": {}, "external_bridge": {}}),
                encoding="utf-8",
            )
            (run / "allocator_status.json").write_text(
                json.dumps({"models_expected": 5, "models_alive": 5, "global_gross_fraction": 0.02}),
                encoding="utf-8",
            )
            (run / "market_proxy_status.json").write_text(
                json.dumps(
                    {
                        "timestamp": now - 7,
                        "source": "clob_fallback",
                        "markets": 220,
                        "upstream_gamma_ok": False,
                        "failures": 3,
                        "last_error": "sensitive upstream diagnostic must not become a metric label",
                        "cache_age_seconds": 4.5,
                        "paper_only": True,
                    }
                ),
                encoding="utf-8",
            )

            collector = V6Collector(run, ROOT / "config" / "paper_v6.json", 20)
            text = collector.collect()
            self.assertIn("polymarket_v6_market_proxy_state_present 1", text)
            self.assertIn("polymarket_v6_market_proxy_upstream_gamma_ok 0", text)
            self.assertIn("polymarket_v6_market_proxy_failures_total 3", text)
            self.assertIn("polymarket_v6_market_proxy_cache_age_seconds 4.5", text)
            self.assertIn("polymarket_v6_market_proxy_markets 220", text)
            self.assertIn('polymarket_v6_market_proxy_info{source="clob_fallback"} 1', text)
            self.assertIn("polymarket_v6_market_proxy_status_age_seconds", text)
            self.assertNotIn("sensitive upstream diagnostic", text)

    def test_missing_market_proxy_status_is_explicitly_unhealthy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary) / "paper_v6_live"
            run.mkdir(parents=True)
            collector = V6Collector(run, ROOT / "config" / "paper_v6.json", 20)
            text = collector.collect()
            self.assertIn("polymarket_v6_market_proxy_state_present 0", text)
            self.assertIn("polymarket_v6_market_proxy_upstream_gamma_ok 0", text)
            self.assertIn('polymarket_v6_market_proxy_info{source="missing"} 0', text)
            self.assertIn("polymarket_v6_market_proxy_status_age_seconds 1e+12", text)


if __name__ == "__main__":
    unittest.main()
