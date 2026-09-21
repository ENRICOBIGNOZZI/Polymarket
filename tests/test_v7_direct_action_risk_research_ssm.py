from pathlib import Path

import ops.v7_direct_action_risk_research_ssm as runner


ROOT = Path(__file__).resolve().parents[1]


def test_direct_action_risk_source_archive_declares_existing_files_only():
    assert runner.SOURCE_PATHS
    for relative in runner.SOURCE_PATHS:
        assert (ROOT / relative).is_file(), relative


def test_direct_action_risk_source_archive_materializes():
    payload = runner.source_archive(ROOT)
    assert isinstance(payload, bytes)
    assert len(payload) > 0
