from __future__ import annotations

import sys
import tempfile
from collections import namedtuple
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_disk_pressure as guard


Usage = namedtuple("Usage", "total used free")


def test_direct_low_disk_blocks_without_marker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "runs/paper_v7_live"
        root.mkdir(parents=True)
        threshold = guard.NEW_RISK_MIN_FREE_BYTES
        assert threshold is not None
        free = threshold - 1
        total = 500 * 1024**3
        with mock.patch.object(guard.shutil, "disk_usage", return_value=Usage(total, total-free, free)):
            state = guard.disk_pressure_status(root)
        assert state["active"] is True
        assert state["marker_present"] is False
        assert state["reason"] == "DISK_HEADROOM_BELOW_SAFE_THRESHOLD"


def test_marker_keeps_gate_closed_even_with_headroom() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        marker = root / "control/DISK_PRESSURE"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        total = 500 * 1024**3
        free = 100 * 1024**3
        with mock.patch.object(guard.shutil, "disk_usage", return_value=Usage(total, total-free, free)):
            state = guard.disk_pressure_status(root)
        assert state["active"] is True
        assert state["marker_present"] is True
        assert state["reason"] == "DISK_PRESSURE_MARKER"


def test_healthy_disk_without_marker_is_open() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "runs/paper_v7_live"
        root.mkdir(parents=True)
        total = 500 * 1024**3
        free = 100 * 1024**3
        with mock.patch.object(guard.shutil, "disk_usage", return_value=Usage(total, total-free, free)):
            state = guard.disk_pressure_status(root)
        assert state["active"] is False
        assert state["reason"] == "HEADROOM_OK"


if __name__ == "__main__":
    test_direct_low_disk_blocks_without_marker()
    test_marker_keeps_gate_closed_even_with_headroom()
    test_healthy_disk_without_marker_is_open()
