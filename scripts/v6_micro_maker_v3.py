#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

try:
    import v6_micro_maker_v2 as v2
except ModuleNotFoundError:
    from scripts import v6_micro_maker_v2 as v2

base = v2.base


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, _finite(value, 0.0)))


def _consume_float_arg(flag: str, default: float) -> float:
    try:
        i = sys.argv.index(flag)
    except ValueError:
        return default
    raw = sys.argv[i + 1] if i + 1 < len(sys.argv) else str(default)
    del sys.argv[i : min(len(sys.argv), i + 2)]
    return _finite(raw, default)


def _consume_int_arg(flag: str, default: int) -> int:
    return max(1, int(round(_consume_float_arg(flag, float(default)))))


def depth_imbalance(book: Any, levels: int) -> float:
    """Bid-minus-ask depth imbalance for a passive buy, clipped to [-1, 1]."""
    n = max(1, int(levels))
    bid = sum(max(0.0, _finite(size, 0.0)) for _, size in list(getattr(book, "bids", []))[:n])
    ask = sum(max(0.0, _finite(size, 0.0)) for _, size in list(getattr(book, "asks", []))[:n])
    return _clip((bid - ask) / (bid + ask + 1e-12))


def microprice_displacement(book: Any) -> float:
    """Microprice displacement from mid, normalized by half-spread for a passive buy."""
    mid = _finite(getattr(book, "mid", math.nan), math.nan)
    micro = _finite(book.micro() if hasattr(book, "micro") else math.nan, math.nan)
    spread = max(0.0, _finite(getattr(book, "spread", 0.0), 0.0))
    if not math.isfinite(mid) or not math.isfinite(micro) or spread <= 1e-12:
        return 0.0
    return _clip((micro - mid) / max(0.5 * spread, 1e-9))


def maker_toxicity_score(
    *,
    signed_flow_short: float,
    signed_flow_long: float,
    micro_displacement: float,
    imbalance_l1: float,
    imbalance_l3: float,
    imbalance_l5: float,
) -> float:
    """Directional adverse-selection score for a passive buy.

    Negative signed flow, microprice displacement and depth imbalance mean that
    SELL pressure is dominating while our passive BUY quote is becoming more
    likely to fill. The weights deliberately make receive-time causal flow the
    primary signal; book shape provides confirmation rather than overriding it.
    """
    adverse_short = max(0.0, -_clip(signed_flow_short))
    adverse_long = max(0.0, -_clip(signed_flow_long))
    adverse_micro = max(0.0, -_clip(micro_displacement))
    adverse_l1 = max(0.0, -_clip(imbalance_l1))
    adverse_l3 = max(0.0, -_clip(imbalance_l3))
    adverse_l5 = max(0.0, -_clip(imbalance_l5))
    score = (
        0.50 * adverse_short
        + 0.20 * adverse_long
        + 0.10 * adverse_micro
        + 0.08 * adverse_l1
        + 0.06 * adverse_l3
        + 0.06 * adverse_l5
    )
    return max(0.0, min(1.0, score))


def toxicity_adjusted_fill_probability(
    raw_fill_probability: float,
    *,
    toxicity: float,
    hard_block_threshold: float,
    discount_strength: float,
) -> float:
    raw = max(0.0, min(1.0, _finite(raw_fill_probability, 0.0)))
    score = max(0.0, min(1.0, _finite(toxicity, 0.0)))
    threshold = max(0.0, min(1.0, _finite(hard_block_threshold, 1.0)))
    strength = max(0.0, _finite(discount_strength, 1.0))
    if score >= threshold:
        return 0.0
    return raw * math.exp(-strength * score)


def install_toxicity_gate(
    *,
    short_seconds: int,
    long_seconds: int,
    hard_block_threshold: float,
    discount_strength: float,
) -> dict[str, Any]:
    """Install a process-local, receive-time-causal toxicity adjustment.

    `v6_micro_maker` asks TapeFlow for a compatible SELL rate immediately before
    it calls `fill_probability_proxy`. We preserve that call order and attach
    only information already available at the decision timestamp. This keeps
    the challenger shadow/paper-only and avoids changing any production module.
    """
    original_tape_flow = base.TapeFlow
    original_fetch_books = base.fetch_books
    original_fill_probability = base.fill_probability_proxy
    context: dict[str, Any] = {
        "books": {},
        "toxicity": 0.0,
        "features": {},
        "evaluations": 0,
        "hard_blocks": 0,
        "discounted": 0,
        "max_toxicity": 0.0,
    }

    class ToxicTapeFlow(original_tape_flow):
        def compatible_sell_rate(self, token_id: str, limit_price: float, *, lookback_seconds: int) -> float:
            rate = super().compatible_sell_rate(token_id, limit_price, lookback_seconds=lookback_seconds)
            short = self.signed_flow(token_id, lookback_seconds=short_seconds)
            long = self.signed_flow(token_id, lookback_seconds=long_seconds)
            book = context["books"].get(token_id)
            micro = microprice_displacement(book) if book is not None else 0.0
            l1 = depth_imbalance(book, 1) if book is not None else 0.0
            l3 = depth_imbalance(book, 3) if book is not None else 0.0
            l5 = depth_imbalance(book, 5) if book is not None else 0.0
            toxicity = maker_toxicity_score(
                signed_flow_short=short,
                signed_flow_long=long,
                micro_displacement=micro,
                imbalance_l1=l1,
                imbalance_l3=l3,
                imbalance_l5=l5,
            )
            context["toxicity"] = toxicity
            context["features"] = {
                "token_id": token_id,
                "signed_flow_short": short,
                "signed_flow_long": long,
                "microprice_displacement": micro,
                "imbalance_l1": l1,
                "imbalance_l3": l3,
                "imbalance_l5": l5,
                "compatible_sell_rate": rate,
            }
            context["evaluations"] += 1
            context["max_toxicity"] = max(_finite(context["max_toxicity"]), toxicity)
            return rate

    @classmethod
    def from_csv(cls, path: Path, *, lookback_seconds: int = 900, now: int | None = None):
        parent = original_tape_flow.from_csv(path, lookback_seconds=lookback_seconds, now=now)
        trades = [trade for rows in parent.by_asset.values() for trade in rows]
        return cls(trades, now=parent.now)

    ToxicTapeFlow.from_csv = from_csv

    def capture_books(clob: str, markets: list[Any]) -> dict[str, Any]:
        books = original_fetch_books(clob, markets)
        context["books"] = books
        return books

    def gated_fill_probability(*args: Any, **kwargs: Any) -> float:
        raw = original_fill_probability(*args, **kwargs)
        toxicity = _finite(context.get("toxicity"), 0.0)
        adjusted = toxicity_adjusted_fill_probability(
            raw,
            toxicity=toxicity,
            hard_block_threshold=hard_block_threshold,
            discount_strength=discount_strength,
        )
        if raw > 0.0 and adjusted <= 0.0:
            context["hard_blocks"] += 1
        elif adjusted + 1e-12 < raw:
            context["discounted"] += 1
        return adjusted

    base.TapeFlow = ToxicTapeFlow
    base.fetch_books = capture_books
    base.fill_probability_proxy = gated_fill_probability
    return context


def main() -> int:
    short_seconds = _consume_int_arg("--toxicity-short-seconds", 30)
    long_seconds = _consume_int_arg("--toxicity-long-seconds", 120)
    hard_block_threshold = _consume_float_arg("--max-toxicity", 0.55)
    discount_strength = _consume_float_arg("--toxicity-discount-strength", 1.5)
    context = install_toxicity_gate(
        short_seconds=short_seconds,
        long_seconds=long_seconds,
        hard_block_threshold=hard_block_threshold,
        discount_strength=discount_strength,
    )
    rc = v2.main()
    run_dir = v2._arg_value("--run-dir")
    if run_dir:
        status = {
            "paper_only": True,
            "authenticated_execution": False,
            "short_seconds": short_seconds,
            "long_seconds": long_seconds,
            "hard_block_threshold": hard_block_threshold,
            "discount_strength": discount_strength,
            "evaluations": int(context.get("evaluations", 0)),
            "hard_blocks": int(context.get("hard_blocks", 0)),
            "discounted": int(context.get("discounted", 0)),
            "max_toxicity": _finite(context.get("max_toxicity"), 0.0),
            "last_features": context.get("features") or {},
        }
        path = Path(run_dir) / "toxicity_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
