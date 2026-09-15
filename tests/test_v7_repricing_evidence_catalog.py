from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_evidence_catalog import classify


class RepricingEvidenceCatalogTest(unittest.TestCase):
    def test_dedicated_repricing_book_is_causal_source(self) -> None:
        row = classify("research/repricing_book/book_observations/current.jsonl")
        self.assertEqual(row["source_family"], "pm_causal_book")
        self.assertEqual(row["schema"], "polymarket_v7_causal_book_observation_v1")
        self.assertEqual(row["retention"], "PERMANENT_NO_UNIQUE_SOURCE_DELETION")

    def test_dedicated_repricing_trade_stream_is_public_trade_source(self) -> None:
        row = classify("research/repricing_book/fillability_ws.jsonl")
        self.assertEqual(row["source_family"], "pm_public_trades")
        self.assertEqual(row["source_kind"], "CAUSAL_SOURCE")
        self.assertEqual(row["retention"], "PERMANENT_NO_UNIQUE_SOURCE_DELETION")

    def test_retention_policy_never_rotates_active_repricing_files(self) -> None:
        policy = json.loads((ROOT / "config/v7_data_retention.json").read_text())
        active = policy["active_files"]
        protected = set(active["never_copytruncate"])
        self.assertIn("research/repricing_book/book_observations/current.jsonl", protected)
        self.assertIn("research/repricing_book/fillability_ws.jsonl", protected)
        self.assertNotIn(
            "research/repricing_book/book_observations/current.jsonl",
            set(active["append_reopen_streams"]),
        )


if __name__ == "__main__":
    unittest.main()
