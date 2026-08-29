import json
from pathlib import Path


def test_cleanup_is_deferred_until_after_main_integration():
    plan=json.loads(Path('research/v7_unified_integration_plan_20260826.json').read_text())
    assert plan['cleanup_before_main'] is False
    assert plan['integration_order'][-1]=='cleanup_superseded_files_branches_workflows'
