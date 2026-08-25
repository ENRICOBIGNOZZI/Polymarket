#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

FIELDS = [
    "bundle_id", "strategy", "event_id", "created_ts", "mode", "expected_edge",
    "max_notional", "market_id", "side", "weight", "limit_price",
    "execution_deadline_ts", "hold_deadline_ts",
]


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
        headers={"User-Agent": "polymarket-v6-paper/queue-filter", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def token_for_market(raw: dict[str, Any], side: str) -> str:
    ids = [str(x) for x in parse_array(raw.get("clobTokenIds"))]
    outcomes = [str(x).strip().upper() for x in parse_array(raw.get("outcomes"))]
    if len(ids) < 2:
        return ""
    wanted = side.strip().upper()
    for idx, outcome in enumerate(outcomes[: len(ids)]):
        if outcome == wanted:
            return ids[idx]
    return ids[0] if wanted == "YES" else ids[1]


def fetch_tokens(gamma: str, legs: list[dict[str, str]]) -> tuple[dict[tuple[str, str], str], list[str]]:
    mapping: dict[tuple[str, str], str] = {}
    failures: list[str] = []
    by_market: dict[str, set[str]] = defaultdict(set)
    for row in legs:
        by_market[row["market_id"]].add(row["side"].upper())
    for market_id, sides in by_market.items():
        try:
            raw = request_json(f"{gamma}/markets/{market_id}")
            if not isinstance(raw, dict):
                raise ValueError("market payload is not an object")
            for side in sides:
                token = token_for_market(raw, side)
                if not token:
                    raise ValueError(f"missing {side} token")
                mapping[(market_id, side)] = token
        except Exception as exc:
            failures.append(f"market:{market_id}:{type(exc).__name__}:{exc}")
    return mapping, failures


def fetch_books(clob: str, tokens: list[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    out: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    unique = list(dict.fromkeys(x for x in tokens if x))
    for start in range(0, len(unique), 80):
        batch = unique[start : start + 80]
        try:
            rows = request_json(f"{clob}/books", [{"token_id": x} for x in batch])
        except Exception as exc:
            failures.append(f"books:{start}:{type(exc).__name__}:{exc}")
            continue
        if not isinstance(rows, list):
            failures.append(f"books:{start}:invalid_payload")
            continue
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            token = str(raw.get("asset_id") or "")
            bids: list[tuple[float, float]] = []
            asks: list[tuple[float, float]] = []
            for level in raw.get("bids", []):
                if not isinstance(level, dict):
                    continue
                p, q = finite(level.get("price")), finite(level.get("size"), 0.0)
                if math.isfinite(p) and 0.0 < p < 1.0 and q > 0.0:
                    bids.append((p, q))
            for level in raw.get("asks", []):
                if not isinstance(level, dict):
                    continue
                p, q = finite(level.get("price")), finite(level.get("size"), 0.0)
                if math.isfinite(p) and 0.0 < p < 1.0 and q > 0.0:
                    asks.append((p, q))
            bids.sort(reverse=True)
            asks.sort()
            if not token or not bids or not asks:
                continue
            out[token] = {
                "bids": bids,
                "asks": asks,
                "best_bid": bids[0][0],
                "best_ask": asks[0][0],
                "min_order": max(1.0, finite(raw.get("min_order_size"), 1.0)),
            }
    return out, failures


def queue_at_limit(book: dict[str, Any], limit: float) -> float:
    tol = 1e-9
    return sum(q for p, q in book["bids"] if abs(p - limit) <= tol)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or any(field not in reader.fieldnames for field in FIELDS):
            return []
        return [{field: str(row.get(field, "")) for field in FIELDS} for row in reader]


def atomic_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def evaluate_bundle(
    rows: list[dict[str, str]],
    token_map: dict[tuple[str, str], str],
    books: dict[str, dict[str, Any]],
    max_queue_ratio: float,
) -> tuple[bool, str, float, list[dict[str, Any]]]:
    if not rows:
        return False, "empty_bundle", math.inf, []
    expected_edge = min(finite(r["expected_edge"], -math.inf) for r in rows)
    max_notional = min(finite(r["max_notional"], 0.0) for r in rows)
    capital_per_unit = sum(max(0.0, finite(r["weight"], 0.0)) * max(0.0, finite(r["limit_price"], 0.0)) for r in rows)
    if not math.isfinite(expected_edge) or expected_edge <= 0.0 or max_notional <= 0.0 or capital_per_unit <= 1e-12:
        return False, "invalid_economics", math.inf, []
    optimistic_units = max_notional / capital_per_unit
    diagnostics: list[dict[str, Any]] = []
    max_ratio = 0.0
    for row in rows:
        market_id, side = row["market_id"], row["side"].upper()
        token = token_map.get((market_id, side), "")
        book = books.get(token)
        if not token or not book:
            return False, "missing_book", math.inf, diagnostics
        limit = finite(row["limit_price"])
        weight = finite(row["weight"], 0.0)
        if not math.isfinite(limit) or limit <= 0.0 or weight <= 0.0:
            return False, "invalid_leg", math.inf, diagnostics
        best_bid, best_ask = float(book["best_bid"]), float(book["best_ask"])
        # The current multileg executor is maker-only and will reprice/cancel stale
        # limits. Reject stale rows here instead of reserving capital behind them.
        if best_bid > limit + 1e-9 or limit >= best_ask - 1e-12:
            return False, "stale_limit", math.inf, diagnostics
        target = optimistic_units * weight
        denominator = max(target, float(book["min_order"]), 1e-9)
        queue = queue_at_limit(book, limit)
        ratio = queue / denominator
        max_ratio = max(max_ratio, ratio)
        diagnostics.append({
            "market_id": market_id,
            "side": side,
            "limit_price": limit,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "queue_ahead": queue,
            "optimistic_target_shares": target,
            "queue_to_target": ratio,
        })
    if max_queue_ratio > 0.0 and max_ratio > max_queue_ratio:
        return False, "queue_ratio", max_ratio, diagnostics
    return True, "accepted", max_ratio, diagnostics


def main() -> int:
    ap = argparse.ArgumentParser(description="Fail-closed queue-aware admission for V6 passive multileg intents.")
    ap.add_argument("--config", type=Path, default=Path("config/paper_v6.json"))
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--max-queue-ratio", type=float, default=50.0)
    ap.add_argument("--max-age-seconds", type=int, default=240)
    args = ap.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    gamma, clob = str(cfg["gamma_url"]), str(cfg["clob_url"])
    now = int(time.time())
    input_rows = read_rows(args.input)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    stale_rows = 0
    for row in input_rows:
        created = int(finite(row["created_ts"], 0.0))
        if args.max_age_seconds > 0 and (created <= 0 or now - created > args.max_age_seconds):
            stale_rows += 1
            continue
        grouped[row["bundle_id"]].append(row)

    token_map, failures = fetch_tokens(gamma, [row for rows in grouped.values() for row in rows])
    books, book_failures = fetch_books(clob, list(token_map.values()))
    failures.extend(book_failures)

    accepted_rows: list[dict[str, str]] = []
    rejections: dict[str, int] = defaultdict(int)
    accepted_bundles = 0
    max_ratio_seen = 0.0
    top_rejections: list[dict[str, Any]] = []
    for bundle_id, rows in grouped.items():
        ok, reason, ratio, leg_diag = evaluate_bundle(rows, token_map, books, args.max_queue_ratio)
        if math.isfinite(ratio):
            max_ratio_seen = max(max_ratio_seen, ratio)
        if ok:
            accepted_rows.extend(rows)
            accepted_bundles += 1
            continue
        rejections[reason] += 1
        top_rejections.append({
            "bundle_id": bundle_id,
            "strategy": rows[0].get("strategy", "") if rows else "",
            "reason": reason,
            "max_queue_to_target": ratio if math.isfinite(ratio) else None,
            "legs": leg_diag,
        })

    top_rejections.sort(key=lambda x: float(x["max_queue_to_target"] or -1.0), reverse=True)
    top_rejections = top_rejections[:10]
    atomic_csv(args.output, accepted_rows)
    status = {
        "timestamp": now,
        "paper_only": True,
        "input_rows": len(input_rows),
        "input_bundles": len(grouped),
        "accepted_rows": len(accepted_rows),
        "accepted_bundles": accepted_bundles,
        "stale_rows": stale_rows,
        "max_queue_ratio": args.max_queue_ratio,
        "max_queue_ratio_seen": max_ratio_seen,
        "rejections": dict(sorted(rejections.items())),
        "top_rejections": top_rejections,
        "failures": failures,
    }
    args.status.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.status.with_suffix(args.status.suffix + ".tmp")
    tmp.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, args.status)
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
