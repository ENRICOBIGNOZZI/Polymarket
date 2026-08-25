#!/usr/bin/env python3
"""Compatibility and test-preserving follow-up for the aggressive V5 upgrade."""
from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / "scripts/apply_aggressive_v5_20260825.py"), run_name="__main__")

filter_path = ROOT / "scripts/filter_coherent_hedges.py"
text = filter_path.read_text(encoding="utf-8")
parser_anchor = '    parser.add_argument("--max-factor-hedge-error", type=float, default=0.65)\n'
parser_addition = parser_anchor + '    parser.add_argument("--allow-factor-model", action="store_true")\n'
if "--allow-factor-model" not in text:
    if parser_anchor not in text:
        raise RuntimeError("factor-model parser anchor missing")
    text = text.replace(parser_anchor, parser_addition, 1)
old = """            factor_coherent = (
                factor_error <= args.max_factor_hedge_error
                and factor_stability >= args.min_factor_stability
            )
"""
new = """            factor_coherent = (
                args.allow_factor_model
                and factor_error <= args.max_factor_hedge_error
                and factor_stability >= args.min_factor_stability
            )
"""
if old in text:
    text = text.replace(old, new, 1)
elif new not in text:
    raise RuntimeError("factor coherence expression missing")
filter_path.write_text(text, encoding="utf-8")

# Opt in only on execution-facing runtime paths. Unit tests and ad-hoc research
# retain the old fail-closed semantic behavior unless explicitly requested.
for relative in [
    ".github/workflows/v4-live-smoke.yml",
    ".github/workflows/deploy-paper-server.yml",
]:
    path = ROOT / relative
    if not path.exists():
        continue
    body = path.read_text(encoding="utf-8")
    if "filter_coherent_hedges.py" not in body or "--allow-factor-model" in body:
        continue
    lines = body.splitlines()
    out: list[str] = []
    in_filter = 0
    inserted = False
    for line in lines:
        if "filter_coherent_hedges.py" in line:
            in_filter = 10
        if in_filter > 0 and ("| tee" in line or (not line.rstrip().endswith("\\") and "filter_coherent_hedges.py" not in line)) and not inserted:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + "--allow-factor-model \\")
            inserted = True
        out.append(line)
        if in_filter > 0:
            in_filter -= 1
    if not inserted:
        raise RuntimeError(f"could not add factor-model opt-in to {relative}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

# Make the standalone maker smoke aggressive even when command formatting differs.
smoke_path = ROOT / ".github/workflows/v4-live-smoke.yml"
smoke = smoke_path.read_text(encoding="utf-8")
smoke = smoke.replace("--markets 120 --min-liquidity 100", "--markets 500 --min-liquidity 25")
smoke = smoke.replace("--min-edge 0.003 --max-order-usd 50", "--min-edge 0.0001 --max-order-usd 40")
smoke = smoke.replace("--adverse-selection-mult 0.50", "--adverse-selection-mult 0.15")
smoke_path.write_text(smoke, encoding="utf-8")

policy_test = ROOT / "tests/test_aggressive_v5_policy.py"
if policy_test.exists():
    body = policy_test.read_text(encoding="utf-8")
    anchor = '    assert "--once --paper" in smoke\n'
    addition = anchor + '    assert "--allow-factor-model" in smoke\n'
    if 'assert "--allow-factor-model" in smoke' not in body:
        if anchor not in body:
            raise RuntimeError("aggressive policy test anchor missing")
        body = body.replace(anchor, addition, 1)
    policy_test.write_text(body, encoding="utf-8")

print("aggressive V5 compatibility follow-up applied")
