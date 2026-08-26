#!/usr/bin/env python3
from __future__ import annotations

# Compatibility adapter used only during the V6 -> V7 runtime migration.
# Importers receive the frozen legacy module API; direct execution uses the
# corrected V7 complete-round-trip worker. This adapter retains one explicit
# atomic JSON writer because the V7 worker currently imports that helper through
# this compatibility surface. The legacy module itself can be deleted once all
# remaining helper imports are migrated to V7-native modules.

if __name__ == "__main__":
    from v7_micro_taker_worker import main
    raise SystemExit(main())

import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

_LEGACY = Path(__file__).with_name("v6_micro_taker_legacy.py")
_SPEC = importlib.util.spec_from_file_location("v6_micro_taker_legacy_runtime", _LEGACY)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError(f"cannot load legacy Micro Taker module from {_LEGACY}")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
for _name in dir(_MODULE):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_MODULE, _name)


def atomic_json(path: Path, value: Any) -> None:
    """Collision-free atomic writer owned by the compatibility boundary."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
