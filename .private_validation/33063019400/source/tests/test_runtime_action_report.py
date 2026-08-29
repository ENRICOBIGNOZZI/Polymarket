from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "runtime_action_report.py"


class RuntimeActionReportTest(unittest.TestCase):
    def test_report_explains_cost_block_and_external_gap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "run"
            run.mkdir()
            (run / "stat_arb_pairs.csv").write_text(
                "y_slug,x_slug,raw_expected_edge,maker_entry_net_edge,taker_net_edge,z\n"
                "A,B,0.01,-0.002,-0.01,2\n",
                encoding="utf-8",
            )
            (run / "stat_arb_pca.csv").write_text(
                "slug,raw_expected_edge,maker_entry_net_edge,taker_net_edge,residual_z,hedge_error,legs\n"
                "Election,0.003,-0.0018,-0.005,2.6,0.001,m1:NO:1|m2:YES:0.5\n",
                encoding="utf-8",
            )
            (run / "structural_latest.csv").write_text(
                "discovered=10 opportunities=1\n"
                "type,event_id,anchor,legs,raw_edge,net_edge_pre_gas,executable_shares,estimated_profit_pre_gas\n"
                "BUY_ALL_YES,e1,a,3,-0.01,-0.02,100,-2\n",
                encoding="utf-8",
            )
            (run / "reward_opportunities.csv").write_text(
                "question,conditional_conservative_daily_score,conservative_daily_score,payout_shortfall_usd\n"
                "Q,0.03,-0.14,0.83\n",
                encoding="utf-8",
            )
            (run / "intents.csv").write_text(
                "bundle_id,strategy,event_id\n", encoding="utf-8"
            )
            (run / "multileg_bundles.csv").write_text(
                "bundle_id,status\n", encoding="utf-8"
            )
            (run / "multileg_legs.csv").write_text(
                "bundle_id,exited\n", encoding="utf-8"
            )
            (run / "multileg_events.csv").write_text(
                "timestamp,event,bundle_id\n", encoding="utf-8"
            )
            (run / "bundle_ledger.csv").write_text(
                "bundle_id,closed_ts,status,net_pnl,abort_reason\n", encoding="utf-8"
            )
            (run / "runtime_supervisor.csv").write_text(
                "timestamp,recorder_alive,broker_alive,recorder_restarts,broker_restarts,recorder_pid,broker_pid\n"
                "999,1,1,0,0,1,2\n",
                encoding="utf-8",
            )
            external = root / "external.csv"
            external.write_text(
                "market_key,q_yes,confidence,source,timestamp\n", encoding="utf-8"
            )
            output_json = root / "report.json"
            output_markdown = root / "report.md"

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--run-root",
                    str(run),
                    "--external-signals",
                    str(external),
                    "--now",
                    "1000",
                    "--output-json",
                    str(output_json),
                    "--output-markdown",
                    str(output_markdown),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            report = json.loads(output_json.read_text(encoding="utf-8"))
            codes = {item["code"] for item in report["reasons"]}
            self.assertEqual(report["schema"], "polymarket_runtime_action_report_v1")
            self.assertEqual(report["candidate_funnel"]["B2_pca_hedges"]["raw_positive"], 1)
            self.assertEqual(report["candidate_funnel"]["B2_pca_hedges"]["maker_admissible"], 0)
            self.assertIn("COST_BLOCKED", codes)
            self.assertIn("NO_FRESH_EXTERNAL_SIGNAL", codes)
            markdown = output_markdown.read_text(encoding="utf-8")
            self.assertIn("## What the system did", markdown)
            self.assertIn("## How it acted", markdown)
            self.assertIn("ABSTAIN", markdown)
            self.assertIn("m1:NO:1|m2:YES:0.5", markdown)

    def test_report_tracks_broker_actions_and_unobserved_intent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run = root / "run"
            run.mkdir()
            for name, header in {
                "stat_arb_pairs.csv": "raw_expected_edge,maker_entry_net_edge,taker_net_edge\n",
                "stat_arb_pca.csv": "raw_expected_edge,maker_entry_net_edge,taker_net_edge\n",
                "reward_opportunities.csv": "conservative_daily_score\n",
            }.items():
                (run / name).write_text(header, encoding="utf-8")
            (run / "structural_latest.csv").write_text(
                "type,event_id,anchor,legs,raw_edge,net_edge_pre_gas,executable_shares,estimated_profit_pre_gas\n",
                encoding="utf-8",
            )
            (run / "intents.csv").write_text(
                "bundle_id,strategy,event_id\nmissing,B2,e\n",
                encoding="utf-8",
            )
            (run / "multileg_bundles.csv").write_text(
                "bundle_id,status\nlive,RESTING\n", encoding="utf-8"
            )
            (run / "multileg_legs.csv").write_text(
                "bundle_id,exited\nlive,0\n", encoding="utf-8"
            )
            (run / "multileg_events.csv").write_text(
                "timestamp,event,bundle_id\n995,POST,live\n996,PARTIAL_FILL,live\n",
                encoding="utf-8",
            )
            (run / "bundle_ledger.csv").write_text(
                "bundle_id,closed_ts,status,net_pnl,abort_reason\n",
                encoding="utf-8",
            )
            (run / "runtime_supervisor.csv").write_text(
                "timestamp,recorder_alive,broker_alive\n999,1,1\n",
                encoding="utf-8",
            )
            external = root / "external.csv"
            external.write_text(
                "market_key,q_yes,confidence,source,timestamp\n"
                "m,0.6,0.8,test,990\n",
                encoding="utf-8",
            )
            output_json = root / "report.json"
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--run-root",
                    str(run),
                    "--external-signals",
                    str(external),
                    "--now",
                    "1000",
                    "--output-json",
                    str(output_json),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads(output_json.read_text(encoding="utf-8"))
            self.assertEqual(report["broker"]["recent_event_counts"]["PARTIAL_FILL"], 1)
            self.assertEqual(report["broker"]["intent_bundles_not_in_state"], ["missing"])
            self.assertTrue(any(action.startswith("ACT:") for action in report["actions"]))
            self.assertIn(
                "BROKER_ADMISSION_GAP", {item["code"] for item in report["reasons"]}
            )
            self.assertEqual(report["candidate_funnel"]["external"]["fresh_rows"], 1)


if __name__ == "__main__":
    unittest.main()
