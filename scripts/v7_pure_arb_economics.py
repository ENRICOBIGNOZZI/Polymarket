#!/usr/bin/env python3
"""Shared exchange-faithful economics for V7 pure-arbitrage PAPER research.

This module has zero execution authority.  It centralizes:
- Polymarket taker fee precision (5 decimals, sub-1e-5 fees become zero);
- causal multi-level FOK/FAK ladder sweeps;
- taker Weighted Volume / tier counterfactuals;
- deterministic tail-risk statistics used by the capital allocator.

Taker rebates are ancillary only. They never rescue a negative entry edge.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any, Iterable

FEE_QUANTUM = Decimal("0.00001")
MIN_CHARGED_FEE = Decimal("0.00001")

TAKER_REBATE_TIERS = (
    (0.0, 0.00, "NONE"),
    (2_000.0, 0.03, "BRONZE"),
    (20_000.0, 0.08, "SILVER"),
    (200_000.0, 0.18, "GOLD"),
    (1_000_000.0, 0.32, "PLATINUM"),
    (4_000_000.0, 0.44, "DIAMOND"),
    (10_000_000.0, 0.50, "OBSIDIAN"),
)
CRYPTO_WEIGHT = 2.3


def finite(value: Any, default: float = math.nan) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def tail_jsonl(
    path: Path | None, *, max_rows: int = 50_000,
    max_bytes: int = 64 * 1024 * 1024,
) -> list[dict[str, Any]]:
    """Read only the bounded tail of an append-only JSONL evidence file."""
    if path is None or max_rows <= 0 or max_bytes <= 0:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            if end <= 0:
                return []
            start = max(0, end - int(max_bytes))
            handle.seek(start)
            raw = handle.read(end - start)
    except OSError:
        return []
    lines = raw.splitlines()
    # If the byte cap starts inside a record, discard that partial record.
    if start > 0 and lines:
        lines = lines[1:]
    out: list[dict[str, Any]] = []
    for raw_line in lines[-int(max_rows):]:
        try:
            value = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def raw_fee_per_share(price: float, rate: float, exponent: float = 1.0) -> float:
    if not (
        math.isfinite(price) and 0.0 < price < 1.0
        and math.isfinite(rate) and 0.0 <= rate <= 1.0
        and math.isfinite(exponent) and exponent >= 0.0
    ):
        return math.nan
    return rate * (price * (1.0 - price)) ** exponent if rate else 0.0


def rounded_fee_usdc(
    shares: float, price: float, rate: float, exponent: float = 1.0,
) -> float:
    """Return venue fee for one matched quantity.

    Polymarket documents fees in USDC, rounded to five decimals, with amounts
    smaller than 0.00001 USDC rounded to zero.
    """
    if not math.isfinite(shares) or shares <= 0.0:
        return math.nan
    per_share = raw_fee_per_share(price, rate, exponent)
    if not math.isfinite(per_share):
        return math.nan
    raw = Decimal(str(shares)) * Decimal(str(per_share))
    if raw < MIN_CHARGED_FEE:
        return 0.0
    return float(raw.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP))


def effective_fee_per_share(
    shares: float, price: float, rate: float, exponent: float = 1.0,
) -> float:
    fee = rounded_fee_usdc(shares, price, rate, exponent)
    return fee / shares if math.isfinite(fee) and shares > 0 else math.nan


def parse_levels(row: dict[str, Any], side: str) -> list[tuple[float, float]]:
    """Read causal ladder levels from a canonical book observation.

    New observations use bid_levels_l10/ask_levels_l10.  Fallback to L1 keeps
    old exact-SHA evidence readable without inventing depth.
    """
    key = "ask_levels_l10" if side.upper() == "BUY" else "bid_levels_l10"
    out: list[tuple[float, float]] = []
    raw = row.get(key)
    if isinstance(raw, list):
        for level in raw:
            if not isinstance(level, dict):
                continue
            p, q = finite(level.get("price")), finite(level.get("size"))
            if math.isfinite(p) and math.isfinite(q) and 0 < p < 1 and q > 0:
                out.append((p, q))
    if out:
        return out
    try:
        if side.upper() == "BUY":
            p, q = float(row["best_ask"]), float(row.get("ask_depth_l1") or 0.0)
        else:
            p, q = float(row["best_bid"]), float(row.get("bid_depth_l1") or 0.0)
    except (KeyError, TypeError, ValueError, OverflowError):
        return []
    return [(p, q)] if 0 < p < 1 and q > 0 else []


def fok_sweep(
    levels: Iterable[tuple[float, float]], quantity: float, side: str,
    limit_price: float | None = None,
) -> dict[str, Any]:
    """Sweep a causal ladder and apply FOK semantics: all requested size or zero."""
    if not math.isfinite(quantity) or quantity <= 0:
        return {"filled": False, "quantity": 0.0, "vwap": None, "worst_price": None,
                "notional": 0.0, "levels_used": 0}
    remaining = quantity
    notional = 0.0
    levels_used = 0
    fills: list[dict[str, float]] = []
    worst: float | None = None
    side = side.upper()
    for price, available in levels:
        if not (0 < price < 1 and available > 0):
            continue
        if limit_price is not None:
            if side == "BUY" and price > limit_price + 1e-12:
                break
            if side == "SELL" and price < limit_price - 1e-12:
                break
        take = min(remaining, available)
        if take <= 0:
            continue
        notional += take * price
        remaining -= take
        levels_used += 1
        fills.append({"price": price, "shares": take})
        worst = price
        if remaining <= 1e-12:
            return {
                "filled": True,
                "quantity": quantity,
                "vwap": notional / quantity,
                "worst_price": worst,
                "notional": notional,
                "levels_used": levels_used,
                "fills": fills,
            }
    return {
        "filled": False, "quantity": 0.0, "vwap": None, "worst_price": worst,
        "notional": 0.0, "levels_used": levels_used, "fills": fills,
    }


def sweep_fee_usdc(
    sweep: dict[str, Any], rate: float, exponent: float = 1.0,
) -> float:
    if sweep.get("filled") is not True:
        return math.nan
    total = 0.0
    fills = sweep.get("fills")
    if not isinstance(fills, list):
        return math.nan
    for fill in fills:
        if not isinstance(fill, dict):
            return math.nan
        price = finite(fill.get("price"))
        shares = finite(fill.get("shares"))
        fee = rounded_fee_usdc(shares, price, rate, exponent)
        if not math.isfinite(fee):
            return math.nan
        total += fee
    return total


def weighted_volume(
    shares: float, entry_price: float, *, category_weight: float = CRYPTO_WEIGHT,
    bonus_multiplier: float = 1.0,
) -> float:
    if not (
        math.isfinite(shares) and shares > 0
        and math.isfinite(entry_price) and 0 < entry_price < 1
        and math.isfinite(category_weight) and category_weight >= 0
        and math.isfinite(bonus_multiplier) and bonus_multiplier >= 0
    ):
        return 0.0
    trade_size = shares * entry_price
    return trade_size * (1.0 - entry_price) * category_weight * bonus_multiplier


def taker_tier(weighted_volume_30d: float) -> dict[str, Any]:
    value = max(0.0, finite(weighted_volume_30d, 0.0))
    threshold, rebate, name = TAKER_REBATE_TIERS[0]
    for candidate in TAKER_REBATE_TIERS:
        if value + 1e-12 >= candidate[0]:
            threshold, rebate, name = candidate
        else:
            break
    return {
        "weighted_volume_30d": value,
        "tier": name,
        "threshold": threshold,
        "rebate_fraction": rebate,
    }


def ancillary_taker_rebate(
    fee_usdc: float, rebate_fraction: float, *, verified: bool,
) -> float:
    """Book no taker rebate unless current tier evidence is explicitly verified."""
    if not verified:
        return 0.0
    if not (math.isfinite(fee_usdc) and fee_usdc >= 0
            and math.isfinite(rebate_fraction) and 0 <= rebate_fraction <= 1):
        return 0.0
    return fee_usdc * rebate_fraction


def quantile(values: list[float], probability: float) -> float | None:
    xs = sorted(x for x in values if math.isfinite(x))
    if not xs:
        return None
    p = min(1.0, max(0.0, probability))
    if len(xs) == 1:
        return xs[0]
    pos = p * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1.0 - w) + xs[hi] * w


def expected_shortfall(values: list[float], probability: float = 0.05) -> float | None:
    xs = sorted(x for x in values if math.isfinite(x))
    if not xs:
        return None
    count = max(1, int(math.ceil(len(xs) * min(1.0, max(0.0, probability)))))
    return sum(xs[:count]) / count


def moving_block_bootstrap_lower(
    values: list[float], *, block_size: int, draws: int, alpha: float,
    seed: int = 20260922,
) -> float | None:
    xs = [x for x in values if math.isfinite(x)]
    n = len(xs)
    if n == 0:
        return None
    if n == 1 or draws <= 0:
        return xs[0]
    block = max(1, min(int(block_size), n))
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(int(draws)):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.randrange(n)
            for offset in range(block):
                sample.append(xs[(start + offset) % n])
                if len(sample) == n:
                    break
        means.append(statistics.fmean(sample))
    return quantile(means, min(1.0, max(0.0, alpha)))


def tail_risk_summary(
    values: list[float], *, block_size: int = 5, draws: int = 1000,
    alpha: float = 0.05, seed: int = 20260922,
) -> dict[str, Any]:
    xs = [x for x in values if math.isfinite(x)]
    if not xs:
        return {
            "samples": 0, "mean": None, "stdev": None, "worst": None,
            "p01": None, "p05": None, "expected_shortfall_05": None,
            "block_bootstrap_lower_95": None,
        }
    return {
        "samples": len(xs),
        "mean": statistics.fmean(xs),
        "stdev": statistics.stdev(xs) if len(xs) >= 2 else 0.0,
        "worst": min(xs),
        "p01": quantile(xs, 0.01),
        "p05": quantile(xs, 0.05),
        "expected_shortfall_05": expected_shortfall(xs, 0.05),
        "block_bootstrap_lower_95": moving_block_bootstrap_lower(
            xs, block_size=block_size, draws=draws, alpha=alpha, seed=seed),
    }
