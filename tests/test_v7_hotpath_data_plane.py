from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_router_decision_loop_is_250ms_with_separate_maintenance():
    source = (ROOT / 'scripts/v7_external_fair_paper_router.py').read_text()
    loop = (ROOT / 'scripts/paper_v7_execution_loop.sh').read_text()
    assert 'def maintenance_step(self) -> None:' in source
    assert 'maintenance_period = 1.0' in source
    assert 'period = max(0.25, interval)' in source
    assert 'next_decision += period' in source
    assert '--config "$EXTERNAL_FAIR_POLICY" --interval 0.25' in loop


def test_repricing_book_observer_is_fair_only_and_persistent_across_maker_rotation():
    observer = (ROOT / 'src/v7_maker_fillability_observer.cpp').read_text()
    loop = (ROOT / 'scripts/paper_v7_execution_loop.sh').read_text()
    assert 'else if (arg == "--fair-only") options.fair_only = true;' in observer
    assert 'if (options.fair_only)' in observer
    assert '--output-dir "$RUN_ROOT/research/repricing_book" --fair-only' in loop
    assert '--book-tape "$RUN_ROOT/research/repricing_book/book_observations/current.jsonl"' in loop
    assert '--book-status "$RUN_ROOT/research/repricing_book/fillability_ws_status.json"' in loop
    assert 'v7_assert_registered_child_count 20' in loop


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
    assert 'v7_canonical_economics.py' in loop  # lightweight health/PnL reconciliation only


def test_router_maintenance_failure_is_fail_closed():
    source = (ROOT / 'scripts/v7_external_fair_paper_router.py').read_text()
    assert 'self.maintenance_ready = True' in source
    assert 'elif not self.maintenance_ready:' in source
    assert 'blocker = "ROUTER_MAINTENANCE_NOT_READY"' in source
    assert 'self.maintenance_ready = False' in source
    assert 'ROUTER_MAINTENANCE_ERROR' in source


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in list(globals().items())
                   if name.startswith("test_") and callable(fn))
    assert tests, "No tests collected"
    for name, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
