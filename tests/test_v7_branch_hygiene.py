import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_prune_merged_branches as hygiene


def test_protected_branches_and_unmerged_refs_are_never_delete_candidates(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    (tmp_path / "x").write_text("base")
    subprocess.run(["git", "-C", str(tmp_path), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "branch", "-M", "main"], check=True)
    for branch in ("main", "paper-validated", "release/keep", "merged/topic"):
        subprocess.run(["git", "-C", str(tmp_path), "update-ref", f"refs/remotes/origin/{branch}", "HEAD"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-qb", "unmerged"], check=True)
    (tmp_path / "x").write_text("unique")
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qam", "unique"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "update-ref", "refs/remotes/origin/unmerged", "HEAD"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "main"], check=True)
    assert hygiene.merged_remote_branches(tmp_path, "origin", "main") == ["merged/topic"]


def test_workflow_has_write_scope_and_ancestry_cleanup_only():
    workflow = (ROOT / ".github/workflows/v7-merged-branch-hygiene.yml").read_text()
    script = (ROOT / "scripts/v7_prune_merged_branches.py").read_text()
    assert "contents: write" in workflow
    assert "--merged=" in script
    assert "--include-merged-pr-branches" in workflow
    assert "--include-closed-operational-branches" in workflow
    assert "GITHUB_TOKEN" in workflow
    assert "unmerged_branches_deleted" in script
    assert "paper-validated" in script


def test_merged_pr_cleanup_protects_open_heads_bases_and_unmerged_work():
    repository = "ENRICOBIGNOZZI/Polymarket"
    branches = {
        "main": "main-sha",
        "paper-validated": "main-sha",
        "telemetry": "telemetry-sha",
        "merged/topic": "merged-sha",
        "merged/alias": "merged-sha",
        "shared/stack-base": "stack-sha",
        "open/topic": "open-sha",
        "closed/research": "closed-sha",
        "fork/head": "fork-sha",
        "tmp-unused-4": "temporary-sha",
    }
    same_repo = {"full_name": repository}
    pulls = [
        {
            "state": "closed",
            "merged_at": "2026-08-29T00:00:00Z",
            "head": {"ref": "merged/topic", "sha": "merged-sha", "repo": same_repo},
            "base": {"ref": "main"},
        },
        {
            "state": "closed",
            "merged_at": "2026-08-29T00:00:00Z",
            "head": {"ref": "shared/stack-base", "sha": "stack-sha", "repo": same_repo},
            "base": {"ref": "main"},
        },
        {
            "state": "open",
            "merged_at": None,
            "head": {"ref": "open/topic", "sha": "open-sha", "repo": same_repo},
            "base": {"ref": "shared/stack-base"},
        },
        {
            "state": "closed",
            "merged_at": None,
            "head": {"ref": "closed/research", "sha": "closed-sha", "repo": same_repo},
            "base": {"ref": "main"},
        },
        {
            "state": "closed",
            "merged_at": "2026-08-29T00:00:00Z",
            "head": {"ref": "fork/head", "sha": "fork-sha", "repo": {"full_name": "other/fork"}},
            "base": {"ref": "main"},
        },
    ]
    assert hygiene.merged_pr_branches(branches, pulls, repository) == [
        "merged/alias",
        "merged/topic",
        "tmp-unused-4",
    ]


def test_telemetry_is_protected():
    assert hygiene.is_protected_branch("telemetry")


def test_closed_operational_cleanup_preserves_research_and_open_stacks():
    repository = "ENRICOBIGNOZZI/Polymarket"
    same_repo = {"full_name": repository}
    branches = {
        "fix/superseded": "fix-sha",
        "integration/obsolete": "integration-sha",
        "research/unique": "research-sha",
        "fix/open-base": "base-sha",
        "research/open-head": "open-sha",
    }
    pulls = [
        {"state": "closed", "merged_at": None, "head": {"ref": "fix/superseded", "repo": same_repo}, "base": {"ref": "main"}},
        {"state": "closed", "merged_at": None, "head": {"ref": "integration/obsolete", "repo": same_repo}, "base": {"ref": "main"}},
        {"state": "closed", "merged_at": None, "head": {"ref": "research/unique", "repo": same_repo}, "base": {"ref": "main"}},
        {"state": "closed", "merged_at": None, "head": {"ref": "fix/open-base", "repo": same_repo}, "base": {"ref": "main"}},
        {"state": "open", "merged_at": None, "head": {"ref": "research/open-head", "repo": same_repo}, "base": {"ref": "fix/open-base"}},
    ]
    assert hygiene.closed_operational_pr_branches(branches, pulls, repository) == [
        "fix/superseded",
        "integration/obsolete",
    ]
