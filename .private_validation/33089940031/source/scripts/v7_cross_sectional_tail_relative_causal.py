#!/usr/bin/env python3
"""Causal current-book wrapper for frozen V7 relative ranking.

The statistical holdout remains owned by ``v7_cross_sectional_tail_relative``.
This wrapper changes only the current executable-book surface: every book used
for the current cross-section must carry exchange timestamp + snapshot hash and
the whole current set must satisfy the shared V7 age/skew contract. Missing or
mixed-time books fail closed rather than creating a synthetic synchronous rank.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import v7_cross_sectional_tail_relative as driver
import v7_model_book_snapshot as snapshots

MARKET_DATA_CONFIG_REL = "config/research_v7_market_data.json"
MARKET_DATA_CONFIG = ROOT / MARKET_DATA_CONFIG_REL
_BOOKS_BY_TOKEN: dict[str, snapshots.CausalBook] = {}
_BOOKS_BY_MARKET: dict[str, tuple[snapshots.CausalBook | None, snapshots.CausalBook | None]] = {}
_PAIR_PROVENANCE: dict[tuple[str, str, int], dict[str, Any]] = {}
_CURRENT_VALIDATION: snapshots.SnapshotValidation | None = None
_GUARD_ATTEMPTS = 0
_GUARD_REJECTIONS = 0
_ORIGINAL_FETCH_BOOKS = driver.base.fetch_books
_ORIGINAL_SELECT = driver.relative.select_relative_pairs
_ORIGINAL_ATOMIC_JSON = driver.atomic_json


def _contract() -> dict[str, Any]:
    cfg = json.loads(MARKET_DATA_CONFIG.read_text(encoding="utf-8"))
    if cfg.get("paper_only") is not True or cfg.get("research_only") is not True:
        raise SystemExit("ranking causal-book config must remain research/PAPER only")
    if cfg.get("authenticated_execution") is not False:
        raise SystemExit("ranking causal-book config cannot enable authenticated execution")
    if cfg.get("require_exchange_timestamp") is not True or cfg.get("require_snapshot_hash") is not True:
        raise SystemExit("ranking causal-book source timestamp/hash are mandatory")
    return cfg


def _validation_args() -> dict[str, int]:
    cfg = _contract()
    return {
        "max_age_ms": max(1, int(float(cfg["maximum_book_age_seconds"]) * 1000.0)),
        "max_exchange_skew_ms": max(0, int(cfg["maximum_cross_book_exchange_skew_ms"])),
        "max_receive_skew_ms": max(0, int(cfg["maximum_cross_book_receive_skew_ms"])),
    }


def _fetch_books(clob: str, markets: list[Any]) -> dict[str, tuple[float, float, float, float]]:
    global _BOOKS_BY_TOKEN, _BOOKS_BY_MARKET, _CURRENT_VALIDATION, _GUARD_ATTEMPTS, _GUARD_REJECTIONS
    cfg = _contract()
    tokens = [token for market in markets for token in (market.yes_token, market.no_token)]
    books = snapshots.fetch_causal_books(
        clob,
        tokens,
        driver.base.request_json,
        batch_size=max(1, int(cfg.get("books_batch_size") or 80)),
    )
    _BOOKS_BY_TOKEN = books
    _BOOKS_BY_MARKET = {
        str(market.market_id): (books.get(market.yes_token), books.get(market.no_token))
        for market in markets
    }
    validation = snapshots.validate_coherent_books(
        books,
        tokens,
        now_ms=time.time_ns() // 1_000_000,
        **_validation_args(),
    )
    _CURRENT_VALIDATION = validation
    _GUARD_ATTEMPTS += 1
    if not validation.ok:
        _GUARD_REJECTIONS += 1
        return {}

    out: dict[str, tuple[float, float, float, float]] = {}
    for market in markets:
        yes_book, no_book = _BOOKS_BY_MARKET.get(str(market.market_id), (None, None))
        if yes_book is None or no_book is None:
            continue
        out[str(market.market_id)] = (
            yes_book.bid,
            yes_book.ask,
            no_book.bid,
            no_book.ask,
        )
    return out


def _pair_snapshot(candidate: Any) -> dict[str, Any] | None:
    top_pair = _BOOKS_BY_MARKET.get(str(candidate.top_market_id))
    bottom_pair = _BOOKS_BY_MARKET.get(str(candidate.bottom_market_id))
    top_yes = top_pair[0] if top_pair else None
    bottom_no = bottom_pair[1] if bottom_pair else None
    if top_yes is None or bottom_no is None:
        return None
    required = [top_yes.token_id, bottom_no.token_id]
    validation = snapshots.validate_coherent_books(
        _BOOKS_BY_TOKEN,
        required,
        now_ms=time.time_ns() // 1_000_000,
        **_validation_args(),
    )
    if not validation.ok:
        return None
    selected = [top_yes, bottom_no]
    return {
        "exchange_ts_ms": min(book.exchange_ts_ms for book in selected),
        "receive_ts_ms": max(book.received_ts_ms for book in selected),
        "decision_ts_ms": time.time_ns() // 1_000_000,
        "book_snapshot_id": validation.snapshot_set_id,
        "snapshot_token_count": validation.token_count,
        "exchange_skew_ms": validation.exchange_skew_ms,
        "receive_skew_ms": validation.receive_skew_ms,
    }


def _select_relative_pairs(*args: Any, **kwargs: Any):
    candidates = _ORIGINAL_SELECT(*args, **kwargs)
    retained = []
    for candidate in candidates:
        provenance = _pair_snapshot(candidate)
        if provenance is None:
            continue
        key = (
            str(candidate.top_market_id),
            str(candidate.bottom_market_id),
            int(candidate.horizon_seconds),
        )
        _PAIR_PROVENANCE[key] = provenance
        retained.append(candidate)
    return retained


def _atomic_json(path: Path, value: Any) -> None:
    if isinstance(value, list):
        rows: list[Any] = []
        for raw in value:
            row = dict(raw) if isinstance(raw, dict) else raw
            if isinstance(row, dict):
                key = (
                    str(row.get("top_market_id") or ""),
                    str(row.get("bottom_market_id") or ""),
                    int(row.get("horizon_seconds") or 0),
                )
                provenance = _PAIR_PROVENANCE.get(key)
                if provenance is not None:
                    row.update(provenance)
            rows.append(row)
        return _ORIGINAL_ATOMIC_JSON(path, rows)
    if isinstance(value, dict):
        value = dict(value)
        value["market_data_config"] = MARKET_DATA_CONFIG_REL
        value["current_cross_section_causal_snapshot_required"] = True
        value["per_pair_causal_snapshot_provenance"] = True
        value["current_book_snapshot_contract"] = {
            "required": True,
            **_validation_args(),
            "guard_attempts": _GUARD_ATTEMPTS,
            "guard_rejections": _GUARD_REJECTIONS,
            "last_validation": asdict(_CURRENT_VALIDATION) if _CURRENT_VALIDATION is not None else None,
        }
    return _ORIGINAL_ATOMIC_JSON(path, value)


def _install() -> None:
    driver.base.fetch_books = _fetch_books
    driver.relative.select_relative_pairs = _select_relative_pairs
    driver.atomic_json = _atomic_json


def _restore() -> None:
    driver.base.fetch_books = _ORIGINAL_FETCH_BOOKS
    driver.relative.select_relative_pairs = _ORIGINAL_SELECT
    driver.atomic_json = _ORIGINAL_ATOMIC_JSON


def main() -> int:
    _contract()
    _install()
    try:
        return driver.main()
    finally:
        _restore()


if __name__ == "__main__":
    raise SystemExit(main())
