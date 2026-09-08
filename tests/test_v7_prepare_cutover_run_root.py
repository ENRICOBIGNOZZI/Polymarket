from __future__ import annotations
import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import v7_prepare_cutover_run_root as cutover
OLD='a'*40; NEW='b'*40

def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value)+'\n',encoding='utf-8')

def fixture(root:Path)->None:
    write(root/'control/runtime_status.json',{
        'version':7,'model_sha':OLD,'pid':99999999,'state':'stopped',
        'paper_only':True,'authenticated_execution':False,'real_order_submission':False})
    write(root/'control/supervisor_status.json',{'supervisor_pid':99999998})
    (root/'control/deployed_sha').write_text(OLD+'\n')
    write(root/'control/portfolio_state.json',{
        'paper_only':True,'authenticated_execution':False,'real_order_submission':False,
        'killed':False,'drawdown':0.0,'max_drawdown':0.15})
    write(root/'external_fair/paper_router_status.json',{
        'model_sha':OLD,'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'open_positions':0,'pending_maker_orders':0})
    write(root/'micro_maker/authorized_make_executor_status.json',{
        'model_sha':OLD,'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'active_orders':0})
    ledger=root/'ledger/execution.jsonl';ledger.parent.mkdir(parents=True);ledger.write_text('',encoding='utf-8')
class PrepareCutoverTests(unittest.TestCase):
    def test_flat_prior_sha_is_atomically_archived(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';fixture(root)
            result=cutover.prepare(root,base/'archives',base,NEW,now=123,ancestor_check=lambda *_:True)
            self.assertEqual(result['state'],'ARCHIVED_PRIOR_SHA')
            self.assertTrue(result['archived'])
            self.assertEqual(result['prior_open_positions'],{'paper_account':0,'maker_active_orders':0})
            self.assertTrue(Path(result['archive_path']).exists())
            self.assertTrue((root/'control/cutover_lineage.json').exists())

    def test_current_inventory_or_orders_block_archive(self):
        cases=(('external_fair/paper_router_status.json','open_positions',1),
               ('external_fair/paper_router_status.json','pending_maker_orders',1),
               ('micro_maker/authorized_make_executor_status.json','active_orders',1))
        for rel,key,value in cases:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                base=Path(d);root=base/'run';fixture(root)
                p=root/rel;row=json.loads(p.read_text());row[key]=value;write(p,row)
                with self.assertRaisesRegex(cutover.CutoverArchiveError,'prior_open_positions'):
                    cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)

    def test_killed_or_drawdown_limited_portfolio_blocks(self):
        for key,value,reason in (('killed',True,'prior_portfolio_state_invalid'),('drawdown',0.15,'prior_portfolio_drawdown_limit')):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as d:
                base=Path(d);root=base/'run';fixture(root)
                p=root/'control/portfolio_state.json';row=json.loads(p.read_text());row[key]=value;write(p,row)
                with self.assertRaisesRegex(cutover.CutoverArchiveError,reason):
                    cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)
    def test_spool_and_nonancestor_ledger_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';fixture(root)
            write(root/'ledger/spool/pending.json',{'pending':True})
            with self.assertRaisesRegex(cutover.CutoverArchiveError,'prior_ledger_spool_not_empty'):
                cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';fixture(root)
            event={'schema_version':1,'event_type':'FINAL','strategy':'CRYPTO_SETTLEMENT_ENGINE',
                   'model_sha':'c'*40,'paper_only':True,'authenticated_execution':False,
                   'record_id':'r1','recorded_ts_ms':1,'final_pnl':0.0,'metadata':{}}
            (root/'ledger/execution.jsonl').write_text(json.dumps(event)+'\n')
            with self.assertRaisesRegex(cutover.CutoverArchiveError,'ledger_sha_not_ancestor'):
                cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda _r,old,new: old==OLD)

    def test_same_sha_recovery_does_not_archive(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';fixture(root)
            (root/'control/deployed_sha').write_text(NEW+'\n')
            runtime=json.loads((root/'control/runtime_status.json').read_text());runtime['model_sha']=NEW;write(root/'control/runtime_status.json',runtime)
            result=cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)
            self.assertEqual(result['state'],'SAME_SHA_RECOVERY');self.assertFalse(result['archived'])

    def test_prepared_empty_run_root_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';root.mkdir()
            result=cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)
            self.assertEqual(result['state'],'NEW_RUN_ROOT')

if __name__=='__main__':unittest.main()
