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
            "feature_name": "external_probability",
            "observed_ts": self.now - 60,
            "q_external": 0.61,
            "confidence": 0.72,
            "mapping_score": 0.90,
        }
        value.update(overrides)
        return value

    def report(self, **overrides):
        evidence = {
            "candidate_id": "external:kalshi:external_probability:3600s",
            "evidence_type": "purged_chronological_external_information_backtest",
            "evidence_state": "APPROVED_FOR_INTEGRATION",
            "gate_pass_before_fdr": True,
            "integration_evidence_pass": True,
            "terminal_calibration_pass": True,
            "critical_failures": [],
            "metrics": {
                "oos_predictions": 100,
                "trades": 40,
                "net_pnl_per_share": 0.12,
                "two_x_cost_stressed_pnl_per_share": 0.03,
            },
        }
        evidence.update(overrides)
        return {"alpha_factory_evidence": evidence}

    def test_approved_direct_probability_materializes(self) -> None:
        rows = bridge.materialize(
            [self.row()], report=self.report(), now=self.now, max_age_seconds=3600
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_key"], "123")
        self.assertAlmostEqual(float(rows[0]["q_yes"]), 0.61)
        self.assertAlmostEqual(float(rows[0]["confidence"]), 0.72 * 0.90)
        self.assertIn("external:kalshi:external_probability:3600s@", rows[0]["source"])

    def test_upstream_evidence_must_be_explicitly_approved(self) -> None:
        rejected = self.report(integration_evidence_pass=False)
        missing_state = self.report(evidence_state="")
        no_calibration = self.report(terminal_calibration_pass=False)
        for report in (rejected, missing_state, no_calibration, {}):
            with self.subTest(report=report):
                self.assertEqual(
                    bridge.materialize(
                        [self.row()], report=report, now=self.now, max_age_seconds=3600
                    ),
                    [],
                )

    def test_source_and_feature_must_match_approved_provenance(self) -> None:
        rows = [
            self.row(source="other"),
            self.row(feature_name="return_1h"),
            self.row(source="binance", feature_name="return_1h", q_external=None),
        ]
        self.assertEqual(
            bridge.materialize(rows, report=self.report(), now=self.now, max_age_seconds=3600),
            [],
        )

    def test_stale_future_invalid_and_weak_mapping_rows_fail_closed(self) -> None:
        rows = [
            self.row(market_id="stale", observed_ts=self.now - 3601),
            self.row(market_id="future", observed_ts=self.now + 301),
            self.row(market_id="weak", mapping_score=0.69),
            self.row(market_id="invalid", q_external=1.0),
        ]
        self.assertEqual(
            bridge.materialize(rows, report=self.report(), now=self.now, max_age_seconds=3600),
            [],
        )

    def test_cli_writes_header_only_when_evidence_is_not_approved(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            observations = root / "observations.jsonl"
            report = root / "report.json"
            output = root / "external_signals.csv"
            observations.write_text(json.dumps(self.row()) + "\n", encoding="utf-8")
            report.write_text(json.dumps(self.report(integration_evidence_pass=False)), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "materialize_external_paper_signals.py"),
                    "--input",
                    str(observations),
                    "--report",
                    str(report),
                    "--output",
                    str(output),
                    "--max-age-seconds",
                    "3600",
                    "--now",
                    str(self.now),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("external_paper_feed_state=abstain_unapproved", completed.stdout)
            with output.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows, [["market_key", "q_yes", "confidence", "source", "timestamp"]])


if __name__ == "__main__":
    unittest.main()
