#!/usr/bin/env python3
from pathlib import Path

router = Path("scripts/v7_external_fair_paper_router.py")
text = router.read_text(encoding="utf-8")
old = '''        self.state["probe_fills"] = sum(
            int((row.get("metadata") or {}).get("paper_bootstrap_probe") is True)
            for row in fills
        )
'''
new = '''        self.state["probe_fills"] = sum(
            int((row.get("metadata") or {}).get("paper_bootstrap_probe") is True)
            for row in fills.values()
        )
'''
if old not in text:
    raise SystemExit("probe fill restore iteration anchor missing")
router.write_text(text.replace(old, new, 1), encoding="utf-8")
print("V7 probe lifecycle semantic finalizer applied")
