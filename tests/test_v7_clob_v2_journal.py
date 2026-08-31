from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evidence = load("v7_real_pnl_evidence")
journal = load("v7_clob_v2_journal")
SHA = "f" * 40
PROVENANCE = "a" * 64


def sealed_trade(side: str = "BUY", *, size: str = "100", price: str = "0.4",
                 status: str = "TRADE_STATUS_CONFIRMED", trader_side: str = "MAKER",
                 fee_rate_bps: str = "0"):
    wire = json.dumps({
        "topic": "user", "type": "trade", "payload": {
            "id": "trade-1", "takerOrderId": "order-1", "owner": "api-key-1",
            "market": "condition-1", "tokenId": "token-1", "side": side, "size": size,
            "price": price, "status": status, "traderSide": trader_side,
            "feeRateBps": fee_rate_bps, "timestamp": 1782753357257,
        },
    }, separators=(",", ":"))
    with tempfile.TemporaryDirectory() as directory:
        path = evidence.evidence_path(Path(directory))
        with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            return writer.append(evidence.clob_user_ws_record(SHA, 1_000, wire))


class ClobV2JournalTests(unittest.TestCase):
    def test_buy_trade_becomes_balanced_integer_pusd_and_token_postings(self) -> None:
        entry = journal.clob_trade_journal(sealed_trade(), provenance_record_hash=PROVENANCE)
        entry.validate(sealed=False)
        postings = {(row.account, row.asset): row.units for row in entry.postings}
        self.assertEqual(postings[("assets:cash:wallet", "pUSD")], -40_000_000)
        self.assertEqual(postings[("assets:outcome:position", "token:token-1")], 100_000_000)
        self.assertEqual(entry.metadata["provenance_record_hash"], PROVENANCE)

    def test_sell_reverses_the_balanced_postings(self) -> None:
        entry = journal.clob_trade_journal(sealed_trade("SELL"), provenance_record_hash=PROVENANCE)
        postings = {(row.account, row.asset): row.units for row in entry.postings}
        self.assertEqual(postings[("assets:cash:wallet", "pUSD")], 40_000_000)
        self.assertEqual(postings[("assets:outcome:position", "token:token-1")], -100_000_000)

    def test_requires_sealed_trade_evidence_and_exact_base_units(self) -> None:
        record = evidence.clob_user_ws_record(SHA, 1_000, json.dumps({
            "topic": "user", "type": "trade", "payload": {
                "id": "trade-1", "takerOrderId": "order-1", "owner": "api-key-1",
                "market": "condition-1", "tokenId": "token-1", "side": "BUY", "size": "1",
                "price": "0.5", "status": "TRADE_STATUS_CONFIRMED", "traderSide": "MAKER",
                "feeRateBps": "0", "timestamp": 1,
            },
        }))
        with self.assertRaisesRegex(journal.ClobJournalError, "sealed"):
            journal.clob_trade_journal(record, provenance_record_hash=PROVENANCE)
        with self.assertRaisesRegex(journal.ClobJournalError, "exact_base_units"):
            journal.clob_trade_journal(sealed_trade(size="0.0000001"), provenance_record_hash=PROVENANCE)

    def test_sealed_hash_is_rechecked_before_conversion(self) -> None:
        with self.assertRaisesRegex(journal.ClobJournalError, "must_be_sealed"):
            journal.clob_trade_journal(
                replace(sealed_trade(), response={}), provenance_record_hash=PROVENANCE,
            )

    def test_pre_final_or_failed_trade_is_not_a_journal_fill(self) -> None:
        for status in ("TRADE_STATUS_MATCHED", "TRADE_STATUS_MINED", "TRADE_STATUS_RETRYING",
                       "TRADE_STATUS_FAILED"):
            with self.subTest(status=status), self.assertRaisesRegex(journal.ClobJournalError, "not_settled"):
                journal.clob_trade_journal(
                    sealed_trade(size="1", price="0.5", status=status),
                    provenance_record_hash=PROVENANCE,
                )

    def test_nonzero_taker_rate_requires_a_separate_observed_fee_fact(self) -> None:
        with self.assertRaisesRegex(journal.ClobJournalError, "observed_taker_fee_required"):
            journal.clob_trade_journal(
                sealed_trade(size="1", price="0.5", trader_side="TAKER", fee_rate_bps="50"),
                provenance_record_hash=PROVENANCE,
            )


if __name__ == "__main__":
    unittest.main()
