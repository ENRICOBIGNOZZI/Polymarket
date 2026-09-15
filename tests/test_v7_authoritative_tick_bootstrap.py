from __future__ import annotations

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class AuthoritativeTickBootstrapContractTest(unittest.TestCase):
    def test_api_uses_public_dedicated_tick_endpoint_fail_closed(self) -> None:
        header = (ROOT / "include/pm/api.hpp").read_text()
        self.assertIn('"/tick-size?token_id=" + token_id', header)
        self.assertIn('"minimum_tick_size"', header)
        self.assertIn("response.status < 200 || response.status >= 300", header)
        self.assertIn('"CLOB tick-size HTTP "', header)
        self.assertIn("throw std::runtime_error", header)
        self.assertNotIn("return 0.01", header)

    def test_fair_only_observer_refreshes_both_ticks_each_bootstrap(self) -> None:
        source = (ROOT / "src/v7_maker_fillability_observer.cpp").read_text()
        self.assertIn("options.fair_only", source)
        self.assertIn("api.fetch_tick_size(pair.second.first)", source)
        self.assertIn("api.fetch_tick_size(pair.second.second)", source)
        self.assertIn("fillability cold-start book fetch failed", source)


if __name__ == "__main__":
    unittest.main()
