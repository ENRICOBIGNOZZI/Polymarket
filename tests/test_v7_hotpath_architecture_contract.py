from __future__ import annotations
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def test_hot_path_has_no_database_or_dataframe_engine():
    # These are the Python processes on the decision/execution path. Persistent
    # evidence stores may use databases on the research plane, never here.
    files=[
        'scripts/v7_external_fair_paper_router.py',
        'scripts/v7_ledger_spool.py',
        'scripts/v7_global_portfolio_coordinator.py',
        'scripts/v7_lead_lag_taker_runtime.py',
    ]
    forbidden=('sqlite3','sqlalchemy','psycopg','pandas','DataFrame')
    for rel in files:
        text=(ROOT/rel).read_text()
        for token in forbidden: assert token not in text, (rel,token)
    router=(ROOT/'scripts/v7_external_fair_paper_router.py').read_text()
    assert 'BOUNDED_PYTHON_LOCATOR_MAP' in router
    assert 'index_bytes' in router

def test_hot_processes_are_explicit_and_cpu_classed():
    manifest=json.loads((ROOT/'config/v7_process_manifest.json').read_text())
    hot={p['id'] for p in manifest['processes'] if p.get('runtime_class')=='HOT_PATH'}
    assert hot=={
        'external_venue_runtime','external_fair_router','ledger_router',
        'global_portfolio_coordinator','lead_lag_taker_v1',
        'authorized_maker_paper_executor',
    }
    launcher=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    assert launcher.count('v7_exec_class HOT_PATH')>=len(hot)-1
    runtime=(ROOT/'scripts/v7_process_runtime.sh').read_text()
    assert 'taskset -c "$cpuset"' in runtime
    assert 'PM_V7_HOT_CPUSET' in runtime

def test_event_driven_features_are_incremental_not_dataframe_recomputed():
    state=(ROOT/'src/v7_external_state.cpp').read_text()
    ingress=(ROOT/'src/v7_external_ingress.cpp').read_text()
    kernel=(ROOT/'src/v7_external_kernel.cpp').read_text()
    assert 'ExternalAssetState' in state
    assert 'on_event' in ingress or 'push' in ingress
    assert 'DataFrame' not in state+ingress+kernel
