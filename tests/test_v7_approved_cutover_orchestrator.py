from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/v7-approved-paper-cutover-orchestrator.yml"


def test_orchestrator_requires_explicit_owner_merged_pr_approval():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.event.pull_request.merged == true" in text
    assert "github.event.pull_request.user.login == github.repository_owner" in text
    assert "V7 PAPER CUTOVER APPROVED:" in text
    assert "github.event.pull_request.merge_commit_sha" in text


def test_orchestrator_uses_canonical_workflows_without_direct_deploy_or_ref_patch():
    text = WORKFLOW.read_text(encoding="utf-8")
    for workflow in (
        "ci.yml", "monitoring.yml", "private-runtime-single-writer-validation.yml",
        "v7-live-paper-validation.yml", "v7-deploy-paper-server.yml",
        "v7-paper-server-health.yml",
    ):
        assert f"gh workflow run {workflow}" in text
    assert "cutover_approved=true" in text
    assert "git/refs/heads/paper-validated" not in text
    assert "ssh " not in text
    assert "real_order_submission" not in text


def test_single_writer_accepts_exact_sha_dispatch_input():
    text = (ROOT / ".github/workflows/private-runtime-single-writer-validation.yml").read_text()
    assert "expected_sha:" in text
    assert "github.event.inputs.expected_sha" in text


def test_orchestrator_requires_exact_sha_health_after_deploy_before_cleanup():
    text = WORKFLOW.read_text(encoding="utf-8")
    deploy = text.index("- name: Dispatch canonical PAPER server deploy")
    health = text.index("- name: Dispatch exact-SHA PAPER server health")
    cleanup = text.index("- name: Start proven-merged branch cleanup")
    assert deploy < health < cleanup
    assert 'expected_sha="$TARGET_SHA"' in text[health:cleanup]
    assert "paper_health_run_id=" in text[health:cleanup]


def test_orchestrator_health_step_is_structurally_uncorrupted():
    text = WORKFLOW.read_text(encoding="utf-8")
    health = text[text.index("- name: Dispatch exact-SHA PAPER server health"):]
    assert "IFS=$'\\t' read -r run_id state conclusion" in health
    assert text.count("- name: Start proven-merged branch cleanup") == 1
    assert health.index("paper_health_run_id=") < health.index("- name: Start proven-merged branch cleanup")


def test_orchestrator_never_reuses_historical_exact_sha_runs():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "CI_PRIOR_RUN_ID=" in text
    assert "MONITORING_PRIOR_RUN_ID=" in text
    assert "SINGLE_WRITER_PRIOR_RUN_ID=" in text
    assert text.count(".id > $prior_run_id") == 4
    assert text.count("prior_run_id=\"$(gh api") == 3
    assert 'wait_for_workflow ci.yml ci "$CI_PRIOR_RUN_ID"' in text
