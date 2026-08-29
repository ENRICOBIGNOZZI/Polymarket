#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import v7_cross_sectional_rank_forward as forward


def main() -> int:
    parser = argparse.ArgumentParser(description="Prospective multi-frequency V7 cross-sectional ranking observer")
    parser.add_argument("--config", type=Path, default=Path("config/research_v7_cross_sectional_rank.json"))
    parser.add_argument("--state-in", type=Path)
    parser.add_argument("--state-out", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--gamma-url", default="https://gamma-api.polymarket.com")
    parser.add_argument("--clob-url", default="https://clob.polymarket.com")
    parser.add_argument("--market-limit", type=int, default=150)
    args = parser.parse_args()

    cfg = json.loads(args.config.read_text(encoding="utf-8"))
    horizons = tuple(sorted({int(x) for x in cfg.get("horizons_minutes", []) if int(x) > 0}))
    if not horizons:
        raise SystemExit("no ranking horizons configured")
    forward.FORWARD_HORIZONS_MINUTES = horizons
    state = forward.normalize_state(forward.read_state(args.state_in))
    new_state, report = forward.run_observer(
        cfg=cfg,
        gamma_url=args.gamma_url,
        clob_url=args.clob_url,
        market_limit=args.market_limit,
        state=state,
        now=int(time.time()),
    )
    report["frequency_registration"] = cfg.get("frequency_registration", {})
    report["evaluated_horizons_minutes"] = list(horizons)
    report["evidence_pooled_across_horizons"] = False
    forward.atomic_json(args.state_out, new_state)
    forward.atomic_json(args.output_json, report)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
