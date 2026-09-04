#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path

SOURCE_COMMIT = "df227d8b38d995340c452d6b28399ccb09a54c78"
SOURCE = Path("scripts/v7_external_fair_paper_router.py")
text = SOURCE.read_text(encoding="utf-8")

constant = '                "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",\n'
dynamic = '                "market_mid_source": metadata.get("market_mid_source"),\n'
if constant in text:
    text = text.replace(constant, dynamic, 1)
elif dynamic not in text:
    raise SystemExit("durable restore source precondition unavailable")

# c2d restored two evidence counters between counterfactual_fills and realized
# PnL. Move them just after the realized-PnL reduction so the pinned patch can
# match its historical anchor without losing either counter.
layout = '''        self.state["counterfactual_fills"] = len(fills)
        self.state["candidates"] = len(candidate_ids)
        self.state["opportunity_sets"] = len(opportunity_ids)
        self.state["counterfactual_realized_pnl"] = sum(
'''
normalized = '''        self.state["counterfactual_fills"] = len(fills)
        self.state["counterfactual_realized_pnl"] = sum(
'''
if layout in text:
    text = text.replace(layout, normalized, 1)
    tail = '''            for row in fill_finals.values()
        )
        self.state["traded_markets"] = sorted({
'''
    relocated = '''            for row in fill_finals.values()
        )
        self.state["candidates"] = len(candidate_ids)
        self.state["opportunity_sets"] = len(opportunity_ids)
        self.state["traded_markets"] = sorted({
'''
    if tail not in text:
        raise SystemExit("durable restore reduction tail unavailable")
    text = text.replace(tail, relocated, 1)
elif normalized not in text:
    raise SystemExit("durable restore counter precondition unavailable")

SOURCE.write_text(text, encoding="utf-8")
original = subprocess.check_output(
    ["git", "show", f"{SOURCE_COMMIT}:scripts/tmp_apply_v7_probe_lifecycle_recovery.py"],
    text=True,
)
namespace = {"__name__": "__main__", "__file__": str(Path(__file__).resolve())}
exec(compile(original, str(Path(__file__).resolve()) + ":pinned", "exec"), namespace)
