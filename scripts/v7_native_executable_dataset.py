#!/usr/bin/env python3
"""Build causal executable repricing labels from sealed native decision windows.

Two execution semantics are published separately:
- PAPER_TOP_PARITY: mirrors the current native PAPER taker adapter's top-only FAK.
- VISIBLE_DEPTH_VWAP: sweeps recorded visible depth up to the submitted/frozen limit.

Neither is an observed exchange fill. Missing depth, unknown venue delay, gaps,
partial liquidation, mismatched token identity, or off-grid timing are censored.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from v7_native_repricing_dataset import (
    _source, _integer, _identity, CAPTURE_HORIZONS,
    MAX_TOTAL_BYTES,
)

# Keep the executable consumer on its previous narrow contract. Broader rejected
# origins are useful for repricing diagnostics but are not executable orders.
ORIGIN_REASONS = {1, 15, 16, 17}
from v7_research_economic_contract import canonical_hash

SCHEMA = "polymarket_v7_native_executable_label_v1"
SUMMARY_SCHEMA = "polymarket_v7_native_executable_dataset_summary_v1"
EXECUTION_MODELS = ("PAPER_TOP_PARITY", "VISIBLE_DEPTH_VWAP")
NS_PER_MS = 1_000_000
MICRO = Decimal(1_000_000)


class ExecutableLabelError(ValueError):
    pass


def _decimal(value: Any, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ExecutableLabelError(name)
    try:
        out = Decimal(str(value))
    except Exception as exc:
        raise ExecutableLabelError(name) from exc
    if not out.is_finite():
        raise ExecutableLabelError(name)
    return out


def _depth(row: dict[str, Any], side: str) -> tuple[tuple[int, int], ...]:
    raw = row.get(side)
    if not isinstance(raw, list) or len(raw) > 10:
        raise ExecutableLabelError("depth_shape")
    out: list[tuple[int, int]] = []
    last = None
    tick = _integer(row.get("tick_e4"), 1)
    for level in raw:
        if not isinstance(level, list) or len(level) != 2:
            raise ExecutableLabelError("depth_level_shape")
        price = _integer(level[0], 1)
        qty = _integer(level[1], 1)
        if price >= 10_000 or price % tick:
            raise ExecutableLabelError("depth_price")
        if last is not None:
            if side == "bids" and price >= last:
                raise ExecutableLabelError("bid_order")
            if side == "asks" and price <= last:
                raise ExecutableLabelError("ask_order")
        last = price
        out.append((price, qty))
    return tuple(out)


def _fee_microusd(quantity_microunits: int, price_e4: int,
                  rate: Decimal, exponent: Decimal) -> Decimal:
    if quantity_microunits <= 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 64
        p = Decimal(price_e4) / Decimal(10_000)
        shares = Decimal(quantity_microunits) / MICRO
        per_share = rate * (p * (Decimal(1) - p)) ** exponent
        return shares * per_share * MICRO


def _sweep(row: dict[str, Any], *, side: str, quantity: int, limit_e4: int,
           execution_model: str, fee_rate: Decimal, fee_exponent: Decimal) -> dict[str, Any]:
    if execution_model not in EXECUTION_MODELS or side not in {"BUY", "SELL"}:
        raise ExecutableLabelError("sweep_contract")
    tick = _integer(row.get("tick_e4"), 1)
    if quantity <= 0 or not 0 < limit_e4 < 10_000 or limit_e4 % tick:
        raise ExecutableLabelError("order_contract")
    levels = _depth(row, "asks" if side == "BUY" else "bids")
    if execution_model == "PAPER_TOP_PARITY":
        levels = levels[:1]
    remaining = quantity
    notional = Decimal(0)
    fees = Decimal(0)
    fills: list[list[int]] = []
    with localcontext() as ctx:
        ctx.prec = 64
        for price, visible in levels:
            if side == "BUY" and price > limit_e4:
                break
            if side == "SELL" and price < limit_e4:
                break
            size = min(remaining, visible)
            if size <= 0:
                continue
            fills.append([price, size])
            notional += Decimal(price) / Decimal(10_000) * (Decimal(size) / MICRO)
            fees += _fee_microusd(size, price, fee_rate, fee_exponent) / MICRO
            remaining -= size
            if remaining == 0:
                break
    return {
        "filled_microunits": quantity - remaining,
        "remaining_microunits": remaining,
        "notional_usd": str(notional),
        "fee_usd": str(fees),
        "fills": fills,
    }


def _partition(row: dict[str, Any], source_hash: str) -> tuple[str, ...]:
    code = _identity(row.get("code_sha"))
    if len(code) != 40 or any(c not in "0123456789abcdef" for c in code):
        raise ExecutableLabelError("code_sha")
    capture = row.get("capture_id")
    scope = _identity(capture) if capture else "UNIDENTIFIED_SOURCE:" + source_hash
    server = _identity(row.get("server_id")) if row.get("server_id") else "UNIDENTIFIED_SERVER"
    return (code, _identity(row.get("run_id")), server, scope, _identity(row.get("market_id")))


def _context_equal(origin: dict[str, Any], row: dict[str, Any]) -> bool:
    return all(origin.get(name) == row.get(name) for name in (
        "asset", "horizon", "connection_epoch", "tick_e4", "close_monotonic_ns",
        "paper_terms_sha256", "fee_rate", "fee_exponent", "paper_venue_delay_ns",
        "paper_assumed_transport_delay_ns", "token_id",
    ))


def _delay_ms(origin: dict[str, Any]) -> int:
    venue = origin.get("paper_venue_delay_ns")
    transport = origin.get("paper_assumed_transport_delay_ns")
    if type(venue) is not int or type(transport) is not int or venue < 0 or transport < 0:
        raise ExecutableLabelError("unknown_delay")
    total = venue + transport
    if total <= 0 or total % NS_PER_MS:
        raise ExecutableLabelError("off_grid_delay")
    ms = total // NS_PER_MS
    if ms not in CAPTURE_HORIZONS:
        raise ExecutableLabelError("unsupported_delay_horizon")
    return ms


def _origin_order(origin: dict[str, Any]) -> tuple[int, int]:
    quantity = origin.get("proposed_quantity")
    price_tick = origin.get("proposed_price_tick")
    tick = origin.get("tick_e4")
    if any(type(v) is not int for v in (quantity, price_tick, tick)):
        raise ExecutableLabelError("missing_candidate_order")
    if quantity <= 0 or price_tick <= 0 or tick <= 0:
        raise ExecutableLabelError("missing_candidate_order")
    limit_e4 = price_tick * tick
    cap = origin.get("taker_maximum_entry_price_e4")
    if type(cap) is int and limit_e4 > cap:
        raise ExecutableLabelError("candidate_above_runtime_cap")
    if not 0 < limit_e4 < 10_000:
        raise ExecutableLabelError("candidate_limit")
    minimum = origin.get("minimum_order_microunits")
    if type(minimum) is int and minimum > 0 and quantity < minimum:
        raise ExecutableLabelError("candidate_below_minimum")
    return quantity, limit_e4


def build(paths: list[Path], *, require_closed: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    origins: dict[tuple[Any, ...], tuple[dict, dict]] = {}
    labels: dict[tuple[Any, ...], tuple[dict, dict]] = {}
    gaps: dict[tuple[str, ...], list[int]] = defaultdict(list)
    sources, seen_paths, seen_hashes, seen_captures = [], set(), set(), set()
    invalid = 0
    total_bytes = 0

    for path in map(Path, paths):
        resolved = path.resolve()
        if resolved in seen_paths:
            raise ExecutableLabelError("duplicate_source_path")
        seen_paths.add(resolved)
        rows, source = _source(path, require_closed)
        if source["decoded_sha256"] in seen_hashes:
            raise ExecutableLabelError("duplicate_source_content")
        seen_hashes.add(source["decoded_sha256"])
        total_bytes += source["decoded_bytes"]
        if total_bytes > MAX_TOTAL_BYTES:
            raise ExecutableLabelError("input_budget_exceeded")
        if source["producer_closed"]:
            ck = tuple(rows[0].get(k) for k in ("code_sha", "run_id", "server_id", "capture_id", "market_id"))
            if ck in seen_captures:
                raise ExecutableLabelError("duplicate_capture")
            seen_captures.add(ck)
        sources.append(source)

        for row in rows:
            try:
                if (row.get("schema") != "polymarket_v7_native_observation_v1"
                        or row.get("paper_only") is not True
                        or row.get("execution_authority") is not False):
                    raise ExecutableLabelError("record_contract")
                partition = _partition(row, source["decoded_sha256"])
                kind = _integer(row.get("kind"), 1)
                observed = _integer(row.get("observed_monotonic_ns"), 1)
                if kind == 5:
                    gaps[partition].append(observed)
                    continue
                if kind not in {2, 6}:
                    continue
                version = _integer(row.get("repricing_origin_signal_version"), 1)
                decision_ns = _integer(row.get("decision_monotonic_ns"), 1)
                key = (*partition, version, decision_ns)
                if kind == 2:
                    if row.get("reason") not in ORIGIN_REASONS:
                        continue
                    if key in origins and origins[key][0] != row:
                        raise ExecutableLabelError("conflicting_origin")
                    origins[key] = (row, source)
                else:
                    h = row.get("repricing_horizon_ms")
                    if type(h) is not int or h not in CAPTURE_HORIZONS:
                        raise ExecutableLabelError("invalid_capture_horizon")
                    lk = (*key, h)
                    if lk in labels and labels[lk][0] != row:
                        raise ExecutableLabelError("conflicting_label")
                    labels[lk] = (row, source)
            except ExecutableLabelError:
                invalid += 1

    for values in gaps.values():
        values.sort()

    out: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for key, (origin, origin_source) in sorted(origins.items()):
        try:
            decision_ns = _integer(origin.get("decision_monotonic_ns"), 1)
            token_id = _identity(origin.get("token_id"))
            quantity, entry_limit_e4 = _origin_order(origin)
            delay_ms = _delay_ms(origin)
            fee_rate = _decimal(origin.get("fee_rate"), "fee_rate")
            fee_exponent = _decimal(origin.get("fee_exponent"), "fee_exponent")
            if not (Decimal(0) <= fee_rate <= Decimal(1) and Decimal(0) <= fee_exponent <= Decimal(10)):
                raise ExecutableLabelError("fee_contract")
            arrival_pair = labels.get((*key, delay_ms))
            if arrival_pair is None:
                raise ExecutableLabelError("missing_entry_arrival")
            arrival, arrival_source = arrival_pair
            if arrival.get("token_id") != token_id or not _context_equal(origin, arrival):
                raise ExecutableLabelError("entry_context_mismatch")
            entry_target_ns = decision_ns + delay_ms * NS_PER_MS
            if _integer(arrival.get("observed_monotonic_ns"), 1) < entry_target_ns:
                raise ExecutableLabelError("entry_label_early")
            close_ns = _integer(origin.get("close_monotonic_ns"), decision_ns + 1)
            if entry_target_ns >= close_ns:
                raise ExecutableLabelError("entry_after_close")
            gap_points = gaps[key[:-2]]
            j = bisect_right(gap_points, decision_ns)
            if j < len(gap_points) and gap_points[j] <= arrival["observed_monotonic_ns"]:
                raise ExecutableLabelError("gap_before_entry")
        except (ExecutableLabelError, ValueError):
            excluded["INVALID_OR_UNAVAILABLE_ENTRY"] += 1
            continue

        for exit_decision_ms in sorted(h for h in CAPTURE_HORIZONS if h > delay_ms):
            exit_arrival_ms = exit_decision_ms + delay_ms
            if exit_arrival_ms not in CAPTURE_HORIZONS:
                continue
            decision_pair = labels.get((*key, exit_decision_ms))
            exit_pair = labels.get((*key, exit_arrival_ms))
            if decision_pair is None or exit_pair is None:
                excluded["MISSING_EXIT_GRID"] += 1
                continue
            exit_decision_row, exit_decision_source = decision_pair
            exit_row, exit_source = exit_pair
            try:
                if any(r.get("token_id") != token_id or not _context_equal(origin, r)
                       for r in (exit_decision_row, exit_row)):
                    raise ExecutableLabelError("exit_context_mismatch")
                exit_decision_target = decision_ns + exit_decision_ms * NS_PER_MS
                exit_target = decision_ns + exit_arrival_ms * NS_PER_MS
                if _integer(exit_decision_row.get("observed_monotonic_ns"), 1) < exit_decision_target:
                    raise ExecutableLabelError("exit_decision_early")
                if _integer(exit_row.get("observed_monotonic_ns"), 1) < exit_target:
                    raise ExecutableLabelError("exit_arrival_early")
                if exit_target >= close_ns:
                    raise ExecutableLabelError("exit_after_close")
                j = bisect_right(gap_points, decision_ns)
                if j < len(gap_points) and gap_points[j] <= exit_row["observed_monotonic_ns"]:
                    raise ExecutableLabelError("gap_before_exit")
                exit_bids = _depth(exit_decision_row, "bids")
                if not exit_bids:
                    raise ExecutableLabelError("no_exit_bid")
                frozen_exit_limit = exit_bids[0][0]
            except (ExecutableLabelError, ValueError):
                excluded["INVALID_OR_UNAVAILABLE_EXIT"] += 1
                continue

            for execution_model in EXECUTION_MODELS:
                try:
                    entry = _sweep(arrival, side="BUY", quantity=quantity,
                                   limit_e4=entry_limit_e4, execution_model=execution_model,
                                   fee_rate=fee_rate, fee_exponent=fee_exponent)
                    filled = entry["filled_microunits"]
                    if filled <= 0:
                        status = "NO_FILL"
                        exit_result = {
                            "filled_microunits": 0, "remaining_microunits": 0,
                            "notional_usd": "0", "fee_usd": "0", "fills": [],
                        }
                        cash_delta = Decimal(0)
                        net_pnl = Decimal(0)
                        residual = 0
                    else:
                        exit_result = _sweep(exit_row, side="SELL", quantity=filled,
                                             limit_e4=frozen_exit_limit,
                                             execution_model=execution_model,
                                             fee_rate=fee_rate, fee_exponent=fee_exponent)
                        residual = filled - exit_result["filled_microunits"]
                        cash_delta = (Decimal(exit_result["notional_usd"])
                                      - Decimal(exit_result["fee_usd"])
                                      - Decimal(entry["notional_usd"])
                                      - Decimal(entry["fee_usd"]))
                        if residual:
                            status = "OPEN_RESIDUAL"
                            net_pnl = None
                        elif filled < quantity:
                            status = "PARTIAL_ENTRY_ROUND_TRIP"
                            net_pnl = cash_delta
                        else:
                            status = "FULL_ROUND_TRIP"
                            net_pnl = cash_delta
                    record = {
                        "schema": SCHEMA, "version": 1,
                        "paper_only": True, "authenticated_execution": False,
                        "real_order_submission": False, "real_capital_at_risk": False,
                        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
                        "evidence_kind": "VISIBLE_BOOK_COUNTERFACTUAL_NOT_OBSERVED_FILL",
                        "execution_model": execution_model,
                        "code_sha": key[0], "run_id": key[1], "server_id": origin.get("server_id"),
                        "capture_id": origin.get("capture_id"), "market_id": key[4],
                        "origin_signal_version": key[5], "decision_monotonic_ns": decision_ns,
                        "asset": origin.get("asset"), "horizon": origin.get("horizon"),
                        "token_id": token_id, "direction": origin.get("direction"),
                        "delay_ms": delay_ms, "entry_horizon_ms": delay_ms,
                        "exit_decision_horizon_ms": exit_decision_ms,
                        "exit_arrival_horizon_ms": exit_arrival_ms,
                        "holding_after_entry_ms": exit_decision_ms - delay_ms,
                        "quantity_microunits": quantity,
                        "entry_limit_e4": entry_limit_e4,
                        "exit_limit_e4": frozen_exit_limit,
                        "fee_rate": str(fee_rate), "fee_exponent": str(fee_exponent),
                        "fee_source": origin.get("fee_source"),
                        "paper_terms_sha256": origin.get("paper_terms_sha256"),
                        "entry": entry, "exit": exit_result,
                        "status": status, "remaining_inventory_microunits": residual,
                        "entry_unfilled_microunits": quantity - filled,
                        "cash_delta_usd": str(cash_delta),
                        "net_pnl_usd": str(net_pnl) if net_pnl is not None else None,
                        "origin_features": {
                            "binance_return_100ms_bp": origin.get("binance_return_100ms_bp"),
                            "confirmation_return_100ms_bp": origin.get("confirmation_return_100ms_bp"),
                            "confirmation_venue": origin.get("confirmation_venue"),
                            "signal_age_ns": origin.get("signal_age_ns"),
                            "tte_ns": origin.get("tte_ns"),
                            "external_features": origin.get("external_features"),
                            "slow_context": origin.get("slow_context"),
                        },
                        "source_hashes": {
                            "origin": origin_source["decoded_sha256"],
                            "entry": arrival_source["decoded_sha256"],
                            "exit_decision": exit_decision_source["decoded_sha256"],
                            "exit": exit_source["decoded_sha256"],
                        },
                        "record_hashes": {
                            "origin": canonical_hash(origin),
                            "entry": canonical_hash(arrival),
                            "exit_decision": canonical_hash(exit_decision_row),
                            "exit": canonical_hash(exit_row),
                        },
                        "producer_closed": bool(origin_source["producer_closed"]
                                                and arrival_source["producer_closed"]
                                                and exit_decision_source["producer_closed"]
                                                and exit_source["producer_closed"]),
                        "queue_priority_claim": False,
                        "matching_guarantee_claim": False,
                        "self_impact_modelled": False,
                        "eligible_for_policy_research": True,
                        "eligible_for_real_money_authority": False,
                    }
                    record["label_hash"] = canonical_hash(record)
                    out.append(record)
                except (ExecutableLabelError, ValueError, ArithmeticError):
                    excluded["EXECUTION_COUNTERFACTUAL_INVALID"] += 1

    out.sort(key=lambda r: (
        r["code_sha"], r["run_id"], str(r["capture_id"]), r["decision_monotonic_ns"],
        r["market_id"], r["execution_model"], r["exit_arrival_horizon_ms"],
    ))
    summary = {
        "schema": SUMMARY_SCHEMA, "version": 1, "paper_only": True,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "sources": sources, "origins": len(origins), "labels": len(out),
        "invalid_records": invalid, "excluded": dict(sorted(excluded.items())),
        "capture_horizons_ms": sorted(CAPTURE_HORIZONS),
        "execution_models": list(EXECUTION_MODELS),
        "observed_fill_claim": False, "real_money_authority": False,
        "require_closed": require_closed,
    }
    return out, summary


def _publish(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ExecutableLabelError("immutable_output_exists")
    fd, temporary = tempfile.mkstemp(prefix=".native-executable-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--allow-unsealed-diagnostics", action="store_true")
    args = parser.parse_args()
    if args.output.resolve() == args.summary.resolve():
        parser.error("output and summary must differ")
    try:
        rows, summary = build(args.input, require_closed=not args.allow_unsealed_diagnostics)
        data = "".join(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
                       for row in rows)
        if len(data.encode()) > MAX_TOTAL_BYTES:
            raise ExecutableLabelError("output_budget_exceeded")
        summary["output_sha256"] = hashlib.sha256(data.encode()).hexdigest()
        summary["dataset_hash"] = canonical_hash({
            "summary_without_dataset_hash": summary,
            "row_hashes": [row["label_hash"] for row in rows],
        })
        _publish(args.output, data)
        _publish(args.summary, json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n")
    except (OSError, ValueError, ArithmeticError) as exc:
        parser.exit(2, str(exc) + "\n")
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())