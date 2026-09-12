import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_maker_anchor_eligibility import evaluate_anchor


class FakeBook:
    def __init__(self):
        self.model_sha = "a" * 40
        self.session = "s1"
        self.epoch = 1
        self.gaps = 0
        self.watermark_monotonic_ns = 2_000_000_000
        self.history = defaultdict(list)


def status(now_ms=1_000_000):
    return {
        "state": "running",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "evidence_complete": True,
        "model_sha": "a" * 40,
        "timestamp_ms": now_ms,
        "observer_session_id": "s1",
        "connection_epoch": 1,
        "book_watermark_receive_monotonic_ns": 2_000_000_000,
    }


def order():
    return {
        "market_id": "m1",
        "token_id": "t1",
        "receive_ts_ms": 1_000_000,
        "metadata": {
            "arrival_receive_monotonic_ns": 1_500_000_000,
            "arrival_exchange_event_ns": 1_400_000_000,
        },
    }


def protocol():
    return {"maker": {"maximum_feature_age_ms": 500}}


class MakerAnchorEligibilityTest(unittest.TestCase):
    def test_valid_arrival_is_eligible(self):
        book = FakeBook()
        book.history[("m1", "t1")].append({
            "receive_monotonic_ns": 1_490_000_000,
            "receive_wall_ms": 999_900,
            "valid": True,
            "lineage_continuous": True,
            "features_valid": True,
            "best_bid": 0.40,
            "best_ask": 0.41,
            "tick_size": 0.01,
        })
        result = evaluate_anchor(order(), book, status(), protocol(), now_ms=1_000_000)
        self.assertEqual(result["state"], "ELIGIBLE")
        self.assertEqual(result["feature_age_ms"], 100)

    def test_missing_watermark_is_pending_not_censored_anchor(self):
        book = FakeBook()
        book.watermark_monotonic_ns = 1_000_000_000
        result = evaluate_anchor(order(), book, status(), protocol(), now_ms=1_000_000)
        self.assertEqual(result["state"], "PENDING")
        self.assertEqual(result["reason"], "BOOK_WATERMARK_BEFORE_ARRIVAL")

    def test_invalid_lineage_is_ineligible_before_anchor(self):
        book = FakeBook()
        book.history[("m1", "t1")].append({
            "receive_monotonic_ns": 1_490_000_000,
            "receive_wall_ms": 999_900,
            "valid": True,
            "lineage_continuous": False,
            "features_valid": True,
            "best_bid": 0.40,
            "best_ask": 0.41,
            "tick_size": 0.01,
        })
        result = evaluate_anchor(order(), book, status(), protocol(), now_ms=1_000_000)
        self.assertEqual(result["state"], "INELIGIBLE")
        self.assertEqual(result["reason"], "INVALID_ARRIVAL_BOOK")

    def test_future_outcomes_are_not_inputs(self):
        book = FakeBook()
        row = {
            "receive_monotonic_ns": 1_490_000_000,
            "receive_wall_ms": 999_900,
            "valid": True,
            "lineage_continuous": True,
            "features_valid": True,
            "best_bid": 0.40,
            "best_ask": 0.41,
            "tick_size": 0.01,
        }
        book.history[("m1", "t1")].append(row)
        before = evaluate_anchor(order(), book, status(), protocol(), now_ms=1_000_000)
        enriched = order()
        enriched["future_markout"] = -0.5
        enriched["settlement"] = 1.0
        after = evaluate_anchor(enriched, book, status(), protocol(), now_ms=1_000_000)
        self.assertEqual(before["state"], after["state"])
        self.assertEqual(before["reason"], after["reason"])


if __name__ == "__main__":
    unittest.main()
