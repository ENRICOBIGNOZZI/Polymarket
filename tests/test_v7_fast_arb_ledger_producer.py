#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from v7_execution_ledger import CanonicalLedgerWriter, load_events
from v7_fast_arb_ledger_producer import (
    FastArbSourceError,
    STRATEGY,
    begin_session,
    ingest_rows,
    load_session,
    load_status,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40


def clocks() -> tuple[int, int, int, int]:
    now = int(time.time() * 1000)
    exchange = now - 800
    receive = now - 600
    decision = now - 500
    observed = now - 400
    return exchange, receive, decision, observed


def session(started: int) -> dict[str, object]:
    return {
        "schema": "polymarket_v7_fast_arb_ledger_session_v1",
        "session_id": "session-test",
        "source": "polymarket_fast_arb_shadow",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "started_ts_ms": started,
        "opportunities_file": "fast_arb_opportunities.csv",
        "status_file": "fast_arb_status.json",
    }


def source_row(*, enriched: bool) -> dict[str, str]:
    exchange, receive, decision, observed = clocks()
    row = {
        "observed_ts_ms": str(observed),
        "kind": "BINARY_COMPLETE_SET",
        "id": "binary:market-1",
        "event_id": "event-1",
        "hard_arbitrage": "1",
        "executable": "1",
        "risk_class": "hard_structural",
        "reject_reason": "",
        "payoff_floor": "1.0",
        "raw_edge_per_share": "0.012",
        "net_edge_per_share": "0.009",
        "executable_shares": "20",
        "capital_required": "19.4",
        "expected_profit": "0.18",
        "exchange_ts_ms": str(exchange),
        "received_ts_ms": str(receive),
        "decision_ts_ms": str(decision),
        "feed_latency_ms": str(receive - exchange),
        "legs": "market-1:token-y:YES:20:0.48:0.001:9.62|market-1:token-n:NO:20:0.49:0.001:9.82",
    }
    if enriched:
        row["model_sha"] = SHA
        row["book_snapshot_id"] = "bundle-snapshot-1"
        row["leg_context_json"] = json.dumps(
            [
                {
                    "token_id": "token-y",
                    "exchange_ts_ms": exchange,
                    "receive_ts_ms": receive,
                    "book_snapshot_id": "snapshot-y",
                },
                {
                    "token_id": "token-n",
                    "exchange_ts_ms": exchange + 10,
                    "receive_ts_ms": receive + 10,
                    "book_snapshot_id": "snapshot-n",
                },
            ]
        )
    return row


class FastArbLedgerProducerTests(unittest.TestCase):
    def test_current_fast_csv_is_opportunity_only_not_assumed_candidate(self) -> None:
        row = source_row(enriched=False)
        started = int(row["observed_ts_ms"]) - 100
        status = {"timestamp_ms": int(row["observed_ts_ms"]) + 100, "mode": "shadow", "real_order_submission": False}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            with CanonicalLedgerWriter(ledger, writer_id="test", model_sha=SHA) as writer:
                summary = ingest_rows([row], session=session(started), status=status, writer=writer)
            events = load_events(ledger, expected_model_sha=SHA)

        self.assertEqual(summary["opportunities_appended"], 1)
        self.assertEqual(summary["canonical_candidates_appended"], 0)
        self.assertEqual(summary["candidate_rows_blocked"], 1)
        self.assertEqual([event.event_type for event in events], ["OPPORTUNITY"])
        self.assertEqual(events[0].strategy, STRATEGY)
        self.assertFalse(events[0].metadata["candidate_contract_ready"])
        self.assertIn("missing_exact_book_snapshot_identity", events[0].metadata["candidate_blockers"])
        self.assertIn("missing_per_leg_clock_lineage", events[0].metadata["candidate_blockers"])
        self.assertFalse(events[0].metadata["promotion_eligible"])

    def test_enriched_exact_sha_source_emits_bundle_and_per_leg_candidates(self) -> None:
        row = source_row(enriched=True)
        started = int(row["observed_ts_ms"]) - 100
        status = {"timestamp_ms": int(row["observed_ts_ms"]) + 100, "mode": "shadow", "real_order_submission": False}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            with CanonicalLedgerWriter(ledger, writer_id="test", model_sha=SHA) as writer:
                summary = ingest_rows([row], session=session(started), status=status, writer=writer)
            events = load_events(ledger, expected_model_sha=SHA)

        self.assertEqual(summary["opportunities_appended"], 1)
        self.assertEqual(summary["canonical_candidates_appended"], 3)
        self.assertEqual([event.event_type for event in events], ["OPPORTUNITY", "CANDIDATE", "CANDIDATE", "CANDIDATE"])
        bundle = events[1]
        self.assertEqual(bundle.book_snapshot_id, "bundle-snapshot-1")
        self.assertEqual(bundle.intended_action, "STRUCTURAL_ARB_PAPER_CANDIDATE")
        legs = events[2:]
        self.assertEqual({event.token_id for event in legs}, {"token-y", "token-n"})
        self.assertEqual({event.book_snapshot_id for event in legs}, {"snapshot-y", "snapshot-n"})
        self.assertTrue(all(event.side == "BUY" for event in legs))
        self.assertFalse(any(event.event_type in {"ORDER_SUBMITTED", "FILL", "FINAL"} for event in events))

    def test_source_row_mixed_sha_fails_closed(self) -> None:
        row = source_row(enriched=True)
        row["model_sha"] = OTHER_SHA
        started = int(row["observed_ts_ms"]) - 100
        status = {"timestamp_ms": int(row["observed_ts_ms"]) + 100, "mode": "shadow", "real_order_submission": False}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            with CanonicalLedgerWriter(ledger, writer_id="test", model_sha=SHA) as writer:
                with self.assertRaisesRegex(FastArbSourceError, "mixed_sha"):
                    ingest_rows([row], session=session(started), status=status, writer=writer)

    def test_idempotent_source_row_does_not_duplicate(self) -> None:
        row = source_row(enriched=True)
        started = int(row["observed_ts_ms"]) - 100
        status = {"timestamp_ms": int(row["observed_ts_ms"]) + 100, "mode": "shadow", "real_order_submission": False}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            with CanonicalLedgerWriter(ledger, writer_id="test-1", model_sha=SHA) as writer:
                ingest_rows([row], session=session(started), status=status, writer=writer)
            first = load_events(ledger, expected_model_sha=SHA)
            keys = {str(event.metadata.get("source_row_key")) for event in first if event.metadata.get("source_row_key")}
            with CanonicalLedgerWriter(ledger, writer_id="test-2", model_sha=SHA) as writer:
                summary = ingest_rows([row], session=session(started), status=status, writer=writer, existing_row_keys=keys)
            second = load_events(ledger, expected_model_sha=SHA)

        self.assertEqual(summary["duplicate_rows"], 1)
        self.assertEqual(len(first), len(second))

    def test_pre_session_rows_are_not_relabelled_as_current_sha(self) -> None:
        row = source_row(enriched=False)
        started = int(row["observed_ts_ms"]) + 1
        status = {"timestamp_ms": started + 100, "mode": "shadow", "real_order_submission": False}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            with CanonicalLedgerWriter(ledger, writer_id="test", model_sha=SHA) as writer:
                summary = ingest_rows([row], session=session(started), status=status, writer=writer)
        self.assertEqual(summary["pre_session_rows"], 1)
        self.assertEqual(summary["opportunities_appended"], 0)

    def test_session_and_status_enforce_paper_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest_path = begin_session(root, model_sha=SHA, started_ts_ms=12345)
            loaded = load_session(manifest_path, expected_model_sha=SHA)
            self.assertTrue(loaded["paper_only"])
            self.assertFalse(loaded["authenticated_execution"])

            bad_status = root / "fast_arb_status.json"
            bad_status.write_text(json.dumps({"timestamp_ms": 12346, "mode": "shadow", "real_order_submission": True}), encoding="utf-8")
            with self.assertRaisesRegex(FastArbSourceError, "not_safe_shadow"):
                load_status(bad_status, session_started_ts_ms=12345)

    def test_status_must_cover_every_ingested_observation(self) -> None:
        row = source_row(enriched=False)
        started = int(row["observed_ts_ms"]) - 100
        status = {"timestamp_ms": int(row["observed_ts_ms"]) - 1, "mode": "shadow", "real_order_submission": False}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            with CanonicalLedgerWriter(ledger, writer_id="test", model_sha=SHA) as writer:
                with self.assertRaisesRegex(FastArbSourceError, "after_status_snapshot"):
                    ingest_rows([row], session=session(started), status=status, writer=writer)


if __name__ == "__main__":
    unittest.main()
