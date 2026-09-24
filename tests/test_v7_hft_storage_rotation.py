from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"monitoring"))
import v7_hft_data_health as health


def test_directory_rotation_is_explicit_not_a_probe_crash(tmp_path,monkeypatch):
    (tmp_path/"current.jsonl").write_text("a\n")
    def walk(root,onerror,followlinks):
        assert followlinks is False
        onerror(FileNotFoundError(2,"rotated",str(root/"sealed")))
        yield str(root),[],["current.jsonl"]
    monkeypatch.setattr(health.os,"walk",walk)
    receipt=health.snapshot(tmp_path)
    assert receipt["total_bytes"]==2
    assert receipt["vanished_directory_count"]==1 and receipt["snapshot_atomic"] is False


def test_permission_error_is_not_silently_zero_storage(tmp_path,monkeypatch):
    def walk(root,onerror,followlinks):
        onerror(PermissionError("unreadable"))
        yield str(root),[],[]
    monkeypatch.setattr(health.os,"walk",walk)
    with pytest.raises(PermissionError):health.snapshot(tmp_path)


def test_missing_storage_root_is_not_a_healthy_empty_sample(tmp_path):
    with pytest.raises(FileNotFoundError):health.snapshot(tmp_path/"missing")
