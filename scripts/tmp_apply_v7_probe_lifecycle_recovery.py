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

c2d_counters = '''        self.state["counterfactual_fills"] = len(fills)
        self.state["orders"] = int(self.state.get("orders") or 0) + len(fills)
        self.state["fills"] = int(self.state.get("fills") or 0) + len(fills)
        self.state["probe_fills"] = int(self.state.get("probe_fills") or 0) + sum(
            int((row.get("metadata") or {}).get("paper_bootstrap_probe") is True)
            for row in fills
        )
        self.state["counterfactual_realized_pnl"] = sum(
'''
normalized_counters = '''        self.state["counterfactual_fills"] = len(fills)
        self.state["counterfactual_realized_pnl"] = sum(
'''
if c2d_counters in text:
    text = text.replace(c2d_counters, normalized_counters, 1)
elif normalized_counters not in text:
    raise SystemExit("durable restore counter precondition unavailable")

SOURCE.write_text(text, encoding="utf-8")
original = subprocess.check_output(
    ["git", "show", f"{SOURCE_COMMIT}:scripts/tmp_apply_v7_probe_lifecycle_recovery.py"],
    text=True,
)
namespace = {"__name__": "__main__", "__file__": str(Path(__file__).resolve())}
exec(compile(original, str(Path(__file__).resolve()) + ":pinned", "exec"), namespace)
