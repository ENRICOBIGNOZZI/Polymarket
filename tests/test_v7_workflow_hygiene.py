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
    assert hygiene.stale_active_workflows(workflows, {".github/workflows/ci.yml