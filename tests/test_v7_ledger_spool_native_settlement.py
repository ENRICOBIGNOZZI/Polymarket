from __future__ import annotations

import tempfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from v7_execution_ledger import LedgerEvent
from v7_ledger_spool import _authority_route

SHA = "a" * 40


def receipt(**overrides):
    value = {
        "schema": "polymarket_v7_native_market_settlement_receipt_v1",
        "owner": "V7_NATIVE_PAPER_MARKET_SETTLEMENT",
        "engine_id": "CRYPTO_SETTLEMENT_ENGINE",
        "model_sha": SHA,
        "market_id": "m1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "real_capital_at_risk": False,
        "source": "GAMMA_CLOSED_MARKET",
        "market_closed": True,
        "settlement_observed_ms": 2_000,
        "winning_token_id": "yes",
        "winning_outcome": "Up",
    }
    value.update(overrides)
    return value


def final_event(value: dict) -> LedgerEvent:
    return LedgerEvent(
        event_type="FINAL",
        strategy="CRYPTO_SETTLEMENT_ENGINE",
        model_sha=SHA,
        record_id="native-final:m1",
        recorded_ts_ms=2_001,
        position_id="native-market:m1",
        market_id="m1",
        event_id="e1",
        final_pnl=1.25,
        realized_cashflow=5.0,
        fee=0.05,
        metadata={"native_market_settlement_receipt": value},
    )


def test_native_closed_market_settlement_appends() -> None:
    event = final_event(receipt())
    event.validate()
    with tempfile.TemporaryDirectory() as directory:
        assert _authority_route(Path(directory), event) == "APPEND"


def test_native_market_settlement_cannot_authorize_execution_or_unclosed_market() -> None:
    bad = [
        {"market_closed": False},
        {"real_order_submission": True},
        {"authenticated_execution": True},
        {"real_capital_at_risk": True},
        {"source": "LOCAL_GUESS"},
        {"owner": "OTHER"},
        {"market_id": "other"},
        {"winning_token_id": ""},
        {"settlement_observed_ms": 0},
    ]
    for mutation in bad:
        value = receipt(**mutation)
        with tempfile.TemporaryDirectory() as directory:
            assert _authority_route(Path(directory), final_event(value)) == "QUARANTINED"
