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
    assert 'launch = list(command)' in manager
    assert '[taskset, "-c", hot_cpuset] + launch' in manager


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


def test_native_manager_launcher_invocation_satisfies_current_cli(tmp_path):
    """Catch full-stack startup regressions: inspect the real launch command."""
    import re, shlex, sys
    sys.path.insert(0,str(ROOT/'scripts'))
    from unittest.mock import patch
    from v7_native_crypto_engine_manager import parse_args
    launcher=(ROOT/'scripts/paper_v7_execution_loop.sh').read_text()
    match=re.search(r'python3 scripts/v7_native_crypto_engine_manager.py\s+\\\n(.*?)\s+>>',launcher,re.S)
    assert match, 'missing native manager invocation'
    command=match.group(1).replace('\\\n',' ')
    environment={'ROOT':str(ROOT),'RUN_ROOT':str(tmp_path),'SHA':'a'*40,
        'RUN_ID':'test-run','SERVER_ID':'test-server',
        'CRYPTO_SETTLEMENT_ENGINE':str(ROOT/'build/polymarket_v7_crypto_settlement_engine'),
        'ALLOC':str(tmp_path/'control/allocations')}
    for key,value in environment.items():command=command.replace('$'+key,value)
    # The optional shell array expands to zero argv elements when no private
    # probability artifact is configured. The static launcher test must model
    # shell expansion rather than pass its literal syntax to argparse.
    command=command.replace('"${PROBABILITY_MODEL_ARGS[@]}"','')
    command=command.replace('"${PROBABILITY_EVALUATION_ARGS[@]}"','')
    argv=shlex.split(command.rstrip().rstrip(chr(92)))
    with patch.object(sys,'argv',['native-manager',*argv]):args=parse_args()
    assert args.allocation==tmp_path/'control/allocations/crypto_settlement_engine.json'
    assert args.market_registry==ROOT/'config/v7_crypto_settlement_markets.json'
    declared=next(p for p in json.loads((ROOT/'config/v7_process_manifest.json').read_text())['processes'] if p['id']=='native_engine_manager')['arguments']
    assert {arg for arg in argv if arg.startswith('--')}=={arg for arg in declared if arg.startswith('--')}
    assert args.asynchronous_settlement
    assert not args.capture_native_observations
    assert args.capture_native_decisions
    assert args.target_quantity_microunits == 5_000_000
    assert args.maximum_entry_price_e4 == 7_500
    assert args.minimum_tte_ns == 105_000_000_000
    assert args.maximum_tte_ns == 120_000_000_000
    assert args.maker_share_cap_microunits==1_000_000


def test_observation_only_defaults_to_decision_capture_not_full_firehose() -> None:
    source=(ROOT/'src/v7_crypto_settlement_native_candidate.cpp').read_text()
    block=source.split('else if (arg == "--observation-only")',1)[1].split('else if (arg == "--capture-native-observations")',1)[0]
    assert 'out.observation_only = true' in block
    assert 'out.capture_native_decisions = true' in block
    assert 'out.capture_native_observations = true' not in block
