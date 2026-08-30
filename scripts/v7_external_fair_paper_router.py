#!/usr/bin/env python3
"""Collect settlement-aware External Fair counterfactuals without execution.

Only public CLOB data is used.  Every decision is revalidated on a fresh L2
arrival snapshot, fees are taken from the contract-bound schedule, and virtual
FAK fills are limited by visible depth. Evidence is written to an append-only
counterfactual tape and never reaches the execution ledger or portfolio cash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from v7_market_common import finite, parse_array, request_json

STRATEGY = "CRYPTO_INFORMED_TAKER"
MODEL_VERSION = "external-fair-structural-v7-paper"
HORIZONS = (1, 10, 45, 60, 300)
FORECAST_TTE_BUCKETS = (240, 180, 120, 90, 60, 45, 30, 20, 15, 10, 5)
FORECAST_BUCKET_TOLERANCE_SECONDS = 1.25
MAX_CLOB_CLOCK_SKEW_MS = 250


def load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()[:32]


def fee_per_share(price: float, schedule: dict[str, Any], *, taker: bool = True) -> float:
    if not 0.0 < price < 1.0:
        return math.inf
    try:
        rate = float(schedule["rate"])
        exponent = float(schedule["exponent"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return math.inf
    if not math.isfinite(rate) or not math.isfinite(exponent) or rate < 0.0 or exponent < 0.0:
        return math.inf
    if not taker and bool(schedule.get("takerOnly", True)):
        return 0.0
    return rate * (price * (1.0 - price)) ** exponent


def entry_tte_allowed(fair: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Fail closed unless the forecast is inside the configured entry window."""
    tte = finite(fair.get("tte_seconds"))
    minimum = finite(policy.get("minimum_entry_tte_seconds"))
    maximum = finite(policy.get("maximum_entry_tte_seconds"))
    return (
        math.isfinite(tte)
        and math.isfinite(minimum)
        and math.isfinite(maximum)
        and 0.0 <= minimum <= maximum
        and minimum <= tte <= maximum
    )


def model_market_disagreement_allowed(fair: dict[str, Any], policy: dict[str, Any]) -> bool:
    """Reject uncalibrated model forecasts that radically contradict the market.

    The market is the benchmark the external model must beat, not an input that
    can be ignored.  Until forward calibration proves otherwise, a large gap is
    evidence of model/semantic risk rather than executable alpha.
    """
    model_yes = finite(fair.get("yes"))
    market_yes = finite(fair.get("pm_mid"))
    maximum = finite(policy.get("maximum_model_market_disagreement"))
    return (
        math.isfinite(model_yes)
        and math.isfinite(market_yes)
        and math.isfinite(maximum)
        and 0.0 <= model_yes <= 1.0
        and 0.0 <= market_yes <= 1.0
        and 0.0 <= maximum <= 1.0
        and abs(model_yes - market_yes) <= maximum
    )


@dataclass(frozen=True)
class Book:
    token_id: str
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    tick_size: float
    min_order_size: float
    exchange_ts_ms: int
    receive_ts_ms: int
    snapshot_id: str


def parse_book(raw: Any, receive_ts_ms: int) -> Book | None:
    if not isinstance(raw, dict):
        return None
    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for key, output in (("bids", bids), ("asks", asks)):
        for row in raw.get(key) if isinstance(raw.get(key), list) else []:
            if not isinstance(row, dict):
                continue
            price, size = finite(row.get("price")), finite(row.get("size"), 0.0)
            if math.isfinite(price) and 0.0 < price < 1.0 and size > 0.0:
                output.append((price, size))
    bids.sort(reverse=True)
    asks.sort()
    token = str(raw.get("asset_id") or "")
    exchange = int(finite(raw.get("timestamp"), 0.0))
    if exchange and exchange < 10_000_000_000:
        exchange *= 1000
    if (not token or (not bids and not asks) or exchange <= 0
            or exchange > receive_ts_ms + MAX_CLOB_CLOCK_SKEW_MS):
        return None
    # The public CLOB clock can lead the local host by a few milliseconds.  A
    # bounded skew is safe to accept, but the canonical ledger clock must stay
    # causal (exchange <= receive).  Larger future timestamps still fail closed.
    exchange = min(exchange, receive_ts_ms)
    snapshot = str(raw.get("hash") or "") or stable_id(token, exchange, bids, asks)
    return Book(
        token, tuple(bids), tuple(asks), max(1e-6, finite(raw.get("tick_size"), 0.01)),
        max(1.0, finite(raw.get("min_order_size"), 1.0)), exchange, receive_ts_ms, snapshot,
    )


def robust_candidates(status: dict[str, Any], books: dict[str, Book], policy: dict[str, Any]) -> list[dict[str, Any]]:
    if status.get("paper_only") is not True or status.get("authenticated_execution") is not False:
        return []
    if status.get("real_order_submission") is not False:
        return []
    contract, reference = status.get("contract") or {}, status.get("settlement_reference") or {}
    oracle, external, fair, market = (
        status.get("oracle") or {}, status.get("external") or {}, status.get("fair") or {}, status.get("market") or {},
    )
    if not (contract.get("verified") and contract.get("rules_hash_recognized") and reference.get("valid")
            and oracle.get("healthy") and oracle.get("continuity") != "CONTINUITY_UNKNOWN"
            and external.get("healthy") and fair.get("valid")):
        return []
    if not entry_tte_allowed(fair, policy):
        return []
    if not model_market_disagreement_allowed(fair, policy):
        return []
    calculated = int(fair.get("calculated_monotonic_ns") or 0)
    valid_until = int(fair.get("valid_until_monotonic_ns") or 0)
    current = time.monotonic_ns()
    if calculated <= 0 or calculated > current or valid_until < current:
        return []
    schedule = market.get("fee_schedule") if isinstance(market.get("fee_schedule"), dict) else {}
    minimum_ev = float(policy.get("minimum_robust_ev_per_share", 0.001))
    execution_risk = float(policy.get("base_execution_risk_per_share", 0.0005))
    rows: list[dict[str, Any]] = []
    for outcome, token, robust_value in (
        ("YES", str(market.get("yes_token") or ""), float(fair.get("lower") or 0.0)),
        ("NO", str(market.get("no_token") or ""), 1.0 - float(fair.get("upper") or 1.0)),
    ):
        book = books.get(token)
        if book is None or not book.asks:
            continue
        ask = book.asks[0][0]
        fee = fee_per_share(ask, schedule)
        robust_ev = robust_value - ask - fee - execution_risk
        if math.isfinite(robust_ev) and robust_ev >= minimum_ev:
            rows.append({
                "outcome": outcome, "token_id": token, "book": book, "ask": ask,
                "fee_per_share": fee, "execution_risk": execution_risk,
                "robust_probability": robust_value, "robust_ev": robust_ev,
                "tte_seconds": float(fair["tte_seconds"]),
            })
    return sorted(rows, key=lambda row: (-row["robust_ev"], row["outcome"]))


def executable_sell_value(book: Book, shares: float, schedule: dict[str, Any]) -> float:
    """Return a full-depth liquidation value net of authoritative exit fees."""
    remaining = max(0.0, shares)
    value = 0.0
    for price, available in book.bids:
        quantity = min(remaining, available)
        value += quantity * max(0.0, price - fee_per_share(price, schedule))
        remaining -= quantity
        if remaining <= 1e-9:
            return value
    return 0.0


class PaperRouter:
    def __init__(self, run_root: Path, model_sha: str, config_path: Path, clob_url: str, gamma_url: str):
        self.root = run_root
        self.directory = run_root / "external_fair"
        self.sha = model_sha
        self.config = load(config_path)
        if (self.config.get("execution_authority") != "SHADOW_ZERO_AUTHORITY"
                or self.config.get("paper_only") is not True
                or self.config.get("authenticated_execution") is not False
                or self.config.get("real_order_submission") is not False):
            raise RuntimeError("external_fair_shadow_contract_invalid")
        self.policy = self.config.get("taker") if isinstance(self.config.get("taker"), dict) else {}
        if (self.policy.get("enabled_for_execution") is not False
                or self.policy.get("authority") != "SHADOW"
                or self.policy.get("counterfactual_enabled") is not True):
            raise RuntimeError("external_fair_taker_not_shadow_authorized")
        self.policy_sha256 = hashlib.sha256(
            json.dumps(self.config, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        fair_policy = self.config.get("fair_value") if isinstance(self.config.get("fair_value"), dict) else {}
        self.model_mature = fair_policy.get("default_model_mature") is True
        self.clob_url = clob_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        self.source = self.directory / "status.json"
        self.status_path = self.directory / "paper_router_status.json"
        self.state_path = self.directory / "paper_router_state.json"
        self.counterfactual_path = self.directory / "counterfactuals.jsonl"
        self.drain_path = run_root / "control" / "CUTOVER_DRAIN"
        allocation = load(run_root / "control" / "allocations" / "manifest.json")
        budgets = allocation.get("budgets") if isinstance(allocation.get("budgets"), dict) else {}
        starting_capital = max(1.0, finite(budgets.get("external"), 4000.0))
        self.state: dict[str, Any] = {
            "model_sha": model_sha, "starting_capital": starting_capital,
            "cash": starting_capital, "orders": 0, "fills": 0,
            "candidates": 0, "counterfactual_fills": 0, "nothing": 0,
            "realized_pnl": 0.0, "counterfactual_realized_pnl": 0.0,
            "peak_equity": starting_capital, "killed": False,
            "attempted_at": {}, "traded_markets": [], "positions": {},
            "book_requests": 0, "book_request_failures": 0,
            "book_parse_failures": 0, "rejection_reasons": {},
            "forecasts": 0, "resolved_forecasts": 0,
            "forecasted_keys": [], "pending_forecasts": {},
            "forecast_settlement_attempted_at": {},
            "last_decision": {},
        }
        prior = load(self.state_path)
        if prior.get("model_sha") == model_sha:
            self.state.update(prior)
        self.last_book_error = ""
        self.last_attempt_reason = ""

    def reject(self, reason: str) -> None:
        reasons = self.state.setdefault("rejection_reasons", {})
        reasons[reason] = int(reasons.get(reason) or 0) + 1

    def emit_counterfactual(self, event_type: str, **values: Any) -> None:
        timestamp_ms = now_ms()
        markout_keys = sorted(
            str(key) for key in (
                values.get("markouts") if isinstance(values.get("markouts"), dict) else {}
            )
        )
        record = {
            "schema": "polymarket_v7_external_fair_counterfactual_v1",
            "record_id": stable_id(
                self.sha, event_type, values.get("counterfactual_id"),
                values.get("forecast_id"), values.get("position_id"),
                values.get("reason"), markout_keys,
            ),
            "event_type": event_type,
            "timestamp_ms": timestamp_ms,
            "model_sha": self.sha,
            "model_version": MODEL_VERSION,
            "policy_sha256": self.policy_sha256,
            "execution_authority": "SHADOW_ZERO_AUTHORITY",
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            **values,
        }
        self.counterfactual_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
        descriptor = os.open(
            self.counterfactual_path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def fetch_book(self, token_id: str) -> Book | None:
        try:
            raw = request_json(
                f"{self.clob_url}/book?token_id={urllib.parse.quote(token_id)}", timeout=4
            )
        except Exception:
            return None
        return parse_book(raw, now_ms())

    def books_for(self, status: dict[str, Any]) -> dict[str, Book]:
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        output: dict[str, Book] = {}
        tokens = [token for token in (
            str(market.get("yes_token") or ""), str(market.get("no_token") or "")
        ) if token]
        self.state["book_requests"] = int(self.state.get("book_requests") or 0) + 1
        self.last_book_error = ""
        try:
            rows = request_json(
                f"{self.clob_url}/books", [{"token_id": token} for token in tokens], timeout=4
            )
        except Exception as exc:
            self.state["book_request_failures"] = int(self.state.get("book_request_failures") or 0) + 1
            self.last_book_error = f"CLOB_BOOK_REQUEST_{type(exc).__name__.upper()}"
            rows = []
        received = now_ms()
        for raw in rows if isinstance(rows, list) else []:
            book = parse_book(raw, received)
            if book is not None and book.token_id in tokens:
                output[book.token_id] = book
        if tokens and len(output) != len(tokens) and not self.last_book_error:
            self.state["book_parse_failures"] = int(self.state.get("book_parse_failures") or 0) + 1
            self.last_book_error = "CLOB_BOOK_SNAPSHOT_INCOMPLETE"
        return output

    def record_forecast(self, status: dict[str, Any], books: dict[str, Book]) -> bool:
        """Persist one trade-independent forecast near each canonical TTE bucket."""
        fair = status.get("fair") if isinstance(status.get("fair"), dict) else {}
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        contract = status.get("contract") if isinstance(status.get("contract"), dict) else {}
        reference = status.get("settlement_reference") if isinstance(status.get("settlement_reference"), dict) else {}
        oracle = status.get("oracle") if isinstance(status.get("oracle"), dict) else {}
        external = status.get("external") if isinstance(status.get("external"), dict) else {}
        tte = finite(fair.get("tte_seconds"))
        model_yes = finite(fair.get("yes"))
        market_yes = finite(fair.get("pm_mid"))
        market_id = str(market.get("market_id") or "")
        if not (
            fair.get("valid") is True and reference.get("valid") is True
            and market_id and tte is not None and tte >= 0.0
            and model_yes is not None and 0.0 <= model_yes <= 1.0
            and market_yes is not None and 0.0 <= market_yes <= 1.0
        ):
            return False
        bucket = min(FORECAST_TTE_BUCKETS, key=lambda value: (abs(value - tte), -value))
        if abs(bucket - tte) > FORECAST_BUCKET_TOLERANCE_SECONDS:
            return False
        key = f"{market_id}:{bucket}"
        seen = self.state.get("forecasted_keys") if isinstance(self.state.get("forecasted_keys"), list) else []
        if key in set(str(value) for value in seen):
            return False
        forecast_id = f"external-forecast-{stable_id(self.sha, market_id, bucket)}"
        yes_token, no_token = str(market.get("yes_token") or ""), str(market.get("no_token") or "")
        yes_book, no_book = books.get(yes_token), books.get(no_token)
        observed_ms = now_ms()
        values = {
            "counterfactual_id": forecast_id, "forecast_id": forecast_id,
            "market_id": market_id, "event_id": str(market.get("event_id") or ""),
            "rules_hash": str(contract.get("rules_hash") or ""),
            "reference_version": int(reference.get("version") or 0),
            "tte_bucket_seconds": bucket, "observed_tte_seconds": tte,
            "model_yes": model_yes, "market_yes": market_yes,
            "lower": finite(fair.get("lower")), "upper": finite(fair.get("upper")),
            "oracle_value": finite(oracle.get("value")),
            "external_venue_count": int(external.get("fresh_venue_count") or 0),
            "fair_calculated_monotonic_ns": int(fair.get("calculated_monotonic_ns") or 0),
            "fair_valid_until_monotonic_ns": int(fair.get("valid_until_monotonic_ns") or 0),
            "yes_best_bid": yes_book.bids[0][0] if yes_book and yes_book.bids else None,
            "yes_best_ask": yes_book.asks[0][0] if yes_book and yes_book.asks else None,
            "no_best_bid": no_book.bids[0][0] if no_book and no_book.bids else None,
            "no_best_ask": no_book.asks[0][0] if no_book and no_book.asks else None,
            "decision": "SHADOW_FORECAST_ONLY",
        }
        self.emit_counterfactual("FORECAST", **values)
        seen.append(key)
        self.state["forecasted_keys"] = seen[-10_000:]
        self.state["forecasts"] = int(self.state.get("forecasts") or 0) + 1
        self.state.setdefault("pending_forecasts", {})[forecast_id] = {
            **values, "yes_token": yes_token, "no_token": no_token,
            "observed_ms": observed_ms,
            "resolution_due_ms": observed_ms + int(tte * 1000.0),
        }
        return True

    @staticmethod
    def forecast_scores(probability: float, actual: float) -> tuple[float, float]:
        clipped = min(1.0 - 1e-12, max(1e-12, probability))
        brier = (clipped - actual) ** 2
        log_loss = -(actual * math.log(clipped) + (1.0 - actual) * math.log(1.0 - clipped))
        return brier, log_loss

    def observe_forecasts(self) -> None:
        current_ms = now_ms()
        pending = self.state.get("pending_forecasts") if isinstance(self.state.get("pending_forecasts"), dict) else {}
        by_market: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for forecast_id, forecast in pending.items():
            if current_ms < int(forecast.get("resolution_due_ms") or 0) + 5000:
                continue
            by_market.setdefault(str(forecast.get("market_id") or ""), []).append((forecast_id, forecast))
        attempted = self.state.setdefault("forecast_settlement_attempted_at", {})
        for market_id, forecasts in by_market.items():
            if not market_id or current_ms - int(attempted.get(market_id) or 0) < 10_000:
                continue
            attempted[market_id] = current_ms
            try:
                raw = request_json(
                    f"{self.gamma_url}/markets/{urllib.parse.quote(market_id)}", timeout=4
                )
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("closed") is not True:
                continue
            tokens = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
            prices = [finite(value) for value in parse_array(raw.get("outcomePrices"))]
            winning_index = next((index for index, price in enumerate(prices)
                                  if math.isfinite(price) and price >= 1.0 - 1e-9), -1)
            if winning_index < 0 or winning_index >= len(tokens):
                continue
            winning_token = tokens[winning_index]
            for forecast_id, forecast in forecasts:
                if winning_token not in {
                    str(forecast.get("yes_token") or ""),
                    str(forecast.get("no_token") or ""),
                }:
                    continue
                actual_yes = 1.0 if winning_token == str(forecast.get("yes_token") or "") else 0.0
                model_brier, model_log_loss = self.forecast_scores(float(forecast["model_yes"]), actual_yes)
                market_brier, market_log_loss = self.forecast_scores(float(forecast["market_yes"]), actual_yes)
                self.emit_counterfactual(
                    "FORECAST_FINAL", counterfactual_id=forecast_id, forecast_id=forecast_id,
                    market_id=market_id, tte_bucket_seconds=forecast["tte_bucket_seconds"],
                    model_yes=forecast["model_yes"], market_yes=forecast["market_yes"],
                    actual_yes=actual_yes, winning_token_id=winning_token,
                    model_brier=model_brier, market_brier=market_brier,
                    model_log_loss=model_log_loss, market_log_loss=market_log_loss,
                )
                pending.pop(forecast_id, None)
                self.state["resolved_forecasts"] = int(self.state.get("resolved_forecasts") or 0) + 1

    def order_size(self, row: dict[str, Any]) -> float:
        book: Book = row["book"]
        ask, visible = book.asks[0]
        depth_fraction = min(
            float(self.policy.get("max_depth_fraction", 0.5)),
            float(self.policy.get("depth_survival_fraction", 0.75)),
        )
        starting_capital = float(self.state.get("starting_capital") or 0.0)
        cash = float(self.state.get("cash") or 0.0)
        if self.model_mature:
            capital_ceiling = starting_capital * float(
                self.policy.get("max_market_capital_fraction", 0.02)
            )
            probability = max(1e-6, min(1.0 - 1e-6, float(row["robust_probability"])))
            kelly = max(0.0, float(row["robust_ev"])) / (probability * (1.0 - probability))
            kelly_notional = cash * float(self.policy.get("fractional_kelly", 0.1)) * kelly
            available_notional = min(capital_ceiling, kelly_notional, cash)
        else:
            # Kelly sizing is not mathematically defensible before probability
            # calibration is mature. Shadow observations use a fixed virtual
            # notional so their economics stay comparable across contracts.
            capital_ceiling = starting_capital * float(
                self.policy.get("immature_exploration_capital_fraction", 0.0025)
            )
            available_notional = min(capital_ceiling, cash)
        size = min(visible * max(0.0, depth_fraction), available_notional / max(ask + row["fee_per_share"], 1e-9))
        size = math.floor(size * 100.0) / 100.0
        return size if size + 1e-9 >= book.min_order_size else 0.0

    def common(self, status: dict[str, Any], row: dict[str, Any], order_id: str, size: float) -> dict[str, Any]:
        market, fair, contract, reference = (
            status.get("market") or {}, status.get("fair") or {}, status.get("contract") or {},
            status.get("settlement_reference") or {},
        )
        book: Book = row["book"]
        decision = now_ms()
        market_id = str(market.get("market_id") or "")
        position_id = f"external-position-{market_id}-{row['outcome']}"
        return dict(
            strategy=STRATEGY, model_sha=self.sha, model_version=MODEL_VERSION,
            candidate_id=order_id, order_id=order_id, position_id=position_id,
            market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"],
            decision_ts_ms=decision, exchange_ts_ms=book.exchange_ts_ms,
            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
            side="BUY", bid=book.bids[0][0] if book.bids else None, ask=book.asks[0][0],
            bid_depth=sum(size for _, size in book.bids), ask_depth=sum(size for _, size in book.asks),
            limit_price=book.asks[0][0], predicted_alpha=row["robust_ev"],
            predicted_fill_probability=1.0, expected_ev=row["robust_ev"] * size,
            intended_action="TAKE", intended_size=size,
            metadata={
                "authority": "SHADOW_ZERO_AUTHORITY", "virtual_tif": "FAK",
                "outcome": row["outcome"], "execution_side": "BUY",
                "fair_yes": fair.get("yes"), "fair_lower": fair.get("lower"),
                "fair_upper": fair.get("upper"), "pm_mid": fair.get("pm_mid"),
                "model_market_disagreement": abs(
                    float(fair.get("yes")) - float(fair.get("pm_mid"))
                ),
                "maximum_model_market_disagreement": self.policy.get(
                    "maximum_model_market_disagreement"
                ),
                "contract_rules_hash": contract.get("rules_hash"),
                "reference_version": reference.get("version"), "expected_fee_per_share": row["fee_per_share"],
                "expected_execution_risk": row["execution_risk"], "economic_maturity": "MORE_EVIDENCE_REQUIRED",
                "tte_seconds": row["tte_seconds"], "robust_probability": row["robust_probability"],
                "robust_ev_per_share": row["robust_ev"],
                "model_family": STRATEGY, "horizon_seconds": 300,
            },
        )

    def attempt(self, status: dict[str, Any], row: dict[str, Any]) -> bool:
        self.last_attempt_reason = ""
        if self.drain_path.exists():
            self.last_attempt_reason = "CUTOVER_DRAIN"
            return False
        if self.state.get("killed") or (self.root / "control" / "KILL").exists():
            self.last_attempt_reason = "GLOBAL_OR_SLEEVE_KILLED"
            return False
        market = status.get("market") if isinstance(status.get("market"), dict) else {}
        market_id = str(market.get("market_id") or "")
        if not market_id or market_id in set(self.state.get("traded_markets") or []):
            self.last_attempt_reason = "MARKET_ALREADY_TRADED_OR_MISSING"
            return False
        key = f"{market_id}:{row['outcome']}"
        current_ms = now_ms()
        if current_ms - int((self.state.get("attempted_at") or {}).get(key) or 0) < 5000:
            self.last_attempt_reason = "ATTEMPT_COOLDOWN"
            return False
        self.state.setdefault("attempted_at", {})[key] = current_ms
        size = self.order_size(row)
        if size <= 0.0:
            self.last_attempt_reason = "BELOW_MINIMUM_EXECUTABLE_SIZE"
            return False
        counterfactual_id = f"external-shadow-{stable_id(self.sha, market_id, row['outcome'], current_ms)}"
        common = self.common(status, row, counterfactual_id, size)
        self.emit_counterfactual("CANDIDATE", counterfactual_id=counterfactual_id, **common)
        self.state["candidates"] = int(self.state.get("candidates") or 0) + 1
        time.sleep(0.1)

        arrival_status = load(self.source)
        arrival_books = self.books_for(arrival_status)
        rows = robust_candidates(arrival_status, arrival_books, self.policy)
        arrival = next((candidate for candidate in rows if candidate["token_id"] == row["token_id"]), None)
        if arrival is None:
            self.emit_counterfactual(
                "REJECTED", counterfactual_id=counterfactual_id,
                reason="ARRIVAL_REVALIDATION_FAILED", market_id=market_id,
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            )
            self.last_attempt_reason = "ARRIVAL_REVALIDATION_FAILED"
            return False
        arrival_book: Book = arrival["book"]
        ask, visible = arrival_book.asks[0]
        if visible + 1e-9 < size or ask > float(common["limit_price"]) + 1e-12:
            self.emit_counterfactual(
                "REJECTED", counterfactual_id=counterfactual_id,
                reason="FAK_VISIBLE_DEPTH_OR_LIMIT", market_id=market_id,
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            )
            self.last_attempt_reason = "FAK_VISIBLE_DEPTH_OR_LIMIT"
            return False
        schedule = (arrival_status.get("market") or {}).get("fee_schedule") or {}
        fee_share = fee_per_share(ask, schedule)
        total_fee, cost = size * fee_share, size * ask
        executable_value = executable_sell_value(arrival_book, size, schedule)
        robust_ev = float(arrival["robust_probability"]) * size - cost - total_fee - size * float(arrival["execution_risk"])
        if robust_ev <= 0.0 or cost + total_fee > float(self.state.get("starting_capital") or 0.0):
            self.emit_counterfactual(
                "REJECTED", counterfactual_id=counterfactual_id,
                reason="ARRIVAL_EV_OR_VIRTUAL_CAPITAL", market_id=market_id,
                event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            )
            self.last_attempt_reason = "ARRIVAL_EV_OR_CAPITAL"
            return False
        fill_id = f"external-shadow-fill-{stable_id(counterfactual_id, arrival_book.exchange_ts_ms, arrival_book.receive_ts_ms)}"
        position_id = str(common["position_id"])
        self.emit_counterfactual(
            "VIRTUAL_FILL", counterfactual_id=counterfactual_id,
            strategy=STRATEGY, model_version=MODEL_VERSION,
            fill_id=fill_id, position_id=position_id, market_id=market_id,
            event_id=str(market.get("event_id") or ""), token_id=row["token_id"], side="BUY",
            exchange_ts_ms=arrival_book.exchange_ts_ms, receive_ts_ms=arrival_book.receive_ts_ms,
            fill_price=ask, filled_size=size, fee=total_fee,
            fee_rate=float(schedule.get("rate") or 0.0), fee_source="GAMMA_AUTHORITATIVE_FEE_SCHEDULE",
            slippage=max(0.0, ask - float(common["ask"])) * size,
            metadata={
                **common["metadata"], "robust_net_ev": robust_ev,
                "arrival_snapshot_id": arrival_book.snapshot_id,
                "arrival_tte_seconds": arrival["tte_seconds"],
                "arrival_robust_probability": arrival["robust_probability"],
                "arrival_robust_ev_per_share": arrival["robust_ev"],
            },
        )
        self.state["counterfactual_fills"] = int(self.state.get("counterfactual_fills") or 0) + 1
        self.state.setdefault("traded_markets", []).append(market_id)
        self.state.setdefault("positions", {})[position_id] = {
            "position_id": position_id, "counterfactual_id": counterfactual_id, "fill_id": fill_id,
            "market_id": market_id, "event_id": str(market.get("event_id") or ""),
            "token_id": row["token_id"], "outcome": row["outcome"], "shares": size,
            "entry_price": ask, "entry_fee": total_fee, "entry_cost": cost,
            "executable_value": executable_value, "opened_ms": arrival_book.receive_ts_ms,
            "fee_schedule": schedule, "markouts": [], "settled": False,
            "model_yes": finite((arrival_status.get("fair") or {}).get("yes")),
            "market_yes": finite((arrival_status.get("fair") or {}).get("pm_mid")),
        }
        self.last_attempt_reason = "VIRTUAL_FILL"
        return True

    def observe_positions(self) -> None:
        current_ms = now_ms()
        for position in list((self.state.get("positions") or {}).values()):
            if position.get("settled"):
                continue
            age_seconds = max(0.0, (current_ms - int(position["opened_ms"])) / 1000.0)
            due = [horizon for horizon in HORIZONS if horizon <= age_seconds and horizon not in position.get("markouts", [])]
            if due:
                book = self.fetch_book(str(position["token_id"]))
                if book is not None:
                    schedule = position.get("fee_schedule") if isinstance(position.get("fee_schedule"), dict) else {}
                    liquidation = executable_sell_value(book, float(position["shares"]), schedule)
                    position["executable_value"] = liquidation
                    per_share = (liquidation - float(position["entry_cost"]) - float(position["entry_fee"])) / float(position["shares"])
                    for horizon in due:
                        self.emit_counterfactual(
                            "VIRTUAL_MARKOUT", strategy=STRATEGY, model_version=MODEL_VERSION,
                            counterfactual_id=str(position["counterfactual_id"]),
                            fill_id=str(position["fill_id"]), position_id=str(position["position_id"]),
                            market_id=str(position["market_id"]), event_id=str(position["event_id"]),
                            token_id=str(position["token_id"]), side="BUY", exchange_ts_ms=book.exchange_ts_ms,
                            receive_ts_ms=book.receive_ts_ms, book_snapshot_id=book.snapshot_id,
                            executable_liquidation_value=liquidation, markouts={f"{horizon}s": per_share},
                            metadata={"full_visible_depth": True, "fill_conditioned": True},
                        )
                        position.setdefault("markouts", []).append(horizon)
            if age_seconds < 300:
                continue
            try:
                raw = request_json(f"{self.gamma_url}/markets/{urllib.parse.quote(str(position['market_id']))}", timeout=4)
            except Exception:
                continue
            if not isinstance(raw, dict) or raw.get("closed") is not True:
                continue
            outcomes = [str(value) for value in parse_array(raw.get("outcomes"))]
            tokens = [str(value) for value in parse_array(raw.get("clobTokenIds"))]
            prices = [finite(value) for value in parse_array(raw.get("outcomePrices"))]
            winning_index = next((index for index, price in enumerate(prices)
                                  if math.isfinite(price) and price >= 1.0 - 1e-9), -1)
            if winning_index < 0 or winning_index >= len(tokens):
                continue
            winning_token = tokens[winning_index]
            resolved = outcomes[winning_index] if winning_index < len(outcomes) else ""
            payout = float(position["shares"]) if winning_token == str(position["token_id"]) else 0.0
            pnl = payout - float(position["entry_cost"]) - float(position["entry_fee"])
            self.state["counterfactual_realized_pnl"] = float(
                self.state.get("counterfactual_realized_pnl") or 0.0
            ) + pnl
            position["settled"] = True
            position["resolved_outcome"] = resolved
            won = winning_token == str(position["token_id"])
            self.emit_counterfactual(
                "VIRTUAL_FINAL", strategy=STRATEGY, model_version=MODEL_VERSION,
                counterfactual_id=str(position["counterfactual_id"]),
                position_id=str(position["position_id"]),
                fill_id=str(position["fill_id"]), market_id=str(position["market_id"]),
                event_id=str(position["event_id"]), token_id=str(position["token_id"]), side="BUY",
                counterfactual_pnl=pnl, virtual_cashflow=payout,
                capital_duration_ms=current_ms - int(position["opened_ms"]),
                metadata={
                    "settlement_outcome": resolved, "winning_token_id": winning_token,
                    "won": won, "hold_to_settlement": True, "counterfactual": True,
                    "model_yes": position.get("model_yes"),
                    "market_yes": position.get("market_yes"),
                    "model_family": STRATEGY, "horizon_seconds": 300,
                },
            )

    def publish(self, active_candidates: int, blocker: str = "") -> None:
        positions = self.state.get("positions") if isinstance(self.state.get("positions"), dict) else {}
        open_positions = sum(1 for position in positions.values() if not position.get("settled"))
        virtual_equity = starting_capital = float(self.state.get("starting_capital") or 0.0)
        virtual_equity += float(self.state.get("counterfactual_realized_pnl") or 0.0)
        virtual_equity += sum(
            float(position.get("executable_value") or 0.0)
            - float(position.get("entry_cost") or 0.0)
            - float(position.get("entry_fee") or 0.0)
            for position in positions.values() if not position.get("settled")
        )
        peak = max(starting_capital, float(self.state.get("peak_equity") or starting_capital), virtual_equity)
        drawdown = max(0.0, 1.0 - virtual_equity / peak) if peak > 0.0 else 1.0
        killed = bool(self.state.get("killed")) or (self.root / "control" / "KILL").exists()
        drain_requested = self.drain_path.exists()
        if drain_requested:
            blocker = "CUTOVER_DRAIN"
        self.state["peak_equity"] = peak
        self.state["killed"] = killed
        atomic_json(self.state_path, self.state)
        atomic_json(self.status_path, {
            "schema": "polymarket_v7_external_fair_paper_router_v1", "timestamp": int(time.time()),
            "code_sha": self.sha, "state": "KILLED" if killed else "DRAINING" if drain_requested else "RUNNING", "paper_only": True,
            "authenticated_execution": False, "real_order_submission": False,
            "execution_mode": "SHADOW_COUNTERFACTUAL",
            "policy_sha256": self.policy_sha256,
            "execution_authority": "SHADOW_ZERO_AUTHORITY", "model_mature": self.model_mature,
            "economic_confidence": "MORE_EVIDENCE_REQUIRED", "active_candidates": active_candidates,
            "entry_tte_window_seconds": {
                "minimum": self.policy.get("minimum_entry_tte_seconds"),
                "maximum": self.policy.get("maximum_entry_tte_seconds"),
            },
            "sizing_regime": (
                "MATURE_SHADOW_FRACTIONAL_KELLY" if self.model_mature
                else "IMMATURE_SHADOW_FIXED_NOTIONAL"
            ),
            "market_capital_ceiling": float(self.state.get("starting_capital") or 0.0) * float(
                self.policy.get(
                    "max_market_capital_fraction" if self.model_mature
                    else "immature_exploration_capital_fraction",
                    0.02 if self.model_mature else 0.0025,
                )
            ),
            "counterfactual_collection_enabled": not killed and not drain_requested,
            "counterfactual_tape": str(self.counterfactual_path),
            "counterfactual_candidates": int(self.state.get("candidates") or 0),
            "candidates_spooled": 0,
            "orders_submitted": int(self.state.get("orders") or 0), "fills": int(self.state.get("fills") or 0),
            "counterfactual_fills": int(self.state.get("counterfactual_fills") or 0),
            "counterfactual_forecasts": int(self.state.get("forecasts") or 0),
            "counterfactual_resolved_forecasts": int(self.state.get("resolved_forecasts") or 0),
            "counterfactual_pending_forecasts": len(
                self.state.get("pending_forecasts")
                if isinstance(self.state.get("pending_forecasts"), dict) else {}
            ),
            "counterfactual_open_positions": open_positions,
            "counterfactual_realized_pnl": float(self.state.get("counterfactual_realized_pnl") or 0.0),
            "counterfactual_equity": virtual_equity,
            "open_positions": 0, "realized_pnl": 0.0,
            "cash": starting_capital, "equity": starting_capital,
            "counterfactual_peak_equity": peak, "counterfactual_drawdown": drawdown,
            "peak_equity": starting_capital, "drawdown": 0.0, "killed": killed,
            "order_submission_enabled": False,
            "drain_requested": drain_requested, "drain_complete": drain_requested,
            "blocker": blocker,
            "book_requests": int(self.state.get("book_requests") or 0),
            "book_request_failures": int(self.state.get("book_request_failures") or 0),
            "book_parse_failures": int(self.state.get("book_parse_failures") or 0),
            "rejection_reasons": self.state.get("rejection_reasons") or {},
            "last_decision": self.state.get("last_decision") or {},
            "actions": {"MAKE": 0, "TAKE": 0, "CANCEL": 0,
                        "WITHDRAW": 0, "NOTHING": int(self.state.get("nothing") or 0)},
            "counterfactual_actions": {"TAKE": int(self.state.get("counterfactual_fills") or 0)},
        })

    def step(self) -> None:
        status = load(self.source)
        blocker = ""
        books: dict[str, Book] = {}
        if self.drain_path.exists():
            blocker = "CUTOVER_DRAIN"
            rows = []
        elif status.get("code_sha") != self.sha:
            blocker = "EXTERNAL_FAIR_SHA_MISMATCH"
            rows: list[dict[str, Any]] = []
        else:
            books = self.books_for(status)
            self.record_forecast(status, books)
            rows = robust_candidates(status, books, self.policy)
        filled = False
        for row in rows:
            if self.attempt(status, row):
                filled = True
                break
        if not filled:
            self.state["nothing"] = int(self.state.get("nothing") or 0) + 1
            if blocker:
                reason = blocker
            elif self.last_book_error:
                reason = self.last_book_error
            elif len(books) < 2:
                reason = "CLOB_BOOKS_UNAVAILABLE"
            elif (status.get("fair") or {}).get("valid") and entry_tte_allowed(
                    status.get("fair") or {}, self.policy) and not model_market_disagreement_allowed(
                        status.get("fair") or {}, self.policy):
                reason = "MODEL_MARKET_DISAGREEMENT_LIMIT"
            elif rows and self.last_attempt_reason:
                reason = self.last_attempt_reason
            elif rows:
                reason = "ROBUST_CANDIDATE_NOT_FILLED"
            elif (status.get("fair") or {}).get("valid") and not entry_tte_allowed(
                    status.get("fair") or {}, self.policy):
                reason = "ENTRY_TTE_OUTSIDE_WINDOW"
            else:
                reason = "NO_ROBUST_EV"
            self.reject(reason)
        else:
            reason = "VIRTUAL_FILL"
        self.state["last_decision"] = {
            "timestamp_ms": now_ms(),
            "market_id": str((status.get("market") or {}).get("market_id") or ""),
            "books": len(books), "robust_candidates": len(rows), "outcome": reason,
            "best_robust_ev_per_share": max((float(row["robust_ev"]) for row in rows), default=None),
        }
        self.observe_positions()
        self.observe_forecasts()
        self.publish(len(rows), blocker)

    def run(self, interval: float) -> None:
        while True:
            try:
                self.step()
            except Exception as exc:
                self.publish(0, f"ROUTER_ERROR:{type(exc).__name__}")
            time.sleep(max(0.25, interval))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--config", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("exact model SHA required")
    PaperRouter(args.run_root.resolve(), args.model_sha, args.config.resolve(), args.clob_url, args.gamma_url).run(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
