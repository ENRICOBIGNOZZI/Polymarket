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
