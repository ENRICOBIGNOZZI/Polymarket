from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v7_consolidation_keeps_canonical_singleton_surfaces() -> None:
    selector = (ROOT / "scripts" / "paper_latest_loop.sh").read_text(encoding="utf-8")
    updater = (ROOT / "ops" / "update_server_macos.sh").read_text(encoding="utf-8")
    assert "runtime_singleton_launcher.py" in selector
    assert "runtime_owner.lock" in selector
    assert "runtime_handoff.request" in selector
    assert "start_champion" in selector
    assert "polymarket_fast_arb_shadow" in selector
    assert "request_runtime_handoff()" in updater
    assert "clear_runtime_handoff()" in updater
