from pathlib import Path

import ops.v7_direct_action_research_ssm as runner


ROOT = Path(__file__).resolve().parents[1]


def test_direct_action_source_archive_declares_only_existing_regular_files():
    assert runner.SOURCE_PATHS
    for relative in runner.SOURCE_PATHS:
        assert (ROOT / relative).is_file(), relative


def test_direct_action_source_archive_materializes_without_runtime_data():
    payload = runner.source_archive(ROOT)
    assert isinstance(payload, bytes)
    assert len(payload) > 0



def _request(tmp_path, **overrides):
    import json
    value = {
        "schema": "polymarket_v7_direct_action_research_ssm_request_v1",
        "version": 1,
        "request_id": "support-mode-test-20260921",
        "instance_id": "i-0fba2bac9fdc5cbeb",
        "minimum_wall_ns": 1789921800000000000,
        "folds": 3,
        "latency_ms": 50,
        "capital_budget": 10000,
        "output_directory": "docs/research/direct-action-value-2026-09-21",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }
    value.update(overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value))
    return path


def test_direct_action_request_defaults_support_mode_to_diagnostic(tmp_path):
    value = runner.load_request(_request(tmp_path))
    assert value["support_policy_mode"] == "DIAGNOSTIC"


def test_direct_action_request_accepts_robust_support_mode_and_output(tmp_path):
    value = runner.load_request(_request(
        tmp_path,
        support_policy_mode="ROBUST_WORST_CASE",
        output_directory="docs/research/direct-action-support-robust-2026-09-21",
    ))
    assert value["support_policy_mode"] == "ROBUST_WORST_CASE"


def test_direct_action_request_rejects_unknown_support_mode(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="support policy mode"):
        runner.load_request(_request(
            tmp_path, support_policy_mode="MAGIC"))
