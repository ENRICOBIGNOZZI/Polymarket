import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "summarize_live_smoke.py"


class LiveSmokeSummaryTest(unittest.TestCase):
    def test_snapshot_contains_only_selected_runtime_metrics_and_log_tails(self):
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


if __name__ == "__main__":
    unittest.main()
