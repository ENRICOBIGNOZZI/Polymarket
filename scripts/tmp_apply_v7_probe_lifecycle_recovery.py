#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from pathlib import Path

SOURCE = Path("scripts/v7_external_fair_paper_router.py")
text = SOURCE.read_text(encoding="utf-8")
constant = '                "market_mid_source": "LIVE_COMPLEMENT_CONSISTENT_CLOB_BATCH",\n'
dynamic = '                "market_mid_source": metadata.get("market_mid_source"),\n'
if constant in text:
    SOURCE.write_text(text.replace(constant, dynamic, 1), encoding="utf-8")
elif dynamic not in text:
    raise SystemExit("durable restore source precondition unavailable")

original = subprocess.check_output(
    ["git", "show", "HEAD^:scripts/tmp_apply_v7_probe_lifecycle_recovery.py"],
    text=True,
)
namespace = {"__name__": "__main__", "__file__": str(Path(__file__).resolve())}
exec(compile(original, str(Path(__file__).resolve()) + ":parent", "exec"), namespace)
