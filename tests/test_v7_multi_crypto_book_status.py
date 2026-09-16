#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from v7_multi_crypto_book_status import summarize  # noqa: E402

SHA = "a" * 40


def selection() -> dict:
    return {
        "schema": "polymarket_v7_multi_crypto_book_selection_v1",
        "paper_only": True,
        "real_order_submission": False,
        "execution_authority": False,
        "generation_sha256": "b" * 64,
        "markets": [{
            "asset": "ETH", "horizon": "M5", "market_id": "m1",
            "yes_token": "yes", "no_token": "no",
        }],
    }


def book(token: str, *, lineage: bool = True, authority="ZERO_AUTHORITY_RESEARCH_ONLY") -> dict:
    return {
        "model_sha": SHA, "token_id": token,
        "paper_only": True, "authenticated_execution": False,
        "real_order_submission": False, "execution_authority": authority,
        "lineage_continuous": lineage, "valid": lineage,
        "best_bid": 0.49 if lineage else 0.0,
        "best_ask": 0.51 if lineage else 0.0,
    }


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_both_tokens_ready_make_market_ready() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); write(root / "yes.json", book("yes")); write(root / "no.json", book("no"))
        value = summarize(selection(), root, SHA)
        assert value["book_ready"] == 1 and value["feeds_warming"] == 0
        assert value["by_lane"]["ETH/M5"]["BOOK_READY"] == 1
        assert value["execution_authority"] is False


def test_missing_lineage_stays_warming() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); write(root / "yes.json", book("yes")); write(root / "no.json", book("no", lineage=False))
        value = summarize(selection(), root, SHA)
        assert value["book_ready"] == 0 and value["feeds_warming"] == 1
        assert value["markets"][0]["reasons"] == ["PM_BOOK_LINEAGE_WARMING"]


def test_authoritative_book_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); write(root / "yes.json", book("yes", authority="REAL")); write(root / "no.json", book("no"))
        value = summarize(selection(), root, SHA)
        assert value["book_ready"] == 0
        assert "PM_BOOK_AUTHORITY_INVALID" in value["markets"][0]["reasons"]


if __name__ == "__main__":
    tests = sorted((name, fn) for name, fn in globals().items()
                   if name.startswith("test_") and callable(fn))
    for _, fn in tests:
        fn()
    print(f"{len(tests)} function tests passed")
