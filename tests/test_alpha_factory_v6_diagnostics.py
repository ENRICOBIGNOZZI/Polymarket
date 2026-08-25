#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "alpha_factory_v6_diagnostics", ROOT / "scripts" / "alpha_factory_v6_diagnostics.py"
)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class AlphaFactoryV6DiagnosticsTests(unittest.TestCase):
    def live(self) -> dict:
        return {
            "run_root": "paper_v6_live",
            "git_sha": "5e26fca4f3dacdc52ec316336e927ed5efc095b8",
            "generated_ts": 1787652905,
            "data_health": {
                "trade_recorder": {
                    "status": "healthy",
                    "fields": {
                        "markets": 220,
                        "new_trades": 118,
                        "errors": 0,
                        "last_trade_ts": 1787652847,
                    },
                }
            },
            "logs": {
                "maker": [
                    "maker_tick markets=220 signals=13 posted=8 resting=8 positions=0 reserved=180 cash=1200 equity=1200 drawdown=0 killed=0"
                ],
                "multileg": [
                    "multileg_tick bundles=0 resting=0 complete=0 aborting=0 closed=0 unwound=0 trades_processed=118 tape_cursor=118 reserved=0 cash=5000 equity=5000 drawdown=0 killed=0"
                ],
            },
            "metrics": {
                "polymarket_runtime_info": {
                    "labels": 'adapter="v6",run_root="paper_v6_live",version="v6"',
                    "value": 1,
                },
                "polymarket_runtime_equity_usd": 10000,
                "polymarket_runtime_gross_exposure_usd": 180,
                "polymarket_runtime_reserved_cash_usd": 680,
                "polymarket_runtime_realized_pnl_usd_total": 0,
                "polymarket_runtime_oos_trades": 0,
            },
        }

    def test_exact_current_v6_zero_fill_state_is_visible(self) -> None:
        diag = mod.build_v6_diagnostics(self.live())
        self.assertTrue(diag["detected"])
        self.assertEqual(diag["micro_maker"]["signals"], 13)
        self.assertEqual(diag["micro_maker"]["posted"], 8)
        self.assertEqual(diag["micro_maker"]["positions"], 0)
        self.assertTrue(diag["micro_maker"]["zero_fill_with_fresh_data"])
        self.assertEqual(diag["trade_recorder"]["new_trades"], 118)
        self.assertEqual(diag["trade_recorder"]["trade_age_seconds"], 58)

    def test_zero_fill_routes_to_queue_research_not_threshold_relaxation(self) -> None:
        experiments = mod.recommend_v6_experiments(mod.build_v6_diagnostics(self.live()))
        identifiers = [item["experiment_id"] for item in experiments]
        self.assertIn("v6_micro_queue_fillability", identifiers)
        self.assertIn("v6_rv_admission_attribution", identifiers)
        text = " ".join(item.get("do_not_do", "") for item in experiments)
        self.assertIn("do not lower taker edge", text)

    def test_stale_or_unhealthy_data_does_not_claim_queue_bottleneck(self) -> None:
        live = self.live()
        live["data_health"]["trade_recorder"]["status"] = "degraded"
        diag = mod.build_v6_diagnostics(live)
        self.assertFalse(diag["micro_maker"]["zero_fill_with_fresh_data"])
        identifiers = [x["experiment_id"] for x in mod.recommend_v6_experiments(diag)]
        self.assertNotIn("v6_micro_queue_fillability", identifiers)

    def test_non_v6_payload_fails_closed(self) -> None:
        report = mod.build_report({"run_root": "paper_v5_live", "logs": {}})
        self.assertFalse(report["diagnostics"]["detected"])
        self.assertEqual(report["next_experiments"], [])
        self.assertTrue(report["research_only"])
        self.assertFalse(report["authenticated_execution"])
        self.assertFalse(report["direct_champion_mutation"])

    def test_malformed_tick_is_bounded(self) -> None:
        self.assertEqual(mod.parse_tick("not-a-tick"), {})
        self.assertEqual(mod.parse_tick("maker_tick signals=3 bad=x posted=2")["signals"], 3)


if __name__ == "__main__":
    unittest.main()
