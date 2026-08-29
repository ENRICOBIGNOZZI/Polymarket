#!/usr/bin/env python3
from __future__ import annotations

# Transitional compatibility path. Runtime callers still naming the historical
# V6 optimizer are delegated to the V7 per-leg quote optimizer. Multi-leg joint
# completion is never estimated here; it is owned by v7_graph_forward_guard.py.

import sys

from v7_bundle_quote_optimizer import main


if __name__ == "__main__":
    argv = list(sys.argv)
    if "--min-joint-fill-probability" in argv:
        index = argv.index("--min-joint-fill-probability")
        if index + 1 >= len(argv) or abs(float(argv[index + 1])) > 1e-15:
            raise SystemExit("V7 forbids marginal-product joint-completion admission")
        del argv[index:index + 2]
        sys.argv = argv
    raise SystemExit(main())
