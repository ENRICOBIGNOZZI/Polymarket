#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import v6_relation_intents as relation

DATA_URL = "https://data-api.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"


def parse_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        data = raw.get("data", [])
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    return []


def row_timestamp(row: dict[str, Any]) -> int:
    value = row.get("timestamp", 0)
    try:
        ts = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return ts // 1000 if ts > 10_000_000_000 else ts


def summarize_rows(rows: list[dict[str, Any]], start: int, end: int,
                   discovered: set[str]) -> dict[str, Any]:
    timestamps = [row_timestamp(row) for row in rows]
    timestamps = [ts for ts in timestamps if ts > 0]
    recent = [row for row in rows if start <= row_timestamp(row) <= end]
    matches = [row for row in rows if str(row.get("conditionId") or "") in discovered]
    recent_matches = [
        row for row in recent if str(row.get("conditionId") or "") in discovered
    ]
    return {
        "response_rows": len(rows),
        "min_event_ts": min(timestamps) if timestamps else 0,
        "max_event_ts": max(timestamps) if timestamps else 0,
        "local_window_rows": len(recent),
        "discovered_condition_matches": len(matches),
        "local_window_discovered_matches": len(recent_matches),
    }


def request_rows(url: str, timeout: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-v6-paper-diagnostic/1"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = int(getattr(response, "status", 200))
            body = response.read().decode("utf-8")
        raw = json.loads(body)
        rows = parse_rows(raw)
        return rows, {
            "http_status": status,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": "",
        }
    except Exception as exc:
        return [], {
            "http_status": 0,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}:{exc}",
        }


def trades_url(*, conditions: list[str] | None = None, start: int | None = None,
               end: int | None = None, limit: int = 1000) -> str:
    parts = [f"limit={int(limit)}", "offset=0", "takerOnly=true"]
    if start is not None:
        parts.append(f"start={int(start)}")
    if end is not None:
        parts.append(f"end={int(end)}")
    if conditions:
        value = urllib.parse.quote(",".join(conditions), safe=",")
        parts.append(f"market={value}")
    return f"{DATA_URL}/trades?{'&'.join(parts)}"


def classify(probes: dict[str, dict[str, Any]]) -> str:
    windowed = probes.get("batch_windowed", {})
    unwindowed = probes.get("batch_unwindowed", {})
    single = probes.get("single_windowed", {})
    global_probe = probes.get("global_recent", {})
    if int(windowed.get("local_window_discovered_matches", 0)) > 0:
        return "canonical_query_has_recent_data"
    if int(unwindowed.get("local_window_discovered_matches", 0)) > 0:
        return "server_window_filter_interaction"
    if int(single.get("local_window_discovered_matches", 0)) > 0:
        return "csv_market_filter_interaction"
    if int(global_probe.get("local_window_rows", 0)) > 0:
        if int(global_probe.get("local_window_discovered_matches", 0)) > 0:
            return "batch_filter_interaction_or_sampling_effect"
        return "global_tape_active_but_sampled_universe_has_no_recent_matches"
    if any(str(p.get("error") or "") for p in probes.values()):
        return "transport_or_upstream_error"
    return "global_recent_tape_empty_or_index_lag"


def run(markets: int, min_liquidity: float, lookback_seconds: int,
        sample_conditions: int, timeout: int) -> dict[str, Any]:
    now = int(time.time())
    start = max(0, now - max(10, lookback_seconds))
    discovered_markets = relation.discover(GAMMA_URL, markets, min_liquidity)
    conditions = list(dict.fromkeys(m.condition_id for m in discovered_markets if m.condition_id))
    sample = conditions[:max(1, sample_conditions)]
    discovered = set(conditions)
    probes: dict[str, dict[str, Any]] = {}

    specs = {
        "batch_windowed": trades_url(conditions=sample, start=start, end=now),
        "batch_unwindowed": trades_url(conditions=sample),
        "single_windowed": trades_url(conditions=sample[:1], start=start, end=now),
        "single_unwindowed": trades_url(conditions=sample[:1]),
        "global_recent": trades_url(),
    }
    for name, url in specs.items():
        rows, meta = request_rows(url, timeout)
        probes[name] = {
            **meta,
            **summarize_rows(rows, start, now, discovered),
            "condition_count": 0 if name == "global_recent" else (1 if name.startswith("single_") else len(sample)),
        }

    return {
        "schema": "trade_api_empty_tape_diagnostic_v1",
        "generated_ts": int(time.time()),
        "paper_only": True,
        "authenticated_execution": False,
        "real_money_execution": False,
        "requested_markets": markets,
        "discovered_markets": len(discovered_markets),
        "discovered_conditions": len(conditions),
        "sampled_conditions": len(sample),
        "min_liquidity": min_liquidity,
        "window_start": start,
        "window_end": now,
        "lookback_seconds": now - start,
        "probes": probes,
        "classification": classify(probes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose successful-empty Polymarket public trade tape responses")
    parser.add_argument("--markets", type=int, default=220)
    parser.add_argument("--min-liquidity", type=float, default=10.0)
    parser.add_argument("--lookback-seconds", type=int, default=900)
    parser.add_argument("--sample-conditions", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run(
        max(1, args.markets), max(0.0, args.min_liquidity), max(10, args.lookback_seconds),
        max(1, min(20, args.sample_conditions)), max(1, args.timeout),
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
