#!/usr/bin/env python3
from __future__ import annotations

import sys

from v7_bundle_quote_optimizer import main


if __name__ == "__main__":
    # Transitional V6 callers used a marginal-joint threshold flag. V7 forbids
    # product-of-marginals completion estimates, so accept only the neutral zero
    # value and remove the flag before delegating to the per-leg quote optimizer.
    argv = list(sys.argv)
    if "--min-joint-fill-probability" in argv:
        index = argv.index("--min-joint-fill-probability")
        if index + 1 >= len(argv) or abs(float(argv[index + 1])) > 1e-15:
            raise SystemExit("V7 forbids marginal-product joint-completion admission")
        del argv[index:index + 2]
        sys.argv = argv
    raise SystemExit(main())
