#!/usr/bin/env python3
"""Historical PAPER research/recovery primitives. No execution loop or OMS authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

from v7_crypto_settlement import load_registry as load_crypto_registry, require_context
from v7_execution_ledger import LedgerEvent
from v7_external_fair_research import Book, fee_per_share, live_market_yes, parse_book
from v7_ledger_spool import spool_event
from v7_market_common import ClobBooksClient, finite, parse_array, request_json
from v7_opportunity import OpportunityEnvelope
from v7_disk_pressure import disk_pressure_status

SCHEMA = "polymarket_v7_lead_lag_taker_v1_status"
MANIFEST_SCHEMA = "polymarket_v7_lead_lag_taker_v1_forward_manifest"
EVENT_SCHEMA = "polymarket_v7_lead_lag_taker_v1_event"
MODEL_VERSION = "lead-lag-taker-v1-forward"
STRATEGY = "CRYPTO_SETTLEMENT_ENGINE"
COMPONENT = "crypto_informed_taker"
CAPACITY_BOOK_MAX_LEVELS = 2048
CAPACITY_BOOK_MAX_BYTES = 262_144


def capacity_book_evidence(book: Book, schedule: dict[str, Any], candidate_limit: float) -> tuple[dict[str, Any] | None, str | None]:
    """Bounded observational metadata; never truncate a ladder and never affect execution."""
    if len(book.asks) > CAPACITY_BOOK_MAX_LEVELS:
        return None, "TOO_MANY_ASK_LEVELS"
    value = {
        "schema": "polymarket_v7_lead_lag_capacity_book_v1",
        "book_snapshot_id": book.snapshot_id,
        "receive_ts_ms": book.receive_ts_ms,
        "ask_levels": [{"price": price, "size": quantity} for price, quantity in book.asks],
        "fee_schedule": schedule,
        "candidate_limit_price": candidate_limit,
        "observed_ladder_complete_as_received": True,
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > CAPACITY_BOOK_MAX_BYTES:
        return None, "SERIALIZED_CAPACITY_BOOK_TOO_LARGE"
    return value, None


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload); os.fsync(fd)
    finally:
        os.close(fd)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(x) for x in parts).encode()).hexdigest()


def exact_sha(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text)


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema", "version", "strategy_id", "paper_only", "authenticated_execution",
        "real_order_submission", "real_capital_at_risk", "research_only",
        "automatic_promotion", "asset", "horizon", "source_rule_id",
        "source_rule_sha256", "shock_source", "confirmation_source", "confirmation",
        "minimum_absolute_binance_return_bp", "minimum_tte_seconds",
        "maximum_tte_seconds", "maximum_signal_age_ms", "target_shares",
        "one_entry_per_market", "hold_to_settlement", "entry_side_rule",
        "order_type", "price_rule", "require_full_visible_depth",
        "coordinator_receipt_timeout_ms", "candidate_ttl_ms", "fast_poll_ms",
        "settlement_poll_ms", "forward_test",
    }
    if set(value) != required:
        raise RuntimeError("lead_lag_config_field_partition")
    if (value["schema"] != "polymarket_v7_lead_lag_taker_config_v1" or value["version"] != 1
            or value["strategy_id"] != "LEAD_LAG_TAKER_V1"):
        raise RuntimeError("lead_lag_config_identity")
    if not (value["paper_only"] is True and value["authenticated_execution"] is False
            and value["real_order_submission"] is False and value["real_capital_at_risk"] is False
            and value["research_only"] is True and value["automatic_promotion"] is False):
        raise RuntimeError("lead_lag_config_safety")
    if value["asset"] != "BTC" or value["horizon"] != "M5":
        raise RuntimeError("lead_lag_config_scope")
    if value["entry_side_rule"] != "UP_BUY_YES_DOWN_BUY_NO" or value["price_rule"] != "ARRIVAL_BEST_ASK_NO_CHASE":
        raise RuntimeError("lead_lag_config_rule")
    if value["one_entry_per_market"] is not True or value["hold_to_settlement"] is not True:
        raise RuntimeError("lead_lag_config_lifecycle")
    if value["require_full_visible_depth"] is not True:
        raise RuntimeError("lead_lag_config_depth")
    if not (0 < float(value["minimum_tte_seconds"]) < float(value["maximum_tte_seconds"]) <= 300):
        raise RuntimeError("lead_lag_config_tte")
    if not (0 < int(value["maximum_signal_age_ms"]) <= 10_000 and float(value["target_shares"]) > 0):
        raise RuntimeError("lead_lag_config_signal_or_size")
    forward = value["forward_test"]
    if not isinstance(forward, dict) or set(forward) != {
        "target_independent_markets", "minimum_positive_windows_before_canary",
        "automatic_promotion", "no_runtime_tuning",
    }:
        raise RuntimeError("lead_lag_config_forward")
    if int(forward["target_independent_markets"]) < 1 or forward["automatic_promotion"] is not False or forward["no_runtime_tuning"] is not True:
        raise RuntimeError("lead_lag_config_forward_contract")
    return value


def adjusted_tte(status: dict[str, Any], current_ns: int) -> float | None:
    cut = status.get("causal_observation") if isinstance(status.get("causal_observation"), dict) else {}
    cut = cut.get("cut") if isinstance(cut.get("cut"), dict) else {}
    fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
    raw = finite(cut.get("observed_tte_seconds"), math.nan)
    observed = int(cut.get("observed_wall_ns") or 0)
    if not math.isfinite(raw):
        raw = finite(fair.get("tte_seconds"), math.nan)
        observed = int(fair.get("calculated_wall_ns") or 0)
    if not math.isfinite(raw) or raw < 0:
        return None
    elapsed = max(0.0, (current_ns - observed) / 1e9) if 0 < observed <= current_ns else 0.0
    return max(0.0, raw - elapsed)


def signal_candidate(config: dict[str, Any], signal: dict[str, Any], status: dict[str, Any], *, model_sha: str, current_ns: int) -> tuple[dict[str, Any] | None, str]:
    if (signal.get("schema") != "polymarket_v7_btc_m5_external_cancel_live_signal_v2"
            or signal.get("code_sha") != model_sha
            or signal.get("rule_id") != config["source_rule_id"]
            or signal.get("rule_sha256") != config["source_rule_sha256"]
            or signal.get("paper_only") is not True
            or signal.get("authenticated_execution") is not False
            or signal.get("real_order_submission") is not False
            or signal.get("real_money_authority") is not False
            or signal.get("research_only") is not True
            or signal.get("shock_source") != config["shock_source"]
            or signal.get("confirmation_source") != config["confirmation_source"]
            or signal.get("confirmation") != config["confirmation"]
            or signal.get("confirmed_non_opposing") is not True):
        return None, "SIGNAL_CONTRACT_INVALID"
    version = int(signal.get("signal_version") or 0)
    trigger_ns = int(signal.get("trigger_receive_wall_ns") or 0)
    if version <= 0 or trigger_ns <= 0 or trigger_ns > current_ns:
        return None, "SIGNAL_CLOCK_INVALID"
    age_ms = (current_ns - trigger_ns) / 1e6
    if age_ms > int(config["maximum_signal_age_ms"]):
        return None, "SIGNAL_TOO_OLD"
    bp = finite(signal.get("binance_return_100ms_bp"), math.nan)
    if not math.isfinite(bp) or abs(bp) + 1e-12 < float(config["minimum_absolute_binance_return_bp"]):
        return None, "SIGNAL_TOO_WEAK"
    direction = str(signal.get("direction") or "")
    if direction not in {"UP", "DOWN"}:
        return None, "SIGNAL_DIRECTION_INVALID"
    if (status.get("code_sha") != model_sha or status.get("paper_only") is not True
            or status.get("authenticated_execution") is not False
            or status.get("real_order_submission") is not False):
        return None, "STATUS_IDENTITY_INVALID"
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
    reference = status.get("settlement_reference") if isinstance(status.get("settlement_reference"), dict) else {}
    if (not str(market.get("market_id") or "") or market.get("accepting_orders") is not True
            or market.get("closed") is True or contract.get("verified") is not True
            or contract.get("rules_hash_recognized") is not True or reference.get("valid") is not True):
        return None, "MARKET_OR_SETTLEMENT_NOT_READY"
    tte = adjusted_tte(status, current_ns)
    if tte is None or not (float(config["minimum_tte_seconds"]) <= tte <= float(config["maximum_tte_seconds"])):
        return None, "TTE_OUTSIDE_FROZEN_WINDOW"
    outcome = "YES" if direction == "UP" else "NO"
    token = str(market.get("yes_token") if outcome == "YES" else market.get("no_token") or "")
    if not token:
        return None, "TOKEN_MISSING"
    return {
        "signal_version": version, "trigger_wall_ns": trigger_ns, "signal_age_ms": age_ms,
        "direction": direction, "outcome": outcome, "token_id": token,
        "market_id": str(market["market_id"]), "event_id": str(market.get("event_id") or ""),
        "tte_seconds": tte, "binance_return_100ms_bp": bp,
        "coinbase_return_100ms_bp": finite(signal.get("coinbase_return_100ms_bp"), math.nan),
    }, "ELIGIBLE"
