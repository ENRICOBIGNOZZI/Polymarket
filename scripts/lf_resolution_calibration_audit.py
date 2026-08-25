#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def source_contract(root: Path) -> dict[str, bool]:
    engine = (root / "src" / "engine.cpp").read_text(encoding="utf-8")
    api = (root / "src" / "api.cpp").read_text(encoding="utf-8")
    return {
        "discovery_active_open_only": "/markets?active=true&closed=false" in api,
        "resolution_inventory_starts_from_active_markets": "std::vector<Market> resolution_markets = markets;" in engine,
        "closed_resolution_lookup_iterates_positions": "for (const auto& [id, p] : positions_)" in engine
        and "if (auto closed = api_.fetch_market_by_id(id)) resolution_markets.push_back" in engine,
        "resolved_scoring_uses_last_forecasts": "auto fit = last_forecasts_.find(m.id);" in engine
        and "expert_brier_[name]" in engine,
        "forecast_state_latest_only": 'forecast_state.csv\", \"market_id,expert,q_yes' in engine
        and 'f << \"market_id,expert,q_yes' in engine,
        "forecast_state_has_no_time_to_resolution": "market_id,expert,q_yes" in engine
        and "market_id,expert,q_yes,timestamp" not in engine
        and "market_id,expert,q_yes,horizon" not in engine,
    }


def lifecycle_fixture() -> dict[str, Any]:
    forecasts = {
        "unheld_bad": {"q": 0.90, "outcome": 0},
        "held_good": {"q": 0.90, "outcome": 1},
    }
    active_after_close: set[str] = set()
    positions = {"held_good"}

    incumbent_resolution_ids = sorted(positions.difference(active_after_close))
    corrected_resolution_ids = sorted(set(forecasts).union(positions).difference(active_after_close))

    def brier(ids: list[str]) -> float:
        losses = [(forecasts[mid]["q"] - forecasts[mid]["outcome"]) ** 2 for mid in ids]
        return sum(losses) / len(losses) if losses else 0.0

    return {
        "forecasted_market_ids": sorted(forecasts),
        "position_market_ids": sorted(positions),
        "incumbent_resolution_lookup_ids": incumbent_resolution_ids,
        "research_union_resolution_lookup_ids": corrected_resolution_ids,
        "incumbent_scored_forecasts": len(incumbent_resolution_ids),
        "research_union_scored_forecasts": len(corrected_resolution_ids),
        "incumbent_mean_brier": brier(incumbent_resolution_ids),
        "all_forecast_mean_brier": brier(corrected_resolution_ids),
    }


def audit(root: Path) -> dict[str, Any]:
    contract = source_contract(root)
    fixture = lifecycle_fixture()
    defect = all(
        contract[key]
        for key in (
            "discovery_active_open_only",
            "resolution_inventory_starts_from_active_markets",
            "closed_resolution_lookup_iterates_positions",
            "resolved_scoring_uses_last_forecasts",
            "forecast_state_latest_only",
            "forecast_state_has_no_time_to_resolution",
        )
    )
    return {
        "schema": "polymarket_lf_resolution_calibration_audit_v1",
        "structural_defect_present": defect,
        "source_contract": contract,
        "deterministic_lifecycle_fixture": fixture,
        "interpretation": {
            "selection_problem": (
                "Forecasted markets that close without an open position are absent from active discovery and are not "
                "explicitly fetched for resolution, so their terminal forecast can remain unscored. Expert Brier "
                "updates can therefore be selected by the trading/position path rather than by the full forecast set."
            ),
            "time_to_resolution_problem": (
                "forecast_state.csv stores only the latest q_yes per market/expert and no forecast timestamp or horizon, "
                "so the online score cannot estimate calibration by time-to-resolution and discards earlier forecasts."
            ),
            "research_only_candidate": (
                "Track every unresolved forecast independently of positions, persist forecast timestamp/horizon, and "
                "score all matured forecasts on resolution before fitting horizon-specific calibration mappings."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = audit(Path(args.root).resolve())
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if result["structural_defect_present"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
