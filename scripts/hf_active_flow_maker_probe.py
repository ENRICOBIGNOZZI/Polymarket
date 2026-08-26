#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.error
import urllib.parse
from collections import defaultdict
from pathlib import Path

import hf_active_flow_maker_batched_probe as batched
import hf_active_flow_maker_core as core
from hf_active_flow_maker_core import *  # noqa: F401,F403


_RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
_PAGE_LIMIT = 1000
_GLOBAL_SANITY_MIN_CONDITIONS = 50
_ACTIVITY_DIAGNOSTICS: dict[str, object] = {}


def _market_query(offset: int, min_liquidity: float) -> str:
    """Use trailing activity as the coarse prior before the causal flow gate."""
    return urllib.parse.urlencode({
        "active": "true",
        "closed": "false",
        "limit": 100,
        "offset": offset,
        "order": "volume24hr",
        "ascending": "false",
        "liquidity_num_min": f"{min_liquidity:.12g}",
    })


def _trade_query(condition_ids: list[str], start_ts: int, end_ts: int) -> str:
    """Build the current documented Data API market-scoped time-window query."""
    market_value = ",".join(condition_ids)
    return (
        f"limit={_PAGE_LIMIT}&offset=0&takerOnly=true"
        f"&start={max(0, int(start_ts))}&end={max(0, int(end_ts))}"
        f"&market={urllib.parse.quote(market_value, safe=',')}"
    )


def _global_trade_query(start_ts: int, end_ts: int) -> str:
    return (
        f"limit={_PAGE_LIMIT}&offset=0&takerOnly=true"
        f"&start={max(0, int(start_ts))}&end={max(0, int(end_ts))}"
    )


def discover_markets_activity_prior(limit: int, min_liquidity: float) -> list[core.Market]:
    target = min(max(1, limit), 2000)
    out: list[core.Market] = []
    seen: set[str] = set()
    offset = 0
    while len(out) < target and offset < 10000:
        raw, _ = core.request_json(f"{core.GAMMA_URL}/markets?{_market_query(offset, min_liquidity)}")
        rows = raw if isinstance(raw, list) else raw.get("markets", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list):
            raise RuntimeError("unexpected Gamma market response")
        for item in rows:
            if not isinstance(item, dict):
                continue
            market = core.parse_market(item, min_liquidity)
            if market and market.market_id not in seen:
                seen.add(market.market_id)
                out.append(market)
                if len(out) >= target:
                    break
        if len(rows) < 100:
            break
        offset += 100
    out.sort(key=lambda m: (m.volume24h, m.liquidity), reverse=True)
    return out[:target]


def _valid_trade_rows(rows: list[object], start_ts: int, end_ts: int) -> list[dict[str, object]]:
    valid: list[dict[str, object]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        ts = int(core.number(item.get("timestamp"), 0))
        token = str(item.get("asset") or "")
        price = core.number(item.get("price"), -1.0)
        size = core.number(item.get("size"), 0.0)
        if start_ts <= ts <= end_ts and token and 0 < price < 1 and size > 0:
            valid.append(item)
    return valid


def fetch_trades_batch(condition_ids: list[str], start_ts: int, end_ts: int,
                       batch_size: int = 5) -> tuple[dict[str, list[core.Trade]], int, list[str]]:
    """Fetch causal recent public trades with current Data API window semantics.

    The current official Data API documents `start` and `end` as epoch-second trade
    window bounds. Omitting them and filtering only the first default page can make
    a busy market look inactive when the first page is not the requested HF window.
    We therefore constrain every scoped query server-side and still re-check event
    timestamps locally. Retryable failures are recursively split to single markets.
    """
    global _ACTIVITY_DIAGNOSTICS
    out: dict[str, list[core.Trade]] = defaultdict(list)
    last_received_ms = 0
    errors: list[str] = []
    unique_conditions = list(dict.fromkeys(x for x in condition_ids if x))
    seen: set[str] = set()
    filtered_recent_rows = 0

    if len(unique_conditions) >= _GLOBAL_SANITY_MIN_CONDITIONS:
        _ACTIVITY_DIAGNOSTICS = {
            "activity_universe_prior": "gamma_volume24hr_desc_then_causal_recent_trade_gate",
            "trade_window_server_side": True,
            "trade_window_start_ts": start_ts,
            "trade_window_end_ts": end_ts,
            "filtered_batch_recent_rows": 0,
            "global_sanity_checked": False,
            "global_sanity_raw_rows": 0,
            "global_sanity_recent_rows": 0,
            "global_sanity_recent_conditions": 0,
            "global_sanity_overlap_conditions": 0,
            "global_sanity_newest_trade_ts": None,
            "global_sanity_newest_age_seconds": None,
            "global_sanity_fallback_used": False,
            "global_trade_tape_state": "not_checked",
        }

    def consume_rows(rows: list[object], chunk: list[str]) -> int:
        nonlocal filtered_recent_rows
        allowed = set(chunk)
        accepted = 0
        for item in _valid_trade_rows(rows, start_ts, end_ts):
            condition = str(item.get("conditionId") or "")
            if condition not in allowed:
                continue
            token = str(item.get("asset") or "")
            side = str(item.get("side") or "").upper()
            ts = int(core.number(item.get("timestamp"), 0))
            price = core.number(item.get("price"), -1.0)
            size = core.number(item.get("size"), 0.0)
            key = ":".join([
                str(item.get("transactionHash") or ""), token, str(ts), side,
                f"{price:.12g}", f"{size:.12g}",
            ])
            if key in seen:
                continue
            seen.add(key)
            out[condition].append(core.Trade(key, token, side, price, size, ts))
            accepted += 1
        filtered_recent_rows += accepted
        return accepted

    def request_chunk(chunk: list[str], label: str) -> None:
        nonlocal last_received_ms
        if not chunk:
            return
        try:
            raw, received_ms = core.request_json(
                f"{core.DATA_URL}/trades?{_trade_query(chunk, start_ts, end_ts)}")
            last_received_ms = max(last_received_ms, received_ms)
        except urllib.error.HTTPError as exc:
            if exc.code in _RETRYABLE_HTTP and len(chunk) > 1:
                mid = max(1, len(chunk) // 2)
                request_chunk(chunk[:mid], label + "a")
                request_chunk(chunk[mid:], label + "b")
                return
            errors.append(f"batch={label}:HTTPError:{exc.code}:conditions={len(chunk)}")
            return
        except Exception as exc:
            if len(chunk) > 1:
                mid = max(1, len(chunk) // 2)
                request_chunk(chunk[:mid], label + "a")
                request_chunk(chunk[mid:], label + "b")
                return
            errors.append(f"batch={label}:{type(exc).__name__}:{exc}:conditions={len(chunk)}")
            return

        rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
        if not isinstance(rows, list):
            errors.append(f"batch={label}:unexpected_response:conditions={len(chunk)}")
            return
        consume_rows(rows, chunk)

    step = max(1, batch_size)
    for lo in range(0, len(unique_conditions), step):
        request_chunk(unique_conditions[lo:lo + step], str(lo // step))

    if len(unique_conditions) >= _GLOBAL_SANITY_MIN_CONDITIONS:
        _ACTIVITY_DIAGNOSTICS["filtered_batch_recent_rows"] = filtered_recent_rows

    # A clean empty selected universe is checked against the same server-side
    # window without a market filter. If the bounded global query is empty, inspect
    # the default latest page only to classify whether the API index itself is stale.
    if not out and not errors and len(unique_conditions) >= _GLOBAL_SANITY_MIN_CONDITIONS:
        try:
            raw, received_ms = core.request_json(
                f"{core.DATA_URL}/trades?{_global_trade_query(start_ts, end_ts)}")
            last_received_ms = max(last_received_ms, received_ms)
            rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
            if not isinstance(rows, list):
                raise RuntimeError("unexpected bounded global trade response")
            bounded_rows = [item for item in rows if isinstance(item, dict)]
            recent = _valid_trade_rows(bounded_rows, start_ts, end_ts)

            latest_rows = bounded_rows
            if not recent:
                latest_raw, latest_received_ms = core.request_json(
                    f"{core.DATA_URL}/trades?limit={_PAGE_LIMIT}&offset=0&takerOnly=true")
                last_received_ms = max(last_received_ms, latest_received_ms)
                latest_rows_any = (
                    latest_raw if isinstance(latest_raw, list)
                    else latest_raw.get("data", []) if isinstance(latest_raw, dict)
                    else []
                )
                if not isinstance(latest_rows_any, list):
                    raise RuntimeError("unexpected default global trade response")
                latest_rows = [item for item in latest_rows_any if isinstance(item, dict)]

            raw_timestamps = [
                int(core.number(item.get("timestamp"), 0))
                for item in latest_rows
                if int(core.number(item.get("timestamp"), 0)) > 0
            ]
            newest_ts = max(raw_timestamps) if raw_timestamps else None
            newest_age = max(0, end_ts - newest_ts) if newest_ts is not None else None
            recent_conditions = {str(x.get("conditionId") or "") for x in recent if x.get("conditionId")}
            overlap = recent_conditions.intersection(unique_conditions)
            if recent and overlap:
                tape_state = "recent_with_universe_overlap"
            elif recent:
                tape_state = "recent_without_universe_overlap"
            elif not latest_rows:
                tape_state = "empty_global_page"
            else:
                tape_state = "stale_for_hf_window"
            _ACTIVITY_DIAGNOSTICS.update({
                "global_sanity_checked": True,
                "global_sanity_raw_rows": len(latest_rows),
                "global_sanity_recent_rows": len(recent),
                "global_sanity_recent_conditions": len(recent_conditions),
                "global_sanity_overlap_conditions": len(overlap),
                "global_sanity_newest_trade_ts": newest_ts,
                "global_sanity_newest_age_seconds": newest_age,
                "global_sanity_fallback_used": False,
                "global_trade_tape_state": tape_state,
            })
            before = sum(len(v) for v in out.values())
            consume_rows(recent, unique_conditions)
            after = sum(len(v) for v in out.values())
            _ACTIVITY_DIAGNOSTICS["global_sanity_fallback_used"] = after > before
        except urllib.error.HTTPError as exc:
            errors.append(f"global:HTTPError:{exc.code}")
        except Exception as exc:
            errors.append(f"global:{type(exc).__name__}:{exc}")

    for condition in unique_conditions:
        out[condition].sort(key=lambda x: (x.ts, x.trade_id))
    return dict(out), last_received_ms, errors


core.discover_markets = discover_markets_activity_prior
batched.fetch_trades_batch = fetch_trades_batch


def activity_data_healthy(result: dict[str, object]) -> bool:
    universe = result.get("universe")
    if not isinstance(universe, dict):
        return False
    discovered = int(universe.get("discovered_markets") or 0)
    active = int(universe.get("active_markets_evaluated") or 0)
    errors = universe.get("flow_errors")
    error_count = len(errors) if isinstance(errors, list) else 0
    checked = bool(universe.get("global_sanity_checked"))
    global_raw = int(universe.get("global_sanity_raw_rows") or 0)
    global_recent = int(universe.get("global_sanity_recent_rows") or 0)
    global_overlap = int(universe.get("global_sanity_overlap_conditions") or 0)
    if discovered > 0 and active == 0 and error_count > 0:
        return False
    if discovered > 0 and active == 0 and checked and (global_raw == 0 or global_recent == 0):
        return False
    if discovered > 0 and active == 0 and checked and global_recent > 0 and global_overlap == 0:
        return False
    return True


def _deterministic_contract_checks() -> None:
    q = urllib.parse.parse_qs(_market_query(0, 2.0))
    assert q.get("order") == ["volume24hr"]
    assert q.get("ascending") == ["false"]
    assert q.get("liquidity_num_min") == ["2"]
    tq = urllib.parse.parse_qs(_trade_query(["0xaaa", "0xbbb"], 100, 200))
    assert tq.get("start") == ["100"]
    assert tq.get("end") == ["200"]
    assert tq.get("market") == ["0xaaa,0xbbb"]
    assert tq.get("takerOnly") == ["true"]
    assert activity_data_healthy({"universe": {
        "discovered_markets": 1000, "active_markets_evaluated": 0,
        "flow_errors": [], "global_sanity_checked": False,
        "global_sanity_raw_rows": 0, "global_sanity_recent_rows": 0,
        "global_sanity_overlap_conditions": 0,
    }})
    assert not activity_data_healthy({"universe": {
        "discovered_markets": 1000, "active_markets_evaluated": 0,
        "flow_errors": [], "global_sanity_checked": True,
        "global_sanity_raw_rows": 0, "global_sanity_recent_rows": 0,
        "global_sanity_overlap_conditions": 0,
    }})
    assert not activity_data_healthy({"universe": {
        "discovered_markets": 1000, "active_markets_evaluated": 0,
        "flow_errors": [], "global_sanity_checked": True,
        "global_sanity_raw_rows": 1000, "global_sanity_recent_rows": 0,
        "global_sanity_overlap_conditions": 0,
    }})
    assert not activity_data_healthy({"universe": {
        "discovered_markets": 1000, "active_markets_evaluated": 0,
        "flow_errors": [], "global_sanity_checked": True,
        "global_sanity_raw_rows": 1000, "global_sanity_recent_rows": 100,
        "global_sanity_overlap_conditions": 0,
    }})


def main() -> int:
    _deterministic_contract_checks()
    args = core.parse_args()
    args.trade_batch_size = 5
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = batched.run_batched(args)
    universe = result.setdefault("universe", {})
    if isinstance(universe, dict):
        universe.update(_ACTIVITY_DIAGNOSTICS)
        universe["trade_batch_size"] = args.trade_batch_size
        universe["trade_page_limit"] = _PAGE_LIMIT
    healthy = activity_data_healthy(result)
    if isinstance(universe, dict):
        universe["activity_data_healthy"] = healthy
    (out / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "result.md").write_text(batched.markdown(result), encoding="utf-8")
    print(batched.markdown(result), end="")
    if not healthy:
        state = universe.get("global_trade_tape_state") if isinstance(universe, dict) else None
        age = universe.get("global_sanity_newest_age_seconds") if isinstance(universe, dict) else None
        print(f"HF activity discovery is unhealthy: global_trade_tape_state={state} newest_age_seconds={age}; refusing zero-activity evidence.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
