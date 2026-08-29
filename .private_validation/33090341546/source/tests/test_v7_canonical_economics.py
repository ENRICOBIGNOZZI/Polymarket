from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ECON = ROOT / "scripts" / "v7_canonical_economics.py"

spec = importlib.util.spec_from_file_location("v7_canonical_economics_test", ECON)
assert spec and spec.loader
econ = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = econ
spec.loader.exec_module(econ)

ledger = econ.ledger
SHA = "a" * 40


def clock() -> int:
    return int(time.time() * 1000) - 10_000


def metadata(family: str, horizon: int, **extra):
    value = {"model_family": family, "horizon_seconds": horizon}
    value.update(extra)
    return value


def order(*, strategy: str, order_id: str, leg_id: str, bundle_id: str | None = None, family: str = "maker_complete_set", horizon: int = 45, required=None, size: float = 1.0):
    now = clock()
    md = metadata(family, horizon)
    if required is not None:
        md["joint_target_legs"] = required
    return ledger.LedgerEvent(
        event_type="ORDER_SUBMITTED", strategy=strategy, model_sha=SHA, bundle_id=bundle_id,
        order_id=order_id, leg_id=leg_id, token_id=leg_id, exchange_ts_ms=now,
        receive_ts_ms=now + 1, decision_ts_ms=now + 2, book_snapshot_id=f"book-{order_id}",
        intended_action="POST_ONLY_BUY", intended_size=size, side="BUY", limit_price=0.49,
        metadata=md,
    )


def fill(*, strategy: str, order_id: str, fill_id: str, leg_id: str, bundle_id: str | None = None, family: str = "maker_complete_set", horizon: int = 45, size: float = 1.0, fee: float = 0.0):
    now = clock()
    return ledger.LedgerEvent(
        event_type="FILL", strategy=strategy, model_sha=SHA, bundle_id=bundle_id,
        order_id=order_id, fill_id=fill_id, leg_id=leg_id, token_id=leg_id,
        exchange_ts_ms=now, receive_ts_ms=now + 1, side="BUY", fill_price=0.49,
        filled_size=size, fee=fee, fee_source="market:fee_schedule", metadata=metadata(family, horizon),
    )


def final(*, strategy: str, order_id: str | None = None, bundle_id: str | None = None, family: str = "maker_complete_set", horizon: int = 45, pnl: float = 1.0, slippage: float | None = 0.0, unwind: float | None = 0.0, capital: float | None = 0.0, latency: float | None = 0.0, cost_complete: bool = True, unwind_accounted: bool = True):
    md = metadata(family, horizon, realized=True, cost_vector_complete=cost_complete, unwind_accounted=unwind_accounted)
    return ledger.LedgerEvent(
        event_type="FINAL", strategy=strategy, model_sha=SHA, bundle_id=bundle_id,
        order_id=order_id, final_pnl=pnl, slippage=slippage, unwind_loss=unwind,
        capital_cost=capital, latency_cost=latency, capital_duration_ms=3_600_000, metadata=md,
    )


def write_events(path: Path, events) -> None:
    with ledger.CanonicalLedgerWriter(path, writer_id="test", model_sha=SHA) as writer:
        for event in events:
            writer.append(event)


class CanonicalEconomicsTest(unittest.TestCase):
    def test_empty_ledger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = econ.assess(Path(tmp) / "missing.jsonl", expected_model_sha=SHA)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("no_submitted_economic_units", report["reason_codes"])
        self.assertTrue(any(code.startswith("canonical_ledger_unreadable") for code in report["reason_codes"]))

    def test_two_leg_completion_is_one_probability_bounded_unit(self) -> None:
        required = [{"leg_id": "YES", "target_quantity": 1.0}, {"leg_id": "NO", "target_quantity": 1.0}]
        events = [
            order(strategy="micro_maker", order_id="o-y", leg_id="YES", bundle_id="b1", required=required),
            order(strategy="micro_maker", order_id="o-n", leg_id="NO", bundle_id="b1", required=required),
            fill(strategy="micro_maker", order_id="o-y", fill_id="f-y", leg_id="YES", bundle_id="b1", fee=0.02),
            fill(strategy="micro_maker", order_id="o-n", fill_id="f-n", leg_id="NO", bundle_id="b1", fee=0.02),
            final(strategy="micro_maker", bundle_id="b1", pnl=1.0, slippage=0.01, unwind=0.0, capital=0.01, latency=0.01),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["submitted_units"], 1)
        self.assertEqual(report["complete_units"], 1)
        self.assertEqual(report["completion_rate"], 1.0)
        self.assertLessEqual(report["completion_rate"], 1.0)
        self.assertEqual(report["mature_terminal_units"], 1)
        self.assertAlmostEqual(report["costs"]["baseline_total"], 0.07)
        self.assertAlmostEqual(report["stressed_net_pnl"]["2x"], 0.93)
        self.assertTrue(report["promotion_ready"])

    def test_one_leg_filled_in_two_leg_bundle_is_not_a_completion(self) -> None:
        required = [{"leg_id": "YES", "target_quantity": 1.0}, {"leg_id": "NO", "target_quantity": 1.0}]
        events = [
            order(strategy="relative_value", order_id="o-y", leg_id="YES", bundle_id="b1", family="relative_value", horizon=7200, required=required),
            order(strategy="relative_value", order_id="o-n", leg_id="NO", bundle_id="b1", family="relative_value", horizon=7200, required=required),
            fill(strategy="relative_value", order_id="o-y", fill_id="f-y", leg_id="YES", bundle_id="b1", family="relative_value", horizon=7200, fee=0.01),
            final(strategy="relative_value", bundle_id="b1", family="relative_value", horizon=7200, pnl=0.2, slippage=0.01, unwind=0.05, capital=0.01, latency=0.01, unwind_accounted=True),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["submitted_units"], 1)
        self.assertEqual(report["complete_units"], 0)
        self.assertEqual(report["partial_units"], 1)
        self.assertEqual(report["completion_rate"], 0.0)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("no_completed_economic_units", report["reason_codes"])

    def test_full_cost_vector_stress_can_flip_pnl_sign(self) -> None:
        events = [
            order(strategy="micro_taker", order_id="o1", leg_id="YES", family="micro_taker", horizon=30),
            fill(strategy="micro_taker", order_id="o1", fill_id="f1", leg_id="YES", family="micro_taker", horizon=30, fee=0.006),
            final(strategy="micro_taker", order_id="o1", family="micro_taker", horizon=30, pnl=0.020, slippage=0.004, unwind=0.006, capital=0.004, latency=0.004),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertAlmostEqual(report["costs"]["baseline_total"], 0.024)
        self.assertAlmostEqual(report["stressed_net_pnl"]["1x"], 0.020)
        self.assertAlmostEqual(report["stressed_net_pnl"]["1.5x"], 0.008)
        self.assertAlmostEqual(report["stressed_net_pnl"]["2x"], -0.004)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("positive_pnl_stress_gate", report["reason_codes"])

    def test_missing_component_or_complete_vector_flag_fails_closed(self) -> None:
        events = [
            order(strategy="micro_taker", order_id="o1", leg_id="YES", family="micro_taker", horizon=30),
            fill(strategy="micro_taker", order_id="o1", fill_id="f1", leg_id="YES", family="micro_taker", horizon=30, fee=0.0),
            final(strategy="micro_taker", order_id="o1", family="micro_taker", horizon=30, pnl=1.0, slippage=0.0, unwind=0.0, capital=0.0, latency=None, cost_complete=False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["mature_terminal_units"], 0)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("no_mature_full_cost_terminal_observations", report["reason_codes"])
        reasons = next(iter(report["unit_reason_codes"].values()))
        self.assertIn("full_cost_vector_unverifiable", reasons)

    def test_dynamic_family_and_horizon_are_not_silently_pooled(self) -> None:
        events = []
        for suffix, family, horizon in (("a", "pca", 3600), ("b", "ranking", 7200)):
            events.extend([
                order(strategy=family, order_id=f"o-{suffix}", leg_id="YES", family=family, horizon=horizon),
                fill(strategy=family, order_id=f"o-{suffix}", fill_id=f"f-{suffix}", leg_id="YES", family=family, horizon=horizon, fee=0.0),
                final(strategy=family, order_id=f"o-{suffix}", family=family, horizon=horizon, pnl=1.0, slippage=0.0, unwind=0.0, capital=0.0, latency=0.0),
            ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            mixed = econ.assess(path, expected_model_sha=SHA)
            pca = econ.assess(path, expected_model_sha=SHA, family="pca", horizon_seconds=3600)
        self.assertFalse(mixed["promotion_ready"])
        self.assertIn("mixed_model_families_require_explicit_filter", mixed["reason_codes"])
        self.assertIn("mixed_model_horizons_require_explicit_filter", mixed["reason_codes"])
        self.assertTrue(pca["promotion_ready"])
        self.assertEqual(pca["model_families_observed"], ["pca"])
        self.assertEqual(pca["model_horizons_seconds_observed"], [3600])


if __name__ == "__main__":
    unittest.main()
