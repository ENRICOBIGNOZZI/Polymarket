from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import v7_global_portfolio_coordinator as coordinator  # noqa: E402
from test_v7_opportunity import envelope  # noqa: E402


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def candidate(action: str, ev: float, key: str) -> dict:
    value = envelope(
        action=action,
        component="professional_maker" if action == "MAKE" else "crypto_informed_taker",
        ev=ev,
        key=key,
        authority="PAPER_EXPLORATION",
        research_only=False,
    )
    value["market_id"] = "same-market"
    value["execution_plan"]["legs"][0]["market_id"] = "same-market"
    value["crypto_context"]["asset"] = "BTC"
    value["crypto_context"]["horizon"] = "M5"
    return value


def bridge_diag() -> dict:
    return {
        "schema": "polymarket_v7_maker_opportunity_bridge_v1",
        "paper_only": True,
        "authenticated_execution": False,
        "real_order_submission": False,
        "state": "OPERATIONAL",
        "reasons": [],
        "candidate_cells": 1,
        "typed_make_opportunities": 1,
    }


def test_make_wins_same_market_when_conservative_ev_is_higher() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        take = candidate("TAKE", 0.4, "take")
        make = candidate("MAKE", 0.8, "make")
        write(root / "opportunities/inbox/take.json", take)
        with mock.patch.object(coordinator, "build_maker_opportunities", return_value=([make], bridge_diag())):
            status = coordinator.process_cut(root, now_ns=150)
        decision = status["last_decision"]
        assert decision["action"] == "MAKE"
        assert decision["selected_replay_key"] == "make"
        competition = decision["crypto_execution_alpha"]["action_competition"]["BTC|M5|same-market"]
        assert competition["best_action"] == "MAKE"
        authorizations = list((root / "micro_maker/authorized_make").glob("*.json"))
        assert len(authorizations) == 1
        authorized = json.loads(authorizations[0].read_text(encoding="utf-8"))
        assert authorized["owner"] == "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        assert authorized["execution_authority"] == "SIMULATED_PAPER_ONLY"
        assert authorized["opportunity_envelope"]["action"] == "MAKE"
        assert authorized["real_order_submission"] is False


def test_take_wins_same_market_when_conservative_ev_is_higher() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        take = candidate("TAKE", 0.9, "take")
        make = candidate("MAKE", 0.2, "make")
        write(root / "opportunities/inbox/take.json", take)
        with mock.patch.object(coordinator, "build_maker_opportunities", return_value=([make], bridge_diag())):
            status = coordinator.process_cut(root, now_ns=150)
        decision = status["last_decision"]
        assert decision["action"] == "TAKE"
        assert decision["selected_replay_key"] == "take"
        assert not (root / "micro_maker/authorized_make").exists()


def test_maker_bridge_failure_is_fault_contained_not_a_second_global_kill() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        take = candidate("TAKE", 0.9, "take")
        write(root / "opportunities/inbox/take.json", take)
        with mock.patch.object(coordinator, "build_maker_opportunities", side_effect=RuntimeError("maker-broken")):
            status = coordinator.process_cut(root, now_ns=150)
        decision = status["last_decision"]
        assert decision["action"] == "TAKE"
        maker = decision["crypto_execution_alpha"]["maker_opportunity_bridge"]
        assert maker["state"] == "FAIL_CLOSED"
        assert maker["typed_make_opportunities"] == 0


if __name__ == "__main__":
    test_make_wins_same_market_when_conservative_ev_is_higher()
    test_take_wins_same_market_when_conservative_ev_is_higher()
    test_maker_bridge_failure_is_fault_contained_not_a_second_global_kill()
