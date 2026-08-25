#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

try:
    import v6_micro_maker as base
except ModuleNotFoundError:
    from scripts import v6_micro_maker as base


def _arg_value(flag: str, default: str = "") -> str:
    try:
        i = sys.argv.index(flag)
    except ValueError:
        return default
    return sys.argv[i + 1] if i + 1 < len(sys.argv) else default


def _replace_arg(flag: str, value: str) -> None:
    try:
        i = sys.argv.index(flag)
    except ValueError:
        sys.argv.extend([flag, value])
        return
    if i + 1 >= len(sys.argv):
        sys.argv.append(value)
    else:
        sys.argv[i + 1] = value


def _consume_float_arg(flag: str, default: float) -> float:
    try:
        i = sys.argv.index(flag)
    except ValueError:
        return default
    raw = sys.argv[i + 1] if i + 1 < len(sys.argv) else str(default)
    del sys.argv[i : min(len(sys.argv), i + 2)]
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if value == value and abs(value) != float("inf") else default


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def gate_inside_fill_probability(
    raw_fill_probability: float,
    *,
    queue_ahead: float,
    confidence: float,
    min_inside_confidence: float,
) -> float:
    """Fail closed on low-confidence inside-spread priority.

    In the maker engine a passive quote at the public bid carries the displayed
    touch queue. A zero queue is therefore the explicit research marker for an
    inside-spread improvement. The gate never changes at-touch fill estimates;
    it only removes the modeled fill-probability benefit of improving inside
    the spread when microstructure confidence is below the configured floor.
    """
    raw = max(0.0, min(1.0, _finite(raw_fill_probability, 0.0)))
    threshold = max(0.0, min(1.0, _finite(min_inside_confidence, 0.0)))
    if threshold <= 0.0:
        return raw
    if _finite(queue_ahead, 0.0) <= 1e-12 and _finite(confidence, 0.0) < threshold:
        return 0.0
    return raw


def install_inside_confidence_gate(min_inside_confidence: float) -> None:
    """Patch only the research process' inside-spread fill proxy.

    The base engine calls ``micro_signal`` once per market and then evaluates
    YES/NO quote economics. We retain that confidence in process-local context
    and suppress only the zero-queue (inside-spread) fill-probability uplift
    when confidence is below the requested research threshold. No production
    module or persistent configuration is changed.
    """
    threshold = max(0.0, min(1.0, _finite(min_inside_confidence, 0.0)))
    if threshold <= 0.0:
        return
    original_micro_signal = base.micro_signal
    original_fill_probability_proxy = base.fill_probability_proxy
    context = {"confidence": 1.0}

    def gated_micro_signal(yes: Any, no: Any) -> tuple[float, float]:
        fair, confidence = original_micro_signal(yes, no)
        context["confidence"] = _finite(confidence, 0.0)
        return fair, confidence

    def gated_fill_probability_proxy(*args: Any, **kwargs: Any) -> float:
        raw = original_fill_probability_proxy(*args, **kwargs)
        queue_ahead = kwargs.get("queue_ahead")
        if queue_ahead is None and args:
            queue_ahead = args[0]
        return gate_inside_fill_probability(
            raw,
            queue_ahead=_finite(queue_ahead, 0.0),
            confidence=context["confidence"],
            min_inside_confidence=threshold,
        )

    base.micro_signal = gated_micro_signal
    base.fill_probability_proxy = gated_fill_probability_proxy


def _recent_compatible_flow(
    tape: Path,
    *,
    token: str,
    limit_price: float,
    created_ts: int,
    now: int,
) -> float:
    if not tape.exists() or tape.stat().st_size == 0:
        return 0.0
    total = 0.0
    try:
        with tape.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                ts = int(_finite(row.get("timestamp"), 0.0))
                if ts <= created_ts or ts > now + 30:
                    continue
                if str(row.get("asset_id") or "") != token:
                    continue
                if str(row.get("side") or "").upper() != "SELL":
                    continue
                price = _finite(row.get("price"), 2.0)
                size = max(0.0, _finite(row.get("size"), 0.0))
                if price <= limit_price + 1e-12:
                    total += size
    except OSError:
        return 0.0
    return total


def projected_remaining_clearance_ratio(
    order: dict[str, Any],
    *,
    now: int,
    ttl_seconds: int,
) -> float:
    """Project whether original causal flow can still clear queue plus own size."""
    created = int(_finite(order.get("created_ts"), now))
    remaining_ttl = max(0.0, created + max(0, int(ttl_seconds)) - now)
    rate = max(0.0, _finite(order.get("flow_rate"), 0.0))
    required = max(
        1e-12,
        max(0.0, _finite(order.get("queue_ahead"), 0.0))
        + max(0.0, _finite(order.get("remaining_shares"), 0.0)),
    )
    return rate * remaining_ttl / required


def should_recycle_dead_order(
    order: dict[str, Any],
    *,
    observed_compatible_flow: float,
    now: int,
    grace_seconds: int,
    ttl_seconds: int,
    min_projected_clearance: float,
) -> bool:
    """Recycle only zero-flow orders whose remaining causal queue hazard is weak.

    A bursty public tape can have no trade during the fixed grace while the
    pre-entry flow estimate still implies that queue plus own size can clear
    before TTL. Those high-hazard orders should keep their priority. Orders at
    or beyond TTL are left to the base engine's canonical TTL cancellation.
    """
    created = int(_finite(order.get("created_ts"), now))
    age = max(0, now - created)
    ttl = max(1, int(ttl_seconds))
    if age < max(1, int(grace_seconds)) or age >= ttl:
        return False
    if _finite(observed_compatible_flow, 0.0) > 1e-12:
        return False
    threshold = max(0.0, _finite(min_projected_clearance, 1.0))
    return projected_remaining_clearance_ratio(order, now=now, ttl_seconds=ttl) < threshold


def recycle_dead_orders(
    run_dir: Path,
    trade_tape: Path,
    *,
    grace_seconds: int,
    ttl_seconds: int,
    min_projected_clearance: float,
) -> dict[str, int]:
    """Cancel only resting paper maker orders with weak remaining causal fill hazard."""
    state_path = run_dir / "state.json"
    if not state_path.exists():
        return {"examined": 0, "cancelled": 0, "preserved_hazard": 0}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"examined": 0, "cancelled": 0, "preserved_hazard": 0}
    orders = state.get("orders") if isinstance(state.get("orders"), dict) else {}
    if not orders:
        return {"examined": 0, "cancelled": 0, "preserved_hazard": 0}

    now = int(time.time())
    cancelled = 0
    examined = 0
    preserved_hazard = 0
    for market_id, order in list(orders.items()):
        examined += 1
        created = int(_finite(order.get("created_ts"), now))
        if now - created < max(1, grace_seconds):
            continue
        flow = _recent_compatible_flow(
            trade_tape,
            token=str(order.get("token_id") or ""),
            limit_price=_finite(order.get("limit_price"), 0.0),
            created_ts=created,
            now=now,
        )
        if flow > 1e-12:
            continue
        if not should_recycle_dead_order(
            order,
            observed_compatible_flow=flow,
            now=now,
            grace_seconds=grace_seconds,
            ttl_seconds=ttl_seconds,
            min_projected_clearance=min_projected_clearance,
        ):
            if now - created < max(1, ttl_seconds) and projected_remaining_clearance_ratio(
                order, now=now, ttl_seconds=ttl_seconds
            ) >= max(0.0, min_projected_clearance):
                preserved_hazard += 1
            continue
        if hasattr(base, "append_csv"):
            fields = [
                "timestamp", "action", "market_id", "slug", "side", "token_id", "limit_price",
                "remaining_shares", "queue_ahead", "signal_edge", "confidence", "fill_probability",
                "expected_fill_edge", "flow_rate", "fee_source",
            ]
            base.append_csv(
                run_dir / "maker_order_log.csv",
                fields,
                {**order, "timestamp": now, "action": "CANCEL_ZERO_CAUSAL_FLOW"},
            )
        del orders[market_id]
        cancelled += 1

    if cancelled:
        state["orders"] = orders
        state["dead_flow_cancellations_total"] = int(_finite(state.get("dead_flow_cancellations_total"), 0.0)) + cancelled
        state["dead_flow_cancellations_last_tick"] = cancelled
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, state_path)
    return {"examined": examined, "cancelled": cancelled, "preserved_hazard": preserved_hazard}


def enforce_total_gross_cap(config_path: Path, run_dir: Path) -> Path:
    """Compensate base maker room for already-filled inventory.

    The base engine subtracts resting reservations from gross room but its current
    research implementation does not subtract open-position cost. This wrapper
    reduces the per-tick config gross fraction by the position-cost fraction so
    resting orders plus filled inventory cannot exceed the configured hard cap.
    """
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    state_path = run_dir / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        state = {}
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else {}
    position_cost = sum(max(0.0, _finite(p.get("cost"), 0.0)) for p in positions.values() if isinstance(p, dict))
    equity = max(1.0, _finite(state.get("equity"), _finite(cfg.get("starting_capital"), 1.0)))
    configured = max(0.0, min(1.0, _finite(cfg.get("max_gross_fraction"), 0.70)))
    cfg["max_gross_fraction"] = max(0.0, configured - position_cost / equity)
    tmp_cfg = run_dir / "maker_tick_config.json"
    tmp_cfg.parent.mkdir(parents=True, exist_ok=True)
    tmp_cfg.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return tmp_cfg


def main() -> int:
    run_dir_text = _arg_value("--run-dir")
    tape_text = _arg_value("--trade-tape")
    config_text = _arg_value("--config")
    ttl_seconds = int(_finite(_arg_value("--ttl-seconds", "90"), 90.0))
    grace = int(os.environ.get("V6_MAKER_DEAD_FLOW_CANCEL_SECONDS", "20"))
    min_projected_clearance = _finite(
        os.environ.get("V6_MAKER_DEAD_FLOW_MIN_PROJECTED_CLEARANCE", "1.0"), 1.0
    )
    min_inside_confidence = _consume_float_arg(
        "--min-inside-confidence",
        _finite(os.environ.get("V6_MAKER_MIN_INSIDE_CONFIDENCE", "0"), 0.0),
    )
    install_inside_confidence_gate(min_inside_confidence)
    if run_dir_text and tape_text:
        stats = recycle_dead_orders(
            Path(run_dir_text),
            Path(tape_text),
            grace_seconds=grace,
            ttl_seconds=max(1, ttl_seconds),
            min_projected_clearance=max(0.0, min_projected_clearance),
        )
        if stats["cancelled"] or stats["preserved_hazard"]:
            print(json.dumps({"maker_dead_flow_recycle": stats}, sort_keys=True), file=sys.stderr)
    if run_dir_text and config_text:
        tick_config = enforce_total_gross_cap(Path(config_text), Path(run_dir_text))
        _replace_arg("--config", str(tick_config))
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
