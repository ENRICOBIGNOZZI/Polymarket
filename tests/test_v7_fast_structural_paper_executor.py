from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import CanonicalLedgerWriter, LedgerEvent  # noqa: E402
from v7_fast_structural_paper_executor import Executor  # noqa: E402

SHA = "a" * 40


class FastStructuralPaperExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "paper_only": True, "starting_capital": 100.0,
            "gamma_url": "https://gamma.invalid",
            "v7": {"authenticated_execution": False, "real_order_submission": False},
        }))
        now = time.time_ns() // 1_000_000
        self.shared = self.root / "market_data" / "shared_state.json"
        self.shared.parent.mkdir(parents=True)
        self.shared.write_text(json.dumps({
            "schema": "polymarket_v7_shared_market_state_v1", "model_sha": SHA,
            "paper_only": True, "authenticated_execution": False,
            "real_order_submission": False, "timestamp_ms": now,
            "snapshot_id": "snapshot-1", "generation": 1,
            "books": [{
                "token_id": "yes", "market_id": "m1", "condition_id": "c1",
                "event_id": "e1", "outcome": "YES", "exchange_ts_ms": now - 2,
                "receive_ts_ms": now - 1, "state_version": 1, "lineage_epoch": 1,
                "lineage_continuous": True, "provenance": "WEBSOCKET",
                "tick_size": 0.01, "min_order_size": 1.0,
                "bids": [{"price": 0.39, "size": 20.0}],
                "asks": [{"price": 0.40, "size": 20.0}],
                "fee_verified": True, "fee_rate": 0.0,
                "fee_exponent": 1.0, "fee_taker_only": True,
            }],
        }))
        ledger = self.root / "ledger" / "execution.jsonl"
        ledger.parent.mkdir(parents=True)
        decision = now - 1
        candidate = LedgerEvent(
            event_type="CANDIDATE", strategy="FAST_STRUCTURAL", model_sha=SHA,
            candidate_id="candidate-1", bundle_id="bundle-1", opportunity_id="opp-1",
            event_id="e1", exchange_ts_ms=now - 3, receive_ts_ms=now - 2,
            decision_ts_ms=decision, book_snapshot_id="detector-book",
            intended_action="ARB", intended_size=10.0, expected_ev=6.0,
            metadata={
                "hard_arbitrage": True, "payoff_floor": 1.0,
                "execution_compatibility": "SEQUENTIAL_FOK_HARD_ARB",
                "capital_required": 4.0, "opportunity_kind": "BINARY_COMPLETE_SET",
                "structured_legs": [{
                    "leg_id": "leg-1-yes", "market_id": "m1", "token_id": "yes",
                    "outcome": "YES", "side": "BUY", "target_quantity": 10.0,
                }],
                "target_quantities": {"leg-1-yes": 10.0},
            },
        )
        with CanonicalLedgerWriter(ledger, writer_id="test", model_sha=SHA) as writer:
            writer.append(candidate)
        self.args = argparse.Namespace(
            run_root=self.root, config=self.config, shared_state=self.shared,
            model_sha=SHA, slippage_bps=0.0, leg_latency_ms=0,
            max_shared_publish_age_ms=2500, interval=0.1, once=True,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_candidate_closes_order_fill_and_terminal_payout(self) -> None:
        executor = Executor(self.args)
        executor.step()
        self.assertEqual(len(executor.state["open_bundles"]), 1)
        spool = [json.loads(path.read_text()) for path in (self.root / "ledger" / "spool").glob("*.json")]
        self.assertEqual({row["event_type"] for row in spool}, {"ORDER_SUBMITTED", "FILL"})
        self.assertAlmostEqual(executor.state["cash"], 96.0)
        with mock.patch("v7_fast_structural_paper_executor.request_json", return_value={"closed": True}):
            executor.step()
        spool = [json.loads(path.read_text()) for path in (self.root / "ledger" / "spool").glob("*.json")]
        self.assertIn("FINAL", {row["event_type"] for row in spool})
        self.assertAlmostEqual(executor.state["cash"], 106.0)
        self.assertAlmostEqual(executor.state["realized_pnl_total"], 6.0)
        status = json.loads((self.root / "fast_structural" / "paper_executor_status.json").read_text())
        self.assertEqual(status["schema"], "polymarket_v7_fast_structural_paper_executor_v1")
        self.assertTrue(status["paper_only"])
        self.assertFalse(status["real_order_submission"])

    def test_joint_arrival_cost_is_rejected_before_first_leg(self) -> None:
        shared = json.loads(self.shared.read_text())
        yes = shared["books"][0]
        yes["asks"] = [{"price": 0.55, "size": 20.0}]
        no = {
            **yes,
            "token_id": "no",
            "outcome": "NO",
            "bids": [{"price": 0.49, "size": 20.0}],
            "asks": [{"price": 0.50, "size": 20.0}],
        }
        shared["books"] = [yes, no]
        self.shared.write_text(json.dumps(shared))

        ledger = self.root / "ledger" / "execution.jsonl"
        current = json.loads(ledger.read_text().splitlines()[0])
        current["metadata"]["capital_required"] = 10.5
        current["metadata"]["structured_legs"] = [
            {
                "leg_id": "leg-1-yes", "market_id": "m1", "token_id": "yes",
                "outcome": "YES", "side": "BUY", "target_quantity": 10.0,
            },
            {
                "leg_id": "leg-2-no", "market_id": "m1", "token_id": "no",
                "outcome": "NO", "side": "BUY", "target_quantity": 10.0,
            },
        ]
        current["metadata"]["target_quantities"] = {
            "leg-1-yes": 10.0, "leg-2-no": 10.0,
        }
        ledger.write_text(json.dumps(current) + "\n")

        executor = Executor(self.args)
        executor.step()
        spool_dir = self.root / "ledger" / "spool"
        self.assertFalse(spool_dir.exists())
        self.assertEqual(executor.state["open_bundles"], {})
        self.assertEqual(executor.state["aborting_bundles"], {})
        self.assertEqual(executor.state["cash"], 100.0)
        self.assertEqual(
            executor.state["rejections"].get("ARRIVAL_BUNDLE_NET_EDGE_NONPOSITIVE"), 1
        )


if __name__ == "__main__":
    unittest.main()
