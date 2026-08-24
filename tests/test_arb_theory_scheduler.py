#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "arb_theory_scheduler.py"


class ArbTheorySchedulerTest(unittest.TestCase):
    def test_generates_fail_closed_candidate_and_compilable_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            policy.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "shadow",
                        "real_order_submission": False,
                        "min_net_edge": 0.0007,
                        "slippage_bps": 2.0,
                        "latency_penalty_bps": 1.0,
                        "max_notional_usd": 50.0,
                    }
                ),
                encoding="utf-8",
            )
            run = root / "run-1"
            run.mkdir()
            with (run / "fast_arb_opportunities.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "observed_ts_ms",
                        "kind",
                        "id",
                        "hard_arbitrage",
                        "executable",
                        "net_edge_per_share",
                        "expected_profit",
                        "executable_shares",
                        "capital_required",
                    ],
                )
                writer.writeheader()
                for index in range(12):
                    writer.writerow(
                        {
                            "observed_ts_ms": 1_000 + index * 200,
                            "kind": "BINARY_COMPLETE_SET",
                            "id": f"binary:{index}",
                            "hard_arbitrage": 1,
                            "executable": 1,
                            "net_edge_per_share": 0.003,
                            "expected_profit": 0.1,
                            "executable_shares": 10,
                            "capital_required": 9.7,
                        }
                    )
            with (run / "fast_arb_latency.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["feed_latency_ms", "decision_latency_us"],
                )
                writer.writeheader()
                for _ in range(100):
                    writer.writerow({"feed_latency_ms": 20, "decision_latency_us": 100})
            (run / "fast_arb_status.json").write_text(
                json.dumps({"ws_messages": 2_000, "book_updates": 2_000}),
                encoding="utf-8",
            )

            output_json = root / "candidate.json"
            output_markdown = root / "report.md"
            output_header = root / "candidate.hpp"
            subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--evidence-root",
                    str(root),
                    "--base-policy",
                    str(policy),
                    "--output-json",
                    str(output_json),
                    "--output-markdown",
                    str(output_markdown),
                    "--output-header",
                    str(output_header),
                ],
                check=True,
            )
            report = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertFalse(report["promotion_ready"])
            self.assertFalse(report["candidate_policy"]["real_order_submission"])
            self.assertGreaterEqual(report["candidate_policy"]["min_net_edge"], 0.0007)
            header = output_header.read_text(encoding="utf-8")
            self.assertIn("kRealOrderSubmission = false", header)
            self.assertIn("kEvidenceSha256", header)

    def test_rejects_policy_that_enables_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "policy.json"
            policy.write_text('{"real_order_submission": true}', encoding="utf-8")
            result = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--base-policy",
                    str(policy),
                    "--output-json",
                    str(root / "x.json"),
                    "--output-markdown",
                    str(root / "x.md"),
                    "--output-header",
                    str(root / "x.hpp"),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
