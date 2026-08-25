#!/usr/bin/env python3
"""Fail-closed V5 launcher for generic one-expert children.

The generic engine currently interprets every expert output as a terminal binary
probability. Microstructure, PCA markouts, graph constraints, and unvalidated
semantic/external signals do not all satisfy that contract. Keep those children
running for diagnostics and exit management, but never allow them to open a new
position. Executable routes (maker and multi-leg) are supervised separately by
the V5 loop.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import multi_strategy_paper  # noqa: E402


def force_scan_only(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    if "--scan-only" not in args:
        args.append("--scan-only")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    return multi_strategy_paper.main(force_scan_only(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    raise SystemExit(main())
