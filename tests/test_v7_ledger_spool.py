#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import EconomicJournalEntry, JournalPosting, LedgerContractError, LedgerEvent, canonical_ledger_path, load_journal_entries
from v7_ledger_spool import (
    _drain_with_existing,
    _existing_record_ids,
    drain_spool,
    spool_event,
    spool_journal_entry,
)

SHA = "1" * 40


class LedgerSpoolTests(unittest.TestCase):
    def test_only_router_writes_canonical_ledger_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = LedgerEvent(
                event_type="OPPORTUNITY",
                strategy="CANONICAL_TEST",
                model_sha=SHA,
                opportunity_id="opp-1",
            )
            path = spool_event(root, event)
            self.assertTrue(path.exists())
            self.assertFalse(canonical_ledger_path(root).exists())
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["appended"], 1)
            rows = [json.loads(line) for line in canonical_ledger_path(root).read_text().splitlines() if line.strip()]
            self.assertEqual([row["record_id"] for row in rows], [event.record_id])

            spool_event(root, event)
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["duplicates"], 1)
            rows2 = [json.loads(line) for line in canonical_ledger_path(root).read_text().splitlines() if line.strip()]
            self.assertEqual(len(rows2), 1)

    def test_cached_loop_state_appends_new_events_without_ledger_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = LedgerEvent(
                event_type="OPPORTUNITY",
                strategy="CANONICAL_TEST",
                model_sha=SHA,
                opportunity_id="opp-cache-1",
            )
            second = LedgerEvent(
                event_type="OPPORTUNITY",
                strategy="CANONICAL_TEST",
                model_sha=SHA,
                opportunity_id="opp-cache-2",
            )
            existing = _existing_record_ids(canonical_ledger_path(root))
            self.assertEqual(existing, set())

            spool_event(root, first)
            result = _drain_with_existing(
                root, model_sha=SHA, existing=existing,
                writer_id="test-cached-router",
            )
            self.assertEqual(result["appended"], 1)
            self.assertIn(first.record_id, existing)

            # Re-spooling the same record is rejected from the in-memory cache;
            # no full canonical-ledger scan is required for steady-state dedup.
            spool_event(root, first)
            result = _drain_with_existing(
                root, model_sha=SHA, existing=existing,
                writer_id="test-cached-router",
            )
            self.assertEqual(result["duplicates"], 1)

            spool_event(root, second)
            result = _drain_with_existing(
                root, model_sha=SHA, existing=existing,
                writer_id="test-cached-router",
            )
            self.assertEqual(result["appended"], 1)
            self.assertIn(second.record_id, existing)
            rows = [json.loads(line) for line in canonical_ledger_path(root).read_text().splitlines() if line.strip()]
            self.assertEqual(len(rows), 2)

    def test_graph_events_must_arrive_with_canonical_execution_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = LedgerEvent(
                event_type="FILL",
                strategy="GRAPH_RV",
                model_sha=SHA,
                bundle_id="b",
                order_id="o",
                fill_id="f",
                leg_id="l",
                token_id="t",
                side="BUY",
                exchange_ts_ms=1000,
                receive_ts_ms=1100,
                fill_price=0.5,
                filled_size=1.0,
                fee=0.0,
                fee_source="test:authoritative",
                metadata={"outcome_side": "YES", "execution_side": "BUY"},
            )
            path = spool_event(root, event)
            raw = json.loads(path.read_text())
            self.assertEqual(raw["side"], "BUY")
            self.assertEqual(raw["metadata"]["outcome_side"], "YES")
            self.assertEqual(raw["metadata"]["execution_side"], "BUY")
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["appended"], 0)
            self.assertEqual(result["routed_research"], 1)
            self.assertFalse(canonical_ledger_path(root).exists())

    def test_engine_candidate_is_routed_to_the_single_opportunity_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = LedgerEvent(
                event_type="CANDIDATE", strategy="FAST_STRUCTURAL",
                model_sha=SHA, candidate_id="candidate-1",
                exchange_ts_ms=1000, receive_ts_ms=1100,
                decision_ts_ms=1200, book_snapshot_id="book-1",
            )
            spool_event(root, event)
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["routed_opportunities"], 1)
            self.assertEqual(result["appended"], 0)
            paths = list((root / "opportunities/inbox").glob("*.json"))
            self.assertEqual(len(paths), 1)
            ingress = json.loads(paths[0].read_text())
            self.assertEqual(ingress["ingress"]["engine_id"], "STRUCTURAL_ARB_ENGINE")

    def test_component_fill_without_coordinator_receipt_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = LedgerEvent(
                event_type="FILL", strategy="HARD_ARB", model_sha=SHA,
                order_id="order-1", fill_id="fill-1", side="BUY",
                token_id="token-1", exchange_ts_ms=1000, receive_ts_ms=1100,
                fill_price=0.5, filled_size=1.0, fee=0.0,
                fee_source="test:authoritative",
            )
            spool_event(root, event)
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["quarantined"], 1)
            self.assertEqual(result["rejected"], 1)
            self.assertFalse(canonical_ledger_path(root).exists())

    def test_component_fill_with_matching_coordinator_receipt_can_reach_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = {
                "schema": "polymarket_v7_global_opportunity_decision_v1",
                "owner": "V7_GLOBAL_PORTFOLIO_COORDINATOR",
                "engine_id": "STRUCTURAL_ARB_ENGINE",
                "action": "ARB",
                "selected_replay_key": "structural-cut-1",
                "new_risk_authorized": True,
            }
            event = LedgerEvent(
                event_type="FILL", strategy="HARD_ARB", model_sha=SHA,
                order_id="order-1", fill_id="fill-1", side="BUY",
                token_id="token-1", exchange_ts_ms=1000, receive_ts_ms=1100,
                fill_price=0.5, filled_size=1.0, fee=0.0,
                fee_source="test:authoritative",
                metadata={"coordinator_receipt": receipt},
            )
            spool_event(root, event)
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["appended"], 1)
            self.assertEqual(result["quarantined"], 0)

    def test_spool_rejects_graph_outcome_as_execution_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            event = LedgerEvent(
                event_type="FILL",
                strategy="GRAPH_RV",
                model_sha=SHA,
                bundle_id="b",
                order_id="o",
                fill_id="f",
                side="YES",
                fill_price=0.5,
                filled_size=1.0,
                fee=0.0,
                fee_source="test:authoritative",
            )
            with self.assertRaises(LedgerContractError):
                spool_event(Path(tmp), event)

    def test_mixed_sha_spool_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spool_event(root, LedgerEvent(event_type="OPPORTUNITY", strategy="GRAPH_RV", model_sha="2" * 40))
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["appended"], 0)
            self.assertEqual(result["rejected"], 1)
            self.assertFalse(canonical_ledger_path(root).exists())

    def test_same_millisecond_fill_cannot_precede_its_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            order = LedgerEvent(
                event_type="ORDER_SUBMITTED", strategy="CANONICAL_TEST",
                model_sha=SHA, record_id="z-order", recorded_ts_ms=2_000,
                order_id="order-1", side="BUY", limit_price=0.5,
                exchange_ts_ms=1_900, receive_ts_ms=1_950,
                decision_ts_ms=1_990, book_snapshot_id="book-1",
                intended_action="TAKE", intended_size=1.0,
                order_state="SUBMITTED_SHADOW",
            )
            fill = LedgerEvent(
                event_type="FILL", strategy="CANONICAL_TEST",
                model_sha=SHA, record_id="a-fill", recorded_ts_ms=2_000,
                order_id="order-1", fill_id="fill-1", token_id="token-1",
                side="BUY", exchange_ts_ms=1_900, receive_ts_ms=1_950,
                fill_price=0.5, filled_size=1.0, complete=True,
                fee=0.0, fee_source="test:authoritative",
            )
            spool_event(root, order)
            spool_event(root, fill)
            self.assertEqual(
                [path.name for path in sorted((root / "ledger/spool").glob("*.json"))],
                ["0000000002000.a-fill.json", "0000000002000.z-order.json"],
            )
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["appended"], 2)
            rows = [
                json.loads(line)
                for line in canonical_ledger_path(root).read_text().splitlines()
                if line.strip()
            ]
            self.assertEqual(
                [row["event_type"] for row in rows],
                ["ORDER_SUBMITTED", "FILL"],
            )

    def test_journal_facts_use_the_same_spool_and_canonical_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            journal = EconomicJournalEntry(
                entry_type="DEPOSIT",
                model_sha=SHA, observed_ts_ms=1, source="WALLET_RPC", source_record_id="deposit-1",
                postings=(
                    JournalPosting("assets:cash:wallet", "pUSD", 10),
                    JournalPosting("equity:external_funding", "pUSD", -10),
                ),
            )
            spool_journal_entry(root, journal)
            result = drain_spool(root, model_sha=SHA)
            self.assertEqual(result["appended"], 1)
            rows = load_journal_entries(canonical_ledger_path(root), expected_model_sha=SHA)
            self.assertEqual(len(rows), 1)
            self.assertIsNotNone(rows[0].entry_hash)
            spool_journal_entry(root, journal)
            self.assertEqual(drain_spool(root, model_sha=SHA)["duplicates"], 1)

    def test_taker_rebate_is_a_first_class_economic_fact(self) -> None:
        entry = EconomicJournalEntry(
            entry_type="TAKER_REBATE", model_sha=SHA, observed_ts_ms=1,
            source="DATA_API", source_record_id="taker-rebate-1",
            postings=(
                JournalPosting("assets:cash:wallet", "pUSD", 7),
                JournalPosting("income:taker_rebate", "pUSD", -7),
            ),
        )
        entry.validate(sealed=False)


if __name__ == "__main__":
    unittest.main()
