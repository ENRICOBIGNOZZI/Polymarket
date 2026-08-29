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
