from pathlib import Path

import ops.v7_maker_value_challenger_ssm as runner


ROOT = Path(__file__).resolve().parents[1]


def test_maker_value_source_archive_declares_only_existing_files():
    assert runner.SOURCE_PATHS
    for relative in runner.SOURCE_PATHS:
        assert (ROOT / relative).is_file(), relative


def test_maker_value_request_is_paper_only(tmp_path):
    request = {
        "schema": "polymarket_v7_maker_value_challenger_ssm_request_v1",
        "version": 1,
        "request_id": "maker-value-test-20260921",
        "instance_id": "i-0fba2bac9fdc5cbeb",
        "markout_horizon": "1s",
        "bootstrap_samples": 3000,
        "output_directory": "docs/research/maker-value-2026-09-21",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }
    path = tmp_path / "request.json"
    path.write_text(__import__("json").dumps(request))
    loaded = runner.load_request(path)
    assert loaded == request


def test_maker_value_request_rejects_execution_authority(tmp_path):
    request = {
        "schema": "polymarket_v7_maker_value_challenger_ssm_request_v1",
        "version": 1,
        "request_id": "maker-value-test-20260921",
        "instance_id": "i-0fba2bac9fdc5cbeb",
        "markout_horizon": "1s",
        "bootstrap_samples": 3000,
        "output_directory": "docs/research/maker-value-2026-09-21",
        "paper_only": True,
        "authenticated_execution": True,
        "real_order_submission": False,
        "real_capital_at_risk": False,
    }
    path = tmp_path / "request.json"
    path.write_text(__import__("json").dumps(request))
    import pytest
    with pytest.raises(ValueError, match="PAPER-only"):
        runner.load_request(path)
