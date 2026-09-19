from dataclasses import asdict, replace
from decimal import Decimal as D, localcontext
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_research_economic_contract import (
    AUTHORITY, BookFrame, BookSeries, CoverageProof, LatencyScenario, MarketEconomics,
    ResearchContractError, ResearchDecision, canonical_hash, round_trip_label, sweep,
)

MS = 1_000_000
H = "a" * 64


def fixture():
    market = MarketEconomics("m1", "yes", "no", "host-boot-1", 500*MS, 5000*MS,
        H, H, H, D("0.01"), D("1"), D("0.07"), "C_RATE_P_ONE_MINUS_P_V1", True, 250*MS)
    def frame(at, bid, ask, seq):
        return BookFrame("m1", "yes", "host-boot-1", "session-1", 1, seq,
            at*MS, at*MS, at*MS, market.hash, ((D(bid), D(10)),), ((D(ask), D(10)),))
    frames = (frame(900, "0.49", "0.50", 1), frame(1300, "0.52", "0.53", 2))
    proof = CoverageProof("host-boot-1", "session-1", 1, 900*MS, 2100*MS, 2110*MS, H, True)
    books = BookSeries(frames, proof)
    features = {"external_return_bp": 0.4, "optional_context": None}
    decision = ResearchDecision("d1", "m1", "yes", "host-boot-1", 1020*MS, 1019*MS,
        canonical_hash(features), H, market.hash, frames[0].hash, D(5), D("0.50"), "FAK")
    latency = LatencyScenario(10*MS, "EXCLUDES_MARKET_DELAY", H, 800*MS, "host-boot-1")
    return market, books, decision, latency, features


class EconomicContractTests(unittest.TestCase):
    def test_fee_on_both_legs_reverses_gross_profit(self):
        market, books, decision, latency, _ = fixture()
        result = round_trip_label(decision, market, books, latency, latency, 200*MS)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(D(result["net_pnl"]), D("-0.07486"))
        self.assertEqual(result["entry_ns"], 1280*MS)
        self.assertEqual(result["exit_decision_ns"], 1480*MS)
        self.assertEqual(result["exit_ns"], 1740*MS)
        self.assertEqual(result["execution_authority"], AUTHORITY)
        self.assertFalse(result["real_order_submission"])

    def test_delay_counted_once_when_included(self):
        market, books, decision, latency, _ = fixture()
        inclusive = replace(latency, duration_ns=260*MS, scope="INCLUDES_MARKET_DELAY")
        a = round_trip_label(decision, market, books, latency, latency, 200*MS)
        b = round_trip_label(decision, market, books, inclusive, inclusive, 200*MS)
        self.assertEqual((a["entry_ns"], a["exit_ns"], a["net_pnl"]),
                         (b["entry_ns"], b["exit_ns"], b["net_pnl"]))

    def test_market_delay_can_remove_an_opportunity(self):
        market, books, decision, latency, _ = fixture()
        changed = replace(books.frames[1], receive_ns=1100*MS, applied_ns=1100*MS, available_ns=1100*MS)
        result = round_trip_label(decision, market, BookSeries((books.frames[0], changed), books.proof), latency, latency, 200*MS)
        self.assertEqual(result["status"], "NO_FILL")
        self.assertEqual(result["net_pnl"], "0")
        self.assertEqual(result["entry_quantity"], "0")

    def test_missing_delay_not_a_zero_default(self):
        market, *_ = fixture()
        data = asdict(market)
        del data["mandatory_delay_ns"]
        with self.assertRaises(TypeError):
            MarketEconomics(**data)

    def test_delay_metadata_consistency(self):
        market, *_ = fixture()
        for overrides in ({"delay_enabled": False}, {"mandatory_delay_ns": 0}, {"delay_enabled": "true"}):
            with self.subTest(overrides=overrides), self.assertRaises(ResearchContractError):
                replace(market, **overrides)

    def test_latency_shorter_than_included_delay_is_invalid(self):
        market, _, decision, latency, _ = fixture()
        with self.assertRaisesRegex(ResearchContractError, "smaller_than"):
            replace(latency, scope="INCLUDES_MARKET_DELAY").arrival(decision.decision_ns, market)

    def test_latency_future_and_unknown_scope_are_invalid(self):
        market, books, decision, latency, _ = fixture()
        with self.assertRaises(ResearchContractError):
            replace(latency, scope="UNKNOWN")
        with self.assertRaisesRegex(ResearchContractError, "future_latency"):
            round_trip_label(decision, market, books, latency, replace(latency, available_ns=1200*MS), 200*MS)

    def test_fee_cannot_be_missing_nonfinite_or_boolean(self):
        market, *_ = fixture()
        for value in (None, True, float("nan"), "Infinity", -1):
            with self.subTest(value=value), self.assertRaises(ResearchContractError):
                replace(market, taker_fee_rate=value)

    def test_zero_fee_must_be_explicit(self):
        market, books, *_ = fixture()
        free = replace(market, taker_fee_rate=D(0))
        book = replace(books.frames[0], economics_hash=free.hash)
        result = sweep(book, free, side="BUY", quantity=D(5), limit_price=D("0.50"), time_in_force="FAK")
        self.assertEqual(result.fees, 0)
        self.assertNotEqual(free.hash, market.hash)

    def test_unsupported_fee_formula_rejected(self):
        market, *_ = fixture()
        with self.assertRaises(ResearchContractError):
            replace(market, fee_formula="CATEGORY_DEFAULT")

    def test_expiry_is_not_bypassed_by_latency(self):
        market, _, decision, latency, _ = fixture()
        with self.assertRaisesRegex(ResearchContractError, "expired"):
            latency.arrival(4900*MS, market)

    def test_sweep_multiple_levels_and_no_chase(self):
        market, books, *_ = fixture()
        book = replace(books.frames[0], asks=((D("0.50"), D(2)), (D("0.51"), D(3)), (D("0.52"), D(20))))
        result = sweep(book, market, side="BUY", quantity=D(7), limit_price=D("0.51"), time_in_force="FAK")
        self.assertEqual(result.quantity, 5)
        self.assertEqual(result.notional, D("2.53"))
        self.assertEqual(len(result.fills), 2)
        self.assertEqual(result.fees, market.fee(D(2), D("0.50")) + market.fee(D(3), D("0.51")))

    def test_fok_is_not_partial(self):
        market, books, *_ = fixture()
        book = replace(books.frames[0], asks=((D("0.50"), D(2)),))
        result = sweep(book, market, side="BUY", quantity=D(5), limit_price=D("0.50"), time_in_force="FOK")
        self.assertEqual((result.quantity, result.notional, result.fees, result.fills), (0, 0, 0, ()))

    def test_partial_exit_is_censored_not_profit(self):
        market, books, decision, latency, _ = fixture()
        partial = replace(books.frames[1], bids=((D("0.52"), D(2)),))
        result = round_trip_label(decision, market, BookSeries((books.frames[0], partial), books.proof), latency, latency, 200*MS)
        self.assertEqual(result["status"], "OPEN_RESIDUAL")
        self.assertEqual(D(result["remaining_quantity"]), 3)
        self.assertIsNone(result["net_pnl"])
        self.assertLess(D(result["cash_delta"]), 0)

    def test_exit_limit_frozen_before_exit_delay(self):
        market, books, decision, latency, _ = fixture()
        late = replace(books.frames[1], sequence=3, receive_ns=1600*MS, applied_ns=1600*MS,
                       available_ns=1600*MS, bids=((D("0.48"), D(20)),), asks=((D("0.49"), D(20)),))
        result = round_trip_label(decision, market, BookSeries(books.frames + (late,), books.proof), latency, latency, 200*MS)
        self.assertEqual(result["exit_quantity"], "0")
        self.assertEqual(result["status"], "OPEN_RESIDUAL")

    def test_depth_is_not_synthesized(self):
        market, books, decision, latency, _ = fixture()
        empty = replace(books.frames[0], asks=())
        decision = replace(decision, origin_book_hash=empty.hash)
        result = round_trip_label(decision, market, BookSeries((empty, books.frames[1]), books.proof), latency, latency, 200*MS)
        self.assertEqual(result["status"], "NO_FILL")

    def test_quiet_but_certified_book_is_not_discarded(self):
        market, books, decision, latency, _ = fixture()
        result = round_trip_label(decision, market, BookSeries((books.frames[0],), books.proof), latency, latency, 200*MS)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["entry_book_hash"], result["exit_book_hash"])

    def test_gap_or_uncovered_target_is_not_flat_price(self):
        market, books, decision, latency, _ = fixture()
        with self.assertRaises(ResearchContractError):
            replace(books.proof, complete=False)
        short = replace(books.proof, end_ns=1500*MS)
        with self.assertRaisesRegex(ResearchContractError, "target_not_covered"):
            round_trip_label(decision, market, BookSeries(books.frames, short), latency, latency, 200*MS)

    def test_asof_uses_availability_not_source_receipt(self):
        market, books, *_ = fixture()
        late = replace(books.frames[1], receive_ns=1100*MS, applied_ns=1250*MS, available_ns=1300*MS)
        series = BookSeries((books.frames[0], late), books.proof)
        self.assertEqual(series.asof(1280*MS, market.hash), books.frames[0])
        self.assertEqual(series.asof(1300*MS, market.hash), late)

    def test_receive_apply_available_order(self):
        _, books, *_ = fixture()
        with self.assertRaises(ResearchContractError):
            replace(books.frames[0], applied_ns=800*MS)

    def test_same_timestamp_respects_applied_sequence(self):
        market, books, *_ = fixture()
        newer = replace(books.frames[0], sequence=2, bids=((D("0.48"), D(10)),))
        series = BookSeries((books.frames[0], newer), books.proof)
        self.assertEqual(series.asof(1000*MS, market.hash), newer)

    def test_no_reordering_or_mixed_epochs(self):
        _, books, *_ = fixture()
        for frames in ((books.frames[1], books.frames[0]),
                       (books.frames[0], replace(books.frames[1], connection_epoch=2))):
            with self.assertRaises(ResearchContractError):
                BookSeries(frames, books.proof)

    def test_cross_host_clock_not_comparable(self):
        market, books, decision, latency, _ = fixture()
        other = BookSeries(tuple(replace(f, clock_domain="other-host") for f in books.frames),
                           replace(books.proof, clock_domain="other-host"))
        with self.assertRaisesRegex(ResearchContractError, "clock_domains"):
            round_trip_label(decision, market, other, latency, latency, 200*MS)

    def test_origin_cut_and_feature_time(self):
        market, books, decision, latency, _ = fixture()
        with self.assertRaises(ResearchContractError):
            replace(decision, feature_available_ns=decision.decision_ns+1)
        with self.assertRaisesRegex(ResearchContractError, "origin_book"):
            round_trip_label(replace(decision, origin_book_hash=H), market, books, latency, latency, 200*MS)

    def test_metadata_change_in_the_middle_is_not_ignored(self):
        market, books, decision, latency, _ = fixture()
        temporary = replace(books.frames[1], sequence=3, receive_ns=1400*MS, applied_ns=1400*MS,
                            available_ns=1400*MS, economics_hash="b"*64)
        restored = replace(books.frames[1], sequence=4, receive_ns=1450*MS, applied_ns=1450*MS, available_ns=1450*MS)
        with self.assertRaisesRegex(ResearchContractError, "changed_during_label"):
            round_trip_label(decision, market, BookSeries(books.frames+(temporary, restored), books.proof), latency, latency, 200*MS)

    def test_tick_size_quantity_and_context(self):
        market, books, *_ = fixture()
        for quantity, price in ((D("0.5"), D("0.50")), (D(5), D("0.505"))):
            with self.assertRaises(ResearchContractError):
                sweep(books.frames[0], market, side="BUY", quantity=quantity, limit_price=price, time_in_force="FAK")
        with self.assertRaises(ResearchContractError):
            sweep(replace(books.frames[0], token_id="other"), market, side="BUY", quantity=D(5), limit_price=D("0.50"), time_in_force="FAK")

    def test_book_validation(self):
        _, books, *_ = fixture()
        for asks in (((D("0.49"), D(1)),), ((D("0.50"), D(-1)),),
                     ((D("0.51"), D(1)), (D("0.50"), D(1))),
                     ((D("0.50"), D(1)), (D("0.50"), D(1)))):
            with self.assertRaises(ResearchContractError):
                replace(books.frames[0], asks=asks)

    def test_ambient_decimal_precision_does_not_change_label(self):
        market, books, decision, latency, _ = fixture()
        expected = round_trip_label(decision, market, books, latency, latency, 200*MS)
        with localcontext() as context:
            context.prec = 6
            actual = round_trip_label(decision, market, books, latency, latency, 200*MS)
        self.assertEqual(actual, expected)

    def test_deterministic_label_hash(self):
        market, books, decision, latency, _ = fixture()
        first = round_trip_label(decision, market, books, latency, latency, 200*MS)
        second = round_trip_label(decision, market, books, latency, latency, 200*MS)
        self.assertEqual(first, second)
        self.assertEqual(first["label_hash"], canonical_hash({k:v for k,v in first.items() if k != "label_hash"}))


if __name__ == "__main__":
    unittest.main()
