#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any


def normalize_timestamp_ms(value: Any) -> int:
    try:
        ts = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    while ts >= 10**15:
        ts //= 1000
    if ts < 10**11:
        ts *= 1000
    return ts


def quantile(values: list[int], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = max(0.0, min(1.0, q)) * (len(ordered) - 1)
    lo = int(pos)
    hi = min(len(ordered) - 1, lo + 1)
    weight = pos - lo
    return float(ordered[lo] * (1.0 - weight) + ordered[hi] * weight)


def _request_json(url: str, body: Any | None = None, timeout: float = 20.0) -> Any:
    headers = {"User-Agent": "polymarket-v7-rest-provenance-research/1.0"}
    data = None
    method = "GET"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _token_ids(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x)]


def discover_markets(gamma_url: str, limit: int, min_liquidity: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    page_size = 100
    while len(out) < limit and offset < 10000:
        query = urllib.parse.urlencode(
            {
                "active": "true",
                "closed": "false",
                "limit": page_size,
                "offset": offset,
                "order": "liquidityNum",
                "ascending": "false",
                "liquidity_num_min": f"{min_liquidity:.12g}",
            }
        )
        payload = _request_json(f"{gamma_url.rstrip('/')}/markets?{query}")
        rows = payload if isinstance(payload, list) else payload.get("markets", [])
        if not isinstance(rows, list):
            raise RuntimeError("unexpected Gamma markets response")
        for row in rows:
            if not isinstance(row, dict):
                continue
            market_id = str(row.get("id", ""))
            tokens = _token_ids(row.get("clobTokenIds"))
            if not market_id or len(tokens) < 2 or market_id in seen:
                continue
            event_id = str(row.get("eventId", ""))
            if not event_id and isinstance(row.get("events"), list) and row["events"]:
                event = row["events"][0]
                if isinstance(event, dict):
                    event_id = str(event.get("id", ""))
            seen.add(market_id)
            out.append(
                {
                    "market_id": market_id,
                    "event_id": event_id,
                    "yes_token": tokens[0],
                    "no_token": tokens[1],
                    "liquidity": float(row.get("liquidityNum") or row.get("liquidity") or 0.0),
                }
            )
            if len(out) >= limit:
                break
        if len(rows) < page_size:
            break
        offset += page_size
    return out


def fetch_books(clob_url: str, tokens: list[str], batch_size: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for pos in range(0, len(tokens), batch_size):
        batch = tokens[pos : pos + batch_size]
        payload = [{"token_id": token} for token in batch]
        rows = _request_json(f"{clob_url.rstrip('/')}/books", payload)
        received_ms = time.time_ns() // 1_000_000
        if not isinstance(rows, list):
            raise RuntimeError("unexpected CLOB /books response")
        for row in rows:
            if not isinstance(row, dict):
                continue
            token = str(row.get("asset_id", ""))
            if not token:
                continue
            exchange_ms = normalize_timestamp_ms(row.get("timestamp"))
            snapshot_hash = str(row.get("hash", ""))
            out[token] = {
                "token_id": token,
                "exchange_ts_ms": exchange_ms,
                "received_ts_ms": received_ms,
                "snapshot_hash": snapshot_hash,
                "age_ms": received_ms - exchange_ms if exchange_ms > 0 else None,
                "bids": len(row.get("bids", [])) if isinstance(row.get("bids"), list) else 0,
                "asks": len(row.get("asks", [])) if isinstance(row.get("asks"), list) else 0,
            }
    return out


def classify_pair(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    max_age_ms: int,
    max_skew_ms: int,
) -> tuple[bool, str, dict[str, int | None]]:
    if not left or not right:
        return False, "missing_book", {"exchange_skew_ms": None, "receive_skew_ms": None, "max_age_ms": None}
    if (
        int(left.get("exchange_ts_ms") or 0) <= 0
        or int(right.get("exchange_ts_ms") or 0) <= 0
        or not str(left.get("snapshot_hash") or "")
        or not str(right.get("snapshot_hash") or "")
    ):
        return False, "missing_provenance", {"exchange_skew_ms": None, "receive_skew_ms": None, "max_age_ms": None}

    le = int(left["exchange_ts_ms"])
    re = int(right["exchange_ts_ms"])
    lr = int(left["received_ts_ms"])
    rr = int(right["received_ts_ms"])
    decision_ms = max(lr, rr)
    ages = [decision_ms - le, decision_ms - re]
    exchange_skew = abs(le - re)
    receive_skew = abs(lr - rr)
    metrics = {
        "exchange_skew_ms": exchange_skew,
        "receive_skew_ms": receive_skew,
        "max_age_ms": max(ages),
    }
    if min(ages) < -max_skew_ms:
        return False, "future_exchange_clock", metrics
    if max(ages) > max_age_ms:
        return False, "stale_exchange_clock", metrics
    if exchange_skew > max_skew_ms:
        return False, "exchange_skew", metrics
    if receive_skew > max_skew_ms:
        return False, "receive_skew", metrics
    return True, "eligible", metrics


def summarize_round(
    markets: list[dict[str, Any]],
    books: dict[str, dict[str, Any]],
    max_age_ms: int,
    max_skew_ms: int,
) -> dict[str, Any]:
    requested_tokens = {m["yes_token"] for m in markets} | {m["no_token"] for m in markets}
    provenance = [
        b for token, b in books.items()
        if token in requested_tokens and int(b.get("exchange_ts_ms") or 0) > 0 and str(b.get("snapshot_hash") or "")
    ]
    ages = [int(b["age_ms"]) for b in provenance if b.get("age_ms") is not None]
    reasons: Counter[str] = Counter()
    pair_metrics: list[dict[str, int | None]] = []
    eligible = 0
    for market in markets:
        ok, reason, metrics = classify_pair(
            books.get(market["yes_token"]),
            books.get(market["no_token"]),
            max_age_ms=max_age_ms,
            max_skew_ms=max_skew_ms,
        )
        reasons[reason] += 1
        pair_metrics.append(metrics)
        eligible += int(ok)

    exchange_skews = [int(x["exchange_skew_ms"]) for x in pair_metrics if x["exchange_skew_ms"] is not None]
    pair_ages = [int(x["max_age_ms"]) for x in pair_metrics if x["max_age_ms"] is not None]
    return {
        "requested_tokens": len(requested_tokens),
        "returned_books": sum(1 for token in requested_tokens if token in books),
        "provenance_books": len(provenance),
        "provenance_coverage": len(provenance) / max(1, len(requested_tokens)),
        "book_age_ms": {
            "p50": quantile(ages, 0.50),
            "p90": quantile(ages, 0.90),
            "p99": quantile(ages, 0.99),
            "max": max(ages) if ages else None,
        },
        "binary_pairs": len(markets),
        "strict_eligible_pairs": eligible,
        "strict_eligible_ratio": eligible / max(1, len(markets)),
        "pair_reject_reasons": dict(sorted(reasons.items())),
        "pair_exchange_skew_ms": {
            "p50": quantile(exchange_skews, 0.50),
            "p90": quantile(exchange_skews, 0.90),
            "p99": quantile(exchange_skews, 0.99),
            "max": max(exchange_skews) if exchange_skews else None,
        },
        "pair_max_age_ms": {
            "p50": quantile(pair_ages, 0.50),
            "p90": quantile(pair_ages, 0.90),
            "p99": quantile(pair_ages, 0.99),
            "max": max(pair_ages) if pair_ages else None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe CLOB /books timestamp/hash provenance for V7 fast structural research")
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--markets", type=int, default=1000)
    parser.add_argument("--min-liquidity", type=float, default=2.0)
    parser.add_argument("--sample-markets", type=int, default=250)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--round-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--max-age-ms", type=int, default=5000)
    parser.add_argument("--max-skew-ms", type=int, default=1500)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    discovered = discover_markets(args.gamma_url, args.markets, args.min_liquidity)
    sample = discovered[: max(1, min(args.sample_markets, len(discovered)))]
    if not sample:
        raise RuntimeError("no markets discovered for REST provenance probe")
    tokens: list[str] = []
    for market in sample:
        tokens.extend([market["yes_token"], market["no_token"]])
    tokens = list(dict.fromkeys(tokens))

    rounds: list[dict[str, Any]] = []
    prior: dict[str, dict[str, Any]] | None = None
    for index in range(max(1, args.rounds)):
        books = fetch_books(args.clob_url, tokens, max(1, args.batch_size))
        summary = summarize_round(sample, books, args.max_age_ms, args.max_skew_ms)
        summary["round"] = index + 1
        if prior is not None:
            comparable = set(prior).intersection(books)
            hash_changed = sum(
                1 for token in comparable
                if str(prior[token].get("snapshot_hash")) != str(books[token].get("snapshot_hash"))
            )
            timestamp_advanced = sum(
                1 for token in comparable
                if int(books[token].get("exchange_ts_ms") or 0) > int(prior[token].get("exchange_ts_ms") or 0)
            )
            summary["cross_round"] = {
                "comparable_tokens": len(comparable),
                "hash_changed": hash_changed,
                "timestamp_advanced": timestamp_advanced,
            }
        rounds.append(summary)
        prior = books
        if index + 1 < max(1, args.rounds):
            time.sleep(max(0.0, args.round_sleep_seconds))

    payload = {
        "schema_version": 1,
        "mode": "research_only",
        "real_order_submission": False,
        "paper_only": True,
        "markets_requested": args.markets,
        "markets_discovered": len(discovered),
        "sample_markets": len(sample),
        "sample_tokens": len(tokens),
        "min_liquidity": args.min_liquidity,
        "max_age_ms": args.max_age_ms,
        "max_skew_ms": args.max_skew_ms,
        "rounds": rounds,
        "interpretation": (
            "REST /books timestamp+hash are point-in-time provenance only. They may seed/rebase L2 state "
            "when each required leg passes the same exchange-age and cross-leg-skew gates; they do not "
            "prove gap-free WebSocket continuity or PAPER joint completion."
        ),
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
