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


def fetch_trades_batch(condition_ids: list[str], start_ts: int, end_ts: int,
                       batch_size: int = 5) -> tuple[dict[str, list[core.Trade]], int, list[str]]:
    """Fetch recent public trade history without unsupported time query fields.

    Data API `/trades` documents `market`, `limit`, `offset`, and `takerOnly`, but
    not `start`/`end`. The prior implementation requested up to 10,000 rows for
    20 conditions at once; live evidence showed widespread 408/500 responses and
    a false zero-activity universe. This adapter keeps the documented contract,
    uses a bounded recent page, and recursively splits retryable failing batches
    down to individual conditions. Event-time [start_ts, end_ts] filtering remains
    local; the request receive time is retained as the causal availability time.
    """
    out: dict[str, list[core.Trade]] = defaultdict(list)
    last_received_ms = 0
    errors: list[str] = []
    unique_conditions = list(dict.fromkeys(x for x in condition_ids if x))

    def consume_rows(rows: list[object], chunk: list[str]) -> None:
        allowed = set(chunk)
        seen = {t.trade_id for condition in chunk for t in out.get(condition, [])}
        for item in rows:
            if not isinstance(item, dict):
                continue
            condition = str(item.get("conditionId") or "")
            token = str(item.get("asset") or "")
            side = str(item.get("side") or "").upper()
            ts = int(core.number(item.get("timestamp"), 0))
            price = core.number(item.get("price"), -1.0)
            size = core.number(item.get("size"), 0.0)
            if condition not in allowed or not token or not (start_ts <= ts <= end_ts):
                continue
            if not (0 < price < 1) or size <= 0:
                continue
            key = ":".join([
                str(item.get("transactionHash") or ""), token, str(ts), side,
                f"{price:.12g}", f"{size:.12g}",
            ])
            if key in seen:
                continue
            seen.add(key)
            out[condition].append(core.Trade(key, token, side, price, size, ts))

    def request_chunk(chunk: list[str], label: str) -> None:
        nonlocal last_received_ms
        if not chunk:
            return
        market_value = ",".join(chunk)
        query = (
            f"limit={_PAGE_LIMIT}&offset=0&takerOnly=true"
            f"&market={urllib.parse.quote(market_value, safe=',')}"
        )
        try:
            raw, received_ms = core.request_json(f"{core.DATA_URL}/trades?{query}")
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

    for condition in unique_conditions:
        out[condition].sort(key=lambda x: (x.ts, x.trade_id))
    return dict(out), last_received_ms, errors


# Keep the runner implementation in one module while replacing only the transport
# adapter with the documented Data-API contract above.
batched.fetch_trades_batch = fetch_trades_batch


def activity_data_healthy(result: dict[str, object]) -> bool:
    universe = result.get("universe")
    if not isinstance(universe, dict):
        return False
    discovered = int(universe.get("discovered_markets") or 0)
    active = int(universe.get("active_markets_evaluated") or 0)
    errors = universe.get("flow_errors")
    error_count = len(errors) if isinstance(errors, list) else 0
    # Zero recent activity can be economically real, but it may not be accepted as
    # research evidence when the same scan suffered transport failures. That was the
    # exact failure mode that previously turned 408/500s into a misleading 0-candidate
    # result while the overall workflow still passed.
    return not (discovered > 0 and active == 0 and error_count > 0)


def main() -> int:
    args = core.parse_args()
    # Start moderately batched; retryable failures are recursively split down to one
    # condition. The bounded page avoids the 10k-row multi-condition timeout mode.
    args.trade_batch_size = 5
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result = batched.run_batched(args)
    healthy = activity_data_healthy(result)
    universe = result.setdefault("universe", {})
    if isinstance(universe, dict):
        universe["activity_data_healthy"] = healthy
        universe["trade_batch_size"] = args.trade_batch_size
        universe["trade_page_limit"] = _PAGE_LIMIT
    (out / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out / "result.md").write_text(batched.markdown(result), encoding="utf-8")
    print(batched.markdown(result), end="")
    if not healthy:
        print("HF activity discovery is unhealthy; refusing to treat transport failures as zero activity.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
