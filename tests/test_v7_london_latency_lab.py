from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_transport_probe_is_public_and_non_executing():
    source=(ROOT/"src/v7_public_paired_clob_transport_probe.cpp").read_text()
    header=(ROOT/"include/pm/v7_clob_pair_transport.hpp").read_text()
    assert '"/time"' in source
    assert '"real_order_submission", false' in source
    assert '"authenticated_execution", false' in source
    for forbidden in ('"/order"','"/orders"',"NativeSettlementAuthority",
                      "NativeClobOrderLane","UserOmsBridge","private_key"):
        assert forbidden not in source
    assert "PersistentTlsSession" in header
    assert "PairPersistentTlsTransport" in header\n    assert "submit_parallel" in header\n    assert "submit_batch" in header


def test_london_latency_lab_is_three_az_paper_only():
    workflow=(ROOT/".github/workflows/london-latency-lab.yml").read_text()
    lab=(ROOT/"ops/v7_london_latency_lab.sh").read_text()
    ssm=(ROOT/"ops/v7_london_ssm_benchmark.py").read_text()
    assert "id-token: write" in workflow
    assert "PolymarketLondonPaperDeployPolicy" in workflow
    assert "v7_london_ssm_bootstrap.py" in workflow
    assert "v7_london_ssm_benchmark.py latency" in workflow
    assert "v7_london_multipath_ssm.py" in workflow
    assert "real_order_submission" in lab and "False" in lab
    assert 'choices=("smoke", "formal", "collect", "latency")' in ssm
    for zone in ("euw2-az1","euw2-az2","euw2-az3"):
        assert zone in ssm
    assert "automatic_cutover" in ssm
    assert '"matching_engine_latency_observed": False' in ssm


def test_latency_lab_requires_measured_10pct_tail_gate():
    lab=(ROOT/"ops/v7_london_latency_lab.sh").read_text()
    assert 'test["p99"] <= base["p99"]*.90' in lab
    assert 'test["p999"] <= base["p999"]' in lab
    assert "PM_V7_ENABLE_IPO" in (ROOT/"CMakeLists.txt").read_text()
    assert "-fprofile-generate=" in lab
    assert "-fprofile-use=" in lab


if __name__=="__main__":
    tests=[v for k,v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for test in tests:test()
    print(f"london_latency_lab_tests={len(tests)}")
