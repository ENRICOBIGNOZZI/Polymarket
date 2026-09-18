from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "economic"))

from native_ev_dataset import (  # noqa: E402
    build_rows,
    dataset,
    labels_from_ledger,
)

SHA = "a" * 40


def observation(
    *,
    market: str = "m1",
    token: str = "yes",
    decision_wall_ns: int = 100_000_000_000,
    sequence: int = 1,
    accepted: bool = True,
    direction: int = 1,
    signal: float = 0.7,
    confirm: float = 0.2,
) -> dict:
    return {
        "schema": "polymarket_v7_native_observation_v1",
        "paper_only": True,
        "execution_authority": False,
        "market_id": market,
        "token_id": token,
        "asset": "BTC",
        "horizon": "M5",
        "capture_id": "capture",
        "sequence": sequence,
        "kind": 2,
        "accepted": accepted,
        "book_valid": True,
        "decision_wall_ns": decision_wall_ns,
        "decision_monotonic_ns": 10_000,
        "signal_version": 7,
        "book_version": 9,
        "signal_return_bp": signal,
        "confirmation_return_bp": confirm,
        "direction": direction,
        "signal_valid": False,
        "confirmed_non_opposing": True,
        "tte_ns": 60_000_000_000,
        "signal_age_ns": 25_000_000,
        "bid_e4": 4000,
        "ask_e4": 4100,
        "bid_quantity": 3_000_000,
        "ask_quantity": 1_000_000,
        "fee_rate": 0.02,
        "fee_exponent": 1.0,
    }


def final(
    *,
    market: str = "m1",
    recorded_ms: int = 200_000,
    yes: float = 1.0,
    no: float = 0.0,
) -> dict:
    return {
        "event_type": "FINAL",
        "strategy": "CRYPTO_SETTLEMENT_ENGINE",
        "model_sha": SHA,
        "paper_only": True,
        "authenticated_execution": False,
        "market_id": market,
        "recorded_ts_ms": recorded_ms,
        "record_id": f"final-{market}",
        "metadata": {
            "asset": "BTC",
            "horizon": "M5",
            "settlement_payouts": {"yes": yes, "no": no},
        },
    }


def test_labels_are_read_only_from_canonical_final(tmp_path: Path) -> None:
    ledger = tmp_path / "execution.jsonl"
    rows = [
        {"event_type": "FILL", "model_sha": SHA},
        final(),
        {**final(market="other"), "model_sha": "b" * 40},
    ]
    ledger.write_text("".join(json.dumps(row) + "\n" for row in rows))
    labels = labels_from_ledger(ledger, expected_code_sha=SHA)
    assert set(labels) == {"m1"}
    assert labels["m1"]["token_payouts"] == {"yes": 1.0, "no": 0.0}
    assert labels["m1"]["observed_ns"] == 200_000_000_000


def test_dataset_uses_first_accepted_market_decision_and_executable_cost() -> None:
    rows = [
        observation(decision_wall_ns=100_000_000_000, sequence=1),
        observation(decision_wall_ns=101_000_000_000, sequence=2),
    ]
    labels = {
        "m1": {
            "token_payouts": {"yes": 1.0, "no": 0.0},
            "observed_ns": 200_000_000_000,
        }
    }
    built, stats = build_rows(rows, labels)
    assert stats["accepted_unique_markets"] == 1
    assert stats["selected_markets"] == 1
    row = built[0]
    assert row["decision_ns"] == 100_000_000_000
    assert row["pm_probability"] == pytest.approx(0.405)
    assert row["executable_ask"] == pytest.approx(0.41)
    assert row["fee_per_share"] == pytest.approx(0.02 * 0.41 * 0.59)
    assert row["break_even_probability"] == pytest.approx(
        0.41 + 0.02 * 0.41 * 0.59
    )
    assert row["features"]["signal_strength_bp"] == pytest.approx(0.7)
    assert row["features"]["confirmation_aligned_bp"] == pytest.approx(0.2)
    assert row["features"]["depth_imbalance"] == pytest.approx(0.5)
    assert row["features"]["technical_signal_valid"] == 0.0
    assert row["features"]["confirmed_non_opposing"] == 1.0


def test_down_signal_aligns_confirmation_to_bought_no_token() -> None:
    rows = [
        observation(
            token="no", direction=-1, signal=-0.8, confirm=-0.3
        )
    ]
    labels = {
        "m1": {
            "token_payouts": {"yes": 0.0, "no": 1.0},
            "observed_ns": 200_000_000_000,
        }
    }
    built, _ = build_rows(rows, labels)
    assert built[0]["outcome"] == 1
    assert built[0]["features"]["signal_strength_bp"] == pytest.approx(0.8)
    assert built[0]["features"]["confirmation_aligned_bp"] == pytest.approx(0.3)
    assert built[0]["features"]["direction_up"] == 0.0


def test_noncausal_or_nonbinary_labels_are_not_training_rows() -> None:
    row = observation(decision_wall_ns=200_000_000_000)
    noncausal, stats = build_rows(
        [row],
        {"m1": {"token_payouts": {"yes": 1.0}, "observed_ns": 199_000_000_000}},
    )
    assert noncausal == []
    assert stats["noncausal_labels"] == 1

    fifty, stats = build_rows(
        [observation()],
        {
            "m1": {
                "token_payouts": {"yes": 0.5, "no": 0.5},
                "observed_ns": 200_000_000_000,
            }
        },
    )
    assert fifty == []
    assert stats["nonbinary_labels"] == 1


def test_dataset_hash_is_repeatable_and_no_promotion(tmp_path: Path) -> None:
    path = tmp_path / "obs.jsonl"
    path.write_text(json.dumps(observation()) + "\n")
    labels = {
        "m1": {
            "token_payouts": {"yes": 1.0, "no": 0.0},
            "observed_ns": 200_000_000_000,
        }
    }
    a = dataset([path], labels)
    b = dataset([path], labels)
    assert a == b
    assert a["automatic_promotion"] is False
    assert a["execution_authority"] is False
    assert len(a["dataset_sha256"]) == 64
