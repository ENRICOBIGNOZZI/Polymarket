from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.materialize_external_signals import materialize


class MaterializeExternalSignalsTest(unittest.TestCase):
    def test_keeps_latest_recent_direct_probability_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "signals.jsonl"
            rows = [
                {
                    "market_id": "1",
                    "q_external": 0.61,
                    "confidence": 0.50,
                    "observed_ts": 900,
                    "source": "kalshi",
                    "source_id": "A",
                },
                {
                    "market_id": "1",
                    "q_external": 0.63,
                    "confidence": 0.45,
                    "observed_ts": 950,
                    "source": "kalshi",
                    "source_id": "B",
                },
                {
                    "market_id": "2",
                    "q_external": None,
                    "confidence": 0.90,
                    "observed_ts": 990,
                    "source": "binance",
                    "source_id": "BTC",
                },
                {
                    "market_id": "3",
                    "q_external": 0.70,
                    "confidence": 0.20,
                    "observed_ts": 990,
                    "source": "kalshi",
                    "source_id": "C",
                },
            ]
            source.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            output = root / "signals.csv"
            self.assertEqual(materialize(source, output, 1000, 0.35, 200), 1)
            with output.open(newline="", encoding="utf-8") as handle:
                data = list(csv.DictReader(handle))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["market_key"], "1")
            self.assertAlmostEqual(float(data[0]["q_yes"]), 0.63)
            self.assertIn("kalshi:B", data[0]["source"])

    def test_empty_or_stale_input_still_writes_valid_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "signals.jsonl"
            source.write_text(
                json.dumps(
                    {
                        "market_id": "1",
                        "q_external": 0.60,
                        "confidence": 0.90,
                        "observed_ts": 1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            output = root / "signals.csv"
            self.assertEqual(materialize(source, output, 1000, 0.30, 100), 0)
            self.assertEqual(
                output.read_text(encoding="utf-8").strip(),
                "market_key,q_yes,confidence,source,timestamp",
            )


if __name__ == "__main__":
    unittest.main()
