#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import v7_local_factor_data as v7_data
import v7_model_book_snapshot as snapshots

# The historical research driver imported a V6-named data helper. Current V7
# supplies the same read-only discovery/history interface directly and never
# restores the retired numerical runtime.
sys.modules["v6_local_factor_intents"] = v7_data

import v7_pca_stat_arb_research_base as driver

_BOOKS_BY_TOKEN: dict[str, snapshots.CausalBook] = {}
_BOOKS_BY_MARKET: dict[str, tuple[snapshots.CausalBook | None, snapshots.CausalBook | None]] = {}
_GUARD_ATTEMPTS = 0
_GUARD_REJECTIONS = 0
_LAST_VALIDATION: snapshots.SnapshotValidation | None = None
_SCORE_PROVENANCE: dict[tuple[str, int], dict[str, Any]] = {}
_ORIGINAL_SCORE = driver.inference.score_with_total_single_leg_risk
_ORIGINAL_ATOMIC_JSON = driver.atomic_json


def _ensure_market_data_config() -> None:
    if not any(value == "--paper-config" or value.startswith("--paper-config=") for value in sys.argv[1:]):
        sys.argv.extend(["--paper-config", "config/research_v7_market_data.json"])


def _config_path() -> Path:
    for index, value in enumerate(sys.argv):
        if value == "--config" and index + 1 < len(sys.argv):
            return Path(sys.argv[index + 1])
        if value.startswith("--config="):
            return Path(value.split("=", 1)[1])
    return Path("config/research_v7_pca_stat_arb.json")


def _current_z_floor() -> float:
    cfg = json.loads(_config_path().read_text(encoding="utf-8"))
    pca = cfg["pca"]
    if float(pca.get("minimum_abs_residual_z_after_multiplicity", -1.0)) != 0.0:
        raise SystemExit("historical PCA residual-z gate must be disabled; current-state gate owns admission")
    value = float(pca.get("minimum_abs_current_residual_z_after_multiplicity", 0.0))
    if value <= 0.0:
        raise SystemExit("positive current PCA residual-z gate is required")
    if pca.get("require_common_factor_conditional_mean_forecast") is not True:
        raise SystemExit("PCA single-leg common-factor conditional-mean forecast is required")
    return value


def _fidelity_minutes() -> int:
    cfg = json.loads(_config_path().read_text(encoding="utf-8"))
    value = int(cfg["history"]["fidelity_minutes"])
    if value <= 0:
        raise SystemExit("positive PCA fidelity_minutes required")
    return value


def _fetch_books(clob: str, markets: list[Any]) -> dict[str, snapshots.CausalBook]:
    global _BOOKS_BY_TOKEN, _BOOKS_BY_MARKET
    tokens = [token for market in markets for token in (market.yes, market.no)]
    books = snapshots.fetch_causal_books(clob, tokens, driver.base.request_json)
    _BOOKS_BY_TOKEN = books
    _BOOKS_BY_MARKET = {
        str(market.market_id): (books.get(market.yes), books.get(market.no))
        for market in markets
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


def _score_with_current_state(panel, model, current_logits, horizon_steps):
    global _GUARD_ATTEMPTS, _GUARD_REJECTIONS, _LAST_VALIDATION
    required_markets = (model.target, *model.controls)
    required_tokens: list[str] = []
    for market_id in required_markets:
        pair = _BOOKS_BY_MARKET.get(market_id)
        yes_book = pair[0] if pair else None
        if yes_book is None:
            _GUARD_ATTEMPTS += 1
            _GUARD_REJECTIONS += 1
            _LAST_VALIDATION = snapshots.SnapshotValidation(False, "missing_required_market_book", None, len(required_markets), None, None, None, None)
            return None
        required_tokens.append(yes_book.token_id)
    target_pair = _BOOKS_BY_MARKET.get(model.target)
    target_no = target_pair[1] if target_pair else None
    if target_no is None:
        _GUARD_ATTEMPTS += 1
        _GUARD_REJECTIONS += 1
        _LAST_VALIDATION = snapshots.SnapshotValidation(False, "missing_target_no_execution_book", None, len(required_tokens) + 1, None, None, None, None)
        return None
    required_tokens.append(target_no.token_id)
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
    score = _ORIGINAL_SCORE(panel, model, current_logits, horizon_steps)
    if score is None:
        return None
    if abs(score.residual_z) < _current_z_floor():
        return None
    if getattr(score, "common_factor_forecast_identified", False) is not True:
        return None
    _SCORE_PROVENANCE[(str(model.target), int(horizon_steps))] = _snapshot_provenance(required_tokens, validation)
    return score


def _atomic_json(path, value):
    if isinstance(value, dict):
        value = dict(value)
        fidelity = _fidelity_minutes()
        horizons = []
        for raw_horizon in value.get("horizons") or []:
            horizon = dict(raw_horizon) if isinstance(raw_horizon, dict) else raw_horizon
            if isinstance(horizon, dict):
                horizon_minutes = int(horizon.get("horizon_minutes") or 0)
                steps = horizon_minutes // fidelity if horizon_minutes > 0 and horizon_minutes % fidelity == 0 else 0
                rows = []
                for raw in horizon.get("shadow_candidates") or []:
                    row = dict(raw) if isinstance(raw, dict) else raw
                    if isinstance(row, dict):
                        provenance = _SCORE_PROVENANCE.get((str(row.get("market_id") or ""), steps))
                        if provenance is not None:
                            row.update(provenance)
                    rows.append(row)
                horizon["shadow_candidates"] = rows
            horizons.append(horizon)
        value["horizons"] = horizons
        value["legacy_runtime_dependency"] = False
        value["historical_residual_z_used_for_admission"] = False
        value["current_residual_z_gate"] = _current_z_floor()
        value["common_factor_conditional_mean_forecast_required"] = True
        value["market_data_config"] = "config/research_v7_market_data.json"
        value["operational_paper_config_introduced"] = False
        value["per_candidate_causal_snapshot_provenance"] = True
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
driver.inference.score_with_total_single_leg_risk = _score_with_current_state
driver.atomic_json = _atomic_json


def main() -> int:
    _ensure_market_data_config()
    _current_z_floor()
    return driver.main()


if __name__ == "__main__":
    raise SystemExit(main())
