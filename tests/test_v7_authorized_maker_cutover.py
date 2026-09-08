from __future__ import annotations
import importlib.util,json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from v7_maker_accounting import authorized_maker_flat_proof,project_maker,settlement_event
spec=importlib.util.spec_from_file_location('maker_fixture',Path(__file__).with_name('test_v7_maker_accounting.py'))
fixture_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture_module)
SHA='a'*40

def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value)+'\n',encoding='utf-8')

def base(root:Path,*,settled:bool=True)->None:
    write(root/'control/runtime_status.json',{
        'model_sha':SHA,'state':'stopped','paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'economic_new_risk_ready':False,'authorized_alpha_actions':[]})
    write(root/'micro_maker/authorized_make_executor_status.json',{
        'model_sha':SHA,'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'active_orders':0})
    events=[fixture_module.event('ORDER_SUBMITTED'),fixture_module.event('FILL')]
    if settled:
        position=next(iter(project_maker(events,{})['positions'].values()))
        final=settlement_event(position,{'id':'m1','closed':True,'clobTokenIds':['m1-yes','m1-no'],
            'outcomePrices':[1.,0.]},400_000)
        assert final is not None; events.append(final)
    ledger=root/'ledger/execution.jsonl';ledger.parent.mkdir(parents=True)
    ledger.write_text(''.join(json.dumps(e.to_dict())+'\n' for e in events),encoding='utf-8')
class AuthorizedMakerCutoverTests(unittest.TestCase):
    def test_settled_current_maker_is_flat(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);base(root)
            proof=authorized_maker_flat_proof(root,SHA)
            self.assertEqual(proof['open_positions'],0)
            self.assertEqual(proof['active_orders'],0)
            self.assertEqual(proof['historical_realized_pnl'],3.0)

    def test_unsettled_fill_blocks_cutover(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);base(root,settled=False)
            with self.assertRaisesRegex(ValueError,'unreconciled_inventory_or_orders'):
                authorized_maker_flat_proof(root,SHA)

    def test_active_order_blocks_cutover(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);base(root)
            p=root/'micro_maker/authorized_make_executor_status.json';v=json.loads(p.read_text());v['active_orders']=1;write(p,v)
            with self.assertRaisesRegex(ValueError,'live_orders'):
                authorized_maker_flat_proof(root,SHA)

    def test_wrong_sha_and_undrained_spool_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);base(root)
            with self.assertRaisesRegex(ValueError,'runtime_not_stopped_safe'):
                authorized_maker_flat_proof(root,'b'*40)
            write(root/'ledger/spool/pending.json',{'pending':True})
            with self.assertRaisesRegex(ValueError,'undrained_spool'):
                authorized_maker_flat_proof(root,SHA)

if __name__=='__main__':unittest.main()
