from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from v7_global_portfolio_coordinator import process_cut  # noqa: E402
from test_v7_opportunity import envelope  # noqa: E402


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_one_consumer_compares_both_engines_but_cannot_authorize_new_risk() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        now = 150
        write(root / "opportunities/inbox/btc.json", envelope(ev=1.0, key="btc"))
        write(root / "opportunities/inbox/structural.json", envelope(
            engine="STRUCTURAL_ARB_ENGINE", action="ARB", component="hard_arb",
            ev=2.0, key="structural",
        ))
        status = process_cut(root, now_ns=now)
        decision = status["last_decision"]
        assert decision["action"] == "NOTHING"
        assert decision["new_risk_authorized"] is False
        assert decision["valid_envelope_count"] == 2
        assert decision["new_risk_policy"] == "CHECKED_IN_DISABLED_NO_RUNTIME_OVERRIDE"


def test_cancel_preempts_and_is_the_only_actionable_safe_output() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cancel = envelope(action="CANCEL", component="professional_maker", key="cancel")
        cancel["side"] = "NONE"
        write(root / "opportunities/inbox/cancel.json", cancel)
        status = process_cut(root, now_ns=150)
        assert status["last_decision"]["action"] == "CANCEL"
        assert status["last_decision"]["new_risk_authorized"] is False


def test_untyped_compatibility_candidate_fails_closed_and_is_archived() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write(root / "opportunities/inbox/legacy.json", {
            "schema_version": 1, "event_type": "CANDIDATE",
            "strategy": "FAST_STRUCTURAL", "model_sha": "a" * 40,
            "metadata": {},
        })
        status = process_cut(root, now_ns=150)
        decision = status["last_decision"]
        assert decision["action"] == "NOTHING"
        assert decision["adapter_error_count"] == 1
        assert (root / "opportunities/archive/legacy.json").exists()
        assert not (root / "opportunities/inbox/legacy.json").exists()


def test_component_candidate_is_typed_by_temporary_adapter_and_forced_to_nothing() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write(root / "control/runtime_status.json", {
            "schema": "polymarket_v7_runtime_status_v3",
            "model_sha": "a" * 40,
            "config_hash": "b" * 64,
            "policy_hash": "c" * 64,
            "run_id": "run-1",
        })
        write(root / "opportunities/inbox/fast.json", {
            "schema_version": 1, "event_type": "CANDIDATE",
            "strategy": "FAST_STRUCTURAL", "model_sha": "a" * 40,
            "record_id": "record-1", "recorded_ts_ms": 1000,
            "candidate_id": "candidate-1", "event_id": "event-1",
            "exchange_ts_ms": 800, "receive_ts_ms": 900,
            "decision_ts_ms": 950, "book_snapshot_id": "book-1",
            "expected_ev": 2.0, "intended_size": 10.0,
            "metadata": {},
            "ingress": {
                "schema": "polymarket_v7_opportunity_ingress_v1",
                "engine_id": "STRUCTURAL_ARB_ENGINE",
            },
        })
        status = process_cut(root, now_ns=1_000_000_000)
        decision = status["last_decision"]
        assert decision["action"] == "NOTHING"
        assert decision["valid_envelope_count"] == 1
        assert decision["adapter_error_count"] == 0


def test_positive_mature_make_publishes_one_receipt_gated_paper_authorization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        value = envelope(action="MAKE", ev=1.25, key="maker-paper", authority="PAPER_EXPLORATION")
        value["execution_alpha"] = {
            "schema": "polymarket_v7_execution_alpha_packet_v1",
            "action": "MAKE", "outcome": "YES", "evidence_status": "MATURE",
            "fill_probability": {"lower": 0.4, "point": 0.5, "upper": 0.6},
            "queue_ahead_shares": 12.0,
            "action_ev": {"conservative": 1.25, "point": 1.5},
            "attribution": {
                "settlement_alpha": 1.25, "spread_capture": 0.0, "rebate": 0.0,
                "fees": 0.0, "slippage": 0.0, "adverse_selection": 0.0,
                "latency": 0.0, "inventory": 0.0, "unwind": 0.0,
                "cancel": 0.0, "capital": 0.0,
            },
        }
        write(root / "opportunities/inbox/make.json", value)
        status = process_cut(root, now_ns=150)
        decision = status["last_decision"]
        assert decision["action"] == "MAKE"
        assert decision["paper_exploration_authorized"] is True
        assert decision["new_risk_authorized"] is False
        files = list((root / "micro_maker/authorized_make").glob("*.json"))
        assert len(files) == 1
        authorization = json.loads(files[0].read_text())
        assert authorization["owner"] == "V7_GLOBAL_PORTFOLIO_COORDINATOR"
        assert authorization["execution_authority"] == "SIMULATED_PAPER_ONLY"
        assert authorization["selected_replay_key"] == "maker-paper"
        assert authorization["opportunity_envelope"]["execution_alpha"]["outcome"] == "YES"


def test_take_never_publishes_maker_authorization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        value = envelope(action="TAKE", ev=1.0, key="take-paper", authority="PAPER_EXPLORATION")
        write(root / "opportunities/inbox/take.json", value)
        status = process_cut(root, now_ns=150)
        assert status["last_decision"]["action"] == "TAKE"
        assert not (root / "micro_maker/authorized_make").exists()


if __name__ == "__main__":
    test_one_consumer_compares_both_engines_but_cannot_authorize_new_risk()
    test_cancel_preempts_and_is_the_only_actionable_safe_output()
    test_untyped_compatibility_candidate_fails_closed_and_is_archived()
    test_component_candidate_is_typed_by_temporary_adapter_and_forced_to_nothing()
    test_positive_mature_make_publishes_one_receipt_gated_paper_authorization()
    test_take_never_publishes_maker_authorization()
