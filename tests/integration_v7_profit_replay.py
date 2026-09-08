import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_causal_book import BookTimeline,SCHEMA
from v7_profit_experiments import replay_anchor
parser=argparse.ArgumentParser();parser.add_argument('--binary',type=Path,required=True);args=parser.parse_args();BINARY=args.binary.resolve()

class NativeReplayTests(unittest.TestCase):
 def run_native(self,life=5000,extra=False):
  trades=[{'observer_sequence':i,'aggressor_side':'SELL','price':.5,'size':q,'exchange_event_ns':t,'receive_monotonic_ns':t}
          for i,t,q in [(1,1100000000,6.25),(2,1200000000,1.75),(3,7000000000,1.)]][:3 if extra else 2]
  request={'start_ns':1000000000,'exchange_ns':1000000000,'tick':.01,'price':.5,'quantity':1.,'queue_ahead':5.,'lifetime_ms':life,'best_ask':.52,'trades':trades}
  output=subprocess.run([str(BINARY)],input=json.dumps(request),text=True,capture_output=True,check=True)
  return json.loads(output.stdout)
 def test_zero_scenario_and_operational_quantity(self):
  row=self.run_native();self.assertEqual(row['simulation_fill_events'],2);self.assertEqual(row['operational_filled_shares'],.5)
  self.assertEqual(len(row['fills']),1);self.assertEqual(row['active_orders'],0)
  self.assertTrue(row['excluded_from_portfolio_equity']);self.assertFalse(row['real_order_submission'])
 def test_longer_life_changes_fill_only_before_cancel(self):
  self.assertEqual(self.run_native(extra=True)['operational_filled_shares'],.5)
  self.assertEqual(self.run_native(life=10000,extra=True)['operational_filled_shares'],1.)
 def test_paired_research_book_gap_and_flow_filter(self):
  base=time.time_ns()//1000000-45000;sha='a'*40
  book=BookTimeline(Path('/unused'),sha,retention_ms=60000)
  for seq,offset,trade in [(1,-10,None),(2,100,{'aggressor_side':'SELL','price':.5,'size':10,'exchange_event_ns':1100000000}),(3,200,None),(4,2000,None),(5,42010,None)]:
   book.ingest({'schema':SCHEMA,'model_sha':sha,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
       'execution_authority':'ZERO_AUTHORITY_RESEARCH_ONLY','observer_session_id':'s','connection_epoch':1,'observer_sequence':seq,
       'market_id':'m','token_id':'yes','receive_wall_ms':base+offset,'receive_monotonic_ns':1000000000+offset*1000000,
       'exchange_event_ns':1000000000+offset*1000000,'valid':offset!=200,'lineage_continuous':offset!=200,'features_valid':offset!=200,
       'best_bid':.5,'best_ask':.52,'tick_size':.01,'bid_depth_l1':5.,'ask_depth_l1':10.,
       'placement_features':{'aggressive_sell_prints_per_second':0.},'public_trade':trade})
  anchor={'origin_ms':base,'market_id':'m','token_id':'yes','book_gap_counter':book.gaps,'observer_session_id':'s','connection_epoch':1,
      'order':{'intended_size':1.,'limit_price':.5,'metadata':{'arrival_receive_monotonic_ns':1000000000,'arrival_exchange_event_ns':1000000000}}}
  status={'state':'running','connection_epoch':1,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'book_events_written':book.sequence,'book_watermark_receive_wall_ms':book.watermark_ms,'evidence_complete':True,'model_sha':sha,'observer_session_id':'s','timestamp_ms':time.time_ns()//1000000}
  protocol=json.loads((ROOT/'config/v7_profit_experiment.json').read_text())
  result=replay_anchor(anchor,book,status,protocol,BINARY);arms={r['arm']:r for r in result['arms']}
  self.assertEqual(arms['FLOW_JOIN_5S']['state'],'FLOW_FILTER_ABSTAIN')
  self.assertGreater(arms['JOIN_5S']['operational_filled_shares'],0)
  self.assertGreater(arms['IMPROVE1_5S']['operational_filled_shares'],0)
  self.assertEqual(arms['IMPROVE1_5S']['common_quote_quantity'],arms['JOIN_5S']['common_quote_quantity'])
  self.assertLessEqual(arms['IMPROVE1_5S']['common_quote_quantity']*.51,.5)
  self.assertGreater(arms['JOIN_5S']['intermediate_invalid_book_rows'],0)
  self.assertIsNone(arms['JOIN_5S']['fills'][0]['markouts']['1000'])
  self.assertIsNotNone(arms['JOIN_5S']['fills'][0]['markouts']['5000'])
  trade_row=next(r for r in book.history[('m','yes')] if r.get('public_trade'))
  trade_row['lineage_continuous']=False
  censored=replay_anchor(anchor,book,status,protocol,BINARY)
  self.assertTrue(all(r['state']=='TRADE_LINEAGE_CENSORED' for r in censored['arms']))
  trade_row['lineage_continuous']=True
  book.invalidate();result=replay_anchor(anchor,book,status,protocol,BINARY)
  self.assertTrue(all(r['operational_filled_shares'] is None for r in result['arms']))

unittest.main(argv=[sys.argv[0]])
