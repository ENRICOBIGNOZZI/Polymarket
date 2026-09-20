from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_current_workflow_inventory_is_final_paper_only():
    names = {p.name for p in (ROOT / ".github/workflows").glob("*.yml")}
    assert names == {
        "ci.yml", "monitoring.yml", "private-runtime-single-writer-validation.yml",
        "v7-deploy-paper-server.yml", "v7-live-paper-validation.yml",
        "v7-paper-server-health.yml", "v7-point-in-time-universe-archive.yml",
        "v7-freeze-maker-forward-window.yml", "v7-public-book-wire-probe.yml",
        "v7-london-aws-provision.yml", "v7-tailscale-oidc-cleanup.yml",
        "v7-deploy-paper-server-aws-oidc.yml",
    }


def test_ci_runs_exact_v7_review_branches_without_enabling_branch_deployment():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text()
    assert 'branches: [main, "codex/v7-*"]' in ci
    assert ci.count("fetch-depth: 0") == 4
    assert "london-runtime-boundary-v7" in ci
    assert "PM_LONDON_RUNTIME_ONLY=ON" in ci
    assert "canonical main does not match the explicitly approved SHA" in deploy


def test_paper_deploy_health_window_covers_crypto_universe_startup():
    workflow = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text()
    assert "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS=390" in workflow
    assert "POLYMARKET_RUNTIME_HEALTH_ATTEMPTS=60" not in workflow


def test_tailnet_workflows_prefer_ephemeral_trust_credentials():
    # The pinned action documents empty statedir as in-memory state.
    # Ephemeral state is not a tailscale up --ephemeral CLI option.
    names = (
        "v7-deploy-paper-server.yml",
        "v7-paper-server-health.yml",
        "v7-point-in-time-universe-archive.yml",
        "v7-freeze-maker-forward-window.yml",
    )
    client_id = "TCb65amTk321CNTRL-kwWnLvpSbF11CNTRL"
    audience_value = "api.tailscale.com/TCb65amTk321CNTRL-kwWnLvpSbF11CNTRL"
    oidc = 'if [[ -n "${TS_OAUTH_CLIENT_ID:-}" && -n "${TS_AUDIENCE:-}" ]]'
    oauth = 'elif [[ -n "${TS_OAUTH_CLIENT_ID:-}" && -n "${TS_OAUTH_SECRET:-}" ]]'
    authkey = 'elif [[ -n "${TS_AUTHKEY:-}" ]]'
    for name in names:
        workflow = (ROOT / ".github/workflows" / name).read_text()
        assert "id-token: write" in workflow
        assert f"TS_OAUTH_CLIENT_ID: {client_id}" in workflow
        assert f"TS_AUDIENCE: {audience_value}" in workflow
        assert oidc in workflow and oauth in workflow and authkey in workflow
        assert workflow.index(oidc) < workflow.index(oauth) < workflow.index(authkey)
        assert f"oauth-client-id: {client_id}" in workflow
        assert f"audience: {audience_value}" in workflow
        assert "--ephemeral" not in workflow
        assert workflow.count("statedir: ''") == workflow.count("uses: tailscale/github-action@") == 3
        assert "version: 1.94.2" in workflow
        assert "ping: ${{ env.SERVER_HOST }}" in workflow


def test_tailscale_oidc_cleanup_is_main_only_and_fail_closed():
    workflow = (ROOT / ".github/workflows/v7-tailscale-oidc-cleanup.yml").read_text()
    script = (ROOT / "scripts/v7_tailscale_oidc_cleanup.py").read_text()
    assert "branches: [main]" in workflow
    assert "id-token: write" in workflow
    assert "oauth/token-exchange" in workflow
    assert "expected_parent_sha" in workflow
    assert 'test "$changed" = "deploy/v7-tailscale-cleanup-request.json"' in workflow
    assert "github-runner" in script
    assert "gh-(?:deploy|health|freeze|inventory)" in script
    assert "maximum_deletions" in script
    assert "offline_ci_devices_remain" in script


def test_deploy_workflow_dispatch_and_linux_cutover_contract():
    workflow = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text()
    assert "name: V7 deploy PAPER server" in workflow
    assert "workflow_dispatch:" in workflow
    assert "remote_platform=\"$(ssh" in workflow
    assert "research/build_runtime_artifacts.sh \"$deploy_sha\"" in workflow
    assert "COLD_START PAPER artifact" in workflow
    linux = workflow.index('if [[ "$(uname -s)" == "Linux" ]]; then')
    stage = workflow.index('v7_london_stage_release.sh', linux)
    cutover = workflow.index('v7_london_cutover.sh', stage)
    assert stage < cutover
    # The detached exact-SHA worktree must still exist for both stage and cutover.
    between = workflow[linux:cutover]
    assert 'git worktree remove --force "$tmp_checkout"' not in between
    recovery = workflow.index('platform="$(uname -s)"')
    darwin_monitoring = workflow.index('if [[ "$platform" == "Darwin" ]]; then', recovery)
    assert workflow.index('http://127.0.0.1:9090/-/ready', darwin_monitoring) > darwin_monitoring
    assert workflow.index('http://127.0.0.1:3000/api/health', darwin_monitoring) > darwin_monitoring
