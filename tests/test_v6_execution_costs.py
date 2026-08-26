from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("v6_queue_filter_test", SCRIPTS / "v6_queue_filter.py")
assert spec and spec.loader
qf = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = qf
spec.loader.exec_module(qf)

from v6_micro_target import label_matured_samples


class V6ExecutionMeasurementTest(unittest.TestCase):
    def test_explicit_fee_disabled_never_falls_back(self) -> None:
        details = qf.resolve_fee_details(
            {"conditionId": "abc", "feesEnabled": False},
            "https://clob.test",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no lookup expected")),
        )
        self.assertFalse(details.enabled)
        self.assertEqual(qf.fee_amount(100.0, 0.5, details, taker=True), 0.0)

    def test_clob_fee_descriptor_beats_conservative_fallback(self) -> None:
        calls = []

        def request(url, *_args):
            calls.append(url)
            return {"feesEnabled": True, "fd": {"rate": 0.04, "exponent": 1.0, "takerOnly": True}}

        details = qf.resolve_fee_details({"conditionId": "abc"}, "https://clob.test", request)
        self.assertEqual(details.source, "clob:fee_schedule")
        self.assertAlmostEqual(details.rate, 0.04)
        self.assertEqual(calls, ["https://clob.test/clob-markets/abc"])

    def test_taker_fee_is_level_specific_and_maker_fee_is_zero(self) -> None:
        details = qf.FeeDetails(True, 0.07, 1.0, True, "test")
        self.assertEqual(qf.fee_amount(100.0, 0.5, details, taker=False), 0.0)
        fill = qf.walk_book_for_shares(
            [(0.50, 5.0), (0.60, 5.0)],
            8.0,
            details,
            buy=True,
            slippage_bps=0.0,
            require_full=True,
        )
        self.assertIsNotNone(fill)
        assert fill is not None
        expected = qf.fee_amount(5.0, 0.50, details) + qf.fee_amount(3.0, 0.60, details)
        self.assertAlmostEqual(fill.fee, expected)
        self.assertGreater(fill.raw_vwap, 0.50)

    def test_depth_walk_rejects_fake_full_fill_and_supports_partial_exit(self) -> None:
        details = qf.FeeDetails(False, 0.0, 1.0, True, "test")
        self.assertIsNone(qf.walk_book_for_shares([(0.50, 2.0)], 5.0, details, buy=True, require_full=True))
        partial = qf.walk_book_for_shares([(0.49, 2.0)], 5.0, details, buy=False, require_full=False)
        self.assertIsNotNone(partial)
        assert partial is not None
        self.assertEqual(partial.filled_shares, 2.0)
        self.assertFalse(partial.complete)

    def test_measurement_branch_does_not_route_live_v6(self) -> None:
        loop = (SCRIPTS / "paper_v6_loop.sh").read_text()
        self.assertNotIn("scripts/v6_queue_filter.py", loop)
        self.assertIn("scripts/v6_micro_taker.py", loop)
        self.assertIn("scripts/v6_hard_arb_paper.py", loop)
        self.assertIn("--completion-threshold 0.75", loop)

    def test_registered_smoke_uses_realistic_execution_probe(self) -> None:
        workflow = (ROOT / ".github/workflows/v6-research-smoke.yml").read_text()
        self.assertIn("scripts/v6_queue_filter.py self-test", workflow)
        self.assertIn("scripts/v6_queue_filter.py hard", workflow)
        self.assertIn("scripts/v6_queue_filter.py micro", workflow)
        self.assertIn("--leg-latency-ms 100", workflow)

    def test_broad_micro_smoke_uses_observable_causal_horizon(self) -> None:
        workflow = (ROOT / ".github/workflows/v6-research-smoke.yml").read_text()
        self.assertIn(
            "--markets 300 --min-liquidity 2 --horizon-seconds 10 "
            "--max-target-staleness-seconds 5",
            workflow,
        )
        self.assertNotIn(
            "--markets 300 --min-liquidity 2 --horizon-seconds 5 "
            "--max-target-staleness-seconds 3",
            workflow,
        )
        self.assertIn(
            "int(x.get('target_observation_ts') or 0)>int(x.get('ts') or 0)+10",
            workflow,
        )

    def test_causal_target_does_not_borrow_post_horizon_snapshot(self) -> None:
        too_fast = [
            {"market_id": "m", "ts": 100, "mid": 0.50, "y": None},
            {"market_id": "m", "ts": 107, "mid": 0.51, "y": None},
        ]
        stats = label_matured_samples(
            too_fast,
            now=110,
            horizon_seconds=5,
            max_target_staleness_seconds=3,
        )
        self.assertEqual(stats["newly_labeled"], 0)
        self.assertEqual(stats["missing_pre_horizon_observation"], 1)
        self.assertIsNone(too_fast[0]["y"])

        observable = [
            {"market_id": "m", "ts": 100, "mid": 0.50, "y": None},
            {"market_id": "m", "ts": 107, "mid": 0.51, "y": None},
        ]
        stats = label_matured_samples(
            observable,
            now=110,
            horizon_seconds=10,
            max_target_staleness_seconds=5,
        )
        self.assertEqual(stats["newly_labeled"], 1)
        self.assertEqual(observable[0]["target_observation_ts"], 107)
        self.assertEqual(observable[0]["target_staleness_seconds"], 3)
        self.assertAlmostEqual(observable[0]["y"], 0.01)


if __name__ == "__main__":
    unittest.main()
