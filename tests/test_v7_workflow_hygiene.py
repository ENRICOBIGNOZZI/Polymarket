import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_prune_workflow_metadata as hygiene


def test_only_active_untracked_workflows_are_stale():
    workflows = [
        {"id": 1, "path": ".github/workflows/ci.yml", "state": "active"},
        {"id": 2, "path": ".github/workflows/v6-retired.yml", "state": "active"},
        {"id": 3, "path": ".github/workflows/old-disabled.yml", "state": "disabled_manually"},
        {"id": 4, "path": "outside/workflows.yml", "state": "active"},
    ]
    assert hygiene.stale_active_workflows(workflows, {".github/workflows/ci.yml"}) == [
        {"id": 2, "path": ".github/workflows/v6-retired.yml", "state": "active"},
    ]


def test_current_workflow_inventory_is_v7_only_and_cleanup_has_actions_scope():
    tracked = hygiene.tracked_workflow_paths(ROOT)
    assert tracked
    assert all(
        Path(path).name in {"ci.yml", "monitoring.yml", "private-runtime-single-writer-validation.yml"}
        or Path(path).name.startswith("v7-")
        for path in tracked
    )
    workflow = (ROOT / ".github/workflows/v7-merged-branch-hygiene.yml").read_text()
    assert "actions: write" in workflow
    assert "scripts/v7_prune_workflow_metadata.py" in workflow


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
    names = (
        "v7-deploy-paper-server.yml",
        "v7-paper-server-health.yml",
        "v7-maker-fillability-evidence.yml",
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
        assert "args: --ephemeral" in workflow
        assert "version: 1.94.2" in workflow
        assert "ping: ${{ env.SERVER_HOST }}" in workflow
