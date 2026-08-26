import json
from pathlib import Path


def test_cleanup_is_deferred_until_after_healthy_same_sha_v7_deploy():
    plan = json.loads(Path("research/v7_unified_integration_plan_20260826.json").read_text())
    directives = json.loads(Path("config/operator_directives.json").read_text())
    retirement = directives["legacy_retirement"]

    assert plan["cleanup_before_main"] is False
    assert plan["integration_order"][-1] == "cleanup_superseded_files_branches_workflows"
    assert directives["architecture"]["cleanup_sequence"] == "unify_validate_deploy_v7_then_delete_all_legacy"
    assert retirement["required"] is True
    assert retirement["gate"] == "v7_merged_to_main_and_exact_sha_validated_and_paper_validated_same_sha_and_deployed_same_sha_and_server_health_green"
    assert "V3/V4/V5/V6" in retirement["before_gate"]
    assert "Do not create new legacy production architecture" in retirement["before_gate"]
    assert "Stop legacy research/runtime schedulers" in retirement["after_gate"]
    assert set(retirement["scope"]) >= {"V3", "V4", "V5", "V6", "migration_adapters", "legacy_workflows", "legacy_tests", "legacy_configs", "legacy_dashboards", "obsolete_research_branches"}


def test_legacy_schedulers_are_migration_only_then_retired():
    directives = json.loads(Path("config/operator_directives.json").read_text())
    assignments = directives["scheduler_assignments"]
    assert "Compatibility/migration evidence only" in assignments["v6-live-data-research"]
    assert "must be retired" in assignments["v6-live-data-research"]
    assert "Compatibility data relay only" in assignments["v6-market-cache-relay"]
    assert "retire this scheduler" in assignments["v6-market-cache-relay"]
    assert "legacy-retirement mode" in assignments["meta-supervisor"]
    assert "stop launching V6 research" in assignments["research-queue"]
