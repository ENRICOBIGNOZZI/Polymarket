#!/usr/bin/env python3
"""Deterministic mutation fuzzing for the V7 authenticated CLOB user stream.

This is deliberately a no-network test: it mutates captured-wire-shaped JSON
and requires the strict parser to either reject it with ``EvidenceError`` or
return a structurally valid normalized event.  A seed and iteration count make
every failure reproducible in CI.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from typing import Any

import v7_real_pnl_evidence as evidence


VALID_WIRES = (
    json.dumps({"topic": "user", "type": "order", "payload": {
        "id": "order-1", "owner": "session-1", "market": "condition-1", "tokenId": "123",
        "side": "BUY", "originalSize": "1", "sizeMatched": "0", "price": "0.50",
        "orderEventType": "PLACEMENT", "status": "LIVE", "timestamp": "1782753357256",
    }}, separators=(",", ":")),
    json.dumps({"topic": "user", "type": "trade", "payload": {
        "id": "trade-1", "takerOrderId": "order-1", "owner": "session-1", "market": "condition-1",
        "tokenId": "123", "side": "SELL", "size": "1", "price": "0.50",
        "status": "TRADE_STATUS_CONFIRMED", "timestamp": "1782753357256",
    }}, separators=(",", ":")),
)


class ProtocolFuzzError(RuntimeError):
    pass


def _mutate(value: str, rng: random.Random) -> str:
    operation = rng.randrange(4)
    position = rng.randrange(len(value) + 1)
    alphabet = "{}[],:\"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\\\u0000\n"
    if operation == 0 and value:
        return value[:position % len(value)] + value[position % len(value) + 1:]
    if operation == 1 and value:
        return value[:position % len(value)] + rng.choice(alphabet) + value[position % len(value) + 1:]
    if operation == 2:
        return value[:position] + rng.choice(alphabet) + value[position:]
    return rng.choice(("", "[]", "{}", "null", "{\"topic\":\"user\"}"))


def run(*, seed: int, iterations: int) -> dict[str, Any]:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ProtocolFuzzError("seed:invalid")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0:
        raise ProtocolFuzzError("iterations:invalid")
    rng = random.Random(seed)
    accepted = rejected = 0
    for index in range(iterations):
        wire = _mutate(VALID_WIRES[index % len(VALID_WIRES)], rng)
        try:
            normalized = evidence.parse_clob_user_ws_wire(wire)
        except evidence.EvidenceError:
            rejected += 1
            continue
        except Exception as exc:  # Parser boundaries must expose only the typed rejection.
            raise ProtocolFuzzError(f"unexpected_exception_at_{index}:{type(exc).__name__}") from exc
        if (not isinstance(normalized, dict) or normalized.get("event_type") not in {"order", "trade"}
                or not isinstance(normalized.get("id"), str) or not normalized["id"]):
            raise ProtocolFuzzError(f"accepted_shape_invalid_at_{index}")
        accepted += 1
    return {"schema": "polymarket_v7_protocol_fuzz_v1", "seed": seed, "iterations": iterations,
            "accepted": accepted, "rejected": rejected, "valid_seed_frames": len(VALID_WIRES)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--iterations", type=int, default=5000)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(seed=args.seed, iterations=args.iterations), sort_keys=True))
        return 0
    except ProtocolFuzzError as exc:
        print(f"v7_protocol_fuzz: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
