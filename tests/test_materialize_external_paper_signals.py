import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.materialize_external_paper_signals import materialize


class MaterializeExternalPaperSignalsTest(unittest.TestCase):
    def test_only_direct_fresh_probabilities_are_materialized(self):
        now = 1_800_000_000
        rows = [
            {
                "market_id": "m1",
                "q_external": None,
                "confidence": 0.9,
                "observed_ts": now,
                "source": "binance",
            },
            {
                "market_id": "m1",
                "q_external": 0.61,
                "confidence": 0.20,
                "mapping_score": 0.5,
                "observed_ts": now - 10,
                "source": "kalshi",
            },
            {
                "market_id": "m1",
                "q_external": 0.63,
                "confidence": 0.30,
                "mapping_score": 1.0,
                "observed_ts": now - 20,
                "source": "kalshi",
            },
            {
                "market_id": "stale",
                "q_external": 0.75,
                "confidence": 0.9,
                "observed_ts": now - 100_000,
                "source": "kalshi",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "signals.jsonl"
            output = root / "signals.csv"
            source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            count = materialize(
                source,
                output,
                min_confidence=0.05,
                max_age_seconds=86_400,
                now=now,
            )
            self.assertEqual(count, 1)
            materialized = list(csv.DictReader(output.open(newline="", encoding="utf-8")))
            self.assertEqual(len(materialized), 1)
            self.assertEqual(materialized[0]["market_key"], "m1")
            self.assertEqual(materialized[0]["source"], "kalshi")
            self.assertAlmostEqual(float(materialized[0]["q_yes"]), 0.63)
            self.assertAlmostEqual(float(materialized[0]["confidence"]), 0.30)


if __name__ == "__main__":
    unittest.main()
