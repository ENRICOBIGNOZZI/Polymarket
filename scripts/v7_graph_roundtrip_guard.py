#!/usr/bin/env python3
from __future__ import annotations

# Canonical runtime adapter for the v3 Graph/RV round-trip guard. The core is
# frozen separately so small runtime/import repairs do not risk rewriting the
# long statistical/economic implementation. This compatibility layer is removed
# in the post-main cleanup tranche.

from collections import Counter

import v7_graph_roundtrip_guard_core as core

# The v3 core intentionally reuses the predecessor's block-bootstrap helpers.
# Bind Counter explicitly in the core module namespace so both CLI execution and
# imported deterministic tests have identical semantics.
core.Counter = Counter

for _name in dir(core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(core, _name)


if __name__ == "__main__":
    raise SystemExit(core.main())
