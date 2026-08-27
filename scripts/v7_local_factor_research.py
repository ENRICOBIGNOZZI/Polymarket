#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from dataclasses import asdict
from typing import Any

import v7_local_factor_core as core
import v7_local_factor_research_base as driver
import v7_model_book_snapshot as snapshots

_BOOKS_BY_TOKEN: dict[str, snapshots.CausalBook] = {}
_YES_BOOK_BY_MARKET: dict[str, snapshots.CausalBook] = {}
_GUARD_ATTEMPTS = 0
_GUARD_REJECTIONS = 0
_LAST_VALIDATION: snapshots.SnapshotValidation | None = None
_SIGNAL_PROVENANCE: dict[tuple[str, str], dict[str, Any]] = {}
_ORIGINAL_BUILD_PAIR_SIGNAL = core.build_pair_signal
_ORIGINAL_ATOMIC_JSON = driver.atomic_json


def _ensure_market_data_config() -> None:
    if not any(value == "--paper-config" or value.startswith("--paper-config=") for value in sys.argv[1:]):
        sys.argv.extend(["--paper-config", "config/research_v7_market_data.json"])


def _fetch_books(clob: str, markets: list[Any]) -> dict[str, snapshots.CausalBook]:
    global _BOOKS_BY_TOKEN, _YES_BOOK_BY_MARKET
    tokens = [token for market in markets for token in (market.yes, market.no)]
    books = snapshots.fetch_causal_books(clob, tokens, driver.base.request_json)
    _BOOKS_BY_TOKEN = books
    _YES_BOOK_BY_MARKET = {
        str(market.market_id): books[market.yes]
        for market in markets
        if market.yes in books
    }
    return books


def _snapshot_provenance(required_tokens: list[str], validation: snapshots.SnapshotValidation) -> dict[str, Any]:
    selected = [_BOOKS_BY_TOKEN[token] for token in required_tokens]
    return {
        "exchange_ts_ms": min(book.exchange_ts_ms for book in selected),
        "receive_ts_ms": max(book.received_ts_ms for book in selected),
        "decision_ts_ms": time.time_ns() // 1_000_000,
        "book_snapshot_id": validation.snapshot_set_id,
        "snapshot_token_count": validation.token_count,
        "exchange_skew_ms": validation.exchange_skew_ms,
        "receive_skew_ms": validation.receive_skew_ms,
    }


def _build_pair_signal(fit, pvalue, _target_only_probabilities, yes_scales, *args, **kwargs):
    global _GUARD_ATTEMPTS, _GUARD_REJECTIONS, _LAST_VALIDATION
    required_markets = (fit.market_a, fit.market_b, *fit.controls)
    if any(market_id not in _YES_BOOK_BY_MARKET for market_id in required_markets):
        _GUARD_ATTEMPTS += 1
        _GUARD_REJECTIONS += 1
        _LAST_VALIDATION = snapshots.SnapshotValidation(False, "missing_required_market_book", None, len(required_markets), None, None, None, None)
        return None
    required_tokens = [_YES_BOOK_BY_MARKET[market_id].token_id for market_id in required_markets]
    validation = snapshots.validate_coherent_books(
        _BOOKS_BY_TOKEN,
        required_tokens,
        now_ms=time.time_ns() // 1_000_000,
        max_age_ms=5_000,
        max_exchange_skew_ms=1_500,
        max_receive_skew_ms=1_500,
    )
    _GUARD_ATTEMPTS += 1
    _LAST_VALIDATION = validation
    if not validation.ok:
        _GUARD_REJECTIONS += 1
        return None
    probabilities = {market_id: _YES_BOOK_BY_MARKET[market_id].mid for market_id in required_markets}
    signal = _ORIGINAL_BUILD_PAIR_SIGNAL(fit, pvalue, probabilities, yes_scales, *args, **kwargs)
    if signal is not None:
        _SIGNAL_PROVENANCE[(str(fit.market_a), str(fit.market_b))] = _snapshot_provenance(required_tokens, validation)
    return signal


def _atomic_json(path, value):
    if isinstance(value, dict):
        value = dict(value)
        signals = []
        for raw in value.get("signals") or []:
            row = dict(raw) if isinstance(raw, dict) else raw
            if isinstance(row, dict):
                key = (str(row.get("market_a") or ""), str(row.get("market_b") or ""))
                provenance = _SIGNAL_PROVENANCE.get(key)
                if provenance is not None:
                    row.update(provenance)
            signals.append(row)
        value["signals"] = signals
        value["current_residual_reconstructed_from_frozen_controls"] = True
        value["market_data_config"] = "config/research_v7_market_data.json"
        value["operational_paper_config_introduced"] = False
        value["per_signal_causal_snapshot_provenance"] = True
        value["current_book_snapshot_contract"] = {
            "required": True,
            "max_age_ms": 5000,
            "max_exchange_skew_ms": 1500,
            "max_receive_skew_ms": 1500,
            "guard_attempts": _GUARD_ATTEMPTS,
            "guard_rejections": _GUARD_REJECTIONS,
            "last_validation": asdict(_LAST_VALIDATION) if _LAST_VALIDATION is not None else None,
        }
    return _ORIGINAL_ATOMIC_JSON(path, value)


driver.base.fetch_books = _fetch_books
driver.core.build_pair_signal = _build_pair_signal
driver.atomic_json = _atomic_json


def main() -> int:
    _ensure_market_data_config()
    return driver.main()


if __name__ == "__main__":
    raise SystemExit(main())
