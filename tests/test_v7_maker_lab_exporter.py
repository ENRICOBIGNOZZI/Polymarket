from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))

from exporter_v7 import render_prometheus
from v7_ledger_metrics import summarize_ledger


class MakerLabExporterTests(unittest.TestCase):
    def test_markout_is_valid_ledger_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "execution.jsonl"
            ledger.write_text(json.dumps({
                "event_type": "MARKOUT",
                "strategy": "MICRO_MAKER_PRO",
                "model_sha": "b" * 40,
                "paper_only": True,
                "authenticated_execution": False,
                "markouts": {"10s": 0.01},
            }) + "\n")
            out = summarize_ledger(ledger)
            self.assertTrue(out["valid"])
            self.assertEqual(out["invalid_rows"], 0)
            self.assertEqual(out["total"]["markout_count"]["10s"], 1)

    def test_prometheus_has_segment_and_conditional_metrics(self) -> None:
        snapshot = {
            "runtime": {"version": 7, "paper_only": True, "authenticated_execution": False, "real_order_submission": False},
            "ledger": {"total": {}, "present": True, "valid": True, "rows": 0, "invalid_rows": 0, "model_shas": []},
            "economics": {"equity": 1, "pnl": 0, "realized_pnl": 0, "unrealized_executable_pnl": 0, "drawdown": 0, "live_units": 0, "killed": False, "gross_exposure": None, "capital_utilization": None},
            "authority": {"valid": True, "max_drawdown": .15},
            "canonical_economics": {},
            "run_root": "paper_v7_live",
            "sha": "c" * 40,
            "runtime_alive": True,
            "portfolio": {"paper_only": True, "authenticated_execution": False},
            "trade_tape": {"rows": 1, "assets": 1},
            "ages": {},
            "strategies": {},
            "maker_lab": {
                "present": True,
                "orders": 10,
                "filled_orders": 2,
                "fills": 2,
                "realized_pnl": .3,
                "attributed_realized_pnl": .3,
                "markouts": {"10s": 2},
                "quality": {"linked_fills": 2, "unlinked_fills": 0, "linked_markouts": 2, "unlinked_markouts": 0, "ofi_exact_orders": 0, "ofi_proxy_orders": 10, "reward_known_orders": 10, "unattributed_sell_fills": 0, "unattributed_merge_pnl": 0, "ofi_source": "proxy", "reward_source": "selection", "merge_pnl_attribution": "pro_rata"},
                "segments": [{"action": "JOIN", "variant": "JOIN", "dimension": "toxicity", "bucket": "LOW", "orders": 5, "filled_orders": 2, "fills": 2, "filled_shares": 4, "realized_pnl": .2, "markout_pnl": {"10s": .04}, "markout_shares": {"10s": 4}, "markout_count": {"10s": 2}}],
                "conditionals": [{"action": "JOIN", "toxicity": "LOW", "queue": "<=1x", "orders": 5, "filled_orders": 2, "realized_pnl": .2, "markout_pnl": {"10s": .04}, "markout_shares": {"10s": 4}, "markout_count": {"10s": 2}}],
                "markets": [{"market": "M1", "action": "JOIN", "orders": 5, "filled_orders": 2, "realized_pnl": .2, "markout_pnl": {"10s": .04}, "markout_shares": {"10s": 4}}],
            },
        }
        text = render_prometheus(snapshot)
        self.assertIn('polymarket_maker_lab_segment_orders{action="JOIN",variant="JOIN",dimension="toxicity",bucket="LOW"} 5', text)
        self.assertIn('polymarket_maker_lab_conditional_orders{action="JOIN",toxicity="LOW",queue="<=1x"} 5', text)
        self.assertIn('polymarket_maker_lab_market_realized_pnl_usd{market="M1",action="JOIN"} 0.2', text)


if __name__ == "__main__":
    unittest.main()
