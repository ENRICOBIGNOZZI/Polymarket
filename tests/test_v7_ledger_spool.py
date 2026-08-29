#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerContractError, LedgerEvent, canonical_ledger_path
from v7_ledger_spool import (
    _drain_with_existing,
    _existing_record_ids,
    drain_spool,
    spool_event,
)

SHA = "1" * 40


class LedgerSpoolTests(unittest.TestCase):
    def test_only_router_writes_canonical_ledger_and_deduplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event = LedgerEvent(
                event_type="OPPORTUNITY",
                strategy="GRAPH_RV",
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
                strategy="GRAPH_RV",
                model_sha=SHA,
                opportunity_id="opp-cache-1",
            )
            second = LedgerEvent(
                event_type="OPPORTUNITY",
                strategy="GRAPH_RV",
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
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 1)

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


if __name__ == "__main__":
    unittest.main()
