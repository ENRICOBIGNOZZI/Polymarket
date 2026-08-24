from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FastRuntimeContractTest(unittest.TestCase):
    def test_fast_engine_is_shadow_only_and_separate(self) -> None:
        policy = json.loads((ROOT / "config" / "fast_arb_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["mode"], "shadow")
        self.assertFalse(policy["real_order_submission"])
        self.assertGreaterEqual(policy["min_net_edge"], 0.0005)
        self.assertGreater(policy["conversion_fixed_cost_usd"], 0.0)

        runtime = ROOT / "src" / "fast_runtime"
        source = (ROOT / "src" / "fast_arb_main.cpp").read_text(encoding="utf-8")
        source += "\n" + "\n".join(
            path.read_text(encoding="utf-8") for path in sorted(runtime.glob("part*.inc"))
        )
        self.assertIn("wss://ws-subscriptions-clob.polymarket.com/ws/market", source)
        self.assertIn("fast_arb_status.json", source)
        self.assertIn('"real_order_submission", false', source)
        self.assertNotIn("PRIVATE_KEY", source)
        self.assertNotIn("--execute", source)
        self.assertNotIn("/order", source)

        transport = (ROOT / "src" / "fast_ws.cpp").read_text(encoding="utf-8")
        self.assertIn("boost/beast/websocket.hpp", transport)
        self.assertIn("SSL_set_tlsext_host_name", transport)
        self.assertNotIn("curl_ws_", transport)

    def test_runtime_selector_supervises_fast_and_champion_planes(self) -> None:
        selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
        self.assertIn("polymarket_fast_arb_shadow", selector)
        self.assertIn('POLYMARKET_FAST_ARB_REQUIRED:-0', selector)
        self.assertIn("shadow_dependency_failure", selector)
        self.assertIn("champion continues without fast shadow", selector)
        self.assertIn("runtime_planes.csv", selector)
        self.assertIn("start_champion", selector)
        self.assertIn("start_fast", selector)
        self.assertIn("--print-champion", selector)

    def test_hourly_operational_and_theory_schedulers_are_distinct(self) -> None:
        operational = (ROOT / ".github" / "workflows" / "fast-arb-hourly.yml").read_text(
            encoding="utf-8"
        )
        theory = (ROOT / ".github" / "workflows" / "arb-theory-hourly.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('cron: "7 * * * *"', operational)
        self.assertIn("polymarket_fast_arb_shadow", operational)
        self.assertIn('status["ws_messages"] > 0', operational)
        self.assertIn('status["book_updates"] > 0', operational)
        self.assertIn("arb_theory_scheduler.py", operational)
        self.assertIn('cron: "37 * * * *"', theory)
        self.assertIn("research/auto-fast-arb-policy", theory)
        self.assertIn("gh pr create --draft", theory)
        self.assertNotIn("gh pr merge", theory)
        self.assertNotIn("--admin", theory)

    def test_generated_candidate_is_fail_closed(self) -> None:
        generated = (ROOT / "include" / "pm" / "generated" / "fast_arb_candidate_policy.hpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("kRealOrderSubmission = false", generated)
        self.assertIn("kPromotionReady = false", generated)


if __name__ == "__main__":
    unittest.main()
