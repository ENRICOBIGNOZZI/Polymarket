from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from v7_global_portfolio_coordinator import process_cut, process_fast_forward_take  # noqa: E402
from test_v7_opportunity import envelope  # noqa: E402
from test_v7_crypto_execution_alpha import packet as execution_packet  # noqa: E402


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


def test_noncanonical_candidate_fails_closed_and_is_archived() -> None:
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


def test_positive_mature_make_publishes_one_receipt_gated_paper_authorization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        value = envelope(action="MAKE", ev=1.25, key="maker-paper", authority="PAPER_EXPLORATION")
        alpha = execution_packet("MAKE")
        alpha["evidence_status"] = "MATURE"
        alpha["action_ev"]["MAKE"] = {"conservative": 1.25, "point": 1.5}
        value["execution_alpha"] = alpha
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
        assert authorization["opportunity_envelope"]["execution_alpha"]["selected_action"] == "MAKE"


def test_take_never_publishes_maker_authorization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        value = envelope(action="TAKE", ev=1.0, key="take-paper", authority="PAPER_EXPLORATION")
        write(root / "opportunities/inbox/take.json", value)
        status = process_cut(root, now_ns=150)
        assert status["last_decision"]["action"] == "TAKE"
        assert not (root / "micro_maker/authorized_make").exists()



def forward_envelope(key: str = "lead-lag-forward") -> dict:
    value = envelope(action="TAKE", ev=0.0, key=key, authority="PAPER_EXPLORATION")
    value["uncertainty"] = {"lower_bound": 0.0, "upper_bound": 1.0, "status": "IMMATURE"}
    value["calibration_status"] = "NOT_APPLICABLE"
    value["latency"]["profile_valid"] = False
    value["fair_value"] = {"lower": 0.0, "point": 0.55, "upper": 1.0}
    value["forward_test"] = {
        "mode": "PAPER_FORWARD_TEST", "strategy_id": "LEAD_LAG_TAKER_V1",
        "protocol_hash": "f" * 64, "research_only": True, "automatic_promotion": False,
        "one_entry_per_market": True, "hold_to_settlement": True,
        "entry_uses_absolute_fair": False, "probability_source": "POLYMARKET_PRIOR_ONLY",
    }
    return value

def test_fast_forward_lane_authorizes_only_frozen_paper_take() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); value=forward_envelope()
        write(root/'opportunities/fast_forward_inbox/one.json',value)
        status=process_fast_forward_take(root,now_ns=150,risk_preempt=False)
        assert status['state']=='TAKE_AUTHORIZED' and status['paper_only'] is True
        receipt=root/'opportunities/receipts/lead-lag-forward.json'
        assert receipt.exists()
        decision=json.loads(receipt.read_text())
        assert decision['paper_forward_test_authorized'] is True
        assert decision['new_risk_authorized'] is False
        assert (root/'opportunities/fast_forward_archive/one.json').exists()

def test_disk_pressure_blocks_new_paper_risk_but_preserves_cancel() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); (root/'control').mkdir(parents=True)
        write(root/'control/DISK_PRESSURE', {'active': True})
        value=envelope(action="MAKE",ev=1.25,key="disk-block",authority="PAPER_EXPLORATION")
        alpha=execution_packet("MAKE"); alpha["evidence_status"]="MATURE"
        alpha["action_ev"]["MAKE"]={"conservative":1.25,"point":1.5}; value["execution_alpha"]=alpha
        write(root/'opportunities/inbox/make.json',value)
        decision=process_cut(root,now_ns=150)['last_decision']
        assert decision['action']=='NOTHING'
        assert 'DISK_PRESSURE_NEW_RISK_BLOCKED' in decision['reasons']
        assert not (root/'micro_maker/authorized_make').exists()
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); (root/'control').mkdir(parents=True)
        write(root/'control/DISK_PRESSURE', {'active': True})
        cancel=envelope(action="CANCEL",component="professional_maker",key="cancel-disk"); cancel['side']='NONE'
        write(root/'opportunities/inbox/cancel.json',cancel)
        decision=process_cut(root,now_ns=150)['last_decision']
        assert decision['action']=='CANCEL'
        assert decision['new_risk_authorized'] is False


def test_disk_pressure_blocks_fast_forward_take() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); (root/'control').mkdir(parents=True)
        write(root/'control/DISK_PRESSURE', {'active': True})
        write(root/'opportunities/fast_forward_inbox/one.json',forward_envelope())
        status=process_fast_forward_take(root,now_ns=150,risk_preempt=False)
        assert status['state']=='FAIL_CLOSED'
        assert 'DISK_PRESSURE_NEW_RISK_BLOCKED' in status['reasons']
        assert not (root/'opportunities/receipts/lead-lag-forward.json').exists()


def test_fast_forward_lane_is_preempted_by_same_tick_risk_action() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root=Path(directory); write(root/'opportunities/fast_forward_inbox/one.json',forward_envelope())
        status=process_fast_forward_take(root,now_ns=150,risk_preempt=True)
        assert status['state']=='FAIL_CLOSED'
        assert 'RISK_ACTION_PREEMPTS_FORWARD_ALPHA' in status['reasons']
        assert not (root/'opportunities/receipts/lead-lag-forward.json').exists()
        assert (root/'opportunities/fast_forward_rejected/one.json').exists()

if __name__ == "__main__":
    test_one_consumer_compares_both_engines_but_cannot_authorize_new_risk()
    test_cancel_preempts_and_is_the_only_actionable_safe_output()
    test_noncanonical_candidate_fails_closed_and_is_archived()
    test_positive_mature_make_publishes_one_receipt_gated_paper_authorization()
    test_take_never_publishes_maker_authorization()
    test_fast_forward_lane_authorizes_only_frozen_paper_take()
    test_disk_pressure_blocks_new_paper_risk_but_preserves_cancel()
    test_disk_pressure_blocks_fast_forward_take()
    test_fast_forward_lane_is_preempted_by_same_tick_risk_action()
