from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "scripts" / "v7_native_market_settlement.py"
spec = importlib.util.spec_from_file_location("native_settlement", PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fill(token: str, side: str, qty: float, price: float, fee: float):
    return {
        "token_id": token, "side": side, "filled_size": qty,
        "fill_price": price, "fee": fee,
    }


def test_market_pnl_is_cashflow_plus_remaining_winner_inventory() -> None:
    fills = [
        fill("yes", "BUY", 5.0, 0.40, 0.10),
        fill("yes", "SELL", 2.0, 0.60, 0.02),
        fill("no", "BUY", 1.0, 0.50, 0.00),
    ]
    pnl, payout, fees, positions = module.market_pnl(fills, "yes")
    assert payout == pytest.approx(3.0)
    assert fees == pytest.approx(0.12)
    assert positions == pytest.approx({"yes": 3.0, "no": 1.0})
    assert pnl == pytest.approx(1.58)


def test_uncovered_short_fails_closed() -> None:
    with pytest.raises(ValueError, match="uncovered short"):
        module.market_pnl([fill("yes", "SELL", 1.0, 0.60, 0.0)], "yes")


def test_gamma_winner_requires_closed_unique_binary_winner() -> None:
    raw = {
        "closed": True,
        "clobTokenIds": ["yes", "no"],
        "outcomes": ["Up", "Down"],
        "outcomePrices": ["1", "0"],
    }
    assert module.winner_from_gamma(raw) == ("yes", "Up")
    raw["closed"] = False
    assert module.winner_from_gamma(raw) is None
    raw["closed"] = True
    raw["outcomePrices"] = ["1", "1"]
    assert module.winner_from_gamma(raw) is None
