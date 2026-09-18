from __future__ import annotations

import tempfile
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import _authority_route


SHA = "a" * 40


def receipt(client_order_id: int = 17, command_id: int = 9) -> dict:
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
        "client_order_id": client_order_id,
        "command_id": command_id,
    }


def order_event(meta: dict) -> LedgerEvent:
    return LedgerEvent(
        event_type="ORDER_SUBMITTED",
        strategy="CRYPTO_SETTLEMENT_ENGINE",
        model_sha=SHA,
        order_id="native:17",
        exchange_ts_ms=1_000,
        receive_ts_ms=1_001,
        decision_ts_ms=1_002,
        recorded_ts_ms=1_003,
        book_snapshot_id="native-book:9",
        intended_action="TAKE",
        intended_size=2.0,
        metadata={"native_settlement_receipt": meta},
    )


def test_native_single_owner_paper_receipt_appends() -> None:
    with tempfile.TemporaryDirectory() as directory:
        event = order_event(receipt())
        event.validate()
        assert _authority_route(Path(directory), event) == "APPEND"


def test_native_receipt_cannot_claim_real_submission_or_wrong_owner() -> None:
    for mutation in (
        {"real_order_submission": True},
        {"authenticated_execution": True},
        {"real_capital_at_risk": True},
        {"owner": "SECOND_OWNER"},
        {"single_owner": False},
        {"owner_chain": ["portfolio", "risk", "oms"]},
        {"model_sha": "b" * 40},
    ):
        value = receipt()
        value.update(mutation)
        with tempfile.TemporaryDirectory() as directory:
            assert _authority_route(Path(directory), order_event(value)) == "QUARANTINED"


def test_native_receipt_is_bound_to_exact_order_and_command() -> None:
    with tempfile.TemporaryDirectory() as directory:
        event = order_event(receipt(client_order_id=18))
        assert _authority_route(Path(directory), event) == "QUARANTINED"
    value = receipt(command_id=0)
    with tempfile.TemporaryDirectory() as directory:
        assert _authority_route(Path(directory), order_event(value)) == "QUARANTINED"


def test_native_order_namespace_prevents_rollover_collision() -> None:
    from dataclasses import replace
    from v7_execution_ledger import native_order_id_matches
    from v7_portfolio_guard import _native_receipt_valid
    from v7_native_crypto_engine_manager import _native_receipt
    from v7_native_paper_settlement import native_receipt
    ids = set()
    for market in ("m1", "m2"):
        meta = receipt()
        meta["order_namespace"] = f"run1:{market}"
        event = replace(order_event(meta), market_id=market,
                        order_id=f"native:run1:{market}:17",
                        metadata={"run_id": "run1", "native_settlement_receipt": meta})
        ids.add(event.order_id)
        assert native_order_id_matches(event)
        assert _native_receipt_valid(event)
        assert _native_receipt(event) is not None
        assert native_receipt(event) is not None
        with tempfile.TemporaryDirectory() as directory:
            assert _authority_route(Path(directory), event) == "APPEND"
            wrong_market = replace(event, market_id="other")
            assert not native_order_id_matches(wrong_market)
            assert _authority_route(Path(directory), wrong_market) == "QUARANTINED"
    assert len(ids) == 2
