#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent, canonical_ledger_path
from v7_ledger_spool import drain_spool, spool_event

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

    def test_graph_outcome_side_is_preserved_but_execution_side_is_buy(self) -> None:
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
                side="YES",
                exchange_ts_ms=1000,
                receive_ts_ms=1100,
                fill_price=0.5,
                filled_size=1.0,
                fee=0.0,
                fee_source="test:authoritative",
            )
            path = spool_event(root, event)
            raw = json.loads(path.read_text())
            self.assertEqual(raw["side"], "BUY")
            self.assertEqual(raw["metadata"]["outcome_side"], "YES")
            self.assertEqual(raw["metadata"]["execution_side"], "BUY")
            self.assertEqual(drain_spool(root, model_sha=SHA)["appended"], 1)

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
