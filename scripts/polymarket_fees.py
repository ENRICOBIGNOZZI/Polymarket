#!/usr/bin/env python3
"""Authoritative Polymarket fee-schedule parsing and resolution.

Per-market descriptors are the economic source of truth.  Category-wide
constants and the CLOB /fee-rate signing endpoint are deliberately not used as
PnL fallbacks.  Missing economic fee metadata therefore fails closed.
"""
from __future__ import annotations

import math
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class FeeDetails:
    enabled: bool
    rate: float
    exponent: float = 1.0
    taker_only: bool = True
    source: str = "unknown"


class FeeScheduleUnavailable(RuntimeError):
    """Raised when neither Gamma nor the CLOB identifies a market's fee."""


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return default


def _from_object(
    raw: dict[str, Any], source: str, enabled_hint: bool | None = None
) -> FeeDetails | None:
    schedule = raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else None
    if schedule is None and isinstance(raw.get("fd"), dict):
        schedule = raw["fd"]
    if schedule is None and any(
        key in raw for key in ("rate", "feeRate", "r", "exponent", "e", "takerOnly", "to")
    ):
        schedule = raw
    if schedule is None:
        return None

    rate = _finite(schedule.get("rate", schedule.get("feeRate", schedule.get("r"))))
    if not math.isfinite(rate):
        return None
    exponent = max(0.0, _finite(schedule.get("exponent", schedule.get("e")), 1.0))
    taker_only = _bool(schedule.get("takerOnly", schedule.get("to")), True)
    rate = max(0.0, rate)
    enabled = rate > 0.0 if enabled_hint is None else bool(enabled_hint and rate > 0.0)
    return FeeDetails(enabled, rate, exponent, taker_only, source)


def parse_fee_details(raw: dict[str, Any], source: str = "market") -> FeeDetails | None:
    if "feesEnabled" in raw and not _bool(raw.get("feesEnabled"), True):
        return FeeDetails(False, 0.0, 1.0, True, f"{source}:fees_disabled")
    enabled_hint = _bool(raw.get("feesEnabled"), False) if "feesEnabled" in raw else None
    return _from_object(raw, f"{source}:fee_schedule", enabled_hint)


def _request(request_json: Callable[..., Any], url: str) -> Any:
    try:
        return request_json(url, None, 10)
    except TypeError:
        return request_json(url)


def resolve_fee_details(
    raw: dict[str, Any], clob: str, request_json: Callable[..., Any]
) -> FeeDetails:
    details = parse_fee_details(raw)
    if details is not None:
        return details

    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "")
    if condition_id:
        try:
            url = clob.rstrip("/") + "/clob-markets/" + urllib.parse.quote(condition_id, safe="")
            descriptor = _request(request_json, url)
            if isinstance(descriptor, dict):
                details = parse_fee_details(descriptor, "clob")
                if details is not None:
                    return details
        except Exception as exc:
            raise FeeScheduleUnavailable(
                f"fee schedule unavailable for condition {condition_id}: {type(exc).__name__}"
            ) from exc

    market_id = str(raw.get("id") or condition_id or "unknown")
    raise FeeScheduleUnavailable(f"fee schedule unavailable for market {market_id}")


def fee_per_share(price: float, details: FeeDetails, *, taker: bool = True) -> float:
    if (
        not 0.0 < price < 1.0
        or not details.enabled
        or details.rate <= 0.0
        or (details.taker_only and not taker)
    ):
        return 0.0
    return details.rate * (price * (1.0 - price)) ** max(0.0, details.exponent)
