#!/usr/bin/env python3
"""Independent verifier entry point; implementation resides in the read-only V7 verifier."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from v7_real_pnl_verifier import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
