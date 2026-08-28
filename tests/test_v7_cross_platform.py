import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import v7_cross_platform as c


def contract(venue, cid, outcome):
    return c.CrossVenueContract(venue, cid, "event", outcome, "official", 1000, "UTC", ">=", "none",
                                "refund", "none", 1.0, "rules")


def equivalence(a, b, kind):
    payload = json.dumps({"a": a.semantic_payload(), "b": b.semantic_payload(), "type": kind.value},
                         sort_keys=True, separators=(",", ":"))
    return c.ContractEquivalence(a, b, kind, hashlib.sha256(payload.encode()).hexdigest(), True)


def test_exact_and_complement_equivalence_are_deterministic():
    a, b = contract("A", "1", "YES"), contract("B", "2", "NO")
    assert c.classify(a, b) is c.EquivalenceType.COMPLEMENT
    assert equivalence(a, b, c.EquivalenceType.COMPLEMENT).hard_arb_authorized


def test_cross_venue_requires_full_depth_balances_and_direct_joint_states():
    a, b = contract("A", "1", "YES"), contract("B", "2", "NO")
    eq = equivalence(a, b, c.EquivalenceType.COMPLEMENT)
    plan = c.plan_cross_venue(eq, quantity=10,
        asks_a=[c.DepthLevel(.45, 10)], asks_b=[c.DepthLevel(.45, 10)], fee_a=0, fee_b=0,
        slippage_bps=0, transfer_cost=0,
        execution_state_probabilities={"NONE":0,"A_ONLY":0,"B_ONLY":0,"FULL":1},
        state_pnl_adjustments={"NONE":0,"A_ONLY":-1,"B_ONLY":-1,"FULL":0},
        balances={"A":10,"B":10}, duration_seconds=100)
    assert plan.executable and abs(plan.expected_net_pnl - 1.0) < 1e-12
    blocked = c.plan_cross_venue(eq, quantity=10,
        asks_a=[c.DepthLevel(.45, 10)], asks_b=[c.DepthLevel(.45, 10)], fee_a=0, fee_b=0,
        slippage_bps=0, transfer_cost=0,
        execution_state_probabilities={"NONE":0,"A_ONLY":0,"B_ONLY":0,"FULL":1},
        state_pnl_adjustments={"NONE":0,"A_ONLY":-1,"B_ONLY":-1,"FULL":0},
        balances={"A":1,"B":10}, duration_seconds=100)
    assert not blocked.executable and blocked.blocker == "prepositioned_balance_insufficient"
