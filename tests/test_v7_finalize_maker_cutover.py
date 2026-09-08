from __future__ import annotations
import json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import v7_finalize_maker_cutover as cutover
SHA='a'*40
NONCE=f"{'b'*40}.123.456"

def write(path:Path,value:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value)+'\n',encoding='utf-8')

def fixture(root:Path)->None:
    write(root/'control/CUTOVER_DRAIN',{
        'schema':'polymarket_v7_cutover_drain_v1','nonce':NONCE,
        'current_sha':SHA,'target_sha':'b'*40,'paper_only':True})
    write(root/'control/runtime_status.json',{
        'model_sha':SHA,'state':'stopped','paper_only':True,
        'authenticated_execution':False,'real_order_submission':False,
        'economic_new_risk_ready':False,'authorized_alpha_actions':[]})
    write(root/'micro_maker/authorized_make_executor_status.json',{
        'schema':'polymarket_v7_authorized_maker_paper_executor_status_v1',
        'model_sha':SHA,'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'active_orders':0})
    write(root/'external_fair/paper_router_status.json',{
        'schema':'polymarket_v7_external_fair_paper_router_status_v1',
        'model_sha':SHA,'paper_only':True,'authenticated_execution':False,
        'real_order_submission':False,'open_positions':0,'pending_maker_orders':0})
    ledger=root/'ledger/execution.jsonl';ledger.parent.mkdir(parents=True);ledger.write_text('',encoding='utf-8')

class FinalizerTests(unittest.TestCase):
    def test_flat_current_paper_state_produces_deterministic_proof(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root)
            out=cutover.finalize(root,SHA,NONCE,now_ms=123456)
            self.assertEqual(out['state'],'MAKER_FLAT')
            self.assertEqual(out['paper_account_open_positions'],0)
            self.assertEqual(out['canonical_flat_proof']['active_orders'],0)
            self.assertEqual(len(out['proof_sha256']),64)
            saved=json.loads((root/'control/maker_cutover_flat_proof.json').read_text())
            self.assertEqual(saved,out)

    def test_active_maker_order_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root)
            p=root/'micro_maker/authorized_make_executor_status.json';v=json.loads(p.read_text());v['active_orders']=1;write(p,v)
            with self.assertRaisesRegex(cutover.MakerCutoverError,'live_orders'):
                cutover.finalize(root,SHA,NONCE)

    def test_open_paper_position_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root)
            p=root/'external_fair/paper_router_status.json';v=json.loads(p.read_text());v['open_positions']=1;write(p,v)
            with self.assertRaisesRegex(cutover.MakerCutoverError,'paper_account_not_flat'):
                cutover.finalize(root,SHA,NONCE)

    def test_wrong_drain_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);fixture(root)
            with self.assertRaisesRegex(cutover.MakerCutoverError,'cutover_drain_identity_mismatch'):
                cutover.finalize(root,SHA,'wrong')

if __name__=='__main__':unittest.main()
