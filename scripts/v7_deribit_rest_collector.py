#!/usr/bin/env python3
"""Bounded public Deribit futures and options-surface collector for V7.

The WebSocket runtime owns the low-latency BTC perpetual context.  This
collector deliberately covers discovery and slow-changing derivative fields
through public REST polling, and marks every observation as polling data.  It
has no execution, capital, OMS, ledger, or promotion authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE = "https://www.deribit.com/api/v2/public"
MAX_SHORT_DATED_DAYS = 14.0
MIN_USABLE_SHORT_DATED_QUOTES = 12
MAX_QUOTE_WIDTH_IV = 35.0


class DeribitRestError(ValueError):
    pass


def _fetch(path: str, timeout_s: float) -> tuple[Any, dict[str, Any]]:
    started_mono = time.monotonic_ns()
    request = urllib.request.Request(BASE + path, headers={"User-Agent": "polymarket-v7-external/1"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # nosec B310: fixed HTTPS origin
        raw = response.read()
        status = int(response.status)
    received_wall = time.time_ns()
    received_mono = time.monotonic_ns()
    if status != 200:
        raise DeribitRestError(f"http_status:{status}")
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeribitRestError("invalid_json") from exc
    if not isinstance(envelope, dict) or "error" in envelope or "result" not in envelope:
        raise DeribitRestError("invalid_jsonrpc_result")
    return envelope["result"], {
        "local_receive_wall_ns": received_wall,
        "local_receive_monotonic_ns": received_mono,
        "request_duration_ns": received_mono - started_mono,
        "raw_payload_hash": hashlib.sha256(raw).hexdigest(),
    }


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DeribitRestError(f"invalid_{name}") from exc
    if not math.isfinite(parsed) or (positive and parsed <= 0.0):
        raise DeribitRestError(f"invalid_{name}")
    return parsed


def _active(instruments: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(instruments, list):
        raise DeribitRestError("instruments_not_list")
    return [row for row in instruments if isinstance(row, dict) and row.get("kind") == kind
            and row.get("is_active") is not False]


def _next_future(instruments: list[dict[str, Any]], now_wall_ns: int) -> dict[str, Any]:
    candidates: list[tuple[int, dict[str, Any]]] = []
    for row in instruments:
        name = row.get("instrument_name")
        delivery = row.get("expiration_timestamp")
        if not isinstance(name, str) or name == "BTC-PERPETUAL":
            continue
        try:
            delivery_ns = int(delivery) * 1_000_000
        except (TypeError, ValueError):
            continue
        if delivery_ns > now_wall_ns:
            candidates.append((delivery_ns, row))
    if not candidates:
        raise DeribitRestError("nearest_future_unavailable")
    return min(candidates, key=lambda item: item[0])[1]


def _option_surface(instruments: list[dict[str, Any]], summaries: Any) -> list[dict[str, Any]]:
    if not isinstance(summaries, list):
        raise DeribitRestError("option_summaries_not_list")
    metadata = {row.get("instrument_name"): row for row in instruments if isinstance(row.get("instrument_name"), str)}
    surface: list[dict[str, Any]] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        name = summary.get("instrument_name")
        instrument = metadata.get(name)
        if instrument is None:
            continue
        try:
            mark_iv = _number(summary.get("mark_iv"), "option_mark_iv", positive=True)
            mark_price = _number(summary.get("mark_price"), "option_mark_price", positive=True)
            strike = _number(instrument.get("strike"), "option_strike", positive=True)
            expiry_ms = int(instrument.get("expiration_timestamp"))
        except DeribitRestError:
            continue
        option_type = instrument.get("option_type")
        if option_type not in {"call", "put"}:
            continue
        surface.append({
            "instrument_id": name, "expiry_ms": expiry_ms, "strike": strike,
            "option_type": option_type, "mark_iv": mark_iv, "mark_price": mark_price,
            "bid_iv": summary.get("bid_iv"), "ask_iv": summary.get("ask_iv"),
            "underlying_price": summary.get("underlying_price"),
            "open_interest": summary.get("open_interest"),
            "greeks": summary.get("greeks"),
        })
    if not surface:
        raise DeribitRestError("option_surface_empty")
    return sorted(surface, key=lambda row: (row["expiry_ms"], row["strike"], row["option_type"]))


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else 0.5 * (ordered[middle - 1] + ordered[middle])


def _surface_features(surface: list[dict[str, Any]], *, spot: float, now_wall_ns: int) -> dict[str, Any]:
    """Derive only diagnostics that the currently observed quotes support.

    This intentionally refuses interpolation, tail probabilities, or a
    probability distribution when the short-dated surface does not contain a
    sufficiently broad two-sided quote set.
    """
    now_ms = now_wall_ns // 1_000_000
    failures: list[str] = []
    short: list[dict[str, Any]] = []
    quote_widths: list[float] = []
    for row in surface:
        expiry_ms = int(row["expiry_ms"])
        days = (expiry_ms - now_ms) / 86_400_000.0
        moneyness = float(row["strike"]) / spot
        if not (0.0 < days <= MAX_SHORT_DATED_DAYS and 0.80 <= moneyness <= 1.20):
            continue
        point = {**row, "days_to_expiry": days, "moneyness": moneyness}
        try:
            bid_iv = _number(row.get("bid_iv"), "bid_iv", positive=True)
            ask_iv = _number(row.get("ask_iv"), "ask_iv", positive=True)
        except DeribitRestError:
            point["quote_width_iv"] = None
        else:
            if ask_iv < bid_iv:
                continue
            point["quote_width_iv"] = ask_iv - bid_iv
            quote_widths.append(ask_iv - bid_iv)
        short.append(point)
    if len(short) < MIN_USABLE_SHORT_DATED_QUOTES:
        failures.append("MINIMUM_SHORT_DATED_QUOTE_COUNT_NOT_MET")

    grouped: dict[int, list[dict[str, Any]]] = {}
    for point in short:
        grouped.setdefault(int(point["expiry_ms"]), []).append(point)
    expiries = sorted(grouped)
    atm_by_expiry: dict[int, float] = {}
    risk_reversal_by_expiry: dict[int, float] = {}
    butterfly_by_expiry: dict[int, float] = {}
    monotonicity_violations = 0
    for expiry in expiries:
        points = grouped[expiry]
        calls = [point for point in points if point["option_type"] == "call"]
        puts = [point for point in points if point["option_type"] == "put"]
        atm = [float(point["mark_iv"]) for point in points if 0.975 <= float(point["moneyness"]) <= 1.025]
        value = _median(atm)
        if value is not None:
            atm_by_expiry[expiry] = value
        call_wing = _median([float(point["mark_iv"]) for point in calls if float(point["moneyness"]) >= 1.025])
        put_wing = _median([float(point["mark_iv"]) for point in puts if float(point["moneyness"]) <= 0.975])
        if call_wing is not None and put_wing is not None:
            risk_reversal_by_expiry[expiry] = call_wing - put_wing
        if value is not None and call_wing is not None and put_wing is not None:
            butterfly_by_expiry[expiry] = 0.5 * (call_wing + put_wing) - value
        for side in (calls, puts):
            ordered = sorted(side, key=lambda point: float(point["strike"]))
            for left, right in zip(ordered, ordered[1:]):
                # In BTC option price units, calls should not rise with strike
                # and puts should not fall. This is a diagnostic only because
                # sparse/noisy marks can otherwise falsely invalidate a tape.
                if side is calls and float(right["mark_price"]) > float(left["mark_price"]):
                    monotonicity_violations += 1
                if side is puts and float(right["mark_price"]) < float(left["mark_price"]):
                    monotonicity_violations += 1
    if not atm_by_expiry:
        failures.append("ATM_IV_UNAVAILABLE")
    if quote_widths and max(quote_widths) > MAX_QUOTE_WIDTH_IV:
        failures.append("OPTION_QUOTE_WIDTH_EXCESSIVE")
    if len(atm_by_expiry) < 2:
        failures.append("VOL_TERM_SLOPE_UNAVAILABLE")

    term_slope = None
    if len(atm_by_expiry) >= 2:
        first, second = sorted(atm_by_expiry)[:2]
        elapsed_days = (second - first) / 86_400_000.0
        if elapsed_days > 0.0:
            term_slope = (atm_by_expiry[second] - atm_by_expiry[first]) / elapsed_days
    first_expiry = expiries[0] if expiries else None
    return {
        "valid": not failures,
        "health": "VALID" if not failures else "INVALID",
        "failure_reasons": failures,
        "raw_surface_points": len(surface), "short_dated_quote_count": len(short),
        "short_dated_expiry_count": len(expiries), "max_short_dated_days": MAX_SHORT_DATED_DAYS,
        "minimum_short_dated_quote_count": MIN_USABLE_SHORT_DATED_QUOTES,
        "option_surface_width_iv": _median(quote_widths),
        "option_surface_max_width_iv": max(quote_widths) if quote_widths else None,
        "atm_iv": atm_by_expiry.get(first_expiry) if first_expiry is not None else None,
        "atm_iv_by_expiry": {str(key): value for key, value in atm_by_expiry.items()},
        "vol_term_slope_iv_per_day": term_slope,
        "put_call_skew_moneyness_proxy": risk_reversal_by_expiry.get(first_expiry) if first_expiry is not None else None,
        "butterfly_iv_moneyness_proxy": butterfly_by_expiry.get(first_expiry) if first_expiry is not None else None,
        "monotonicity_violations": monotonicity_violations,
        "interpolation_used": False, "tail_probability_available": False,
        "greeks_available": any(point.get("greeks") is not None for point in short),
    }


def collect_once(*, timeout_s: float = 10.0) -> tuple[dict[str, Any], dict[str, Any]]:
    query = urllib.parse.urlencode({"currency": "BTC", "expired": "false"})
    instruments, instruments_meta = _fetch(f"/get_instruments?{query}", timeout_s)
    futures = _active(instruments, "future")
    options = _active(instruments, "option")
    next_future = _next_future(futures, int(instruments_meta["local_receive_wall_ns"]))
    option_summary, option_summary_meta = _fetch(
        "/get_book_summary_by_currency?" + urllib.parse.urlencode({"currency": "BTC", "kind": "option"}), timeout_s)
    perpetual, perpetual_meta = _fetch("/ticker?" + urllib.parse.urlencode({"instrument_name": "BTC-PERPETUAL"}), timeout_s)
    future_name = next_future["instrument_name"]
    future, future_meta = _fetch("/ticker?" + urllib.parse.urlencode({"instrument_name": future_name}), timeout_s)
    historical_volatility, volatility_meta = _fetch("/get_historical_volatility?currency=BTC", timeout_s)
    if not isinstance(perpetual, dict) or not isinstance(future, dict) or not isinstance(historical_volatility, list):
        raise DeribitRestError("ticker_or_volatility_shape_invalid")
    surface = _option_surface(options, option_summary)
    mark = _number(perpetual.get("mark_price"), "perpetual_mark_price", positive=True)
    index = _number(perpetual.get("index_price"), "perpetual_index_price", positive=True)
    future_mark = _number(future.get("mark_price"), "future_mark_price", positive=True)
    derived_surface = _surface_features(
        surface, spot=mark, now_wall_ns=int(instruments_meta["local_receive_wall_ns"]))
    receive_mono = max(int(meta["local_receive_monotonic_ns"]) for meta in
                       (instruments_meta, option_summary_meta, perpetual_meta, future_meta, volatility_meta))
    row = {
        "schema": "polymarket_v7_deribit_rest_observation_v1",
        "source_id": "deribit_btc", "provider": "Deribit", "venue": "DERIBIT",
        "asset": "BTC", "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
        "perpetual": {"instrument_id": "BTC-PERPETUAL", "mark_price": mark, "index_price": index,
                      "current_funding": perpetual.get("current_funding"), "funding_8h": perpetual.get("funding_8h"),
                      "open_interest": perpetual.get("open_interest")},
        "nearest_future": {"instrument_id": future_name, "expiry_ms": next_future.get("expiration_timestamp"),
                           "mark_price": future_mark, "open_interest": future.get("open_interest"),
                           "basis_bps_to_perpetual": (future_mark / mark - 1.0) * 10_000.0},
        "option_surface": surface,
        "option_surface_features": derived_surface,
        "historical_volatility": historical_volatility[-1] if historical_volatility else None,
        "instrument_request": instruments_meta, "option_summary_request": option_summary_meta,
        "perpetual_request": perpetual_meta, "future_request": future_meta, "volatility_request": volatility_meta,
        "local_receive_monotonic_ns": receive_mono,
        "paper_only": True, "authenticated_execution": False, "real_order_submission": False,
        "execution_authority": False, "capital_authority": False, "oms_authority": False,
        "ledger_writer_authority": False, "promotion_authority": False,
    }
    status = {"schema": "polymarket_v7_deribit_rest_status_v1", "state": "OPERATIONAL",
              "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
              "option_surface_points": len(surface), "option_surface_valid": derived_surface["valid"],
              "option_surface_health": derived_surface["health"], "nearest_future": future_name,
              "latest": row,
              "blocker": None if derived_surface["valid"] else "OPTION_SURFACE_FEATURES_INVALID"}
    return row, status


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    while True:
        try:
            row, status = collect_once()
            args.tape.parent.mkdir(parents=True, exist_ok=True)
            with args.tape.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        except (DeribitRestError, OSError, urllib.error.URLError) as exc:
            status = {"schema": "polymarket_v7_deribit_rest_status_v1", "state": "DEGRADED",
                      "transport": "PUBLIC_REST_POLLING", "polling_latency_not_event_latency": True,
                      "blocker": str(exc), "execution_authority": False}
        _write_json(args.status, status)
        if not args.loop:
            return 0 if status["state"] == "OPERATIONAL" else 2
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
