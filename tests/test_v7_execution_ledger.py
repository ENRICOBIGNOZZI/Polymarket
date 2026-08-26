from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "v7_execution_ledger.py"
spec = importlib.util.spec_from_file_location("v7_execution_ledger_test", SCRIPT)
assert spec and spec.loader
ledger = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ledger
spec.loader.exec_module(ledger)

SHA_A = "a" * 40
SHA_B = "b" * 40


def candidate(**overrides):
    values = dict(
        event_type="CANDIDATE",
        strategy="MICRO_TAKER",
        model_sha=SHA_A,
        recorded_ts_ms=1_030,
        exchange_ts_ms=1_000,
        receive_ts_ms=1_010,
        decision_ts_ms=1_020,
        book_snapshot_id="book-1",
        candidate_id="candidate-1",
        market_id="market-1",
        bid=0.49,
        ask=0.51,
        bid_depth=100.0,
        ask_depth=80.0,
        predicted_alpha=0.02,
        predicted_fill_probability=1.0,
        expected_ev=0.005,
        intended_action="BUY_TAKER",
        intended_size=10.0,
    )
    values.update(overrides)
    return ledger.LedgerEvent(**values)


def fill(**overrides):
    values = dict(
        event_type="FILL",
        strategy="FAST_STRUCTURAL_ARB",
        model_sha=SHA_A,
        recorded_ts_ms=2_030,
        exchange_ts_ms=2_000,
        receive_ts_ms=2_010,
        opportunity_id="opp-1",
        bundle_id="bundle-1",
        order_id="order-1",
        leg_id="leg-1",
        market_id="market-1",
        token_id="token-yes",
        side="BUY",
        fill_price=0.40,
        filled_size=5.0,
        fee=0.01,
        fee_rate=0.005,
        fee_source="authoritative_clob_fee",
        slippage=0.02,
        markouts={"1s": -0.01, "10s": 0.0, "45s": 0.01},
    )
    values.update(overrides)
    return ledger.LedgerEvent(**values)


class CanonicalExecutionLedgerTest(unittest.TestCase):
    def test_candidate_is_paper_only_and_receive_time_causal(self) -> None:
        candidate().validate()
        for overrides in ({"authenticated_execution": True}, {"paper_only": False}):
            with self.assertRaisesRegex(ledger.LedgerContractError, "safety:not_paper_only"):
                candidate(**overrides).validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "decision_before_receive"):
            candidate(receive_ts_ms=1_020, decision_ts_ms=1_010).validate()

    def test_fill_requires_causal_execution_and_authoritative_fee(self) -> None:
        fill().validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "missing_exchange_receive_clock"):
            fill(exchange_ts_ms=None).validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "missing_price"):
            fill(fill_price=None).validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "missing_authoritative_fee"):
            fill(fee_source=None).validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "missing_positive_size"):
            fill(filled_size=0.0).validate()

    def test_position_mark_requires_causal_executable_liquidation_value(self) -> None:
        mark = ledger.LedgerEvent(
            event_type="POSITION_MARK",
            strategy="MICRO_TAKER",
            model_sha=SHA_A,
            recorded_ts_ms=3_030,
            exchange_ts_ms=3_000,
            receive_ts_ms=3_010,
            book_snapshot_id="book-mark-1",
            position_id="position-1",
            executable_liquidation_value=4.75,
            unrealized_pnl=-0.10,
        )
        mark.validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "missing_executable_liquidation_value"):
            ledger.LedgerEvent(
                event_type="POSITION_MARK",
                strategy="MICRO_TAKER",
                model_sha=SHA_A,
                recorded_ts_ms=3_030,
                receive_ts_ms=3_010,
                book_snapshot_id="book-mark-1",
                position_id="position-1",
            ).validate()

    def test_single_writer_owner_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = ledger.canonical_ledger_path(Path(temporary))
            first = ledger.CanonicalLedgerWriter(path, writer_id="paper-runtime", model_sha=SHA_A)
            second = ledger.CanonicalLedgerWriter(path, writer_id="other-runtime", model_sha=SHA_A)
            first.acquire()
            try:
                with self.assertRaisesRegex(ledger.LedgerOwnershipError, "ledger_already_owned"):
                    second.acquire()
                first.append(candidate())
                with self.assertRaisesRegex(ledger.LedgerContractError, "mixed_sha_append"):
                    first.append(candidate(model_sha=SHA_B))
            finally:
                first.close()
            self.assertFalse(first.owner_path.exists())

    def test_global_history_is_read_only_for_one_explicit_sha_at_a_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = ledger.canonical_ledger_path(Path(temporary))
            with ledger.CanonicalLedgerWriter(path, writer_id="runtime-a", model_sha=SHA_A) as writer:
                writer.append(candidate())
            with ledger.CanonicalLedgerWriter(path, writer_id="runtime-b", model_sha=SHA_B) as writer:
                writer.append(candidate(model_sha=SHA_B, record_id="b-record"))

            rows_a = ledger.load_events(path, expected_model_sha=SHA_A)
            rows_b = ledger.load_events(path, expected_model_sha=SHA_B)
            self.assertEqual([row.model_sha for row in rows_a], [SHA_A])
            self.assertEqual([row.model_sha for row in rows_b], [SHA_B])
            with self.assertRaisesRegex(ledger.LedgerContractError, "not_exact_git_sha"):
                ledger.load_events(path, expected_model_sha="main")

    def test_reader_validates_nonselected_history_instead_of_silently_skipping_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = ledger.canonical_ledger_path(Path(temporary))
            path.parent.mkdir(parents=True, exist_ok=True)
            good = candidate().to_dict()
            bad = candidate(model_sha=SHA_B).to_dict()
            bad["authenticated_execution"] = True
            path.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ledger.LedgerContractError, "safety:not_paper_only"):
                ledger.load_events(path, expected_model_sha=SHA_A)

    def test_markout_horizons_and_price_ranges_fail_closed(self) -> None:
        with self.assertRaisesRegex(ledger.LedgerContractError, "unsupported_horizon"):
            fill(markouts={"30s": 0.01}).validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "fill_price:out_of_range"):
            fill(fill_price=1.01).validate()

    def test_final_requires_realized_pnl(self) -> None:
        ledger.LedgerEvent(
            event_type="FINAL",
            strategy="GRAPH_RV",
            model_sha=SHA_A,
            recorded_ts_ms=4_000,
            final_pnl=-0.25,
            capital_duration_ms=10_000,
        ).validate()
        with self.assertRaisesRegex(ledger.LedgerContractError, "final:missing_pnl"):
            ledger.LedgerEvent(
                event_type="FINAL",
                strategy="GRAPH_RV",
                model_sha=SHA_A,
                recorded_ts_ms=4_000,
            ).validate()


if __name__ == "__main__":
    unittest.main()
