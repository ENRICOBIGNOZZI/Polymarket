from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_live_smoke.py"


class LiveSmokeSummaryTest(unittest.TestCase):
    def test_snapshot_is_v7_runtime_and_execution_evidence_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "execution"
            run.mkdir(parents=True)
            now = 1_800_000_000
            (run / "runtime_status.json").write_text(
                json.dumps(
                    {
                        "schema": "polymarket_v7_runtime_status_v1",
                        "version": 7,
                        "timestamp": now - 10,
                        "paper_only": True,
                        "authenticated_execution": False,
                        "equity": 10010.0,
                        "pnl": 10.0,
                        "realized_pnl": 4.0,
                        "drawdown": 0.01,
                        "killed": False,
                        "gross_exposure": 100.0,
                        "reserved_cash": 25.0,
                        "live_units": 3,
                        "execution_staleness": 2.0,
                        "strategies": {
                            "micro_maker": {"fills": 5, "pnl": 1.0},
                            "micro_taker": {"fills": 2, "pnl": 0.5},
                            "relative_value": {"fills": 1, "pnl": 2.0},
                            "hard_arb": {"fills": 0, "pnl": 0.0},
                            "external": {"fills": 0, "pnl": 0.0},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run / "strategy_status.csv").write_text(
                "name,fills,pnl\nmicro_maker,5,1\nmicro_taker,2,0.5\nrelative_value,1,2\nhard_arb,0,0\nexternal,0,0\n",
                encoding="utf-8",
            )
            (run / "v7_execution_evidence.json").write_text(
                json.dumps(
                    {
                        "schema": "polymarket_execution_evidence_v1",
                        "generated_ts": now - 8,
                        "evidence_id": "evidence-1",
                        "summary": {
                            "models": 5,
                            "paper_eligible_models": 1,
                            "insufficient_evidence_models": 4,
                        },
                        "models": {
                            "micro_maker": {"paper_eligible": True, "state": "PAPER_ELIGIBLE"},
                            "micro_taker": {"paper_eligible": False, "state": "INSUFFICIENT_EVIDENCE"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run / "action_report.json").write_text(
                json.dumps({"schema": "polymarket_runtime_action_report_v1", "status": "ok"}),
                encoding="utf-8",
            )
            (run / "market_proxy_status.json").write_text(
                json.dumps(
                    {
                        "schema": "polymarket_v7_market_proxy_status_v1",
                        "timestamp": now - 4,
                        "source": "gamma_offset_retried",
                        "markets": 300,
                        "failures": 0,
                    }
                ),
                encoding="utf-8",
            )
            output = root / "snapshot.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--run-root",
                    str(run),
                    "--output",
                    str(output),
                    "--git-sha",
                    "abc",
                    "--run-id",
                    "42",
                    "--now",
                    str(now),
                ],
                check=True,
            )
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], "polymarket_v7_public_live_smoke_v1")
            self.assertEqual(data["git_sha"], "abc")
            self.assertEqual(data["run_id"], "42")
            self.assertTrue(data["paper_only"])
            self.assertFalse(data["authenticated_execution"])
            self.assertEqual(data["runtime"]["version"], 7)
            self.assertEqual(data["runtime"]["total_fills"], 8)
            self.assertEqual(data["runtime"]["strategy_count"], 5)
            self.assertEqual(data["runtime"]["pnl_usd"], 10.0)
            self.assertEqual(data["execution_evidence"]["eligible_models"], ["micro_maker"])
            self.assertEqual(data["execution_evidence"]["insufficient_models"], ["micro_taker"])
            self.assertEqual(data["data_health"]["market_proxy_markets"], 300)
            text = json.dumps(data).lower()
            self.assertNotIn("b1", text)
            self.assertNotIn("b2", text)
            self.assertNotIn("b3", text)
            self.assertNotIn("walk_forward", text)

    def test_missing_runtime_fails_closed_as_missing_evidence_without_inventing_legacy_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "execution"
            run.mkdir(parents=True)
            output = root / "snapshot.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--run-root",
                    str(run),
                    "--output",
                    str(output),
                    "--git-sha",
                    "abc",
                    "--run-id",
                    "1",
                    "--now",
                    "1800000000",
                ],
                check=True,
            )
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(data["runtime"]["present"])
            self.assertFalse(data["execution_evidence"]["present"])
            self.assertEqual(data["runtime"]["total_fills"], 0)
            self.assertEqual(data["execution_evidence"]["eligible_models"], [])


if __name__ == "__main__":
    unittest.main()
