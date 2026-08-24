from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "monitoring"))
from exporter_v4 import V4Collector


class V4ExporterTest(unittest.TestCase):
    def test_multileg_terminal_and_oos_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "paper_v4_live"
            root.mkdir(parents=True)
            cfg = Path(td) / "paper_v4.json"
            cfg.write_text(json.dumps({"starting_capital": 10000.0, "max_drawdown": 0.15}))
            (root / "trade_tape.csv").write_text("timestamp,asset_id,side,price,size\n1,tok,BUY,0.5,2\n", encoding="utf-8")
            (root / "multileg_equity.csv").write_text(
                "timestamp,cash,equity,reserved_cash,gross_entry_cash,peak_equity,drawdown,killed,live_bundles\n"
                "1,9990,10005,5,10,10010,0.0004995,0,1\n", encoding="utf-8")
            (root / "multileg_bundles.csv").write_text(
                "bundle_id,strategy,event_id,status,created_ts,expected_edge,max_notional,execution_deadline_ts,hold_deadline_ts,ledger_written,abort_reason\n"
                "b1,B1,e1,RESTING,1,0.01,10,9999999999,9999999999,0,\n", encoding="utf-8")
            (root / "multileg_legs.csv").write_text(
                "bundle_id,market_id,event_id,side,token_id,weight,target_shares,filled_shares,limit_price,queue_ahead,arrival_ms,cancel_effective_ms,replace_count,entry_cash,entry_fee,exit_cash,exit_fee,slippage_cost,first_fill_ts,last_fill_ts,adverse_mark_pnl,adverse_recorded,order_state\n"
                "b1,m1,e1,YES,t1,1,10,8,0.5,3,0,0,0,4,0,0,0,0,1,1,0,0,RESTING\n"
                "b1,m2,e1,NO,t2,1,10,2,0.5,7,0,0,0,1,0,0,0,0,1,1,0,0,RESTING\n", encoding="utf-8")
            (root / "bundle_ledger.csv").write_text(
                "bundle_id,strategy,event_id,created_ts,closed_ts,status,expected_edge,max_notional,entry_cash,gross_pnl,fees,slippage,net_pnl,return_on_capital,fill_fraction,adverse_mark_pnl,abort_reason\n"
                "z,B1,e0,1,2,CLOSED,0.01,10,10,1,0.1,0.1,0.8,0.08,1,0,\n", encoding="utf-8")

            terminal = root / "terminal"
            terminal.mkdir()
            (terminal / "status.json").write_text(json.dumps({
                "timestamp": 2,
                "cash": 9950,
                "equity": 10012,
                "peak_equity": 10015,
                "drawdown": 0.00029955,
                "gross_exposure": 62,
                "open_positions": 2,
                "killed": False,
            }), encoding="utf-8")
            (terminal / "broker_state.csv").write_text(
                "market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid\n"
                "m1,e1,s1,YES,t1,60,0.5,30,0.1\n"
                "m3,e3,s3,NO,t3,50,0.6,30,0.1\n",
                encoding="utf-8",
            )
            (terminal / "fills.csv").write_text(
                "timestamp,market_id,slug,action,side,shares,price,notional,fee\n"
                "1,m1,s1,BUY,YES,10,0.5,5,0.01\n"
                "2,m2,s2,SELL,NO,8,0.6,4.8,0.01\n",
                encoding="utf-8",
            )

            (root / "walk_forward.json").write_text(json.dumps({
                "input_trades": 40,
                "eligible_for_tiny_pilot": True,
                "production_threshold": 0.003,
                "bootstrap_one_sided_pvalue": 0.04,
                "oos": {"trades": 30, "net_pnl": 12.0, "max_drawdown": 0.03, "profit_factor": 1.4},
                "oos_cost_stress": {"net_pnl": 5.0}
            }), encoding="utf-8")
            text = V4Collector(root, cfg, 10).collect()
            self.assertIn("polymarket_multileg_equity_usd 10005", text)
            self.assertIn("polymarket_multileg_max_fill_imbalance_ratio 0.6", text)
            self.assertIn("polymarket_multileg_realized_net_pnl_usd_total 0.8", text)
            self.assertIn("polymarket_terminal_state_present 1", text)
            self.assertIn("polymarket_terminal_equity_usd 10012", text)
            self.assertIn("polymarket_terminal_pnl_usd 12", text)
            self.assertIn("polymarket_terminal_realized_pnl_usd 10", text)
            self.assertIn("polymarket_terminal_unrealized_pnl_usd 2", text)
            self.assertIn("polymarket_terminal_open_cost_basis_usd 60", text)
            self.assertIn("polymarket_terminal_open_positions 2", text)
            self.assertIn("polymarket_terminal_fills_total 2", text)
            self.assertIn("polymarket_terminal_buy_fills_total 1", text)
            self.assertIn("polymarket_terminal_sell_fills_total 1", text)
            self.assertIn("polymarket_terminal_closed_positions_total 1", text)
            self.assertIn("polymarket_terminal_fees_usd_total 0.02", text)
            self.assertIn("polymarket_terminal_turnover_usd_total 9.8", text)
            self.assertIn("polymarket_oos_eligible_for_tiny_pilot 1", text)
            self.assertIn("polymarket_oos_stressed_net_pnl_usd 5", text)
            self.assertIn("polymarket_trade_recorder_rows 1", text)


if __name__ == "__main__":
    unittest.main()
