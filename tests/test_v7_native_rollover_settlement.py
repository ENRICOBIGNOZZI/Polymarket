from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent
from v7_native_crypto_engine_manager import fee_parameters, select_market
from v7_native_paper_settlement import aggregate_fills, native_receipt


SHA = "a" * 40


def universe_row(*, start: int) -> dict:
    return {
        "market_id": "m1",
        "condition_id": "c1",
        "event_ids": ["e1"],
        "clob_token_ids": ["yes", "no"],
        "outcomes": ["Up", "Down"],
        "asset": "BTC",
        "horizon": "M5",
        "horizon_seconds": 300,
        "window_start_unix": start,
        "research_only": False,
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "fees_enabled": True,
        "fees_enabled_explicit": True,
        "fee_schedule": {"rate": 0.02, "exponent": 1.0, "takerOnly": True},
    }


def test_manager_selects_only_current_canonical_btc_m5() -> None:
    now = 1_000_100
    snapshot = {
        "schema": "polymarket_v7_crypto_universe_snapshot_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "model_sha": SHA,
        "discovery_exhaustive": True,
        "markets": [universe_row(start=1_000_000)],
    }
    selected = select_market(snapshot, SHA, now_s=now)
    assert selected is not None and selected["market_id"] == "m1"
    snapshot["markets"].append({**universe_row(start=1_000_000), "market_id": "m2"})
    assert select_market(snapshot, SHA, now_s=now) is None
    snapshot["markets"] = [{**universe_row(start=1_000_000), "asset": "ETH"}]
    assert select_market(snapshot, SHA, now_s=now) is None


def test_manager_fee_schedule_is_fail_closed() -> None:
    assert fee_parameters({
        "fees_enabled": False, "fees_enabled_explicit": True, "fee_schedule": {}
    }) == (0.0, 1.0, "GAMMA_FEES_DISABLED_EXPLICIT")
    assert fee_parameters(universe_row(start=1))[0:2] == (0.02, 1.0)
    try:
        fee_parameters({"fees_enabled": True, "fees_enabled_explicit": True, "fee_schedule": {}})
    except RuntimeError as exc:
        assert str(exc) == "fee_schedule_invalid"
    else:
        raise AssertionError("missing authoritative fee schedule accepted")


def receipt(client: int, command: int) -> dict:
    return {
        "schema": "polymarket_v7_native_settlement_receipt_v1",
        "owner": "V7_NATIVE_CRYPTO_SETTLEMENT_ENGINE",
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "execution_mode": "PAPER_SIMULATED",
        "paper_simulation_authority": True,
        "real_new_risk_authorized": False,
        "single_owner": True,
        "owner_chain": ["portfolio", "risk", "capital", "oms", "inventory"],
        "client_order_id": client,
        "command_id": command,
    }


def fill(*, fill_id: str, order_id: str, token: str, side: str, qty: float, price: float, fee: float, client: int) -> LedgerEvent:
    return LedgerEvent(
        event_type="FILL",
        strategy="CRYPTO_SETTLEMENT_ENGINE",
        model_sha=SHA,
        order_id=order_id,
        fill_id=fill_id,
        market_id="m1",
        event_id="e1",
        token_id=token,
        side=side,
        exchange_ts_ms=1_000,
        receive_ts_ms=1_001,
        recorded_ts_ms=1_002,
        fill_price=price,
        filled_size=qty,
        fee=fee,
        fee_source="TEST",
        metadata={"native_settlement_receipt": receipt(client, client + 100)},
    )


def test_settlement_aggregates_buy_and_inventory_backed_sell() -> None:
    events = [
        fill(fill_id="f1", order_id="native:1", token="yes", side="BUY", qty=3.0, price=0.4, fee=0.01, client=1),
        fill(fill_id="f2", order_id="native:2", token="yes", side="SELL", qty=1.0, price=0.6, fee=0.01, client=2),
        fill(fill_id="f3", order_id="native:3", token="no", side="BUY", qty=2.0, price=0.3, fee=0.00, client=3),
    ]
    inventory, cash, fills = aggregate_fills(events)
    assert len(fills) == 3
    assert abs(inventory["yes"] - 2.0) < 1e-12
    assert abs(inventory["no"] - 2.0) < 1e-12
    assert abs(cash - (-1.2 - 0.01 + 0.6 - 0.01 - 0.6)) < 1e-12
    assert native_receipt(events[0]) is not None


def test_settlement_rejects_naked_sell() -> None:
    events = [
        fill(fill_id="f1", order_id="native:1", token="yes", side="SELL", qty=1.0, price=0.6, fee=0.0, client=1)
    ]
    try:
        aggregate_fills(events)
    except RuntimeError as exc:
        assert str(exc) == "native_naked_sell_in_ledger"
    else:
        raise AssertionError("naked native PAPER sell accepted")
