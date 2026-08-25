#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "materialize_external_paper_signals", ROOT / "scripts" / "materialize_external_paper_signals.py"
)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


class ExternalPaperSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1_800_000_000

    def row(self, **overrides):
        value = {
            "market_id": "123",
            "source": "kalshi",
            "observed_ts": self.now - 60,
            "q_external": 0.61,
            "confidence": 0.72,
        }
        value.update(overrides)
        return value

    def test_direct_probability_materializes_in_engine_schema(self) -> None:
        rows = bridge.materialize([self.row()], now=self.now, max_age_seconds=3600)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "123")
        self.assertEqual(rows[0]["source"], "kalshi")
        self.assertAlmostEqual(float(rows[0]["q_yes"]), 0.61)
        self.assertAlmostEqual(float(rows[0]["confidence"]), 0.72)
        self.assertEqual(int(rows[0]["timestamp"]), self.now - 60)

    def test_raw_features_are_not_fabricated_into_probabilities(self) -> None:
        raw = self.row(source="binance", q_external=None, feature_name="return_1h", feature_value=0.02)
        self.assertEqual(bridge.materialize([raw], now=self.now, max_age_seconds=3600), [])

    def test_stale_future_and_invalid_probability_rows_fail_closed(self) -> None:
        rows = [
            self.row(market_id="stale", observed_ts=self.now - 3601),
            self.row(market_id="future", observed_ts=self.now + 301),
            self.row(market_id="badq", q_external=1.2),
            self.row(market_id="badconf", confidence=0.0),
        ]
        self.assertEqual(bridge.materialize(rows, now=self.now, max_age_seconds=3600), [])

    def test_latest_row_wins_per_market(self) -> None:
        rows = [
            self.row(observed_ts=self.now - 120, q_external=0.55, confidence=0.9),
            self.row(observed_ts=self.now - 30, q_external=0.63, confidence=0.4),
        ]
        output = bridge.materialize(rows, now=self.now, max_age_seconds=3600)
        self.assertEqual(len(output), 1)
        self.assertAlmostEqual(float(output[0]["q_yes"]), 0.63)
        self.assertEqual(int(output[0]["timestamp"]), self.now - 30)

    def test_cli_writes_header_even_when_no_direct_probability_is_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "signals.jsonl"
            source.write_text(json.dumps(self.row(q_external=None)) + "\n", encoding="utf-8")
            output = root / "external.csv"
            completed = subprocess.run(
                [
                    "python3", str(ROOT / "scripts" / "materialize_external_paper_signals.py"),
                    "--input", str(source), "--output", str(output),
                    "--now", str(self.now), "--max-age-seconds", "3600",
                ],
                cwd=ROOT, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            with output.open(encoding="utf-8", newline="") as handle:
                reader = csv.reader(handle)
                self.assertEqual(next(reader), list(bridge.FIELDS))
                self.assertEqual(list(reader), [])


if __name__ == "__main__":
    unittest.main()
