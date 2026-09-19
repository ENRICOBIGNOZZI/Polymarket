from pathlib import Path
import ast
import re
ROOT = Path(__file__).resolve().parents[1]


def test_parallel_legacy_execution_entrypoints_are_absent():
    for name in ("scripts/v7_external_fair_paper_router.py", "scripts/v7_global_portfolio_coordinator.py",
                 "scripts/v7_lead_lag_taker_runtime.py", "src/v7_authorized_maker_paper_executor.cpp",
                 "src/v7_crypto_settlement_native_candidate.cpp"):
        assert not (ROOT / name).exists()
    cmake = (ROOT / "CMakeLists.txt").read_text()
    assert cmake.count("add_executable(polymarket_v7_crypto_settlement_engine") == 1
    assert "add_executable(polymarket_v7_crypto_settlement_native_candidate" not in cmake
    assert "add_executable(polymarket_v7_authorized_maker_paper_executor" not in cmake


def test_retained_historical_math_has_no_execution_class_or_cli():
    for name in ("v7_external_fair_research.py", "v7_lead_lag_policy.py"):
        tree = ast.parse((ROOT / "scripts" / name).read_text())
        definitions = {n.name for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef))}
        assert not definitions.intersection({"PaperRouter", "LeadLagRuntime", "main"})


def test_fast_path_consumes_pod_context_without_slow_io():
    source = (ROOT / "src/v7_crypto_settlement_engine.cpp").read_text()
    loop = source[source.index("while (monotonic_now_ns() < deadline)"):source.index("// Market rollover")]
    assert "slow_feed.consume(slow_cache" in loop
    for forbidden in ("std::ifstream", "json::parse", "read_json(", "publish_contexts("):
        assert forbidden not in loop
    assert loop.index("pm_faults.exchange") < loop.index("slow_feed.consume")
    assert "quote_is_adverse_to_shock" in loop
    assert "required_slow_context_mask" in (ROOT / "src/v7_crypto_decision_lane.cpp").read_text()


def test_cmake_sources_and_cutover_hash_inputs_exist():
    source = (ROOT / "CMakeLists.txt").read_text()
    for path in re.findall(r"\b(?:src|tests)/[a-zA-Z0-9_./-]+\.(?:cpp|py)", source):
        assert (ROOT / path).is_file(), path
    source = (ROOT / "scripts/v7_cutover_contract.py").read_text()
    assert "scripts/v7_global_portfolio_coordinator.py" not in source


def test_cold_publisher_retains_failure_diagnostics_and_no_authority():
    source = (ROOT / "scripts/v7_native_crypto_engine_manager.py").read_text()
    assert "slow_context_failures" in source and "slow_context_error" in source
    assert '"--slow-context"' in source
    assert '"model_used_mask", 0' in (ROOT / "src/v7_native_runtime_evidence.cpp").read_text()


def test_research_validation_dependency_stays_out_of_london_runtime():
    workflow=(ROOT/'.github/workflows/ci.yml').read_text()
    sanitizer=workflow.split('  sanitizer:',1)[1].split('  build-test:',1)[0]
    builds=workflow.split('  build-test:',1)[1].split('  london-runtime-boundary:',1)[0]
    london=workflow.split('  london-runtime-boundary:',1)[1]
    for job in (sanitizer,builds):
        install=next(line for line in job.splitlines() if 'apt-get install' in line)
        assert 'python3-pytest' in install and 'python3-numpy' in install
        assert '-DPython3_EXECUTABLE=/usr/bin/python3' in job
    assert 'python3-numpy' not in london
    assert '-DBUILD_TESTING=OFF' in london
