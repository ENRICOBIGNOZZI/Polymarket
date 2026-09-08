from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_current_workflow_inventory_is_final_paper_only():
    names = {p.name for p in (ROOT / ".github/workflows").glob("*.yml")}
    assert names == {
        "ci.yml", "monitoring.yml", "private-runtime-single-writer-validation.yml",
        "v7-deploy-paper-server.yml", "v7-live-paper-validation.yml",
        "v7-paper-server-health.yml", "v7-point-in-time-universe-archive.yml",
    }


def test_ci_runs_exact_v7_review_branches_without_enabling_branch_deployment():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    deploy = (ROOT / ".github/workflows/v7-deploy-paper-server.yml").read_text()
    assert 'branches: [main, "codex/v7-*"]' in ci
    assert ci.count("fetch-depth: 0") == 3
    assert "canonical main does not match the explicitly approved SHA" in deploy


def test_paper_deploy_health_window_covers_exhaustive_universe_startup():
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
    )
    oidc = 'if [[ -n "${TS_OAUTH_CLIENT_ID:-}" && -n "${TS_AUDIENCE:-}" ]]'
    oauth = 'elif [[ -n "${TS_OAUTH_CLIENT_ID:-}" && -n "${TS_OAUTH_SECRET:-}" ]]'
    authkey = 'elif [[ -n "${TS_AUTHKEY:-}" ]]'
    for name in names:
        workflow = (ROOT / ".github/workflows" / name).read_text()
        assert "id-token: write" in workflow
        assert "TS_AUDIENCE: ${{ secrets.TS_AUDIENCE }}" in workflow
        assert oidc in workflow and oauth in workflow and authkey in workflow
        assert workflow.index(oidc) < workflow.index(oauth) < workflow.index(authkey)
        assert "audience: ${{ secrets.TS_AUDIENCE }}" in workflow
        assert "--ephemeral" not in workflow
        assert workflow.count("statedir: ''") == workflow.count("uses: tailscale/github-action@") == 3
        assert "version: 1.94.2" in workflow
        assert "ping: ${{ env.SERVER_HOST }}" in workflow
