from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent
from v7_native_crypto_engine_manager import fee_parameters, open_native_orders, select_market
from v7_native_paper_settlement import aggregate_fills, native_receipt, resolved_payouts


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
        "external_symbols": {"binance_spot": "BTCUSDT", "coinbase_spot": "BTC-USD"},
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


def test_restart_detects_submitted_native_order_until_terminal_state(tmp_path: Path) -> None:
    root = tmp_path
    ledger = root / "ledger" / "execution.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    submitted = LedgerEvent(
        event_type="ORDER_SUBMITTED",
        strategy="CRYPTO_SETTLEMENT_ENGINE",
        model_sha=SHA,
        order_id="native:1",
        market_id="m1",
        event_id="e1",
        token_id="yes",
        side="BUY",
        exchange_ts_ms=1000,
        receive_ts_ms=1001,
        decision_ts_ms=1002,
        recorded_ts_ms=1003,
        book_snapshot_id="b1",
        intended_action="MAKE",
        intended_size=1.0,
        metadata={"native_settlement_receipt": receipt(1, 101)},
    )
    ledger.write_text(json.dumps(submitted.to_dict()) + "\n", encoding="utf-8")
    assert list(open_native_orders(root, SHA)) == ["native:1"]

    cancelled = LedgerEvent(
        event_type="ORDER_STATE",
        strategy="CRYPTO_SETTLEMENT_ENGINE",
        model_sha=SHA,
        order_id="native:1",
        market_id="m1",
        event_id="e1",
        token_id="yes",
        side="BUY",
        order_state="CANCELLED",
        recorded_ts_ms=1004,
        metadata={"native_settlement_receipt": receipt(1, 101)},
    )
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(cancelled.to_dict()) + "\n")
    assert open_native_orders(root, SHA) == {}



def test_daily_equal_close_supports_fifty_fifty_payout(monkeypatch) -> None:
    import v7_native_paper_settlement as settlement
    monkeypatch.setattr(settlement, "public_json", lambda _url: {
        "closed": True,
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["yes", "no"],
        "outcomePrices": ["0.5", "0.5"],
    })
    result = resolved_payouts("https://gamma-api.polymarket.com", "m-daily")
    assert result == ({"yes": 0.5, "no": 0.5}, "50-50")

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

def test_manager_selects_all_six_assets_and_five_horizons() -> None:
    from v7_native_crypto_engine_manager import select_markets
    now = 1_000_100
    markets = []
    seconds = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
    index = 0
    for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
        for horizon, duration in seconds.items():
            index += 1
            markets.append({
                **universe_row(start=1_000_000),
                "market_id": f"m{index}",
                "asset": asset,
                "horizon": horizon,
                "horizon_seconds": duration,
                "close_timestamp_unix": 1_000_000 + duration,
                "external_symbols": {
                    "binance_spot": f"{asset}USDT",
                    "coinbase_spot": None if asset == "BNB" else f"{asset}-USD",
                },
            })
    snapshot = {
        "schema": "polymarket_v7_crypto_universe_snapshot_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "execution_authority": False,
        "model_sha": SHA,
        "discovery_exhaustive": True,
        "markets": markets,
    }
    selected = select_markets(snapshot, SHA, now_s=now)
    assert len(selected) == 30
    assert set(selected) == {
        f"{asset}:{horizon}"
        for asset in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB")
        for horizon in seconds
    }


def test_partitioned_paper_budget_never_exceeds_engine_envelope(tmp_path: Path) -> None:
    from v7_native_crypto_engine_manager import (
        _enabled_context_count, _execution_budget_microdollars,
    )
    allocation = tmp_path / "allocation.json"
    allocation.write_text(json.dumps({
        "capital_scope": {
            "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
            "scope_class": "ENGINE_ENVELOPE",
            "independent_capital_authority": False,
            "execution_budget": 10000.0,
        }
    }))
    registry_path = ROOT / "config" / "v7_crypto_settlement_markets.json"
    total = _execution_budget_microdollars(allocation)
    count = _enabled_context_count(registry_path)
    partition = total // count
    assert count == 30
    assert partition == 333_333_333
    assert partition * count <= total


def test_settlement_writer_links_actual_position_and_supports_payout_vectors(tmp_path, monkeypatch):
    import types
    import v7_native_paper_settlement as settlement
    import pytest
    event=fill(fill_id='f1',order_id='native:1',token='yes',side='BUY',qty=5,price=.4,fee=.01,client=1)
    from dataclasses import replace
    event=replace(event,position_id='native-position:m1:yes',metadata={**event.metadata,'crypto_context':{'asset':'ETH','horizon':'D1'},'model_family':'crypto_informed_taker'})
    monkeypatch.setattr(settlement,'market_events',lambda *a:[event])
    monkeypatch.setattr(settlement,'wait_for_record',lambda *a,**k:True)
    captured=[]
    monkeypatch.setattr(settlement,'spool_event',lambda root,row:captured.append(row))
    args=types.SimpleNamespace(run_root=tmp_path,model_sha=SHA,market_id='m1',timeout_seconds=1,gamma_url='unused')
    for payouts,label,pnl in [({'yes':1.0,'no':0.0},'Up',2.99),({'yes':.5,'no':.5},'50-50',.49)]:
        monkeypatch.setattr(settlement,'resolved_payouts',lambda *a:(payouts,label))
        assert settlement.settle(args)==0
        row=captured[-1]
        assert row.token_id=='yes'
        assert row.final_pnl==pytest.approx(pnl)
        assert row.metadata['included_position_ids']==['native-position:m1:yes']
        assert row.metadata['crypto_context']=={'asset':'ETH','horizon':'D1'}
        assert row.metadata['settlement_payouts']==payouts


def test_manager_cli_defaults_match_frequency_and_size_policy(monkeypatch) -> None:
    import v7_native_crypto_engine_manager as manager
    monkeypatch.setattr(sys, "argv", [
        "manager",
        "--repository-root", "/tmp/repo",
        "--run-root", "/tmp/run",
        "--model-sha", SHA,
        "--run-id", "run",
        "--server-id", "server",
        "--universe", "/tmp/universe.json",
        "--engine", "/tmp/engine",
        "--settler", "/tmp/settler.py",
        "--engine-log", "/tmp/engine.log",
        "--allocation", "/tmp/allocation.json",
        "--market-registry", "/tmp/registry.json",
    ])
    args = manager.parse_args()
    assert args.min_order_microunits == 5_000_000
    assert args.target_quantity_microunits == 20_000_000
    assert args.minimum_tte_ns == 5_000_000_000
    assert args.maximum_tte_ns == 120_000_000_000
    assert args.maker_share_cap_microunits == 5_000_000


def test_clob_venue_minimum_is_loaded_and_fail_closed(monkeypatch) -> None:
    import v7_native_crypto_engine_manager as manager
    monkeypatch.setattr(manager, "public_json", lambda _url: {"min_order_size": "5"})
    assert manager.venue_minimum_microunits("token") == 5_000_000
    monkeypatch.setattr(manager, "public_json", lambda _url: {"min_order_size": "7.5"})
    assert manager.venue_minimum_microunits("token") == 7_500_000
    monkeypatch.setattr(manager, "public_json", lambda _url: {"min_order_size": None})
    try:
        manager.venue_minimum_microunits("token")
    except RuntimeError:
        pass
    else:
        raise AssertionError("invalid CLOB venue minimum accepted")


def test_async_settlement_detaches_context_after_canonical_commit(monkeypatch, tmp_path) -> None:
    import types
    import v7_native_crypto_engine_manager as manager

    class Process:
        def __init__(self, rc=0):
            self.rc = rc
            self.pid = 1234
        def poll(self):
            return self.rc

    class Log:
        def __init__(self):
            self.closed = False
        def close(self):
            self.closed = True

    owner = manager.Manager.__new__(manager.Manager)
    owner.run_root = tmp_path
    owner.args = types.SimpleNamespace(
        model_sha=SHA, run_id="run", repository_root=ROOT,
        asynchronous_settlement=True, python="python3",
        settler=ROOT / "scripts/v7_native_paper_settlement.py",
        settlement_timeout_seconds=600,
    )
    owner.workers = {}
    owner.pending_settlements = {}
    owner.completed_market_ids = set()
    worker = manager.Worker(
        context="BTC:M5", market={"market_id": "m1"},
        budget_microdollars=100_000_000, process=Process(0), log_handle=Log(),
    )
    owner.workers["BTC:M5"] = worker
    settlement = Process(None)
    monkeypatch.setattr(manager, "native_commit_barrier", lambda *a, **k: True)
    monkeypatch.setattr(manager.subprocess, "Popen", lambda *a, **k: settlement)

    assert owner._finish_worker("BTC:M5", worker) is None
    assert worker.state == "SETTLING"
    assert worker.log_handle.closed
    assert "BTC:M5" not in owner.workers
    assert owner.pending_settlements["m1"] is worker
    assert worker.settlement is settlement
