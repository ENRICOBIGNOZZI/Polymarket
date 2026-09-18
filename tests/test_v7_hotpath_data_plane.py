from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_native_decision_loop_is_event_driven_and_router_free():
    source = (ROOT / 'src/v7_crypto_settlement_native_candidate.cpp').read_text()
    loop = (ROOT / 'scripts/paper_v7_execution_loop.sh').read_text()
    assert 'NativeCryptoDecisionLane' in source
    assert 'ExternalVenueWsClient' in source
    assert 'MarketWebSocketFeed' in source
    assert 'lane.construct_candidate' in source
    assert 'authority.submit' in source
    assert 'v7_external_fair_paper_router.py' not in loop
    assert 'scripts/v7_native_crypto_engine_manager.py' in loop
    assert '--engine "$CRYPTO_SETTLEMENT_ENGINE"' in loop


def test_repricing_book_observer_is_fair_only_and_outside_hot_path():
    observer = (ROOT / 'src/v7_maker_fillability_observer.cpp').read_text()
    loop = (ROOT / 'scripts/paper_v7_execution_loop.sh').read_text()
    manifest = json.loads((ROOT / 'config/v7_process_manifest.json').read_text())
    assert 'else if (arg == "--fair-only") options.fair_only = true;' in observer
    assert 'if (options.fair_only)' in observer
    assert '--output-dir "$RUN_ROOT/research/repricing_book" --fair-only' in loop
    rows = {row['id']: row for row in manifest['processes']}
    assert rows['pm_book_observer']['runtime_class'] == 'COLLECTOR'
    assert rows['crypto_settlement_engine']['runtime_class'] == 'HOT_PATH'
    assert rows['crypto_settlement_engine']['dependencies'] == []
    assert 'v7_assert_registered_child_count 9' in loop


def test_lineage_invalidation_is_instrumented_without_relaxing_fail_closed_rules():
    header = (ROOT / 'include/pm/v7_market_ws.hpp').read_text()
    decoder = (ROOT / 'src/v7_market_ws.cpp').read_text()
    observer = (ROOT / 'src/v7_maker_fillability_observer.cpp').read_text()
    for field in (
        'lineage_invalid_book_snapshot', 'lineage_invalid_price_change',
        'lineage_invalid_tick_size_change', 'price_change_without_lineage',
    ):
        assert field in header
        assert field in observer
    assert 'invalidate(*state, result, output);' in decoder
    assert '++result.price_change_without_lineage;' in decoder


def test_retrospective_analytics_are_off_london_and_complete_on_research_plane():
    loop = (ROOT / 'scripts/paper_v7_execution_loop.sh').read_text()
    research = (ROOT / 'research/run_offline_analytics.sh').read_text()
    heavy = (
        'v7_generate_economic_artifacts.py', 'v7_profit_attribution.py',
        'v7_profit_report.py', 'v7_fast_cancel_latency_report.py',
        'v7_maker_execution_horse_race.py', 'v7_economic_decision_report.py',
        'v7_lossless_data_compaction.py', 'v7_permanent_evidence.py',
        'v7_joint_execution_policy.py', 'v7_learned_execution_model.py',
    )
    for script in heavy:
        assert script not in loop
        assert f'scripts/{script}' in research
    assert 'v7_canonical_economics.py' in loop


def test_native_rollover_settlement_failure_is_fail_closed():
    manager = (ROOT / 'scripts/v7_native_crypto_engine_manager.py').read_text()
    assert 'SETTLEMENT_BLOCKED' in manager
    assert 'NATIVE_PAPER_SETTLEMENT_INCOMPLETE' in manager
    assert 'return 79' in manager
    assert 'self._terminate_all()' in manager


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in list(globals().items())
                   if name.startswith("test_") and callable(fn))
    assert tests, "No tests collected"
    for name, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
