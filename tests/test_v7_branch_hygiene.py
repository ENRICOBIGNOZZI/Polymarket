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
    assert "unmerged_branches_deleted" in script
    assert "paper-validated" in script
