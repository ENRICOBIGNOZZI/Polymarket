#!/usr/bin/env python3
"""Zero-authority graph candidate entrypoint for the native execution shadow.

The implementation is intentionally shared with the frozen binary execution
shadow.  Its input adapter accepts only graph-originated same-market YES/NO
complete sets; no graph relation gains order authority from this wrapper.
"""
from v7_pure_arb_exchange_execution_shadow import main

if __name__ == "__main__":
    raise SystemExit(main())
