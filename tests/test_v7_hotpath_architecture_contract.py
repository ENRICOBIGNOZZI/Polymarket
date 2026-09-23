from __future__ import annotations
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def test_hot_path_has_no_database_dataframe_or_python_execution_owner():
    native=(ROOT/'src/v7_crypto_settlement_engine.cpp').read_text()
    manager=(ROOT/'scripts/v7_pure_arb_multi_manager.py').read_text()
    forbidden=('sqlite3','sqlalchemy','psycopg','pandas','DataFrame')
    for token in forbidden:
        assert token not in native, token
    assert 'NativeCryptoDecisionLane' in native
    assert 'NativeSettlementAuthority authority' in native
    assert 'NativePaperExecutionAdapter' in native
    assert 'subprocess.Popen(' in manager
    assert 'self.child = subprocess.Popen' in manager
    assert '"partitioned_native_workers": False' in manager
    assert 'launch_worker' not in manager


def test_hot_process_is_single_native_owner_and_cpu_classed():
    manifest=json.loads((ROOT/'config/v7_process_manifest.json').read_text())
    hot=[p for p in manifest['processes'] if p.get('runtime_class')=='HOT_PATH']
    assert [p['id'] for p in hot]==['crypto_settlement_engine']
    engine=hot[0]
    assert engine['executable'].endswith('PURE_ARB_MULTI_RUNTIME}')
    assert engine['dependencies']==[]
    for owner in ('capital_allocator','global_portfolio_coordinator','inventory','oms','risk_engine'):
        assert engine['authority_overrides'][owner] is True
    launcher=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    assert 'v7_exec_class HOT_PATH python3' not in launcher
    assert 'scripts/v7_pure_arb_multi_manager.py' in launcher
    assert 'scripts/v7_native_crypto_engine_manager.py' not in launcher
    assert 'polymarket_v7_crypto_settlement_engine' not in launcher
    manager=(ROOT/'scripts/v7_pure_arb_multi_manager.py').read_text()
    assert 'PM_V7_HOT_CPUSET' in manager
    assert 'taskset' in manager
    assert 'shell=True' not in manager
    assert 'worker_process_count' in manager
    runtime=(ROOT/'scripts/v7_process_runtime.sh').read_text()
    assert 'PM_V7_HOT_CPUSET' in runtime


def test_event_driven_features_are_incremental_not_dataframe_recomputed():
    state=(ROOT/'src/v7_external_state.cpp').read_text()
    ingress=(ROOT/'src/v7_external_ingress.cpp').read_text()
    kernel=(ROOT/'src/v7_external_kernel.cpp').read_text()
    assert 'ExternalAssetState' in state
    assert 'on_event' in ingress or 'push' in ingress
    assert 'DataFrame' not in state+ingress+kernel


def test_native_manager_launcher_invocation_satisfies_current_cli(tmp_path):
    """The launcher must start one cold manager for one multi-market process."""
    import re, shlex, sys
    sys.path.insert(0,str(ROOT/'scripts'))
    from unittest.mock import patch
    from v7_pure_arb_multi_manager import parse_args
    launcher=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    match=re.search(r'python3 scripts/v7_pure_arb_multi_manager.py\s+\\\n(.*?)\s+>>',launcher,re.S)
    assert match, 'missing PureArb multi manager invocation'
    command=match.group(1).replace('\\\n',' ')
    environment={
        'ROOT':str(ROOT),'RUN_ROOT':str(tmp_path),'SHA':'a'*40,
        'RUN_ID':'test-run','SERVER_ID':'test-server',
        'PURE_ARB_MULTI_RUNTIME':str(tmp_path/'polymarket_v7_pure_arb_multi_runtime'),
        'ALLOC':str(tmp_path/'control/allocations'),
    }
    for key,value in environment.items():command=command.replace('$'+key,value)
    argv=shlex.split(command.rstrip().rstrip(chr(92)))
    with patch.object(sys,'argv',['multi-manager',*argv]), \
         patch('pathlib.Path.is_file',return_value=True):
        args=parse_args()
    assert args.selection==tmp_path/'universe/book_selection.json'
    assert args.allocation==tmp_path/'control/allocations/manifest.json'
    assert args.risk_policy==ROOT/'config/v7_native_risk_policy.json'
    assert args.engine==tmp_path/'polymarket_v7_pure_arb_multi_runtime'
    declared=next(p for p in json.loads((ROOT/'config/v7_process_manifest.json').read_text())['processes'] if p['id']=='native_engine_manager')['arguments']
    assert {arg for arg in argv if arg.startswith('--')}=={arg for arg in declared if arg.startswith('--')}
    assert '--universe' not in argv
    assert '--settler' not in argv
    assert '--observation-only' not in argv


def test_single_process_pure_arb_runtime_replaces_context_fanout():
    launcher=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    manager=(ROOT/'scripts/v7_pure_arb_multi_manager.py').read_text()
    runtime=(ROOT/'src/v7_pure_arb_multi_runtime.cpp').read_text()
    manifest=json.loads((ROOT/'deploy/london/runtime_manifest.json').read_text())
    assert launcher.count('scripts/v7_pure_arb_multi_manager.py')==1
    assert 'scripts/v7_native_crypto_engine_manager.py' not in launcher
    assert 'polymarket_v7_pure_arb_multi_runtime' in manifest['binaries']
    assert 'polymarket_v7_crypto_settlement_engine' not in manifest['binaries']
    assert 'scripts/v7_pure_arb_multi_manager.py' in manifest['python_entrypoints']
    assert 'scripts/v7_native_crypto_engine_manager.py' not in manifest['python_entrypoints']
    assert '"single_process",true' in runtime
    assert '"direct_decision_queue_depth",0' in runtime
    assert '"worker_process_count": 1 if alive else 0' in manager


def test_observation_only_defaults_to_decision_capture_not_full_firehose() -> None:
    source=(ROOT/'src/v7_crypto_settlement_engine.cpp').read_text()
    block=source.split('else if (arg == "--observation-only")',1)[1].split('else if (arg == "--capture-native-observations")',1)[0]
    assert 'out.observation_only = true' in block
    assert 'out.capture_native_decisions = true' in block
    assert 'out.capture_native_observations = true' not in block
