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
            self.assertEqual(result['prior_open_positions'],{
                'paper_account':0,'maker_active_orders':0,'native_open_orders':0,
                'native_unsettled_markets':0,
                'native_carryover_microdollars':0})
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

    def test_native_fill_without_final_blocks_cross_sha_cutover(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';fixture(root)
            fill={
                'event_type':'FILL','strategy':'CRYPTO_SETTLEMENT_ENGINE',
                'model_sha':OLD,'paper_only':True,'authenticated_execution':False,
                'market_id':'m1','record_kind':'ECONOMIC_JOURNAL',
                'metadata':{'native_settlement_receipt':{
                    'owner':'V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE'}},
            }
            (root/'ledger/execution.jsonl').write_text(json.dumps(fill)+'\n',encoding='utf-8')
            with self.assertRaisesRegex(cutover.CutoverArchiveError,'prior_native_unsettled_markets:1'):
                cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)

    def test_native_final_closes_cutover_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);root=base/'run';fixture(root)
            receipt={'owner':'V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE'}
            rows=[
                {'event_type':'FILL','strategy':'CRYPTO_SETTLEMENT_ENGINE',
                 'model_sha':OLD,'paper_only':True,'authenticated_execution':False,
                 'market_id':'m1','record_kind':'ECONOMIC_JOURNAL',
                 'metadata':{'native_settlement_receipt':receipt}},
                {'event_type':'FINAL','strategy':'CRYPTO_SETTLEMENT_ENGINE',
                 'model_sha':OLD,'paper_only':True,'authenticated_execution':False,
                 'market_id':'m1','record_kind':'ECONOMIC_JOURNAL',
                 'metadata':{'native_settlement_receipt':receipt,
                             'native_market_settlement_id':'native-settlement:m1'}},
            ]
            (root/'ledger/execution.jsonl').write_text(
                ''.join(json.dumps(row)+'\n' for row in rows),encoding='utf-8')
            result=cutover.prepare(root,base/'archives',base,NEW,now=124,ancestor_check=lambda *_:True)
            self.assertEqual(result['state'],'ARCHIVED_PRIOR_SHA')

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


def _native_fill_for_carryover() -> dict:
    return {
        'event_type':'FILL','strategy':'CRYPTO_SETTLEMENT_ENGINE',
        'model_sha':OLD,'paper_only':True,'authenticated_execution':False,
        'market_id':'m1','record_kind':'ECONOMIC_JOURNAL','fill_id':'f1',
        'token_id':'yes','filled_size':20.0,'fill_price':0.4,'fee':0.1,'side':'BUY',
        'metadata':{
            'native_settlement_receipt':{'owner':'V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE'},
            'crypto_context':{'asset':'BTC','horizon':'M5'},
        },
    }


def test_explicit_native_carryover_archives_and_reserves_cost_basis() -> None:
    with tempfile.TemporaryDirectory() as d:
        base=Path(d);root=base/'run';fixture(root)
        (root/'ledger/execution.jsonl').write_text(
            json.dumps(_native_fill_for_carryover())+'\n',encoding='utf-8')
        result=cutover.prepare(
            root,base/'archives',base,NEW,now=125,
            ancestor_check=lambda *_:True,allow_native_carryover=True)
        self_carry=json.loads((root/'control/native_carryover_exposure.json').read_text())
        assert result['state']=='ARCHIVED_PRIOR_SHA'
        assert result['prior_open_positions']['native_unsettled_markets']==1
        assert self_carry['target_model_sha']==NEW
        assert self_carry['total_unsettled_microdollars']==8_100_000
        assert self_carry['context_claims_microdollars']=={'BTC:M5':8_100_000}
        assert self_carry['markets'][0]['market_id']=='m1'
        assert self_carry['markets'][0]['claim_microdollars']==8_100_000
        assert Path(result['archive_path']).joinpath('ledger/execution.jsonl').is_file()


def test_inherited_carryover_is_chained_without_credit() -> None:
    with tempfile.TemporaryDirectory() as d:
        base=Path(d);root=base/'run';fixture(root)
        prior={
            'schema':cutover.CARRYOVER_SCHEMA,'paper_only':True,
            'authenticated_execution':False,'real_order_submission':False,
            'target_model_sha':OLD,'source_model_shas':['c'*40],
            'source_archives':['/archive/older'],
            'markets':[{'model_sha':'c'*40,'market_id':'old-m','context':'ETH:H1','claim_microdollars':2_000_000}],
            'context_claims_microdollars':{'ETH:H1':2_000_000},
            'total_unsettled_microdollars':2_000_000,
        }
        write(root/'control/native_carryover_exposure.json',prior)
        (root/'ledger/execution.jsonl').write_text(
            json.dumps(_native_fill_for_carryover())+'\n',encoding='utf-8')
        cutover.prepare(root,base/'archives',base,NEW,now=126,
            ancestor_check=lambda *_:True,allow_native_carryover=True)
        carry=json.loads((root/'control/native_carryover_exposure.json').read_text())
        assert carry['total_unsettled_microdollars']==10_100_000
        assert carry['context_claims_microdollars']=={'BTC:M5':8_100_000,'ETH:H1':2_000_000}
        assert len(carry['markets'])==2
        assert '/archive/older' in carry['source_archives']


def test_git_ancestor_check_scopes_safe_directory(monkeypatch) -> None:
    captured={}
    class Result:
        returncode=0
    def fake_run(command, **kwargs):
        captured["command"]=command
        captured["kwargs"]=kwargs
        return Result()
    monkeypatch.setattr(cutover.subprocess,"run",fake_run)
    repo=Path("/tmp/native-owned-repo")
    assert cutover.git_is_ancestor(repo,"a"*40,"b"*40) is True
    assert captured["command"][:4]==["git","-c",f"safe.directory={repo}","-C"]
    assert captured["command"][4:]==[str(repo),"merge-base","--is-ancestor","a"*40,"b"*40]


def native_fixture(root: Path) -> None:
    fixture(root)
    (root/'external_fair/paper_router_status.json').unlink()
    (root/'micro_maker/authorized_make_executor_status.json').unlink()
    write(root/'control/native_engine_manager_status.json',{
        'schema':'polymarket_v7_native_engine_manager_status_v1',
        'model_sha':OLD,'state':'STOPPED','active_worker_count':0,
        'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'real_capital_at_risk':False,
        'single_native_portfolio_owner':True,
    })


def test_native_generation_does_not_require_legacy_position_surfaces(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        base=Path(d);root=base/'run';native_fixture(root)
        monkeypatch.setattr(cutover,'open_native_orders',lambda *_:{})
        result=cutover.prepare(root,base/'archives',base,NEW,now=127,ancestor_check=lambda *_:True)
        assert result['state']=='ARCHIVED_PRIOR_SHA'
        assert result['prior_open_positions']['native_open_orders']==0
        assert result['prior_open_positions']['paper_account']==0
        assert result['prior_open_positions']['maker_active_orders']==0


def test_native_generation_requires_stopped_manager(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        base=Path(d);root=base/'run';native_fixture(root)
        p=root/'control/native_engine_manager_status.json'
        row=json.loads(p.read_text());row['state']='RUNNING';row['active_worker_count']=1;write(p,row)
        monkeypatch.setattr(cutover,'open_native_orders',lambda *_:{})
        with unittest.TestCase().assertRaisesRegex(cutover.CutoverArchiveError,'prior_native_manager_not_stopped'):
            cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)


def test_native_generation_open_orders_block_cutover(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as d:
        base=Path(d);root=base/'run';native_fixture(root)
        monkeypatch.setattr(cutover,'open_native_orders',lambda *_:{'o1':object()})
        with unittest.TestCase().assertRaisesRegex(cutover.CutoverArchiveError,'prior_native_open_orders:1'):
            cutover.prepare(root,base/'archives',base,NEW,ancestor_check=lambda *_:True)
