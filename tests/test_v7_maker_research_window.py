from __future__ import annotations
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
from v7_maker_research_window import MakerWindow, continuity, WAITING


class MakerWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.start=100_000;self.mono=100_000_000_000
        self.anchor={'market_id':'m','token_id':'t','origin_ms':self.start,'observer_session_id':'session',
          'connection_epoch':1,'book_gap_counter':0,'order':{'record_id':'order','metadata':{'arrival_receive_monotonic_ns':self.mono}}}
        self.book=SimpleNamespace(session='session',epoch=1,gaps=0,sequence=100,watermark_ms=self.start+6000,model_sha='a'*40,
          history={('m','t'):[{'receive_monotonic_ns':self.mono+h*1_000_000,'valid':True,'lineage_continuous':True,
                             'best_bid':bid,'best_ask':bid+.02,'bid_depth_l1':20} for h,bid in [(0,.48),(4000,.40),(6000,.70)]]})
        self.protocol=json.loads((ROOT/'config/v7_profit_experiment.json').read_text());self.records=[]
        def emit(kind,**kw):
            r={'kind':kind,**kw};self.records.append(r);return r
        self.owner=SimpleNamespace(book=self.book,protocol=self.protocol,binary=Path('not-executed'),output=Path(self.tmp.name),emit=emit)

    def status(self,now=106_000):
        return {'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'model_sha':self.book.model_sha,
          'observer_session_id':self.book.session,'connection_epoch':self.book.epoch,'state':'running','timestamp_ms':now,
          'book_events_written':self.book.sequence,'book_watermark_receive_wall_ms':now,'evidence_complete':True}

    def replay(self,anchor,book,status,protocol,binary,**kw):
        arms=[]
        for a in protocol['maker']['arms']:
            filled=a['id']=='JOIN_5S'
            arms.append({'arm':a['id'],'state':'OBSERVED','operational_filled_shares':1. if filled else 0.,
              'fills':[{'price':.5,'quantity':1.,'receive_monotonic_ns':self.mono+3_000_000_000}] if filled else []})
        return {'arms':arms,'source':{'anchor':anchor,'origin_book':{},'path':[]}}

    def test_published_status_ahead_waits_without_irreversible_censor(self):
        w=MakerWindow(self.anchor);status=self.status();status['book_events_written']=101
        with patch('v7_maker_research_window.replay_anchor',side_effect=self.replay) as replay:
            self.assertIsNone(w.advance(self.owner,status,106_000_000_000));self.assertEqual(self.records,[]);replay.assert_not_called()
            self.assertEqual(continuity(self.anchor,self.book,status,105100,106000),WAITING)
            w.advance(self.owner,self.status(),106_000_000_000)
            self.assertEqual(w.executions['JOIN_5S']['state'],'OBSERVED');self.assertGreater(w.waits,0)

    def test_later_transport_gap_censors_markout_not_frozen_execution_and_restart(self):
        w=MakerWindow(self.anchor)
        with patch('v7_maker_research_window.replay_anchor',side_effect=self.replay):
            w.advance(self.owner,self.status(),106_000_000_000)
            # Exact 1s markout selects the 4s cut, never the available later 6s quote.
            label=w.markouts['JOIN_5S|0|1000'];self.assertAlmostEqual(label['markout']['best_bid_minus_fill'],-.10)
            before=copy.deepcopy(w.executions['JOIN_5S'])
            restored=MakerWindow(self.anchor,self.records)
            self.assertEqual(restored.executions['JOIN_5S'],before)
            self.book.gaps+=1;self.book.session='new-session';self.book.history={};self.book.watermark_ms=145000
            result=restored.advance(self.owner,self.status(145000),145_000_000_000)
            self.assertIsNotNone(result)
            arm=next(a for a in result['arms'] if a['arm']=='JOIN_5S')
            self.assertEqual(arm['state'],'OBSERVED');self.assertEqual(arm['operational_filled_shares'],1)
            self.assertIsNotNone(arm['fills'][0]['markouts']['1000'])
            self.assertIsNone(arm['fills'][0]['markouts']['30000'])
            self.assertEqual(arm['fills'][0]['markout_states']['30000'],'TRANSPORT_GAP_OR_SESSION_CHANGE')
            self.assertEqual(next(a for a in result['arms'] if a['arm']=='JOIN_10S')['state'],'TRANSPORT_GAP_OR_SESSION_CHANGE')

    def test_publication_wait_is_bounded_and_never_zero_filled(self):
        w=MakerWindow(self.anchor);self.book.watermark_ms=100001
        with patch('v7_maker_research_window.replay_anchor',side_effect=self.replay):
            self.assertIsNone(w.advance(self.owner,self.status(),106_000_000_000))
            result=w.advance(self.owner,self.status(130000),130_000_000_000)
            self.assertTrue(all(a['state']=='PUBLICATION_TIMEOUT_CENSORED' for a in result['arms']))
            self.assertTrue(all(a['operational_filled_shares'] is None for a in result['arms']))

if __name__=='__main__':unittest.main()
