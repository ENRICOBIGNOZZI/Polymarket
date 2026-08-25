#!/usr/bin/env python3
"""Apply the complete aggressive V5 release and remove one-shot scaffolding."""
from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(
    str(ROOT / "scripts/apply_aggressive_v5_execution_followup_20260825.py"),
    run_name="__main__",
)

# The resulting main branch keeps only production code, permanent policy,
# permanent tests and the activity-aware operations controller.
for path in (ROOT / "scripts").glob("apply_aggressive_v5*20260825.py"):
    path.unlink(missing_ok=True)
for path in (ROOT / ".github/workflows").glob("aggressive-v5-upgrade*.yml"):
    path.unlink(missing_ok=True)

print("aggressive V5 production release prepared; one-shot scaffolding removed")
