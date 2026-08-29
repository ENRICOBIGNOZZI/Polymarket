#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def request_json(url: str, payload: Any | None = None, timeout: int = 20) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "polymarket-v6-paper/lf-research", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass(frozen=True)
class FeeDetails:
    rate: float
    exponent: float
    taker_only: bool
    verified: bool
    source: str


def _fee_from_gamma(raw: dict[str, Any]) -> FeeDetails | None:
    if raw.get("feesEnabled") is False:
        return FeeDetails(0.0, 1.0, True, True, "gamma:fees_disabled")
    schedule = raw.get("feeSchedule")
    if not isinstance(schedule, dict):
        return None
    rate = finite(schedule.get("rate"))
    exponent = finite(schedule.get("exponent"), 1.0)
    if not math.isfinite(rate) or rate < 0.0:
        return None
    return FeeDetails(
        rate=max(0.0, rate),
        exponent=max(0.0, exponent),
        taker_only=bool(schedule.get("takerOnly", True)),
        verified=True,
        source="gamma:feeSchedule",
    )


def resolve_fee_details(
    raw_market: dict[str, Any],
    clob_url: str,
    condition_id: str,
    token_id: str,
    *,
    timeout: int = 10,
) -> FeeDetails:
    gamma = _fee_from_gamma(raw_market)
    if gamma is not None:
        return gamma
    if condition_id:
        try:
            root = request_json(
                f"{clob_url.rstrip('/')}/clob-markets/{urllib.parse.quote(condition_id)}",
                timeout=timeout,
            )
            fd = root.get("fd") if isinstance(root, dict) else None
            if isinstance(fd, dict):
                rate = finite(fd.get("r"))
                exponent = finite(fd.get("e"), 1.0)
                if math.isfinite(rate) and rate >= 0.0:
                    return FeeDetails(
                        rate=max(0.0, rate),
                        exponent=max(0.0, exponent),
                        taker_only=bool(fd.get("to", True)),
                        verified=True,
                        source="clob:fd",
                    )
        except Exception:
            pass
    if token_id:
        try:
            root = request_json(
                f"{clob_url.rstrip('/')}/fee-rate?token_id={urllib.parse.quote(token_id)}",
                timeout=timeout,
            )
            base_fee = finite(root.get("base_fee")) if isinstance(root, dict) else math.nan
            if math.isfinite(base_fee) and base_fee >= 0.0:
                return FeeDetails(
                    rate=max(0.0, base_fee) / 10000.0,
                    exponent=1.0,
                    taker_only=True,
                    verified=True,
                    source="clob:fee-rate",
                )
        except Exception:
            pass
    return FeeDetails(0.07, 1.0, True, False, "legacy_unverified_fallback")


def fee_per_share(price: float, details: FeeDetails, *, taker: bool = True) -> float:
    if not 0.0 < price < 1.0 or details.rate <= 0.0:
        return 0.0
    if not taker and details.taker_only:
        return 0.0
    return details.rate * (price * (1.0 - price)) ** max(0.0, details.exponent)


@dataclass(frozen=True)
class TapeTrade:
    exchange_ts: int
    received_ms: int
    asset_id: str
    side: str
    price: float
    size: float

    @property
    def knowledge_ts(self) -> float:
        return self.received_ms / 1000.0 if self.received_ms > 0 else float(self.exchange_ts)


class TapeFlow:
    """Public compatible-flow view ordered on the local knowledge clock.

    The recorder can backfill old exchange timestamps. `received_ms` is therefore
    authoritative for what the strategy could have known at decision time; the
    exchange timestamp is retained only as event metadata/fallback.
    """

    def __init__(self, trades: list[TapeTrade], now: int | None = None):
        self.now = int(time.time()) if now is None else int(now)
        self.by_asset: dict[str, list[TapeTrade]] = {}
        for trade in trades:
            self.by_asset.setdefault(trade.asset_id, []).append(trade)
        for values in self.by_asset.values():
            values.sort(key=lambda x: (x.knowledge_ts, x.exchange_ts))

    @classmethod
    def from_csv(
        cls,
        path: Path,
        *,
        lookback_seconds: int = 900,
        now: int | None = None,
    ) -> "TapeFlow":
        current = int(time.time()) if now is None else int(now)
        cutoff = current - max(1, int(lookback_seconds))
        trades: list[TapeTrade] = []
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    exchange_ts = int(finite(row.get("timestamp"), 0.0))
                    received_ms = int(finite(row.get("received_ms"), 0.0))
                    knowledge_ts = received_ms / 1000.0 if received_ms > 0 else float(exchange_ts)
                    asset = str(row.get("asset_id") or "")
                    side = str(row.get("side") or "").strip().upper()
                    price = finite(row.get("price"))
                    size = finite(row.get("size"), 0.0)
                    if knowledge_ts < cutoff or knowledge_ts > current + 30:
                        continue
                    if asset and side in {"BUY", "SELL"} and math.isfinite(price) and 0 < price < 1 and size > 0:
                        trades.append(TapeTrade(exchange_ts, received_ms, asset, side, price, size))
        except OSError:
            pass
        return cls(trades, now=current)

    def compatible_sell_volume(self, token_id: str, limit_price: float, *, lookback_seconds: int) -> float:
        cutoff = self.now - max(1, int(lookback_seconds))
        return sum(
            trade.size
            for trade in self.by_asset.get(token_id, [])
            if trade.knowledge_ts >= cutoff and trade.side == "SELL" and trade.price <= limit_price + 1e-12
        )

    def compatible_sell_rate(self, token_id: str, limit_price: float, *, lookback_seconds: int) -> float:
        window = max(1, int(lookback_seconds))
        return self.compatible_sell_volume(token_id, limit_price, lookback_seconds=window) / window


def fill_probability_proxy(
    *,
    queue_ahead: float,
    own_shares: float,
    compatible_flow_per_second: float,
    horizon_seconds: float,
    prior_flow_per_second: float = 0.0,
) -> float:
    q = max(0.0, queue_ahead)
    own = max(1e-9, own_shares)
    observed_rate = max(0.0, compatible_flow_per_second)
    requested_prior = max(0.0, prior_flow_per_second)
    effective_prior = min(requested_prior, observed_rate) if observed_rate > 0.0 else 0.0
    rate = observed_rate + effective_prior
    expected_flow = rate * max(0.0, horizon_seconds)
    if expected_flow <= 0.0:
        return 0.0
    ratio = expected_flow / max(q + own, 1e-9)
    return max(0.0, min(1.0, 1.0 - math.exp(-ratio)))
