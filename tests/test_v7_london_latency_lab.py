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
    assert "test -f ops/v7_london_latency_lab.sh" in latency
    assert "bash -n ops/v7_london_latency_lab.sh" in latency
    assert "test -x ops/v7_london_latency_lab.sh" not in latency


def test_three_az_bootstrap_repairs_only_disabled_benchmark_checkout():
    bootstrap = (ROOT / "ops/v7_london_ssm_bootstrap.py").read_text()
    stop = bootstrap.index("! systemctl is-active --quiet polymarket-v7-paper.service")
    reset = bootstrap.index('git -C "$APP" reset --hard HEAD')
    clean = bootstrap.index('git -C "$APP" clean -fd')
    fetch = bootstrap.index('git -C "$APP" fetch --no-tags origin main')
    assert stop < reset < clean < fetch
    assert 'git -C "$APP" clean -fdx' not in bootstrap
    assert "benchmark_source_checkout_dirty=1" in bootstrap
    assert "POLYMARKET_REUSE_EXACT_SHA_CI=1" in bootstrap
    assert "PM_V7_CI_REPOSITORY=ENRICOBIGNOZZI/Polymarket" in bootstrap


def test_london_bootstrap_dependencies_retrigger_latency_lab():
    bootstrap = (ROOT / "ops/v7_london_bootstrap.sh").read_text()
    workflow = (ROOT / ".github/workflows/v7-london-aws-provision.yml").read_text()
    assert "python3-numpy" in bootstrap
    assert "- ops/v7_london_bootstrap.sh" in workflow
    assert "- ops/v7_london_ssm_bootstrap.py" in workflow


def test_three_az_bootstrap_repairs_git_metadata_ownership_before_fetch():
    bootstrap = (ROOT / "ops/v7_london_ssm_bootstrap.py").read_text()
    stop = bootstrap.index("! systemctl is-active --quiet polymarket-v7-paper.service")
    owner = bootstrap.index('bad_git_owner_count=')
    chown = bootstrap.index('chown -R {service_user}:"$SERVICE_GROUP" "$APP/.git"')
    fetch = bootstrap.index('git -C "$APP" fetch --no-tags origin main')
    assert stop < owner < chown < fetch
    assert 'realpath "$APP"' in bootstrap
    assert '! -L "$APP/.git"' in bootstrap
    assert 'chown -R {service_user}:"$SERVICE_GROUP" "$APP"' not in bootstrap
    assert 'chown -R {service_user}:"$SERVICE_GROUP" "$APP/.git"' in bootstrap


def test_london_bootstrap_propagates_exact_sha_ci_reuse_to_stage_release():
    bootstrap = (ROOT / "ops/v7_london_bootstrap.sh").read_text()
    assert 'REUSE_EXACT_SHA_CI="${POLYMARKET_REUSE_EXACT_SHA_CI:-0}"' in bootstrap
    assert 'CI_REPOSITORY="${PM_V7_CI_REPOSITORY:-ENRICOBIGNOZZI/Polymarket}"' in bootstrap
    call = bootstrap[bootstrap.index("# Stage the exact release"):]
    assert 'POLYMARKET_REUSE_EXACT_SHA_CI="$REUSE_EXACT_SHA_CI"' in call
    assert 'PM_V7_CI_REPOSITORY="$CI_REPOSITORY"' in call


def test_london_bootstrap_forwards_exact_sha_ci_reuse_to_stage_release():
    bootstrap = (ROOT / "ops/v7_london_bootstrap.sh").read_text()
    assert 'REUSE_EXACT_SHA_CI="${POLYMARKET_REUSE_EXACT_SHA_CI:-0}"' in bootstrap
    assert 'CI_REPOSITORY="${PM_V7_CI_REPOSITORY:-ENRICOBIGNOZZI/Polymarket}"' in bootstrap
    assert 'POLYMARKET_REUSE_EXACT_SHA_CI="$REUSE_EXACT_SHA_CI"' in bootstrap
    assert 'PM_V7_CI_REPOSITORY="$CI_REPOSITORY"' in bootstrap


def test_london_runtime_staging_and_ci_receipts_are_service_user_owned():
    bootstrap = (ROOT / "ops/v7_london_bootstrap.sh").read_text()
    assert '"$RUNTIME_ROOT"' in bootstrap
    assert '"$RUNTIME_ROOT/by-sha"' in bootstrap
    assert '"$RUNTIME_ROOT/ci-receipts"' in bootstrap
    assert 'sudo install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP"' in bootstrap


def test_public_transport_probe_reconnects_only_after_transport_loss():
    probe = (ROOT / "src/v7_public_paired_clob_transport_probe.cpp").read_text()
    assert "reconnect_if_needed" in probe
    assert "transport.ready()" in probe
    assert "transport.connect(options.ca_file)" in probe
    assert '"transport_reconnects"' in probe
    assert '"transport_reconnect_failures"' in probe
    # The success bar stays unchanged: reconnect prevents one closed socket
    # from poisoning all later samples; it does not weaken sample validity.
    assert "options.samples * 95" in probe


def test_public_transport_probe_classifies_failure_causes():
    probe = (ROOT / "src/v7_public_paired_clob_transport_probe.cpp").read_text()
    for key in (
        "wire_failures", "response_failures", "http_4xx_failures",
        "http_5xx_failures", "timing_failures",
    ):
        assert key in probe


def test_latency_failure_surfaces_bounded_partial_evidence():
    ssm = (ROOT / "ops/v7_london_ssm_benchmark.py").read_text()
    assert 'latency_lab_rc=$lab_rc' in ssm
    assert 'public-paired-clob.json' in ssm
    assert 'tail -c 12000' in ssm
    assert 'exit "$lab_rc"' in ssm


def test_latency_lab_requires_measured_10pct_tail_gate():
    lab = (ROOT / "ops/v7_london_latency_lab.sh").read_text()
    assert 'test["p99"] <= base["p99"]*.90' in lab
    assert 'test["p999"] <= base["p999"]' in lab
    assert "PM_V7_ENABLE_IPO" in (ROOT / "CMakeLists.txt").read_text()
    assert "-fprofile-generate=" in lab
    assert "-fprofile-use=" in lab


def test_no_duplicate_latency_workflow_is_added():
    assert not (ROOT / ".github/workflows/london-latency-lab.yml").exists()


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
