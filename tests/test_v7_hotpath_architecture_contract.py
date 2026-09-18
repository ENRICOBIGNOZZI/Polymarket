from __future__ import annotations
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def test_hot_path_has_no_database_dataframe_or_python_execution_owner():
    native=(ROOT/'src/v7_crypto_settlement_native_candidate.cpp').read_text()
    manager=(ROOT/'scripts/v7_native_crypto_engine_manager.py').read_text()
    forbidden=('sqlite3','sqlalchemy','psycopg','pandas','DataFrame')
    for token in forbidden:
        assert token not in native, token
    assert 'NativeCryptoDecisionLane' in native
    assert 'NativeSettlementAuthority authority' in native
    assert 'NativePaperExecutionAdapter' in native
    assert 'subprocess.Popen(' in manager
    assert 'launch_command = list(command)' in manager
    assert '[taskset, "-c", hot_cpuset] + launch_command' in manager


def test_hot_process_is_single_native_owner_and_cpu_classed():
    manifest=json.loads((ROOT/'config/v7_process_manifest.json').read_text())
    hot=[p for p in manifest['processes'] if p.get('runtime_class')=='HOT_PATH']
    assert [p['id'] for p in hot]==['crypto_settlement_engine']
    engine=hot[0]
    assert engine['executable'].endswith('CRYPTO_SETTLEMENT_ENGINE}')
    assert engine['dependencies']==[]
    for owner in ('capital_allocator','global_portfolio_coordinator','inventory','oms','risk_engine'):
        assert engine['authority_overrides'][owner] is True
    launcher=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    assert 'v7_exec_class HOT_PATH python3' not in launcher
    assert 'scripts/v7_native_crypto_engine_manager.py' in launcher
    manager=(ROOT/'scripts/v7_native_crypto_engine_manager.py').read_text()
    assert 'PM_V7_HOT_CPUSET' in manager
    assert 'taskset' in manager
    assert 'shell=True' not in manager
    runtime=(ROOT/'scripts/v7_process_runtime.sh').read_text()
    assert 'PM_V7_HOT_CPUSET' in runtime


def test_event_driven_features_are_incremental_not_dataframe_recomputed():
    state=(ROOT/'src/v7_external_state.cpp').read_text()
    ingress=(ROOT/'src/v7_external_ingress.cpp').read_text()
    kernel=(ROOT/'src/v7_external_kernel.cpp').read_text()
    assert 'ExternalAssetState' in state
    assert 'on_event' in ingress or 'push' in ingress
    assert 'DataFrame' not in state+ingress+kernel
