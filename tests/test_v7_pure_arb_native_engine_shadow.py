from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_legacy_native_shadow_remains_zero_authority_and_undeployed():
    engine=(ROOT/"src/v7_crypto_settlement_engine.cpp").read_text()
    manager=(ROOT/"scripts/v7_native_crypto_engine_manager.py").read_text()
    manifest=(ROOT/"config/v7_process_manifest.json").read_text()

    assert '#include "pm/v7_pure_arb_lane.hpp"' in engine
    assert "--pure-arb-native-shadow" in engine
    assert "--pure-arb-native-shadow" in manager
    assert '"pure_arb_execution_authority": False' in manager
    assert '"execution_handoff", false' in engine
    assert '"authority", "ZERO_AUTHORITY_RESEARCH_ONLY"' in engine
    assert "scripts/v7_native_crypto_engine_manager.py" not in manifest
    assert "${PURE_ARB_MULTI_RUNTIME}" in manifest

    start=engine.index("// Zero-authority complete-set arbitrage shadow.")
    end=engine.index("if (options.capture_native_observations",start)
    shadow=engine[start:end]
    assert "pure_arb::fee_usdc" in shadow
    assert "pure_arb::price" in shadow
    assert shadow.count("pure_arb::sweep(")==2
    for forbidden in (
        "authority.submit(",
        "append_candidate(",
        "ExecutionPlan",
        "paper_execution.submit(",
        "NativeClobOrderLane",
        "ofstream",
        "filesystem",
    ):
        assert forbidden not in shadow


def test_shadow_terms_match_frozen_observer_reserve_and_venue_fees():
    engine=(ROOT/"src/v7_crypto_settlement_engine.cpp").read_text()
    manager=(ROOT/"scripts/v7_native_crypto_engine_manager.py").read_text()
    observer=(ROOT/"src/v7_maker_fillability_observer.cpp").read_text()

    assert 'double pure_arb_reserve_per_share = 0.0005' in engine
    assert '"--pure-arb-reserve-per-share", "0.0005"' in manager
    assert 'double pure_arb_reserve_per_share = 0.0005' in observer
    assert "options.taker_fee_rate" in engine
    assert "options.taker_fee_exponent" in engine
    assert "pure_arb::fee_usdc" in engine
    assert "pure_arb::sweep" in engine
    assert "pure_arb_max_leg_skew_ns = 100'000'000LL" in engine
    assert '"--pure-arb-max-leg-skew-ns", "100000000"' in manager


if __name__=="__main__":
    tests=[v for k,v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for test in tests:test()
    print(f"pure_arb_native_engine_shadow_tests={len(tests)}")
