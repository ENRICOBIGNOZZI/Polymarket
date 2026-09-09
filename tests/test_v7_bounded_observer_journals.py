import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from v7_compressed_journal import journal_rows
import v7_global_portfolio_coordinator as coordinator
import v7_market_maker_rewards as rewards
import v7_binance_usdm_rest_collector as binance
import v7_deribit_rest_collector as deribit
import v7_coinbase_l2_rest_collector as coinbase


class BoundedObserverTests(unittest.TestCase):
    def test_coordinator_persists_exact_result_without_executing_cut_twice(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'events.jsonl';row={'state':'NOTHING','decisions':[{'opportunity_id':'one','selected':False}]}
            with patch.object(sys,'argv',['coordinator','--run-root',d,'--event-log',str(path)]),\
                    patch.object(coordinator,'process_cut',return_value=row) as process:
                self.assertEqual(coordinator.main(),0)
            process.assert_called_once_with(Path(d))
            self.assertEqual(list(journal_rows(path)),[row])

    def test_selector_preserves_published_runtime_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);row={'state':'PINNED','paper_only':True,'market_id':'m'}
            with patch.object(sys,'argv',['selector','--output',str(root/'selection.json'),'--event-log',str(root/'events.jsonl')]),\
                    patch.object(rewards,'build_snapshot',return_value={'candidate':'other'}),\
                    patch.object(rewards,'publish_runtime_selection',return_value=(row,True)) as publish:
                self.assertEqual(rewards.main(),0)
            publish.assert_called_once()
            self.assertEqual(list(journal_rows(root/'events.jsonl')),[row])

    def test_rest_journals_preserve_complete_raw_responses_and_status(self):
        for module,state in ((binance,'OPERATIONAL'),(deribit,'OPERATIONAL'),(coinbase,'OPERATIONAL_POLLING')):
            with self.subTest(module=module.__name__),tempfile.TemporaryDirectory() as d:
                root=Path(d);row={'raw_response':{'prices':[.1,.2],'absent':None},'receive_wall_ns':123}
                status={'state':state,'execution_authority':False}
                with patch.object(sys,'argv',['rest','--status',str(root/'status.json'),'--tape',str(root/'raw.jsonl')]),\
                        patch.object(module,'collect_once',return_value=(row,status)) as collect:
                    self.assertEqual(module.main(),0)
                collect.assert_called_once()
                self.assertEqual(list(journal_rows(root/'raw.jsonl')),[row])
                self.assertEqual(json.loads((root/'status.json').read_text()),status)


if __name__=='__main__':unittest.main()
