from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_transport_probe_is_public_and_non_executing():
    source = (ROOT / "src/v7_public_paired_clob_transport_probe.cpp").read_text()
    header = (ROOT / "include/pm/v7_clob_pair_transport.hpp").read_text()
    assert '"/time"' in source
    assert '"real_order_submission", false' in source
    assert '"authenticated_execution", false' in source
    for forbidden in (
        '"/order"', '"/orders"', "NativeSettlementAuthority",
        "NativeClobOrderLane", "UserOmsBridge", "private_key",
    ):
        assert forbidden not in source
    assert "PairPersistentTlsTransport" in header
    assert "submit_parallel" in header
    assert "submit_batch" in header
    assert "successful batch response is NOT treated as" in header


def test_london_latency_lab_is_three_az_paper_only():
    workflow = (ROOT / ".github/workflows/v7-london-aws-provision.yml").read_text()
    lab = (ROOT / "ops/v7_london_latency_lab.sh").read_text()
    ssm = (ROOT / "ops/v7_london_ssm_benchmark.py").read_text()
    assert "latency-lab" in workflow
    assert "id-token: write" in workflow
    assert "PolymarketLondonPaperDeployPolicy" in workflow
    assert "Configure permanent AWS OIDC credentials for latency lab" in workflow
    assert "v7_london_ssm_bootstrap.py" in workflow
    assert "v7_london_ssm_benchmark.py latency" in workflow
    assert "v7_london_multipath_ssm.py" in workflow
    assert "real_order_submission" in lab and "False" in lab
    assert 'choices=("smoke", "formal", "collect", "latency")' in ssm
    for zone in ("euw2-az1", "euw2-az2", "euw2-az3"):
        assert zone in ssm
    assert "automatic_cutover" in ssm
    assert '"matching_engine_latency_observed": False' in ssm


def test_latency_lab_does_not_require_cutover_readiness():
    workflow = (ROOT / ".github/workflows/v7-london-aws-provision.yml").read_text()
    start = workflow.index('if [[ "$PHASE" != "latency-lab" ]]; then')
    end = workflow.index("python3 - <<'PY'", start)
    gate = workflow[start:end]
    assert "v7_cutover_contract.py" in gate
    assert "Latency hosts deliberately keep the PAPER runtime disabled" in gate
    non_latency, latency = gate.split("else", 1)
    assert "v7_cutover_contract.py" in non_latency
    assert "v7_cutover_contract.py" not in latency
    assert "v7_london_ssm_benchmark.py" in latency
    assert "v7_london_ssm_bootstrap.py" in latency


def test_latency_lab_requires_measured_10pct_tail_gate():
    lab = (ROOT / "ops/v7_london_latency_lab.sh").read_text()
    assert 'test["p99"] <= base["p99"]*.90' in lab
    assert 'test["p999"] <= base["p999"]' in lab
    assert "PM_V7_ENABLE_IPO" in (ROOT / "CMakeLists.txt").read_text()
    assert "-fprofile-generate=" in lab
    assert "-fprofile-use=" in lab


def test_no_duplicate_latency_workflow_is_added():
    assert not (ROOT / ".github/workflows/london-latency-lab.yml").exists()


def test_host_tuning_lab_is_reversible_and_default_off():
    host = (ROOT / "ops/v7_host_latency_ab.py").read_text()
    probe = (ROOT / "src/v7_public_paired_clob_transport_probe.cpp").read_text()
    tls_header = (ROOT / "include/pm/v7_clob_tls.hpp").read_text()
    socket_header = (ROOT / "include/pm/socket_tuning.hpp").read_text()
    ssm = (ROOT / "ops/v7_london_ssm_benchmark.py").read_text()
    bootstrap = (ROOT / "ops/v7_london_bootstrap.sh").read_text()

    for profile in (
        "performance_governor",
        "cstate_dma_latency",
        "ena_interrupt_moderation_zero",
        "irq_feed_affinity",
        "socket_busy_poll_50",
        "decision_affinity",
        "combined_runtime_tuning",
    ):
        assert profile in host

    assert "restore(original)" in host
    assert "restored_exactly" in host
    assert "persistent_tuning" in host
    assert '"persistent_tuning": False' in host
    assert "isolcpus=" in host
    assert "taskset" in host
    assert "pin_decision" in host
    assert "socket_busy_poll_us = 0" in tls_header
    assert "int socket_busy_poll_us = 0" in probe
    assert "kMaxBusyPollUs" in socket_header
    assert "apply_busy_poll" in socket_header
    assert "incoming_napi_id" in socket_header
    assert "v7_host_latency_ab.py" in ssm
    assert "host-tuning.json" in ssm
    assert "ethtool" in bootstrap


def test_host_tuning_is_evidence_only_and_has_tail_gate():
    host = (ROOT / "ops/v7_host_latency_ab.py").read_text()
    assert 'cp99 <= bp99 * 0.90' in host
    assert 'cp999 <= bp999' in host
    assert 'cf <= bf' in host
    assert "polymarket-v7-paper.service" in host
    assert 'raise SystemExit("PAPER runtime must be stopped")' in host
    assert "real_order_submission" in host
    assert "authenticated_execution" in host


def test_final_latency_evidence_has_zero_direct_queue_and_replay_parity():
    lab = (ROOT / "ops/v7_london_latency_lab.sh").read_text()
    runtime = (ROOT / "src/v7_pure_arb_multi_runtime.cpp").read_text()
    cmake = (ROOT / "CMakeLists.txt").read_text()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    parity = (ROOT / "tests/test_v7_pure_arb_replay_parity.cpp").read_text()
    assert '"direct_decision_queue_depth":0' in lab
    assert '{"direct_decision_queue_depth",0}' in runtime
    assert '{"latency_queue_depth",tape_status.queued}' in runtime
    assert "pm_v7_pure_arb_replay_parity_tests" in cmake
    assert "pure_arb_replay_parity_tests" in ci
    assert "reference_evaluate" in parity
    assert "std::bit_cast<std::uint64_t>" in parity


if __name__ == "__main__":
    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    for test in tests:
        test()
    print(f"london_latency_lab_tests={len(tests)}")
