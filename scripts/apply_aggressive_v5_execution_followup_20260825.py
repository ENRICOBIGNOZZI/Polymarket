#!/usr/bin/env python3
"""Align persistent deployment commands with the aggressive V5 policy."""
from __future__ import annotations

import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(
    str(ROOT / "scripts/apply_aggressive_v5_scheduler_governance_20260825.py"),
    run_name="__main__",
)

for relative in [
    ".github/workflows/deploy-paper-server.yml",
    ".github/workflows/v4-live-smoke.yml",
]:
    path = ROOT / relative
    if not path.exists():
        continue
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"--markets\s+(?:120|160|180|240|300|400|600)\b", "--markets 500", text)
    text = re.sub(r"--min-liquidity\s+(?:50|100|250)(?:\.0)?\b", "--min-liquidity 25", text)
    text = re.sub(r"--min-edge\s+(?:0\.003|0\.002|0\.0015|0\.001|0\.00075|0\.0005)\b", "--min-edge 0.0001", text)
    text = re.sub(r"--adverse-selection-mult\s+(?:0\.50|0\.5)\b", "--adverse-selection-mult 0.15", text)
    text = re.sub(r"--max-order-usd\s+(?:50|75|100)\b", "--max-order-usd 40", text)
    text = re.sub(r"--history-universe\s+(?:32|80|100|160)\b", "--history-universe 250", text)
    text = re.sub(r"--universe\s+(?:29|80|100|160)\b", "--universe 250", text)
    text = re.sub(r"--min-z\s+(?:1\.50|1\.5|1\.25)\b", "--min-z 0.65", text)
    text = re.sub(r"--min-t-reversion\s+(?:1\.75|1\.5|1\.0)\b", "--min-t-reversion 0.50", text)
    text = re.sub(r"--max-factor-hedge-error\s+(?:0\.20|0\.2)\b", "--max-factor-hedge-error 0.65", text)
    path.write_text(text, encoding="utf-8")

# Ensure the execution-facing coherent PCA filter opts into factor coherence in
# every workflow that invokes it, not only in the smoke path.
for path in (ROOT / ".github/workflows").glob("*.yml"):
    text = path.read_text(encoding="utf-8")
    if "filter_coherent_hedges.py" not in text or "--allow-factor-model" in text:
        continue
    lines = text.splitlines()
    out: list[str] = []
    in_command = 0
    inserted = False
    for line in lines:
        if "filter_coherent_hedges.py" in line:
            in_command = 12
        if in_command > 0 and "| tee" in line and not inserted:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + "--allow-factor-model \\")
            inserted = True
        out.append(line)
        if in_command > 0:
            in_command -= 1
    if not inserted:
        raise RuntimeError(f"factor filter invocation in {path} has no supported pipe anchor")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")

# The policy test now verifies that the deployment, not just CI, is configured
# for the 500-market aggressive runtime.
test_path = ROOT / "tests/test_aggressive_v5_policy.py"
body = test_path.read_text(encoding="utf-8")
anchor = '    assert "control-plane.yml" in controller\n'
addition = anchor + '''    deploy = (ROOT / ".github/workflows/deploy-paper-server.yml").read_text()
    if "polymarket_maker_paper" in deploy:
        assert "--min-edge 0.0001" in deploy
        assert "--adverse-selection-mult 0.15" in deploy
    if "multi_strategy_paper.py" in deploy:
        assert "--markets 500" in deploy
'''
if 'deploy = (ROOT / ".github/workflows/deploy-paper-server.yml")' not in body:
    if anchor not in body:
        raise RuntimeError("execution policy test anchor missing")
    body = body.replace(anchor, addition, 1)
test_path.write_text(body, encoding="utf-8")

print("persistent execution commands aligned with aggressive V5 policy")
