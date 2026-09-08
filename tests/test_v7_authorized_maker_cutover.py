from pathlib import Path
import json,sys,tempfile,unittest,importlib.util,hashlib
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import v7_finalize_maker_cutover as finalize
import v7_prepare_cutover_run_root as archive
from v7_maker_accounting import project_maker,settlement_event,authorized_maker_flat_proof
spec=importlib.util.spec_from_file_location('maker_account_fixture',Path(__file__).with_name('test_v7_maker_accounting.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
OLD='a'*40;NEW='b'*40;NONCE='strict-cutover-test'
def write(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v)+'\n')
def fixture(root,settled=True):
    write(root/'control/CUTOVER_DRAIN',{'schema':'polymarket_v7_cutover_drain_v1','current_sha':OLD,'target_sha':NEW,'nonce':NONCE,'paper_only':True})
    write(root/'control/runtime_status.json',{'model_sha':OLD,'pid':99999999,'state':'stopped','version':7,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'economic_new_risk_ready':False,'authorized_alpha_actions':[]})
    write(root/'control/supervisor_status.json',{'supervisor_pid':99999998})
    write(root/'control/portfolio_state.json',{'paper_only':True,'authenticated_execution':False,'killed':False,'drawdown':0.,'max_drawdown':.15})
    (root/'control/deployed_sha').write_text(OLD+'\n')
    write(root/'micro_maker/authorized_make_executor_status.json',{'model_sha':OLD,'paper_only':True,'authenticated_execution':False,'real_order_submission':False,'active_orders':0})
    write(root/'micro_maker/status.json',{'model_sha':OLD,'source':'not_started','paper_only':True,'authenticated_execution':False,'real_order_submission':False})
    write(root/'external_fair/paper_router_status.json',{'paper_only':True,'authenticated_execution':False,'open_positions':0,'counterfactual_open_positions':0})
    write(root/'external_fair/paper_router_state.json',{'positions':{}})
    events=[m.event('ORDER_SUBMITTED'),m.event('FILL')]
    if settled:
        p=next(iter(project_maker(events,{})['positions'].values()))
        events.append(settlement_event(p,{'id':'m1','closed':True,'clobTokenIds':['m1-yes','m1-no'],'outcomePrices':[1.,0.]},400_000))
    path=root/'ledger/execution.jsonl';path.parent.mkdir()
    for e in events:e.validate()
    path.write_text(''.join(json.dumps(e.to_dict())+'\n' for e in events));return path.read_bytes()
class AuthorizedMakerCutoverTests(unittest.TestCase):
    def test_real_maker_finalization_and_archive_not_never_started(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';original=fixture(root)
            receipt=finalize.finalize(root,OLD,NONCE,now_ms=500_000)
            self.assertFalse(receipt['never_started']);self.assertTrue(receipt['authorized_maker_reconciled'])
            self.assertEqual(receipt['canonical_flat_proof']['historical_realized_pnl'],3.)
            self.assertEqual(receipt['net_cashflow'],0.);self.assertEqual(receipt['final_pnl'],0.)
            self.assertEqual((root/'ledger/execution.jsonl').read_bytes(),original)
            result=archive.prepare(root,base/'archives',base,NEW,now=123,ancestor_check=lambda *_:True)
            self.assertEqual(result['state'],'ARCHIVED_PRIOR_SHA')
            self.assertEqual((Path(result['archive_path'])/'ledger/execution.jsonl').read_bytes(),original)
    def test_unsettled_actual_fill_blocks_even_with_zero_observer(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root,settled=False)
            with self.assertRaisesRegex(finalize.MakerCutoverError,'unreconciled_inventory'):
                finalize.finalize(root,OLD,NONCE,now_ms=500_000)
    def test_active_order_and_running_runtime_block(self):
        for path,key,value in [('micro_maker/authorized_make_executor_status.json','active_orders',1),('control/runtime_status.json','state','running')]:
            with tempfile.TemporaryDirectory() as d:
                root=Path(d);fixture(root);p=root/path;v=json.loads(p.read_text());v[key]=value;write(p,v)
                with self.assertRaises(finalize.MakerCutoverError):finalize.finalize(root,OLD,NONCE,now_ms=500_000)
    def test_receipt_tamper_cannot_archive(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';original=fixture(root)
            receipt=finalize.finalize(root,OLD,NONCE,now_ms=500_000)
            receipt['canonical_flat_proof']['historical_realized_pnl']=999
            write(root/'control/maker_cutover_liquidation.json',receipt)
            with self.assertRaisesRegex(archive.CutoverArchiveError,'receipt_invalid'):
                archive.prepare(root,base/'archive',base,NEW,now=123,ancestor_check=lambda *_:True)
            self.assertEqual((root/'ledger/execution.jsonl').read_bytes(),original)
    def test_undrained_spool_and_wrong_sha_block(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root);write(root/'ledger/spool/pending.json',{'pending':True})
            with self.assertRaisesRegex(ValueError,'undrained_spool'):authorized_maker_flat_proof(root,OLD)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root)
            with self.assertRaises(ValueError):authorized_maker_flat_proof(root,NEW)
if __name__=='__main__':unittest.main()
