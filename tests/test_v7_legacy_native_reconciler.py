from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import v7_legacy_native_reconciler as reconciler
from v7_legacy_native_claims import build_registry

SHA = "a" * 40
TARGET = "b" * 40


def fill() -> dict:
    return {
        "event_type": "FILL", "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "model_sha": SHA, "paper_only": True, "authenticated_execution": False,
        "market_id": "m1", "fill_id": "f1", "order_id": "o1", "position_id": "p1",
        "token_id": "yes", "side": "BUY", "filled_size": 2.0,
        "fill_price": 0.4, "fee": 0.01,
        "metadata": {"component": "crypto_informed_taker", "asset": "BTC", "horizon": "M5",
                     "crypto_context": {"asset": "BTC", "horizon": "M5"},
                     "native_settlement_receipt": {"owner": "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE"}},
    }


def registry(tmp_path: Path) -> dict:
    current = tmp_path / "paper_v7_london_bbbbbbbb"; current.mkdir()
    old = tmp_path / "paper_v7_london_aaaaaaaa"; (old / "ledger").mkdir(parents=True)
    (old / "ledger/execution.jsonl").write_text(json.dumps(fill()) + "\n")
    return build_registry(current_run_root=current, target_sha=TARGET, scan_parent=tmp_path)


def test_groups_are_source_and_sha_bound(tmp_path: Path) -> None:
    value = registry(tmp_path)
    rows = reconciler.groups(value)
    assert len(rows) == 1
    assert rows[0]["model_sha"] == SHA
    assert rows[0]["markets"] == ["m1"]


def test_existing_writer_skips_reconciliation_but_retains_claim(monkeypatch, tmp_path: Path) -> None:
    value = registry(tmp_path)
    monkeypatch.setattr(reconciler, "existing_ledger_writers", lambda: [123])
    report = reconciler.reconcile(value, repository_root=ROOT, target_sha=TARGET)
    assert report["state"] == "SKIPPED_EXISTING_LEDGER_WRITER"
    assert report["initial_claim_microdollars"] == 810_000
    assert report["groups"] == []


def test_resolution_pending_is_not_global_failure(monkeypatch, tmp_path: Path) -> None:
    value = registry(tmp_path)
    monkeypatch.setattr(reconciler, "existing_ledger_writers", lambda: [])

    class Writer:
        def __init__(self): self.returncode = None; self.pid = 99; self.stopped = False
        def poll(self): return None if not self.stopped else self.returncode
        def send_signal(self, _sig): self.stopped = True; self.returncode = 0
        def wait(self, timeout=None): return self.returncode
        def kill(self): self.stopped = True; self.returncode = -9

    writer = Writer()
    monkeypatch.setattr(reconciler.subprocess, "Popen", lambda *a, **k: writer)
    monkeypatch.setattr(reconciler.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        reconciler.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=79, stdout=""),
    )
    report = reconciler.reconcile(value, repository_root=ROOT, target_sha=TARGET)
    assert report["state"] == "COMPLETE"
    assert report["groups"][0]["settlements"] == [{
        "market_id": "m1", "returncode": 79,
        "state": "RESOLUTION_PENDING_CLAIM_RETAINED",
    }]
    assert writer.stopped


def test_runtime_reserves_legacy_claims_but_never_reconciles_old_ledgers() -> None:
    text = (ROOT / "scripts/paper_v7_execution_loop.sh").read_text()
    assert "v7_legacy_native_reconciler.py" not in text
    scan = text.index("v7_legacy_native_claims.py")
    writer = text.index("v7_ledger_spool.py", scan)
    manager = text.index("v7_native_crypto_engine_manager.py", writer)
    assert scan < writer < manager


def test_london_cutover_reconciles_before_new_canonical_writer_starts() -> None:
    text = (ROOT / "ops/v7_london_cutover.sh").read_text()
    prepare = text.index("v7_prepare_cutover_run_root.py")
    scan = text.index("v7_legacy_native_claims.py", prepare)
    reconcile = text.index("v7_legacy_native_reconciler.py", scan)
    rescan = text.index("v7_legacy_native_claims.py", reconcile)
    start = text.index("systemctl enable --now polymarket-v7-paper.service", rescan)
    assert prepare < scan < reconcile < rescan < start
