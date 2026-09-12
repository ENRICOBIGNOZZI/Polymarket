from collections import Counter, defaultdict
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_prospective_profit_experiments import ProspectiveProfitExperiments
from v7_profit_experiments import rows


class FakeLedger:
    def __init__(self, values):
        self.values = list(values)

    def poll(self):
        values, self.values = self.values, []
        return values


class FakeBook:
    def __init__(self):
        self.model_sha = "a" * 40
        self.session = "session-1"
        self.epoch = 1
        self.gaps = 0
        self.watermark_monotonic_ns = 2_000_000_000
        self.history = defaultdict(list)


def status(now_ms=1_000_000, watermark=2_000_000_000):
    return {
        "state": "running",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "evidence_complete": True,
        "model_sha": "a" * 40,
        "timestamp_ms": now_ms,
        "observer_session_id": "session-1",
        "connection_epoch": 1,
        "book_watermark_receive_monotonic_ns": watermark,
    }


def order():
    return {
        "schema_version": 1,
        "record_id": "order-1",
        "event_type": "ORDER_SUBMITTED",
        "market_id": "market-1",
        "token_id": "token-1",
        "receive_ts_ms": 1_000_000,
        "model_sha": "a" * 40,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "metadata": {
            "component": "professional_maker",
            "counterfactual": False,
            "excluded_from_portfolio_equity": False,
            "arrival_receive_monotonic_ns": 1_500_000_000,
            "arrival_exchange_event_ns": 1_400_000_000,
            "opportunity_envelope": {
                "settlement_model": {"model_hash": "b" * 64},
            },
        },
    }


def valid_cut(lineage=True):
    return {
        "schema": "polymarket_v7_causal_book_observation_v1",
        "model_sha": "a" * 40,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "receive_monotonic_ns": 1_490_000_000,
        "receive_wall_ms": 999_900,
        "valid": True,
        "lineage_continuous": lineage,
        "features_valid": True,
        "best_bid": 0.40,
        "best_ask": 0.41,
        "tick_size": 0.01,
    }


class ProspectiveProfitExperimentTest(unittest.TestCase):
    def experiment(self, directory, book, ledger):
        exp = ProspectiveProfitExperiments.__new__(ProspectiveProfitExperiments)
        exp.run_root = Path(directory) / "run"
        exp.output = Path(directory) / "cohort"
        exp.output.mkdir(parents=True)
        (exp.run_root / "micro_maker").mkdir(parents=True)
        exp.book = book
        exp.sha = "a" * 40
        exp.protocol = {
            "protocol_id": "test-prospective",
            "maker": {
                "validity_semantics": "SEPARATE_EXECUTION_AND_MARKOUT_WINDOWS",
                "maximum_feature_age_ms": 500,
                "anchor_eligibility_wait_ms": 1000,
            },
        }
        exp.manifest = {
            "manifest_sha256": "c" * 64,
            "forward_start_ns": 900_000 * 1_000_000,
            "confirmatory_end_ns": 1_100_000 * 1_000_000,
            "frozen_model_hash": "b" * 64,
        }
        exp.accept_new_anchors = True
        exp.anchors = set()
        exp.maker_pending = {}
        exp.maker_candidates = {}
        exp.maker_skipped_order_ids = set()
        exp.anchor_eligibility_wait_ms = 1000
        exp.counts = Counter()
        exp.ledger = ledger
        return exp

    def write_status(self, exp, value):
        path = exp.run_root / "micro_maker" / "fillability_ws_status.json"
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def test_valid_arrival_becomes_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            book = FakeBook()
            book.history[("market-1", "token-1")].append(valid_cut())
            exp = self.experiment(directory, book, FakeLedger([order()]))
            self.write_status(exp, status())
            exp.collect_anchors(1_000_000 * 1_000_000)
            self.assertIn("market-1", exp.anchors)
            self.assertIn("market-1", exp.maker_pending)
            observed = list(rows(exp.output / "observations.jsonl"))
            self.assertEqual([row["kind"] for row in observed], ["MAKER_ANCHOR"])
            self.assertEqual(observed[0]["anchor_eligibility"]["state"], "ELIGIBLE")

    def test_invalid_lineage_is_skipped_before_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            book = FakeBook()
            book.history[("market-1", "token-1")].append(valid_cut(lineage=False))
            exp = self.experiment(directory, book, FakeLedger([order()]))
            self.write_status(exp, status())
            exp.collect_anchors(1_000_000 * 1_000_000)
            self.assertNotIn("market-1", exp.anchors)
            observed = list(rows(exp.output / "observations.jsonl"))
            self.assertEqual([row["kind"] for row in observed], ["MAKER_ANCHOR_SKIP"])
            self.assertEqual(observed[0]["eligibility_reason"], "INVALID_ARRIVAL_BOOK")

    def test_pending_watermark_times_out_to_skip_not_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            book = FakeBook()
            book.watermark_monotonic_ns = 1_000_000_000
            exp = self.experiment(directory, book, FakeLedger([order()]))
            self.write_status(exp, status(watermark=1_000_000_000))
            start = 1_000_000 * 1_000_000
            exp.collect_anchors(start)
            self.assertEqual(list(rows(exp.output / "observations.jsonl")), [])
            exp.collect_anchors(start + 1_100_000_000)
            observed = list(rows(exp.output / "observations.jsonl"))
            self.assertEqual([row["kind"] for row in observed], ["MAKER_ANCHOR_SKIP"])
            self.assertTrue(observed[0]["eligibility_reason"].startswith("ELIGIBILITY_TIMEOUT:"))
            self.assertNotIn("market-1", exp.anchors)


if __name__ == "__main__":
    unittest.main()
