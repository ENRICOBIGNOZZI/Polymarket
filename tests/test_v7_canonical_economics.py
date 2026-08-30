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


def order(*, strategy: str, order_id: str, leg_id: str, bundle_id: str | None = None, family: str = "maker_complete_set", horizon: int = 45, required=None, size: float = 1.0, event_id: str = "event-1"):
    now = clock()
    md = metadata(family, horizon)
    if required is not None:
        md["joint_target_legs"] = required
    return ledger.LedgerEvent(
        event_type="ORDER_SUBMITTED", strategy=strategy, model_sha=SHA, bundle_id=bundle_id,
        order_id=order_id, leg_id=leg_id, token_id=leg_id, event_id=event_id,
        exchange_ts_ms=now, receive_ts_ms=now + 1, decision_ts_ms=now + 2,
        book_snapshot_id=f"book-{order_id}", intended_action="POST_ONLY_BUY", intended_size=size,
        side="BUY", limit_price=0.49, metadata=md,
    )


def fill(*, strategy: str, order_id: str, fill_id: str, leg_id: str, bundle_id: str | None = None, family: str = "maker_complete_set", horizon: int = 45, size: float = 1.0, fee: float = 0.0, price: float = 0.49, event_id: str = "event-1"):
    now = clock()
    return ledger.LedgerEvent(
        event_type="FILL", strategy=strategy, model_sha=SHA, bundle_id=bundle_id,
        order_id=order_id, fill_id=fill_id, leg_id=leg_id, token_id=leg_id, event_id=event_id,
        exchange_ts_ms=now, receive_ts_ms=now + 1, side="BUY", fill_price=price,
        filled_size=size, fee=fee, fee_source="market:fee_schedule", metadata=metadata(family, horizon),
    )


def final(*, strategy: str, order_id: str | None = None, bundle_id: str | None = None, family: str = "maker_complete_set", horizon: int = 45, pnl: float = 1.0, slippage: float | None = 0.0, unwind: float | None = 0.0, capital: float | None = 0.0, latency: float | None = 0.0, cost_complete: bool = True, unwind_accounted: bool = True, event_id: str = "event-1"):
    md = metadata(family, horizon, realized=True, cost_vector_complete=cost_complete, unwind_accounted=unwind_accounted)
    return ledger.LedgerEvent(
        event_type="FINAL", strategy=strategy, model_sha=SHA, bundle_id=bundle_id,
        order_id=order_id, event_id=event_id, final_pnl=pnl, slippage=slippage,
        unwind_loss=unwind, capital_cost=capital, latency_cost=latency,
        capital_duration_ms=3_600_000, metadata=md,
    )


def write_events(path: Path, events) -> None:
    with ledger.CanonicalLedgerWriter(path, writer_id="test", model_sha=SHA) as writer:
        for event in events:
            writer.append(event)


class CanonicalEconomicsTest(unittest.TestCase):
    def test_inventory_merge_does_not_manufacture_a_market_fill(self) -> None:
        events = [
            ledger.LedgerEvent(
                event_type="INVENTORY_MERGE", strategy="MICRO_MAKER_PRO",
                model_sha=SHA, position_id="merge-position", market_id="market-1",
                event_id="event-merge", intended_size=3.0,
                intended_action="INVENTORY_MERGE", realized_cashflow=0.6,
                final_pnl=0.6,
                metadata={"transformation": "INVENTORY_MERGE",
                          "consumed_inventory_provenance_complete": True},
            ),
            ledger.LedgerEvent(
                event_type="FINAL", strategy="MICRO_MAKER_PRO", model_sha=SHA,
                position_id="merge-position", market_id="market-1", event_id="event-merge",
                final_pnl=0.6, realized_cashflow=0.6, fee=0.0, slippage=0.0,
                unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
                metadata={"realized": True, "unwind_accounted": True,
                          "cost_vector_complete": True, "terminal_id": "merge-position:final"},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["submitted_units"], 0)
        self.assertEqual(report["complete_units"], 0)
        self.assertEqual(report["mature_terminal_units"], 0)
        self.assertIsNone(report["net_pnl"])
        self.assertIn(
            "inventory_transform_not_market_execution",
            report["unit_reason_codes"]["position:merge-position"],
        )

    def test_shadow_counterfactual_is_in_ledger_but_excluded_from_paper_pnl(self) -> None:
        now = clock()
        shadow = {
            "model_family": "CRYPTO_INFORMED_TAKER",
            "horizon_seconds": 300,
            "counterfactual": True,
            "economic_authority": "SHADOW_COUNTERFACTUAL",
            "excluded_from_portfolio_equity": True,
        }
        events = [
            ledger.LedgerEvent(
                event_type="ORDER_SUBMITTED", strategy="CRYPTO_INFORMED_TAKER",
                model_sha=SHA, order_id="shadow-order", position_id="shadow-position",
                event_id="shadow-event", token_id="YES", side="BUY",
                intended_size=2.0, intended_action="VIRTUAL_FAK", limit_price=0.4,
                exchange_ts_ms=now, receive_ts_ms=now + 1,
                decision_ts_ms=now + 2, book_snapshot_id="shadow-book",
                metadata=shadow,
            ),
            ledger.LedgerEvent(
                event_type="FILL", strategy="CRYPTO_INFORMED_TAKER",
                model_sha=SHA, order_id="shadow-order", position_id="shadow-position",
                fill_id="shadow-fill", event_id="shadow-event", token_id="YES",
                side="BUY", fill_price=0.4, filled_size=2.0, fee=0.01,
                slippage=0.0, fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
                exchange_ts_ms=now + 3, receive_ts_ms=now + 4,
                metadata=shadow,
            ),
            ledger.LedgerEvent(
                event_type="FINAL", strategy="CRYPTO_INFORMED_TAKER",
                model_sha=SHA, order_id="shadow-order", position_id="shadow-position",
                event_id="shadow-event", token_id="YES", side="BUY", final_pnl=1.19,
                realized_cashflow=1.19, fee=0.0, slippage=0.0, unwind_loss=0.0,
                capital_cost=0.0, latency_cost=0.0, capital_duration_ms=300_000,
                metadata={**shadow, "realized": True, "unwind_accounted": True,
                          "cost_vector_complete": True, "terminal_id": "shadow-final"},
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["economic_units"], 0)
        self.assertIsNone(report["net_pnl"])
        self.assertEqual(report["shadow_counterfactual"]["mature_terminal_units"], 1)
        self.assertAlmostEqual(report["shadow_counterfactual"]["net_pnl"], 1.19)
        self.assertTrue(report["shadow_counterfactual"]["excluded_from_portfolio_equity"])

    def test_micro_taker_round_trip_is_one_mature_cost_complete_unit(self) -> None:
        now = clock()
        position_id = "micro-position"
        events = []
        for suffix, side, price, fee_value in (
            ("entry", "BUY", 0.40, 0.01),
            ("exit", "SELL", 0.46, 0.01),
        ):
            order_id = f"micro-{suffix}"
            events.extend([
                ledger.LedgerEvent(
                    event_type="ORDER_SUBMITTED", strategy="MICRO_TAKER",
                    model_sha=SHA, position_id=position_id, order_id=order_id,
                    event_id="event-micro", token_id="YES", side=side,
                    intended_size=10.0, intended_action=f"TAKER_{suffix.upper()}",
                    limit_price=price, exchange_ts_ms=now, receive_ts_ms=now + 1,
                    decision_ts_ms=now + 2, book_snapshot_id=f"book-{suffix}",
                ),
                ledger.LedgerEvent(
                    event_type="FILL", strategy="MICRO_TAKER", model_sha=SHA,
                    position_id=position_id, order_id=order_id,
                    fill_id=f"fill-{suffix}", event_id="event-micro",
                    token_id="YES", side=side, fill_price=price,
                    filled_size=10.0, fee=fee_value, slippage=0.005,
                    fee_source="market:fee_schedule", exchange_ts_ms=now + 3,
                    receive_ts_ms=now + 4,
                ),
            ])
        events.append(ledger.LedgerEvent(
            event_type="FINAL", strategy="MICRO_TAKER", model_sha=SHA,
            position_id=position_id, event_id="event-micro", token_id="YES",
            final_pnl=0.58, realized_cashflow=0.58, fee=0.0, slippage=0.0,
            unwind_loss=0.0, capital_cost=0.0, latency_cost=0.0,
            capital_duration_ms=30_000,
            metadata={"realized": True, "unwind_accounted": True,
                      "cost_vector_complete": True,
                      "terminal_id": "micro_taker:micro-position:final"},
        ))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["economic_units"], 1)
        self.assertEqual(report["complete_units"], 1)
        self.assertEqual(report["mature_terminal_units"], 1)
        self.assertAlmostEqual(report["net_pnl"], 0.58)
        self.assertAlmostEqual(report["costs"]["baseline_total"], 0.03)

    def test_hard_arb_explicit_multileg_target_is_mature_after_terminal(self) -> None:
        required = {"YES": 10.0, "NO": 10.0}
        now = clock()
        events = [ledger.LedgerEvent(
            event_type="OPPORTUNITY", strategy="HARD_ARB", model_sha=SHA,
            bundle_id="hard-bundle", event_id="event-hard", expected_ev=0.02,
            intended_action="SEQUENTIAL_FOK_COMPLETE_SET",
            metadata={"target_quantities": required},
        )]
        for leg, price in (("YES", 0.48), ("NO", 0.49)):
            events.extend([
                order(strategy="HARD_ARB", order_id=f"hard-{leg}", leg_id=leg,
                      bundle_id="hard-bundle", family="HARD_ARB", required=required,
                      size=10.0, event_id="event-hard"),
                fill(strategy="HARD_ARB", order_id=f"hard-{leg}",
                     fill_id=f"hard-fill-{leg}", leg_id=leg,
                     bundle_id="hard-bundle", family="HARD_ARB", size=10.0,
                     fee=0.01, price=price, event_id="event-hard"),
            ])
        events.append(ledger.LedgerEvent(
            event_type="FINAL", strategy="HARD_ARB", model_sha=SHA,
            bundle_id="hard-bundle", position_id="hard-bundle",
            event_id="event-hard", final_pnl=0.28, realized_cashflow=0.28,
            fee=0.0, slippage=0.0, unwind_loss=0.0, capital_cost=0.0,
            latency_cost=0.0, capital_duration_ms=300_000,
            metadata={"realized": True, "unwind_accounted": True,
                      "cost_vector_complete": True,
                      "terminal_id": "hard:hard-bundle:final"},
        ))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["submitted_units"], 1)
        self.assertEqual(report["complete_units"], 1)
        self.assertEqual(report["mature_terminal_units"], 1)
        self.assertAlmostEqual(report["net_pnl"], 0.28)

    def test_order_to_position_identity_is_one_mature_economic_unit(self) -> None:
        now = clock()
        order_id = "external-order-1"
        position_id = "external-position-1"
        token_id = "yes-token"
        common = dict(
            strategy="CRYPTO_INFORMED_TAKER", model_sha=SHA,
            order_id=order_id, event_id="event-external", token_id=token_id,
            side="BUY", metadata=metadata("CRYPTO_INFORMED_TAKER", 300),
        )
        events = [
            ledger.LedgerEvent(
                event_type="ORDER_SUBMITTED", intended_size=10.0,
                intended_action="TAKE", limit_price=0.4,
                exchange_ts_ms=now, receive_ts_ms=now + 1,
                decision_ts_ms=now + 2, book_snapshot_id="book-submit", **common,
            ),
            ledger.LedgerEvent(
                event_type="FILL", position_id=position_id, fill_id="fill-external",
                fill_price=0.4, filled_size=10.0, fee=0.2, slippage=0.0,
                fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
                exchange_ts_ms=now + 3, receive_ts_ms=now + 4, **common,
            ),
            ledger.LedgerEvent(
                event_type="FINAL", position_id=position_id, final_pnl=5.8,
                realized_cashflow=10.0, unwind_loss=0.0, capital_cost=0.0,
                latency_cost=0.0, capital_duration_ms=300_000,
                metadata=metadata(
                    "CRYPTO_INFORMED_TAKER", 300, realized=True,
                    cost_vector_complete=True, unwind_accounted=True,
                ),
                strategy="CRYPTO_INFORMED_TAKER", model_sha=SHA,
                order_id=order_id, event_id="event-external", token_id=token_id,
                side="BUY",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(
                path, expected_model_sha=SHA, family="CRYPTO_INFORMED_TAKER",
            )
        self.assertEqual(report["economic_units"], 1)
        self.assertEqual(report["submitted_units"], 1)
        self.assertEqual(report["complete_units"], 1)
        self.assertEqual(report["mature_terminal_units"], 1)
        self.assertAlmostEqual(report["net_pnl"], 5.8)

    def test_order_to_multiple_positions_fails_closed(self) -> None:
        now = clock()
        order_event = order(
            strategy="micro_taker", order_id="conflicted-order", leg_id="YES",
            family="micro_taker", horizon=300,
        )
        fills = [
            ledger.LedgerEvent(
                event_type="FILL", strategy="micro_taker", model_sha=SHA,
                order_id="conflicted-order", position_id=position_id,
                fill_id=f"fill-{position_id}", event_id="event-conflict",
                token_id="YES", side="BUY", fill_price=0.4, filled_size=1.0,
                fee=0.0, fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
                exchange_ts_ms=now, receive_ts_ms=now + 1,
                metadata=metadata("micro_taker", 300),
            )
            for position_id in ("position-a", "position-b")
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, [order_event, *fills])
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertIn(
            "order_position_identity_conflict:conflicted-order",
            report["reason_codes"],
        )

    def test_empty_ledger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = econ.assess(Path(tmp) / "missing.jsonl", expected_model_sha=SHA)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("no_submitted_economic_units", report["reason_codes"])
        self.assertTrue(any(code.startswith("canonical_ledger_unreadable") for code in report["reason_codes"]))

    def test_two_leg_completion_is_one_probability_bounded_unit_but_not_enough_evidence(self) -> None:
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
        self.assertEqual(report["distinct_event_clusters"], 1)
        self.assertAlmostEqual(report["costs"]["baseline_total"], 0.07)
        self.assertAlmostEqual(report["stressed_net_pnl"]["2x"], 0.93)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("insufficient_distinct_event_clusters", report["reason_codes"])

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

    def test_no_fill_final_does_not_become_mature_economic_evidence(self) -> None:
        events = [
            order(strategy="graph_rv", order_id="never-filled", leg_id="YES",
                  family="graph_rv", horizon=45),
            final(strategy="graph_rv", order_id="never-filled", family="graph_rv",
                  horizon=45, pnl=0.0),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["complete_units"], 0)
        self.assertEqual(report["mature_terminal_units"], 0)
        self.assertIn("no_mature_full_cost_terminal_observations", report["reason_codes"])

    def test_dynamic_family_and_horizon_are_not_silently_pooled(self) -> None:
        events = []
        for suffix, family, horizon, event_id in (("a", "pca", 3600, "event-a"), ("b", "ranking", 7200, "event-b")):
            events.extend([
                order(strategy=family, order_id=f"o-{suffix}", leg_id="YES", family=family, horizon=horizon, event_id=event_id),
                fill(strategy=family, order_id=f"o-{suffix}", fill_id=f"f-{suffix}", leg_id="YES", family=family, horizon=horizon, fee=0.0, event_id=event_id),
                final(strategy=family, order_id=f"o-{suffix}", family=family, horizon=horizon, pnl=1.0, slippage=0.0, unwind=0.0, capital=0.0, latency=0.0, event_id=event_id),
            ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            mixed = econ.assess(path, expected_model_sha=SHA)
            pca = econ.assess(path, expected_model_sha=SHA, family="pca", horizon_seconds=3600)
        self.assertFalse(mixed["promotion_ready"])
        self.assertIn("mixed_model_families_require_explicit_filter", mixed["reason_codes"])
        self.assertIn("mixed_model_horizons_require_explicit_filter", mixed["reason_codes"])
        self.assertFalse(pca["promotion_ready"])
        self.assertIn("insufficient_distinct_event_clusters", pca["reason_codes"])
        self.assertEqual(pca["model_families_observed"], ["pca"])
        self.assertEqual(pca["model_horizons_seconds_observed"], [3600])

    def test_repeated_units_from_one_event_do_not_create_independent_evidence(self) -> None:
        events = []
        for index in range(econ.MIN_EVENT_CLUSTERS_FOR_PROMOTION):
            oid = f"o-{index}"
            events.extend([
                order(strategy="micro_maker", order_id=oid, leg_id="YES", event_id="same-event"),
                fill(strategy="micro_maker", order_id=oid, fill_id=f"f-{index}", leg_id="YES", event_id="same-event"),
                final(strategy="micro_maker", order_id=oid, pnl=1.0, event_id="same-event"),
            ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["mature_terminal_units"], econ.MIN_EVENT_CLUSTERS_FOR_PROMOTION)
        self.assertEqual(report["distinct_event_clusters"], 1)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("insufficient_distinct_event_clusters", report["reason_codes"])

    def test_twelve_distinct_positive_event_clusters_can_promote(self) -> None:
        events = []
        for index in range(econ.MIN_EVENT_CLUSTERS_FOR_PROMOTION):
            oid = f"o-{index}"
            event_id = f"event-{index}"
            events.extend([
                order(strategy="micro_maker", order_id=oid, leg_id="YES", event_id=event_id),
                fill(strategy="micro_maker", order_id=oid, fill_id=f"f-{index}", leg_id="YES", fee=0.0, event_id=event_id),
                final(strategy="micro_maker", order_id=oid, pnl=1.0, slippage=0.0, unwind=0.0, capital=0.0, latency=0.0, event_id=event_id),
            ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertEqual(report["distinct_event_clusters"], econ.MIN_EVENT_CLUSTERS_FOR_PROMOTION)
        self.assertEqual(report["positive_chronological_event_fold_fraction_2x"], 1.0)
        self.assertTrue(report["positive_under_1x_1_5x_2x"])
        self.assertTrue(report["promotion_ready"])

    def test_missing_final_event_identity_fails_closed_for_promotion(self) -> None:
        events = [
            order(strategy="micro_taker", order_id="o1", leg_id="YES", family="micro_taker", horizon=30, event_id="event-a"),
            fill(strategy="micro_taker", order_id="o1", fill_id="f1", leg_id="YES", family="micro_taker", horizon=30, event_id="event-a"),
            final(strategy="micro_taker", order_id="o1", family="micro_taker", horizon=30, pnl=1.0, event_id="event-a"),
        ]
        events[-1] = ledger.LedgerEvent(**{**events[-1].to_dict(), "event_id": None})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "execution.jsonl"
            write_events(path, events)
            report = econ.assess(path, expected_model_sha=SHA)
        self.assertFalse(report["promotion_ready"])
        self.assertIn("economic_event_identity_incomplete", report["reason_codes"])
        self.assertIn("economic_event_id_missing", report["unit_reason_codes"]["order:o1"])


if __name__ == "__main__":
    unittest.main()
