#!/usr/bin/env python3
"""V6 native telemetry entrypoint.

Compatibility note: this preserves the historical `v6_legacy_health_view`
contract shape expected by already-installed health tooling, but there is no V5
expert or mixture. The implementation delegates to the V6 native v2 collector,
which derives orders, fills and PnL from durable model/broker ledgers.
"""
from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("v6_runtime_status_v2.py")), run_name="__main__")
