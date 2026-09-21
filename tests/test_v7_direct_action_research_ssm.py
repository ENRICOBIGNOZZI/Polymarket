from pathlib import Path

import ops.v7_direct_action_research_ssm as runner


ROOT = Path(__file__).resolve().parents[1]


def test_direct_action_source_archive_declares_only_existing_regular_files():
    assert runner.SOURCE_PATHS
    for relative in runner.SOURCE_PATHS:
        path = ROOT / relative
        assert path.is_file(), relative


def test_direct_action_source_archive_materializes_without_runtime_data():
    payload = runner.source_archive(ROOT)
    assert isinstance(payload, bytes)
    assert len(payload) > 0
