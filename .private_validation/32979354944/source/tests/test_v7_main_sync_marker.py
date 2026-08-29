from pathlib import Path


def test_v7_branch_sync_marker_exists():
    assert Path("config/v7_frequency_matrix.json").exists()
