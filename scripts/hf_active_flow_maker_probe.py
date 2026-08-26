#!/usr/bin/env python3
from __future__ import annotations

import urllib.parse
from collections import defaultdict

import hf_active_flow_maker_batched_probe as batched
import hf_active_flow_maker_core as core
from hf_active_flow_maker_core import *  # noqa: F401,F403


def fetch_trades_batch(condition_ids: list[str], start_ts: int, end_ts: int,
                       batch_size: int = 20) -> tuple[dict[str, list[core.Trade]], int, list[str]]:
    """Fetch supported Data-API fields, then enforce the event-time window locally.

    The public Data API documents `market` as a comma-separated condition-id list,
    but does not document `start`/`end` query parameters. Passing those unsupported
    fields returned empty successful responses in the live probe, creating a false
    zero-activity universe. Causality is preserved by filtering returned trade
    timestamps locally to [start_ts, end_ts].
    """
    out: dict[str, list[core.Trade]] = defaultdict(list)
    last_received_ms = 0
    errors: list[str] = []
    unique_conditions = list(dict.fromkeys(x for x in condition_ids if x))
    for lo in range(0, len(unique_conditions), batch_size):
        chunk = unique_conditions[lo:lo + batch_size]
        market_value = ",".join(chunk)
        seen: set[str] = set()
        for offset in (0, 10000):
            query = (
                f"limit=10000&offset={offset}&takerOnly=true"
                f"&market={urllib.parse.quote(market_value, safe=',')}"
            )
            try:
                raw, received_ms = core.request_json(f"{core.DATA_URL}/trades?{query}")
                last_received_ms = max(last_received_ms, received_ms)
            except Exception as exc:
                errors.append(f"batch={lo // batch_size}:{type(exc).__name__}:{exc}")
                break
            rows = raw if isinstance(raw, list) else raw.get("data", []) if isinstance(raw, dict) else []
            if not isinstance(rows, list):
                errors.append(f"batch={lo // batch_size}:unexpected_response")
                break
            for item in rows:
                if not isinstance(item, dict):
                    continue
                condition = str(item.get("conditionId") or "")
                token = str(item.get("asset") or "")
                side = str(item.get("side") or "").upper()
                ts = int(core.number(item.get("timestamp"), 0))
                price = core.number(item.get("price"), -1.0)
                size = core.number(item.get("size"), 0.0)
                if condition not in chunk or not token or not (start_ts <= ts <= end_ts):
                    continue
                if not (0 < price < 1) or size <= 0:
                    continue
                key = ":".join([str(item.get("transactionHash") or ""), token, str(ts), side,
                                f"{price:.12g}", f"{size:.12g}"])
                if key in seen:
                    continue
                seen.add(key)
                out[condition].append(core.Trade(key, token, side, price, size, ts))
            if len(rows) < 10000:
                break
        for condition in chunk:
            out[condition].sort(key=lambda x: (x.ts, x.trade_id))
    return dict(out), last_received_ms, errors


# Keep the runner implementation in one module while replacing only the transport
# adapter with the documented Data-API contract above.
batched.fetch_trades_batch = fetch_trades_batch


def main() -> int:
    return batched.main()


if __name__ == "__main__":
    raise SystemExit(main())
