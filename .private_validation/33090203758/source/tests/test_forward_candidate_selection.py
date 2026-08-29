import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "select_forward_candidates.py"
spec = importlib.util.spec_from_file_location("select_forward_candidates", SCRIPT)
selector = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = selector
spec.loader.exec_module(selector)


class ForwardCandidateSelectionTest(unittest.TestCase):
    def test_round_robin_keeps_activity_edge_and_balanced_strata(self):
        rows = [
            {
                "market_id": "active",
                "condition_id": "ca",
                "event_id": "ea",
                "quote_shares": "20",
                "locked_complete_set_edge": ".01",
                "volume24h": "100000",
                "estimated_native_daily_value": "0",
                "market_competitiveness": "10",
            },
            {
                "market_id": "edge",
                "condition_id": "ce",
                "event_id": "ee",
                "quote_shares": "50",
                "locked_complete_set_edge": ".08",
                "volume24h": "1",
                "estimated_native_daily_value": "0",
                "market_competitiveness": "10",
            },
            {
                "market_id": "reward",
                "condition_id": "cr",
                "event_id": "er",
                "quote_shares": "50",
                "locked_complete_set_edge": ".02",
                "volume24h": "5000",
                "estimated_native_daily_value": "2",
                "market_competitiveness": "1",
            },
            {
                "market_id": "duplicate-event",
                "condition_id": "cd",
                "event_id": "ea",
                "quote_shares": "50",
                "locked_complete_set_edge": ".07",
                "volume24h": "500",
                "estimated_native_daily_value": "0",
                "market_competitiveness": "10",
            },
        ]
        selected = selector.select(rows, 3, 1)
        self.assertEqual({row["market_id"] for row in selected}, {"active", "edge", "reward"})
        self.assertEqual(len({row["event_id"] for row in selected}), 3)

    def test_cli_preserves_input_schema(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / "source.csv"
            output = td / "selected.csv"
            source.write_text(
                "market_id,condition_id,event_id,quote_shares,locked_complete_set_edge,volume24h,estimated_native_daily_value,market_competitiveness\n"
                "m1,c1,e1,20,.01,1000,0,1\n",
                encoding="utf-8",
            )
            old = sys.argv
            try:
                sys.argv = [
                    str(SCRIPT),
                    "--input",
                    str(source),
                    "--output",
                    str(output),
                    "--markets",
                    "1",
                ]
                self.assertEqual(selector.main(), 0)
            finally:
                sys.argv = old
            self.assertEqual(
                output.read_text(encoding="utf-8").splitlines()[0],
                source.read_text(encoding="utf-8").splitlines()[0],
            )


if __name__ == "__main__":
    unittest.main()
