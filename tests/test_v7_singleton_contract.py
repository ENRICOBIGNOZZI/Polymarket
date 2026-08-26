from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v7_champion_plane_stays_under_canonical_runtime_singleton() -> None:
    selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
    updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
    v7 = (ROOT / "scripts" / "paper_v7_loop.sh").read_text(encoding="utf-8")
    assert "runtime_singleton_launcher.py" in selector
    assert "runtime_owner.lock" in selector
    assert "runtime_handoff.request" in selector
    assert "request_runtime_handoff()" in updater
    assert "clear_runtime_handoff()" in updater
    assert "runtime_singleton_launcher.py" not in v7
    assert "runtime_owner.lock" not in v7
