#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

PATTERNS = (
    re.compile(r"\bV(?!7\b)\d+\b"),
    re.compile(r"\bpaper_v(?!7(?:\b|_))\d+(?:\b|_)"),
    re.compile(r"(?<![A-Za-z0-9])v(?!7(?:_|-))\d+[_-][A-Za-z0-9]"),
)

def finite(value: object, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default

def main() -> int:
    parser = argparse.ArgumentParser(description="V7-only hard safety policy")
    parser.add_argument("--base-ref")
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    changed = [line.strip() for line in args.changed_files.read_text(encoding="utf-8").splitlines() if line.strip()]
    errors: list[str] = []
    for rel in changed:
        if any(pattern.search(rel) for pattern in PATTERNS):
            errors.append(f"non_v7_path:{rel}")
            continue
        target = root / rel
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern.search(text) for pattern in PATTERNS):
            errors.append(f"non_v7_content:{rel}")
    cfg = json.loads((root / "config/paper_v7.json").read_text(encoding="utf-8"))
    v7 = cfg.get("v7") or {}
    if cfg.get("paper_only") is not True or v7.get("paper_only") is not True:
        errors.append("paper_only_required")
    if v7.get("authenticated_execution") is not False:
        errors.append("authenticated_execution_must_be_false")
    if v7.get("real_order_submission") is not False:
        errors.append("real_order_submission_must_be_false")
    if finite(cfg.get("max_drawdown"), math.inf) > 0.15:
        errors.append("max_drawdown_exceeds_operator_ceiling")
    if finite((cfg.get("multi_strategy") or {}).get("global_max_drawdown"), math.inf) > 0.15:
        errors.append("global_max_drawdown_exceeds_operator_ceiling")
    for key in ("max_trade_fraction", "max_market_fraction", "max_event_fraction", "max_gross_fraction"):
        value = finite(cfg.get(key), math.inf)
        if value < 0.0 or value > 1.0:
            errors.append(f"invalid_fraction:{key}")
    lines = ["# V7 hard safety policy", "", f"- changed files: `{len(changed)}`", "- PAPER only: `true`", "- authenticated execution: `false`", "- real order submission: `false`"]
    if errors:
        lines += ["", "## Blocking reasons", *[f"- {item}" for item in sorted(set(errors))]]
    else:
        lines += ["", "V7-only safety contract passed."]
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output.read_text(encoding="utf-8"), end="")
    return 1 if errors else 0

if __name__ == "__main__":
    raise SystemExit(main())
