#!/usr/bin/env python3
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import CanonicalLedgerWriter, LedgerEvent
from v7_joint_execution_policy import build

SHA = "a" * 40


class JointExecutionPolicyTests(unittest.TestCase):
    def test_direct_complete_state_is_published_without_marginal_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            with CanonicalLedgerWriter(ledger, writer_id="test", model_sha=SHA) as writer:
                targets = {"l0": 5.0, "l1": 5.0}
                for i in range(2):
                    writer.append(LedgerEvent(
                        event_type="ORDER_SUBMITTED", strategy="GRAPH_RV", model_sha=SHA,
                        bundle_id="b", order_id=f"o{i}", leg_id=f"l{i}", token_id=f"t{i}", side="YES",
                        decision_ts_ms=1000, exchange_ts_ms=900, receive_ts_ms=950, book_snapshot_id=f"s{i}",
                        intended_action="MAKER" if i == 0 else "TAKER", intended_size=5.0,
                        metadata={"target_quantities": targets, "entry_style": "MAKER/TAKER"},
                    ))
                    writer.append(LedgerEvent(
                        event_type="FILL", strategy="GRAPH_RV", model_sha=SHA,
                        bundle_id="b", order_id=f"o{i}", fill_id=f"f{i}", leg_id=f"l{i}", token_id=f"t{i}", side="YES",
                        exchange_ts_ms=1100, receive_ts_ms=1150, fill_price=.5, filled_size=5.0,
                        fee=0.0, fee_source="test:authoritative", complete=True,
                    ))
            report = build(ledger, model_sha=SHA, min_bundles=1)
            row = report["signatures"]["2"]["MAKER/TAKER"]
            self.assertEqual(row["p_complete"], 1.0)
            self.assertFalse(report["uses_product_of_marginals"])
            self.assertFalse(report["uses_minimum_marginal_proxy"])


if __name__ == "__main__":
    unittest.main()
