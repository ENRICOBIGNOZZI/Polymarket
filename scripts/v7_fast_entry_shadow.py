#!/usr/bin/env python3
"""Zero-authority fast-entry shadow for BTC M5 execution research.

This process measures the part of entry latency we can actually control without
changing the live PAPER runtime. It polls the already-published External Fair
status, immediately requests one fresh complement-consistent CLOB book batch,
re-runs the existing arrival-time robust-candidate logic, and appends an
immutable research observation. It never writes opportunities, receipts,
orders, fills, positions, the canonical ledger, or portfolio state.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from v7_external_fair_paper_router import (
    Book,
    load,
    live_market_yes,
    parse_book,
    robust_candidates,
    stable_id,
)
from v7_market_common import request_json

SCHEMA = "polymarket_v7_fast_entry_shadow_v1"
STATUS_SCHEMA = "polymarket_v7_fast_entry_shadow_status_v1"


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    temp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _books(
    status: dict[str, Any], *, clob_url: str, timeout_seconds: float,
) -> tuple[dict[str, Book], int, str]:
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    tokens = [str(token) for token in (market.get("yes_token"), market.get("no_token")) if token]
    if len(tokens) != 2 or tokens[0] == tokens[1]:
        return {}, 0, "TOKEN_BINDING_NOT_READY"
    started = time.time_ns()
    try:
        rows = request_json(
            f"{clob_url.rstrip('/')}/books",
            [{"token_id": token} for token in tokens],
            timeout=timeout_seconds,
        )
    except Exception as exc:
        return {}, max(0, (time.time_ns() - started) // 1_000_000), f"BOOK_REQUEST_{type(exc).__name__.upper()}"
    received_ms = time.time_ns() // 1_000_000
    output: dict[str, Book] = {}
    for raw in rows if isinstance(rows, list) else []:
        book = parse_book(raw, received_ms)
        if book is not None and book.token_id in tokens:
            output[book.token_id] = book
    latency_ms = max(0, (time.time_ns() - started) // 1_000_000)
    if len(output) != 2:
        return output, latency_ms, "BOOK_BATCH_INCOMPLETE"
    return output, latency_ms, ""


def observe_once(
    status: dict[str, Any], policy: dict[str, Any], *, model_sha: str,
    clob_url: str, timeout_seconds: float = 0.5, latency_only: bool = False,
) -> dict[str, Any]:
    decision_wall_ns = time.time_ns()
    reason = ""
    if (
        status.get("code_sha") != model_sha
        or status.get("paper_only") is not True
        or status.get("authenticated_execution") is not False
        or status.get("real_order_submission") is not False
    ):
        reason = "STATUS_IDENTITY_OR_SAFETY_INVALID"
        books, latency_ms = {}, 0
    else:
        books, latency_ms, reason = _books(
            status, clob_url=clob_url, timeout_seconds=timeout_seconds,
        )
    market = status.get("market") if isinstance(status.get("market"), dict) else {}
    market_yes = live_market_yes(books, market) if len(books) == 2 else None
    if not reason and len(books) == 2 and market_yes is None:
        reason = "BOOK_BATCH_RECEIVED_BUT_COMPLEMENT_INCOHERENT"
    arrival_wall_ns = time.time_ns()
    exchange_times = [book.exchange_ts_ms for book in books.values()]
    receive_times = [book.receive_ts_ms for book in books.values()]
    exchange_span_ms = max(exchange_times) - min(exchange_times) if exchange_times else None
    oldest_book_age_ms = max(
        (receive - exchange for receive, exchange in zip(receive_times, exchange_times)),
        default=None,
    )
    if latency_only:
        return {
            "schema": SCHEMA,
            "record_id": stable_id(model_sha, "LATENCY_ONLY", decision_wall_ns, market.get("market_id"), latency_ms),
            "code_sha": model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "measurement_mode": "LATENCY_ONLY_NO_ECONOMIC_ENDPOINTS",
            "decision_wall_ns": decision_wall_ns,
            "arrival_wall_ns": arrival_wall_ns,
            "decision_to_fresh_book_ms": latency_ms,
            "book_exchange_timestamp_span_ms": exchange_span_ms,
            "oldest_book_age_at_receive_ms": oldest_book_age_ms,
            "market_id": str(market.get("market_id") or ""),
            "fresh_book_count": len(books),
            "arrival_revalidated": len(books) == 2 and market_yes is not None,
            "reason": reason or "FRESH_COMPLEMENT_CONSISTENT_BOOK_BATCH",
        }
    candidates = robust_candidates(status, books, policy) if not reason else []
    best = max(candidates, key=lambda row: float(row.get("robust_ev") or -math.inf), default=None)
    return {
        "schema": SCHEMA,
        "record_id": stable_id(model_sha, decision_wall_ns, market.get("market_id"), market_yes, latency_ms),
        "code_sha": model_sha,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
        "decision_wall_ns": decision_wall_ns,
        "arrival_wall_ns": arrival_wall_ns,
        "decision_to_fresh_book_ms": latency_ms,
        "market_id": str(market.get("market_id") or ""),
        "event_id": str(market.get("event_id") or ""),
        "market_yes": market_yes,
        "fresh_book_count": len(books),
        "arrival_revalidated": len(books) == 2 and market_yes is not None,
        "candidate_count": len(candidates),
        "best_outcome": str(best.get("outcome") or "") if best else None,
        "best_robust_ev_per_share": float(best["robust_ev"]) if best else None,
        "tte_seconds": float((status.get("fair") or {}).get("tte_seconds")) if isinstance(status.get("fair"), dict) and isinstance((status.get("fair") or {}).get("tte_seconds"), (int, float)) else None,
        "reason": reason or ("ROBUST_CANDIDATE" if best else "NO_ROBUST_CANDIDATE"),
    }


def run(args: argparse.Namespace) -> int:
    run_root = args.run_root.resolve()
    source = run_root / "external_fair" / "status.json"
    output = args.output or run_root / "research" / "evidence" / "fast_entry_shadow" / "events.jsonl"
    status_path = args.status_output or output.with_name("status.json")
    policy_config = load(args.external_fair_config.resolve())
    policy = policy_config.get("taker") if isinstance(policy_config.get("taker"), dict) else {}
    interval = max(0.250, args.scan_interval_ms / 1000.0)
    observations = failures = 0
    while True:
        started = time.monotonic()
        status = load(source)
        row = observe_once(
            status, policy, model_sha=args.model_sha,
            clob_url=args.clob_url, timeout_seconds=args.timeout_seconds,
            latency_only=args.latency_only,
        )
        append_jsonl(output, row)
        observations += 1
        failures += int(not row["arrival_revalidated"])
        atomic_json(status_path, {
            "schema": STATUS_SCHEMA,
            "timestamp_ns": time.time_ns(),
            "code_sha": args.model_sha,
            "paper_only": True,
            "authenticated_execution": False,
            "real_order_submission": False,
            "real_capital_at_risk": False,
            "execution_authority": "ZERO_AUTHORITY_RESEARCH_ONLY",
            "scan_interval_ms": int(interval * 1000),
            "synthetic_revalidation_sleep_ms": 0,
            "measurement_mode": "LATENCY_ONLY_NO_ECONOMIC_ENDPOINTS" if args.latency_only else "FULL_ZERO_AUTHORITY_SHADOW",
            "observations": observations,
            "arrival_revalidation_failures": failures,
            "last": row,
        })
        if args.once:
            return 0
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, interval - elapsed))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--model-sha", required=True)
    parser.add_argument("--external-fair-config", type=Path, default=Path("config/v7_external_fair.json"))
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--scan-interval-ms", type=int, default=250)
    parser.add_argument("--timeout-seconds", type=float, default=0.5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--status-output", type=Path)
    parser.add_argument("--latency-only", action="store_true", help="Measure fresh-book latency without computing or recording economic candidates.")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if len(args.model_sha) != 40 or any(ch not in "0123456789abcdef" for ch in args.model_sha):
        raise SystemExit("exact 40-character model SHA required")
    if args.scan_interval_ms != 250:
        raise SystemExit("research contract requires exactly 250ms scan interval")
    if not 0.05 <= args.timeout_seconds <= 1.0:
        raise SystemExit("timeout must be in [0.05, 1.0] seconds")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
