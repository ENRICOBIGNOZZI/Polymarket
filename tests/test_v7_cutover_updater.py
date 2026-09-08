from __future__ import annotations
import subprocess,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
UPDATER=ROOT/'ops/update_server_v7.sh'

class V7CutoverUpdaterTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text=UPDATER.read_text(encoding='utf-8')

    def test_shell_is_valid_and_exact_sha_is_required(self):
        result=subprocess.run(['bash','-n',str(UPDATER)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('EXPECTED_DEPLOY_SHA must be the exact 40-char approved SHA',self.text)
        self.assertIn('origin/main $MAIN_SHA != exact approved SHA $EXPECTED_SHA',self.text)
        self.assertIn('v7_exact_sha_ci_gate.py',self.text)

    def test_candidate_is_prevalidated_before_checkout_mutation(self):
        pre=self.text.index('prevalidate_candidate\n')
        checkout=self.text.index('git checkout --detach "$EXPECTED_SHA"')
        self.assertLess(pre,checkout)
        self.assertIn('ctest --test-dir build --output-on-failure',self.text)
        self.assertIn('v7_cutover_contract.py --repository-root . --expected-head "$EXPECTED_SHA"',self.text)

    def test_cutover_order_is_drain_stop_proof_archive_then_checkout(self):
        start=self.text.index('if [[ "$RUNTIME_PRESENT" == 1 ]]; then\n  request_cutover_drain')
        drain=self.text.index('wait_for_cutover_drain "$OLD_SHA"',start)
        stop=self.text.index('stop_production_runtime',drain)
        stopped_flag=self.text.index('RUNTIME_STOPPED_BY_DEPLOY=1',stop)
        proof=self.text.index('python3 "$MAKER_CUTOVER_FINALIZER"',stopped_flag)
        archive=self.text.index('python3 "$CUTOVER_ARCHIVER"',proof)
        checkout=self.text.index('git checkout --detach "$EXPECTED_SHA"',archive)
        self.assertTrue(start<drain<stop<stopped_flag<proof<archive<checkout)
    def test_drain_contract_uses_only_current_account_executor_and_spool(self):
        block=self.text[self.text.index('cutover_positions_drained(){'):self.text.index('record_deployed_sha(){')]
        for required in ('external_fair/paper_router_status.json','micro_maker/authorized_make_executor_status.json',
                         "open_positions", "pending_maker_orders", "active_orders", "ledger/spool"):
            self.assertIn(required,block)
        for removed in ('micro_maker/state.json','micro_taker','maker_cutover_mark','never_started'):
            self.assertNotIn(removed,block)

    def test_failed_precheckout_cutover_can_restore_exact_previous_runtime(self):
        self.assertIn('RUNTIME_STOPPED_BY_DEPLOY=0',self.text)
        self.assertIn('restart_stopped_runtime_on_failure',self.text)
        self.assertIn('active checkout no longer matches $OLD_SHA',self.text)
        self.assertIn('deployed identity no longer matches $OLD_SHA',self.text)
        self.assertIn('RUNTIME_STOPPED_BY_DEPLOY=1',self.text)

    def test_only_current_v7_launchd_labels_are_managed(self):
        self.assertIn('label="com.polymarket.v7.paper"', self.text)
        self.assertIn('com.polymarket.v7.$label', self.text)
        for name in ('exporter','prometheus','grafana','retention'):
            self.assertIn(name, self.text)
        for old in ('com.polymarket.paper','com.polymarket.exporter','com.polymarket.prometheus','com.polymarket.grafana'):
            self.assertNotIn(old,self.text)

    def test_final_checkout_is_rebuilt_and_health_checked(self):
        self.assertIn('rm -rf "$APP_DIR/build"',self.text)
        self.assertIn('cmake -S "$APP_DIR" -B "$APP_DIR/build" -DCMAKE_BUILD_TYPE=Release',self.text)
        self.assertIn('start_production_runtime',self.text)
        self.assertIn('start_monitoring',self.text)
        self.assertIn('runtime_health "$DASHBOARD_UID"',self.text)
        self.assertIn('record_runtime_identity',self.text)

    def test_monitoring_unknown_listener_is_never_killed_blindly(self):
        self.assertIn('refusing to replace unknown listener',self.text)
        self.assertIn('stop_stale_monitoring_listener exporter 9108',self.text)
        self.assertIn('stop_stale_monitoring_listener prometheus 9090',self.text)
        self.assertIn('stop_stale_monitoring_listener grafana 3000',self.text)

if __name__=='__main__':unittest.main()
