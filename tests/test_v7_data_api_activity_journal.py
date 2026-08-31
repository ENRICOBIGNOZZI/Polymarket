from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
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
activity = load("v7_data_api_activity_journal")
SHA = "e" * 40


def sealed_activity(row: dict):
    with tempfile.TemporaryDirectory() as directory:
        path = evidence.evidence_path(Path(directory))
        with evidence.EvidenceTapeWriter(path, writer_id="test", model_sha=SHA) as writer:
            return writer.append(evidence.EvidenceRecord(
                model_sha=SHA, source="DATA_API_ACTIVITY", source_record_id="page-1",
                received_ts_ms=1_000, request_method="GET",
                endpoint="https://data-api.polymarket.com/activity", response=[row],
            ))


class DataApiActivityJournalTests(unittest.TestCase):
    def test_deposit_withdraw_reward_and_rebates_are_balanced_pusd_facts(self) -> None:
        cases = {
            "DEPOSIT": ("DEPOSIT", 5_000_000, -5_000_000),
            "WITHDRAWAL": ("WITHDRAW", -5_000_000, 5_000_000),
            "REWARD": ("LIQUIDITY_REWARD", 5_000_000, -5_000_000),
            "MAKER_REBATE": ("MAKER_REBATE", 5_000_000, -5_000_000),
            "TAKER_REBATE": ("TAKER_REBATE", 5_000_000, -5_000_000),
        }
        for source_type, (entry_type, cash, counterpart) in cases.items():
            entry = activity.activity_journal(sealed_activity({
                "type": source_type, "amount": "5", "timestamp": 123,
                "transactionHash": "0xabc",
            }), 0)
            entry.validate(sealed=False)
            self.assertEqual(entry.entry_type, entry_type)
            self.assertEqual(entry.postings[0].units, cash)
            self.assertEqual(entry.postings[1].units, counterpart)

    def test_unknown_or_inexact_activity_is_evidence_only(self) -> None:
        unknown = sealed_activity({"type": "SPLIT", "amount": "1", "timestamp": 1, "transactionHash": "0xabc"})
        with self.assertRaisesRegex(activity.ActivityJournalError, "not_accountable"):
            activity.activity_journal(unknown, 0)
        inexact = sealed_activity({"type": "REWARD", "amount": "0.0000001", "timestamp": 1, "transactionHash": "0xabc"})
        with self.assertRaisesRegex(activity.ActivityJournalError, "exact_pusd"):
            activity.activity_journal(inexact, 0)


if __name__ == "__main__":
    unittest.main()
