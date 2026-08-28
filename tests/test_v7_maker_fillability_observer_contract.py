from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class V7MakerFillabilityObserverContractTest(unittest.TestCase):
    def test_observer_is_read_only_bounded_and_exact_sha(self) -> None:
        source = (ROOT / "src" / "v7_maker_fillability_observer.cpp").read_text(encoding="utf-8")
        self.assertIn("polymarket_v7_maker_fillability_ws_trade_v1", source)
        self.assertIn("SpscRing<TradeEvidence, kEvidenceCapacity>", source)
        self.assertIn('event["paper_only"] = true', source)
        self.assertIn('event["authenticated_execution"] = false', source)
        self.assertIn('event["real_order_submission"] = false', source)
        self.assertIn("dropped_events", source)
        self.assertIn("decoder_failures", source)
        self.assertIn("connection_epoch", source)
        self.assertIn("lineage_continuous", source)
        self.assertNotIn("StrategyIntent", source)
        self.assertNotIn("OmsOrder", source)
        self.assertNotIn("SleeveCapitalAccount", source)

    def test_runtime_launches_observer_without_execution_authority(self) -> None:
        loop = (ROOT / "scripts" / "paper_v7_execution_loop.sh").read_text(encoding="utf-8")
        self.assertIn("polymarket_v7_maker_fillability_observer", loop)
        self.assertIn("PM_V7_WS_JSON_ARENA_FILLABILITY_MAX_BYTES", loop)
        self.assertIn("fillability_observer.log", loop)
        self.assertIn("fillability_ws_status.json", (ROOT / "src" / "v7_maker_fillability_observer.cpp").read_text(encoding="utf-8"))

    def test_build_contains_exact_ws_observer(self) -> None:
        cmake = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("add_executable(polymarket_v7_maker_fillability_observer", cmake)
        self.assertIn("pm_v7_common pm_fast_arb pm_core", cmake)


if __name__ == "__main__":
    unittest.main()
