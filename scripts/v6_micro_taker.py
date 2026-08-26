#!/usr/bin/env python3
from __future__ import annotations

# Compatibility adapter used only during the V6 -> V7 runtime migration.
# Importers receive the frozen legacy module API; direct execution uses the
# corrected V7 complete-round-trip worker.  This file is deleted in the cleanup
# tranche after all callers point directly at v7_micro_taker_worker.py.

if __name__ == "__main__":
    from v7_micro_taker_worker import main
    raise SystemExit(main())

import importlib.util
import sys
from pathlib import Path

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
