from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_crypto_pnl_attribution import aggregate, attribute_terminal  # noqa: E402


def terminal(record_id: str, pnl: float, attribution: dict | None = None, *, market: str = "m1") -> dict:
    metadata = {}
    if attribution is not None:
        metadata["pnl_attribution"] = attribution
    return {
        "event_type": "FINAL",
        "record_id": record_id,
        "paper_only": True,
        "authenticated_execution": False,
        "strategy": "CRYPTO_INFORMED_TAKER",
        "market_id": market,
        "final_pnl": pnl,
        "metadata": metadata,
    }


def test_terminal_identity_closes_exactly() -> None:
    row = terminal("a", 10.0, {
        "settlement_alpha": 14.0,
        "fees": -1.0,
        "slippage": -2.0,
        "latency": -0.5,
    })
    result = attribute_terminal(row)
    assert result is not None
    assert abs(sum(result["components"].values()) - 10.0) < 1e-12
    assert result["components"]["other"] == -0.5


def test_missing_economic_decomposition_is_explicit_other_not_guessed() -> None:
    result = attribute_terminal(terminal("a", -3.0))
    assert result is not None
    assert result["components"]["other"] == -3.0
    assert result["fully_attributed"] is False


def test_aggregate_deduplicates_and_preserves_total_pnl() -> None:
    first = terminal("a", 2.0, {"settlement_alpha": 3.0, "fees": -1.0}, market="m1")
    second = terminal("b", -1.0, {"adverse_selection": -1.0}, market="m2")
    report = aggregate([first, first, second])
    assert report["terminal_records"] == 2
    assert report["duplicates_removed"] == 1
    assert report["realized_pnl"] == 1.0
    assert abs(sum(report["components"].values()) - 1.0) < 1e-12
    assert report["identity_error"] == 0.0
    assert report["by_market"]["m1"]["settlement_alpha"] == 3.0


def test_nonpaper_terminal_is_excluded() -> None:
    row = terminal("a", 1.0)
    row["paper_only"] = False
    assert attribute_terminal(row) is None


if __name__ == "__main__":
    test_terminal_identity_closes_exactly()
    test_missing_economic_decomposition_is_explicit_other_not_guessed()
    test_aggregate_deduplicates_and_preserves_total_pnl()
    test_nonpaper_terminal_is_excluded()
