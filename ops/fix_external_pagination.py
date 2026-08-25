#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# Gamma currently returns at most 100 markets per request. Asking for 500 and
# interpreting a 100-row response as a short final page silently truncated the
# external universe to the top 100 markets. Page at the actual API boundary.
replace_once(
    "scripts/external_intelligence.py",
    '    page_size = max(1, min(500, integer(universe.get("page_size"), 500)))',
    '    page_size = max(1, min(100, integer(universe.get("page_size"), 100)))',
)

config_path = ROOT / "config" / "external_intelligence.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
config["universe"]["page_size"] = 100
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

Path(__file__).unlink()
print("external Gamma pagination fixed at 100 rows per page")
