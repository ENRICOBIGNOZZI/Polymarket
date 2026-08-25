#!/usr/bin/env python3
"""Research-only audit of the V5 external sleeve activation contract.

The V5 allocator gives each enabled strategy a dedicated child capital account.
The external child can only act when its configured CSV contains at least one
external forecast.  The public external-intelligence worker is deliberately
research-only and is forbidden from writing production signals.  This module
makes that handoff gap explicit without changing any production behavior.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Mapping


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def signal_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def external_strategy(config: Mapping[str, Any]) -> Mapping[str, Any]:
    multi = config.get("multi_strategy")
    strategies = multi.get("strategies") if isinstance(multi, Mapping) else None
    if not isinstance(strategies, list):
        raise ValueError("multi_strategy.strategies is required")
    matches = [row for row in strategies if isinstance(row, Mapping) and row.get("expert") == "external"]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one external strategy, found {len(matches)}")
    return matches[0]


def assess(
    paper_config: Mapping[str, Any],
    external_config: Mapping[str, Any],
    *,
    repository_signal_rows: int,
) -> dict[str, Any]:
    strategy = external_strategy(paper_config)
    enabled = bool(strategy.get("enabled", True))
    fraction = float(strategy.get("capital_fraction", 0.0))
    starting_capital = float(paper_config.get("starting_capital", 0.0))
    research_write_allowed = bool(external_config.get("allow_production_signal_write", False))
    source_path = str(paper_config.get("external_signals_file", ""))

    activation_gap = enabled and fraction > 0.0 and repository_signal_rows == 0 and not research_write_allowed
    return {
        "schema": "polymarket_lf_external_sleeve_activation_audit_v1",
        "external_strategy_enabled": enabled,
        "external_capital_fraction": fraction,
        "external_starting_capital_usd": starting_capital * fraction,
        "external_signal_file": source_path,
        "repository_signal_rows": int(repository_signal_rows),
        "external_research_allow_production_signal_write": research_write_allowed,
        "activation_gap": activation_gap,
        "interpretation": (
            "enabled external child has dedicated capital but no repository-provided forecast rows and the "
            "research worker is explicitly forbidden from writing production signals"
            if activation_gap
            else "no static repository activation gap detected"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit the V5 external sleeve activation handoff")
    parser.add_argument("--paper-config", type=Path, default=Path("config/paper_v5.json"))
    parser.add_argument("--external-config", type=Path, default=Path("config/external_intelligence.json"))
    parser.add_argument("--signals", type=Path)
    args = parser.parse_args()

    paper = _read_json(args.paper_config)
    external = _read_json(args.external_config)
    signals = args.signals or Path(str(paper.get("external_signals_file", "data/external_signals.csv")))
    report = assess(paper, external, repository_signal_rows=signal_rows(signals))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
