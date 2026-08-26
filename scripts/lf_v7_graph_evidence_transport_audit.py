#!/usr/bin/env python3
from __future__ import annotations

import math
from typing import Any, Iterable


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _vector(row: dict[str, Any], key: str) -> tuple[float, ...] | None:
    value = row.get(key)
    if not isinstance(value, (list, tuple)):
        return None
    out = tuple(_finite(item) for item in value)
    if not out or any(not math.isfinite(item) or item < 0.0 for item in out):
        return None
    return out


def transportable_session(current: dict[str, Any], historical: dict[str, Any], tol: float = 1e-12) -> bool:
    """Conservative research comparator for reusing Graph/RV execution evidence.

    Structural identity is necessary but not sufficient. A historical session is
    transportable only when it was at least as difficult to fill and no richer in
    quoted edge than the current candidate, at the same observation horizon.

    This is intentionally conservative and research-only. It is a diagnostic
    contract for the successor conditional execution model, not a production
    fill-probability estimator.
    """
    if str(current.get("signature") or "") != str(historical.get("signature") or ""):
        return False
    current_window = int(_finite(current.get("window_seconds"), -1.0))
    historical_window = int(_finite(historical.get("window_seconds"), -2.0))
    if current_window <= 0 or historical_window != current_window:
        return False

    current_edge = _finite(current.get("expected_edge"))
    historical_edge = _finite(historical.get("expected_edge"))
    if not (math.isfinite(current_edge) and math.isfinite(historical_edge)):
        return False
    if current_edge + tol < historical_edge:
        return False

    current_required = _vector(current, "required_flow")
    historical_required = _vector(historical, "required_flow")
    current_targets = _vector(current, "target_shares")
    historical_targets = _vector(historical, "target_shares")
    if None in (current_required, historical_required, current_targets, historical_targets):
        return False
    assert current_required is not None and historical_required is not None
    assert current_targets is not None and historical_targets is not None
    if not (len(current_required) == len(historical_required) == len(current_targets) == len(historical_targets)):
        return False

    if any(h + tol < c for c, h in zip(current_required, historical_required)):
        return False
    if any(h + tol < c for c, h in zip(current_targets, historical_targets)):
        return False
    return True


def transportable_sessions(current: dict[str, Any], completed: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in completed if transportable_session(current, row)]


def deterministic_counterexample() -> dict[str, Any]:
    signature = "GRAPH_RV|event|a:YES|b:YES"
    historical = [
        {
            "signature": signature,
            "window_seconds": 180,
            "expected_edge": 0.02,
            "required_flow": [100.0, 100.0],
            "target_shares": [10.0, 10.0],
            "full_completion": True,
            "stress_pnl": {"1x": 1.0, "1.5x": 0.8, "2x": 0.5},
        }
        for _ in range(4)
    ]
    current_harder_lower_edge = {
        "signature": signature,
        "window_seconds": 180,
        "expected_edge": 0.00005,
        "required_flow": [1000.0, 1000.0],
        "target_shares": [50.0, 50.0],
    }
    current_easier_higher_edge = {
        "signature": signature,
        "window_seconds": 180,
        "expected_edge": 0.03,
        "required_flow": [50.0, 50.0],
        "target_shares": [5.0, 5.0],
    }
    return {
        "historical_sessions": len(historical),
        "state_blind_history_has_positive_pnl": all(row["stress_pnl"]["2x"] > 0.0 for row in historical),
        "harder_lower_edge_transportable_sessions": len(transportable_sessions(current_harder_lower_edge, historical)),
        "easier_higher_edge_transportable_sessions": len(transportable_sessions(current_easier_higher_edge, historical)),
        "finding": "structural signature alone cannot transport fill/PnL evidence across changed edge, queue burden, target size, or horizon",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(deterministic_counterexample(), indent=2, sort_keys=True))
