import importlib.util
import json
import tempfile
import unittest
from unittest import mock
import sys
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "monitoring" / "exporter.py"
spec = importlib.util.spec_from_file_location("polymarket_exporter", MODULE_PATH)
exporter = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = exporter
spec.loader.exec_module(exporter)


class ExporterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "maker").mkdir()
        (self.root / "terminal").mkdir()
        self.config = self.root / "paper_v3.json"
        self.config.write_text(json.dumps({"starting_capital": 10000, "max_drawdown": 0.15}))

        (self.root / "maker" / "maker_equity.csv").write_text(
            "timestamp,cash,equity,reserved_cash,resting_orders,positions,peak_equity,drawdown,killed\n"
            "1000,9500,10100,200,2,1,10200,0.00980392,0\n"
        )
        (self.root / "maker" / "maker_positions.csv").write_text(
            "market_id,event_id,slug,side,token_id,shares,entry_price,entry_ts\n"
            "m1,e1,will-x-happen,YES,t1,100,0.4,900\n"
        )
        (self.root / "maker" / "maker_orders.csv").write_text(
            "market_id,event_id,slug,side,token_id,limit_price,shares,queue_ahead,created_last_trade,created_ts\n"
            "m2,e2,will-y-happen,NO,t2,0.3,50,10,0.31,950\n"
        )
        (self.root / "maker" / "maker_fills.csv").write_text(
            "timestamp,market_id,slug,action,side,shares,price,fee,reason\n"
            "900,m1,will-x-happen,BUY_MAKER,YES,100,0.4,0,strict_trade_through\n"
            "990,m1,will-x-happen,SELL_TAKER,YES,100,0.42,0.2,max_hold\n"
        )
        (self.root / "maker" / "maker_order_log.csv").write_text(
            "timestamp,action,market_id,slug,side,token_id,limit_price,shares,queue_ahead,signal_edge,confidence\n"
            "880,POST,m1,will-x-happen,YES,t1,0.4,100,20,0.01,0.8\n"
            "900,FILL,m1,will-x-happen,YES,t1,0.4,100,20,0,0\n"
        )

        (self.root / "terminal" / "status.json").write_text(
            json.dumps(
                {
                    "timestamp": 995,
                    "cash": 9800,
                    "equity": 10050,
                    "peak_equity": 10100,
                    "drawdown": 0.0049505,
                    "gross_exposure": 250,
                    "open_positions": 1,
                    "killed": False,
                }
            )
        )
        (self.root / "terminal" / "broker_state.csv").write_text(
            "market_id,event_id,slug,side,token_id,shares,avg_price,cost_basis,fees_paid\n"
            "tm1,te1,terminal-market,YES,tt1,20,0.5,10,0.1\n"
        )
        (self.root / "terminal" / "signals.csv").write_text(
            "timestamp,market_id,slug,side,mid,exec_price,fair_side,fair_yes,uncertainty,fee_per_share,slippage_per_share,gross_edge,cost_adjusted_edge,net_edge,score,desired_notional,experts\n"
            "995,tm1,terminal-market,YES,0.5,0.51,0.55,0.55,0.02,0,0,0.04,0.04,0.03,1.5,100,external:0.55:0.8\n"
        )
        (self.root / "terminal" / "fills.csv").write_text(
            "timestamp,market_id,slug,action,side,shares,price,notional,fee\n"
            "994,tm1,terminal-market,BUY,YES,20,0.5,10,0.1\n"
        )

        (self.root / "structural_latest.csv").write_text(
            "discovered=600 negRisk_event_ids=10 scanned_events=8 opportunities=3 raw_positive=2 net_positive_pre_gas=1\n"
            "type,event_id,anchor,legs,raw_edge,net_edge_pre_gas,executable_shares,estimated_profit_pre_gas\n"
            "BUY_ALL_YES,e10,event-a,4,0.02,0.01,10,0.1\n"
            "NO_TO_OTHER_YES,e11,event-b,3,0.01,-0.005,20,-0.1\n"
        )
        (self.root / "stat_arb_pairs.csv").write_text(
            "relation,y_market,y_slug,x_market,x_slug,window,aligned,ret_corr,beta,phi,half_life_h,t_reversion,stability,z,raw_expected_edge,taker_net_edge,maker_entry_net_edge,executable_notional,y_side,x_side,y_limit,x_limit,y_weight,x_weight\n"
            "semantic,y1,y-slug,x1,x-slug,96,90,0.8,1.1,0.9,6,-2.5,0.8,2.0,0.04,0.01,0.02,300,YES,NO,0.4,0.5,1,1\n"
        )
        (self.root / "stat_arb_pca.csv").write_text(
            "market,slug,side,obs,hedges,explained,residual_z,phi,half_life_h,t_reversion,stability,hedge_error,expected_mark_move,raw_expected_edge,taker_net_edge,maker_entry_net_edge,executable_notional,legs\n"
            "p1,pca-slug,YES,100,3,0.7,-2.2,0.85,4,-3,0.9,0.05,0.03,0.03,0.008,0.015,250,legs\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_collects_core_and_strategy_metrics(self):
        collector = exporter.Collector(self.root, self.config, top_opportunities=10)
        with mock.patch.object(exporter.time, "time", return_value=1100):
            text = collector.collect()

        self.assertIn("polymarket_maker_state_present 1", text)
        self.assertIn("polymarket_maker_equity_usd 10100", text)
        self.assertIn("polymarket_maker_pnl_usd 100", text)
        self.assertIn("polymarket_maker_staleness_seconds 100", text)
        self.assertIn('polymarket_maker_position_notional_usd{market_id="m1",side="YES",slug="will-x-happen"} 40', text)
        self.assertIn('polymarket_maker_fills_total{action="BUY_MAKER",side="YES"} 1', text)
        self.assertIn("polymarket_maker_fees_paid_usd_total 0.2", text)
        self.assertIn("polymarket_terminal_equity_usd 10050", text)
        self.assertIn('polymarket_structural_scan_total{field="scanned_events"} 8', text)
        self.assertIn("polymarket_structural_positive_opportunities 1", text)
        self.assertIn('polymarket_stat_arb_max_net_edge_ratio{basis="maker",sleeve="pair"} 0.02', text)
        self.assertIn('polymarket_stat_arb_max_net_edge_ratio{basis="maker",sleeve="pca"} 0.015', text)
        self.assertIn("polymarket_exporter_scrape_errors 0", text)

    def test_incremental_log_aggregation_does_not_double_count(self):
        collector = exporter.Collector(self.root, self.config)
        first = collector.collect()
        second = collector.collect()
        self.assertIn('polymarket_maker_fills_total{action="SELL_TAKER",side="YES"} 1', first)
        self.assertIn('polymarket_maker_fills_total{action="SELL_TAKER",side="YES"} 1', second)

        with (self.root / "maker" / "maker_fills.csv").open("a") as handle:
            handle.write("1090,m2,will-y-happen,BUY_MAKER,NO,10,0.3,0,strict_trade_through\n")
        third = collector.collect()
        self.assertIn('polymarket_maker_fills_total{action="BUY_MAKER",side="NO"} 1', third)
        self.assertIn("polymarket_maker_traded_notional_usd_total 85", third)

    def test_missing_files_are_optional(self):
        empty = self.root / "empty"
        empty.mkdir()
        collector = exporter.Collector(empty, self.config)
        text = collector.collect()
        self.assertIn("polymarket_exporter_info", text)
        self.assertIn("polymarket_maker_state_present 0", text)
        self.assertIn("polymarket_maker_max_drawdown_ratio 0.15", text)
        self.assertIn("polymarket_exporter_scrape_errors 0", text)


if __name__ == "__main__":
    unittest.main()
