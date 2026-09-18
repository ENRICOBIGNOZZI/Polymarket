from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_legacy_native_claims import build_registry, validate_registry
from v7_native_crypto_engine_manager import _budget_after_legacy_claims

SHA = "a" * 40
TARGET = "b" * 40


def fill() -> dict:
    return {
        "event_type": "FILL",
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "market_id": "m1",
        "fill_id": "f1",
        "order_id": "o1",
        "position_id": "p1",
        "token_id": "yes",
        "side": "BUY",
        "filled_size": 2.0,
        "fill_price": 0.4,
        "fee": 0.01,
        "metadata": {
            "component": "crypto_informed_taker",
            "model_family": "crypto_informed_taker",
            "asset": "BTC",
            "horizon": "M5",
            "crypto_context": {"asset": "BTC", "horizon": "M5"},
            "run_id": "old-run",
            "native_settlement_receipt": {
                "owner": "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE",
                "model_sha": SHA,
                "single_owner": True,
            },
        },
    }


def final() -> dict:
    return {
        "event_type": "FINAL",
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "market_id": "m1",
        "final_pnl": 1.19,
        "realized_cashflow": 2.0,
        "metadata": {
            "native_market_settlement_id": "native-settlement:m1",
            "included_fill_ids": ["f1"],
            "winning_token_id": "yes",
            "settlement_payouts": {"yes": 1.0, "no": 0.0},
            "run_id": "old-run",
        },
    }


def write_ledger(root: Path, rows: list[dict]) -> Path:
    ledger = root / "ledger/execution.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return ledger


def test_previous_run_claim_is_reserved_and_hash_bound(tmp_path: Path) -> None:
    current = tmp_path / "paper_v7_london_bbbbbbbb"
    current.mkdir()
    old = tmp_path / "paper_v7_london_aaaaaaaa"
    write_ledger(old, [fill()])
    registry = build_registry(current_run_root=current, target_sha=TARGET, scan_parent=tmp_path)
    assert registry["total_claim_microdollars"] == 810_000
    assert len(registry["sources"]) == 1
    assert registry["sources"][0]["claims"] == [{
        "model_sha": SHA, "market_id": "m1", "claim_microdollars": 810_000,
    }]
    assert validate_registry(registry, target_sha=TARGET) == registry


def test_canonical_final_releases_claim_on_rescan(tmp_path: Path) -> None:
    current = tmp_path / "paper_v7_london_bbbbbbbb"; current.mkdir()
    old = tmp_path / "paper_v7_london_aaaaaaaa"
    write_ledger(old, [fill(), final()])
    registry = build_registry(current_run_root=current, target_sha=TARGET, scan_parent=tmp_path)
    assert registry["total_claim_microdollars"] == 0
    assert registry["sources"] == []


def test_registry_tamper_fails_closed(tmp_path: Path) -> None:
    current = tmp_path / "paper_v7_london_bbbbbbbb"; current.mkdir()
    old = tmp_path / "paper_v7_london_aaaaaaaa"; write_ledger(old, [fill()])
    registry = build_registry(current_run_root=current, target_sha=TARGET, scan_parent=tmp_path)
    registry["total_claim_microdollars"] -= 1
    with pytest.raises(ValueError):
        validate_registry(registry, target_sha=TARGET)


def test_manager_budget_is_net_of_legacy_claim(tmp_path: Path) -> None:
    current = tmp_path / "paper_v7_london_bbbbbbbb"; current.mkdir()
    old = tmp_path / "paper_v7_london_aaaaaaaa"; write_ledger(old, [fill()])
    registry = build_registry(current_run_root=current, target_sha=TARGET, scan_parent=tmp_path)
    path = tmp_path / "legacy.json"; path.write_text(json.dumps(registry))
    net, claim, digest = _budget_after_legacy_claims(10_000_000_000, path, TARGET)
    assert claim == 810_000
    assert net == 9_999_190_000
    assert digest == registry["registry_sha256"]


def test_claim_cannot_exhaust_or_increase_budget(tmp_path: Path) -> None:
    current = tmp_path / "paper_v7_london_bbbbbbbb"; current.mkdir()
    old = tmp_path / "paper_v7_london_aaaaaaaa"; write_ledger(old, [fill()])
    registry = build_registry(current_run_root=current, target_sha=TARGET, scan_parent=tmp_path)
    path = tmp_path / "legacy.json"; path.write_text(json.dumps(registry))
    with pytest.raises(RuntimeError):
        _budget_after_legacy_claims(800_000, path, TARGET)
