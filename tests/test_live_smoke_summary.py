import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_live_smoke.py"


class LiveSmokeSummaryTest(unittest.TestCase):
    def test_snapshot_contains_metrics_candidates_intents_and_log_tails(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            run = td / "paper_v4_live"
            run.mkdir()
            (run / "metrics.prom").write_text(
                'polymarket_runtime_info{adapter="v4",run_root="paper_v4_live",version="v4"} 1\n'
                'polymarket_runtime_equity_usd 10001\n'
                'polymarket_runtime_pnl_usd 1\n'
                'unrelated_metric 99\n',
                encoding="utf-8",
            )
            (run / "stat_arb_pairs_latest.log").write_text("a\nb\nc\n", encoding="utf-8")
            (run / "stat_arb_pairs.csv").write_text(
                "y_market,x_market,maker_entry_net_edge\nlow,x,0.001\nhigh,y,0.009\n",
                encoding="utf-8",
            )
            (run / "stat_arb_pca.csv").write_text(
                "market,maker_entry_net_edge\np1,-0.002\np2,0.004\n",
                encoding="utf-8",
            )
            (run / "intents.csv").write_text(
                "bundle_id,strategy,event_id,created_ts,mode,expected_edge,max_notional,market_id,side,weight,limit_price,execution_deadline_ts,hold_deadline_ts\n"
                "b1,B1,e,1,MAKER,0.006,20,m1,YES,1,0.4,2,3\n"
                "b1,B1,e,1,MAKER,0.006,20,m2,NO,1,0.4,2,3\n",
                encoding="utf-8",
            )
            (run / "walk_forward.json").write_text(json.dumps({"eligible_for_tiny_pilot": False}), encoding="utf-8")
            out = td / "snapshot.json"
            subprocess.run(
                [sys.executable, str(SCRIPT), "--run-root", str(run), "--output", str(out), "--git-sha", "abc", "--run-id", "42", "--tail-lines", "2"],
                check=True,
            )
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["git_sha"], "abc")
            self.assertEqual(data["github_run_id"], "42")
            self.assertEqual(data["metrics"]["polymarket_runtime_equity_usd"], 10001.0)
            self.assertNotIn("unrelated_metric", data["metrics"])
            self.assertEqual(data["logs"]["b1"], ["b", "c"])
            self.assertFalse(data["walk_forward"]["eligible_for_tiny_pilot"])
            self.assertEqual(data["candidates"]["b1"][0]["y_market"], "high")
            self.assertEqual(data["candidates"]["b2"][0]["market"], "p2")
            self.assertEqual(data["intents"]["rows"], 2)
            self.assertEqual(data["intents"]["bundles"], 1)
            self.assertEqual(data["intents"]["strategies"], {"B1": 2})
            self.assertEqual(data["intents"]["max_expected_edge"], 0.006)


if __name__ == "__main__":
    unittest.main()
