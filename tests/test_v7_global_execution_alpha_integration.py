from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from test_v7_opportunity import envelope  # noqa: E402
from v7_global_portfolio_coordinator import process_cut  # noqa: E402


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def crypto_candidate(market: str, *, capacity: float, ev: float, key: str) -> dict:
    value = envelope(
        action="TAKE", component="crypto_informed_taker", ev=ev, key=key,
        authority="PAPER_EXPLORATION", research_only=False,
    )
    value["market_id"] = market
    value["capacity"]["executable_size"] = capacity
    value["execution_plan"]["legs"][0]["market_id"] = market
    value["execution_plan"]["legs"][0]["target_quantity"] = max(1e-6, capacity)
    value["portfolio_exposure_delta"] = min(1.0, capacity * 0.05)
    return value


def test_coordinator_concentrates_ordinary_paper_risk_on_top_market_window() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # All four are positive PAPER opportunities.  m1 deliberately has the
        # largest raw EV but almost no executable depth.  Market selection must
        # prevent a large-looking but poor-quality opportunity from monopolizing
        # the quote/capital budget.
        values = [
            crypto_candidate("m1", capacity=0.001, ev=10.0, key="m1"),
            crypto_candidate("m2", capacity=1.0, ev=1.0, key="m2"),
            crypto_candidate("m3", capacity=10.0, ev=1.0, key="m3"),
            crypto_candidate("m4", capacity=100.0, ev=1.0, key="m4"),
        ]
        for index, value in enumerate(values):
            write(root / f"opportunities/inbox/{index}.json", value)
        status = process_cut(root, now_ns=150)
        decision = status["last_decision"]
        assert decision["paper_exploration_authorized"] is True
        assert decision["action"] == "TAKE"
        assert decision["selected_replay_key"] == "m4"
        selection = decision["crypto_execution_alpha"]
        assert selection["state"] == "EVALUATED"
        assert selection["market_count"] == 4
        assert selection["retained_market_count"] == 1
        assert selection["retained_market_keys"] == ["BTC|M5|m4"]
        assert selection["filtered_ordinary_crypto_candidates"] == 3
        assert decision["selected_envelope_count"] == 1


def test_cancel_survives_market_selection_and_preempts_alpha() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write(root / "opportunities/inbox/alpha.json", crypto_candidate(
            "m4", capacity=100.0, ev=5.0, key="alpha",
        ))
        cancel = envelope(
            action="CANCEL", component="professional_maker", key="cancel",
            authority="PAPER_EXPLORATION", research_only=False,
        )
        cancel["side"] = "NONE"
        write(root / "opportunities/inbox/cancel.json", cancel)
        decision = process_cut(root, now_ns=150)["last_decision"]
        assert decision["action"] == "CANCEL"
        assert decision["selected_replay_key"] == "cancel"
        assert decision["crypto_execution_alpha"]["risk_actions_never_filtered"] is True


if __name__ == "__main__":
    test_coordinator_concentrates_ordinary_paper_risk_on_top_market_window()
    test_cancel_survives_market_selection_and_preempts_alpha()
